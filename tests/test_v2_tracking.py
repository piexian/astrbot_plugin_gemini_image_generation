"""TrackerProjection：v2 调度生命周期投影到生成历史的端到端与钩子级测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tl.core.requests import GenerationRequest
from tl.core.results import GenerationResult
from tl.core.stack import build_generation_stack
from tl.core.states import JobState
from tl.generation_scheduler import _PROGRESS
from tl.generation_tracker import GenerationTracker
from tl.v2_tracking import TrackerProjection


class _RecordingClient:
    """记录 config 并可在生成期间触发进度回调的假 legacy client。"""

    def __init__(self, response=None):
        self.response = response or (
            ["https://img.test/a.png"],
            [],
            "好的",
            None,
        )
        self.configs = []

    async def generate_image(self, config, **kwargs):
        self.configs.append(config)
        callback = _PROGRESS.get()
        if callback is not None:
            result = callback("queued")
            if hasattr(result, "__await__"):
                await result
        return self.response


async def _archive_ok(urls, paths, **kwargs):
    return [f"gallery-{index}.png" for index in range(len(urls) + len(paths))]


@pytest.mark.asyncio
async def test_projection_end_to_end_success(tmp_path):
    """accepted → 进度回调 → result → finished：记录 id 等于 v2 job_id，状态 succeeded。"""

    tracker = GenerationTracker(tmp_path, 30)
    updates: list[dict] = []
    original_update = tracker.update

    async def spy_update(job_id, **changes):
        updates.append(changes)
        return await original_update(job_id, **changes)

    tracker.update = spy_update
    client = _RecordingClient()
    service = build_generation_stack(
        client,
        [{"provider": "legacy", "model": "auto"}],
        tmp_path / "store",
        (),
        observers=[TrackerProjection(tracker, _archive_ok)],
    )
    try:
        handle = await service.submit(
            GenerationRequest(prompt="draw", source="command")
        )
        result = await service.wait(handle.job_id)
        assert result.state is JobState.SUCCEEDED
        record = tracker.get(handle.job_id)
        assert record is not None
        assert record["status"] == "succeeded"
        assert record["job_id"] == handle.job_id
        assert record["images"] == ["gallery-0.png"]
        assert record["text_content"] == "好的"
        assert record["stats"]["provider"] == "legacy"
        # 进度回调经由 runtime_scope 的 contextvar 到达 tracker.update。
        assert updates == [{"status": "queued"}]
    finally:
        await service.close()
        await tracker.close()


@pytest.mark.asyncio
async def test_projection_partial_archive_marks_partial_success(tmp_path):
    tracker = GenerationTracker(tmp_path, 30)
    client = _RecordingClient((["https://img.test/a.png", "b.png"], [], None, None))

    async def archive_incomplete(urls, paths, **kwargs):
        return ["only-one.png"]

    service = build_generation_stack(
        client,
        [{"provider": "legacy", "model": "auto"}],
        tmp_path / "store",
        (),
        observers=[TrackerProjection(tracker, archive_incomplete)],
    )
    try:
        handle = await service.submit(
            GenerationRequest(prompt="draw", source="command")
        )
        result = await service.wait(handle.job_id)
        assert result.state is JobState.SUCCEEDED
        record = tracker.get(handle.job_id)
        assert record["status"] == "partial_success"
        assert record["error"] == "画廊归档不完整（1/2）"
    finally:
        await service.close()
        await tracker.close()


def _fake_job(job_id: str, state: JobState, **extra):
    return SimpleNamespace(job_id=job_id, state=state, **extra)


@pytest.mark.asyncio
async def test_projection_job_finished_failed_writes_error_text(tmp_path):
    tracker = GenerationTracker(tmp_path, 30)
    projection = TrackerProjection(tracker)
    try:
        await projection.job_accepted(
            _fake_job(
                "job-fail-1",
                JobState.QUEUED,
                request=SimpleNamespace(
                    prompt="draw",
                    source="command",
                    image_count=1,
                    resolution=None,
                    aspect_ratio=None,
                    provider=None,
                    model=None,
                    quality=None,
                    negative_prompt=None,
                    requester={},
                    metadata={},
                ),
                parent_job_id=None,
            )
        )
        await projection.job_finished(
            _fake_job(
                "job-fail-1",
                JobState.FAILED,
                error_code="provider_http_429",
                error_message="限流",
            )
        )
        record = tracker.get("job-fail-1")
        assert record["status"] == "failed"
        assert record["error"] == "限流"
    finally:
        await tracker.close()


@pytest.mark.asyncio
async def test_projection_job_finished_cancelled_writes_interrupted(tmp_path):
    tracker = GenerationTracker(tmp_path, 30)
    projection = TrackerProjection(tracker)
    try:
        await projection.job_finished(_fake_job("job-cancel-1", JobState.CANCELLED))
        # 无记录时静默（begin 失败不影响生成）。
        assert tracker.get("job-cancel-1") is None
    finally:
        await tracker.close()


@pytest.mark.asyncio
async def test_projection_job_result_uses_split_artifacts_and_stats(tmp_path):
    tracker = GenerationTracker(tmp_path, 30)
    projection = TrackerProjection(tracker, _archive_ok)
    try:
        await projection.job_accepted(
            _fake_job(
                "job-result-1",
                JobState.QUEUED,
                request=SimpleNamespace(
                    prompt="draw",
                    source="webui",
                    image_count=2,
                    resolution="2K",
                    aspect_ratio="16:9",
                    provider="doubao",
                    model="seedream",
                    quality=None,
                    negative_prompt=None,
                    requester={"user_id": "u1"},
                    metadata={},
                ),
                parent_job_id=None,
            )
        )
        await projection.job_result(
            _fake_job("job-result-1", JobState.RESULT_READY),
            GenerationResult(
                artifacts=("https://img.test/a.png", "/tmp/b.png"),
                text="done",
                provider="doubao",
                model="seedream",
                candidate_id="cand-1",
                stats={"retry_count": 2, "successful_model_alias": "别名"},
            ),
        )
        await projection.job_finished(_fake_job("job-result-1", JobState.SUCCEEDED))
        record = tracker.get("job-result-1")
        assert record["status"] == "succeeded"
        assert record["source"] == "webui"
        assert record["images"] == ["gallery-0.png", "gallery-1.png"]
        assert record["source_urls"] == ["https://img.test/a.png"]
        assert record["stats"] == {
            "provider": "doubao",
            "model": "seedream",
            "alias": "别名",
            "retry_count": 2,
        }
    finally:
        await tracker.close()


@pytest.mark.asyncio
async def test_batch_projection_archives_children_but_not_parent(tmp_path):
    tracker = GenerationTracker(tmp_path, 30)
    archives = []

    async def archive(urls, paths, **kwargs):
        archives.append((list(urls), list(paths)))
        return ["gallery-child.png"]

    projection = TrackerProjection(tracker, archive)

    def request(metadata, count):
        return SimpleNamespace(
            prompt="batch",
            source="llm_batch",
            image_count=count,
            resolution=None,
            aspect_ratio=None,
            provider=None,
            model=None,
            quality=None,
            negative_prompt=None,
            requester={},
            metadata=metadata,
        )

    parent = _fake_job(
        "parent-1",
        JobState.QUEUED,
        request=request({"batch_parent": True}, 2),
        parent_job_id=None,
    )
    child = _fake_job(
        "child-1",
        JobState.QUEUED,
        request=request({"item_name": "条目一"}, 2),
        parent_job_id="parent-1",
    )
    result = GenerationResult(artifacts=("https://img.test/a.png", "/tmp/b.png"))
    try:
        await projection.job_accepted(parent)
        await projection.job_accepted(child)
        await projection.job_result(child, result)
        await projection.child_finished(parent, child, result)
        assert tracker.get("parent-1")["generated_images"] == 2

        await projection.job_result(parent, result)

        assert len(archives) == 1
        parent_record = tracker.get("parent-1")
        assert parent_record["status"] == "succeeded"
        assert parent_record["generated_images"] == 2
        assert parent_record["images"] == []
        child_record = tracker.get("child-1")
        assert child_record["parent_job_id"] == "parent-1"
        assert child_record["item_name"] == "条目一"
        assert child_record["images"] == ["gallery-child.png"]
    finally:
        await tracker.close()


# ===== 已完成的记录不被投递阶段的取消/失败改写 =====


def _accepted_job(job_id: str):
    return _fake_job(
        job_id,
        JobState.QUEUED,
        request=SimpleNamespace(
            prompt="draw",
            source="llm_tool",
            image_count=1,
            resolution=None,
            aspect_ratio=None,
            provider=None,
            model=None,
            quality=None,
            negative_prompt=None,
            requester={},
            metadata={},
        ),
        parent_job_id=None,
    )


class _ParkedDelivery:
    """deliver 停在闸门上（Job 处于 DELIVERY_PENDING）；可选直接抛错。"""

    def __init__(self, error=None):
        self.error = error
        self.entered = asyncio.Event()
        self.failures: list[JobState] = []

    async def deliver(self, job, result):
        self.entered.set()
        if self.error is not None:
            raise self.error
        await asyncio.Event().wait()

    async def deliver_failure(self, job):
        self.failures.append(job.state)


@pytest.mark.asyncio
async def test_cancel_during_delivery_keeps_the_succeeded_record(tmp_path):
    tracker = GenerationTracker(tmp_path, 30)
    delivery = _ParkedDelivery()
    service = build_generation_stack(
        _RecordingClient(),
        [{"provider": "legacy", "model": "auto"}],
        tmp_path / "store",
        (),
        observers=[TrackerProjection(tracker, _archive_ok)],
    )
    try:
        handle = await service.submit(
            GenerationRequest(prompt="draw", source="llm_tool"), delivery=delivery
        )
        await asyncio.wait_for(delivery.entered.wait(), 2)
        # job_result 已把记录完成并归档，投递阶段才被取消。
        assert tracker.get(handle.job_id)["status"] == "succeeded"

        snapshot = await service.cancel(handle.job_id)

        assert snapshot.state is JobState.CANCELLED
        assert snapshot.result is not None
        record = tracker.get(handle.job_id)
        assert record["status"] == "succeeded"
        assert record["images"] == ["gallery-0.png"]
        assert record["text_content"] == "好的"
        assert record["error"] is None
        assert delivery.failures == []
    finally:
        await service.close()
        await tracker.close()


@pytest.mark.asyncio
async def test_failed_delivery_keeps_the_succeeded_record(tmp_path):
    tracker = GenerationTracker(tmp_path, 30)
    delivery = _ParkedDelivery(error=RuntimeError("agent down"))
    service = build_generation_stack(
        _RecordingClient(),
        [{"provider": "legacy", "model": "auto"}],
        tmp_path / "store",
        (),
        observers=[TrackerProjection(tracker, _archive_ok)],
    )
    try:
        handle = await service.submit(
            GenerationRequest(prompt="draw", source="llm_tool"), delivery=delivery
        )
        result = await service.wait(handle.job_id)

        assert result.state is JobState.PARTIAL
        assert result.error_code == "delivery_failed"
        record = tracker.get(handle.job_id)
        assert record["status"] == "succeeded"
        assert record["images"] == ["gallery-0.png"]
        assert record["error"] is None
    finally:
        await service.close()
        await tracker.close()


_ABORTED = [
    (JobState.FAILED, "failed"),
    (JobState.CANCELLED, "interrupted"),
    (JobState.INTERRUPTED, "interrupted"),
    (JobState.ORPHANED, "interrupted"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "_aborted_status"), _ABORTED)
async def test_job_finished_skips_records_completed_by_job_result(
    tmp_path, state, _aborted_status
):
    tracker = GenerationTracker(tmp_path, 30)
    projection = TrackerProjection(tracker, _archive_ok)
    result = GenerationResult(artifacts=("https://img.test/a.png",), text="done")
    try:
        await projection.job_accepted(_accepted_job("job-kept"))
        await projection.job_result(
            _fake_job("job-kept", JobState.RESULT_READY), result
        )

        await projection.job_finished(
            _fake_job(
                "job-kept",
                state,
                result=result,
                error_code="delivery_failed",
                error_message="投递失败",
            )
        )

        record = tracker.get("job-kept")
        assert record["status"] == "succeeded"
        assert record["images"] == ["gallery-0.png"]
        assert record["error"] is None
    finally:
        await tracker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "aborted_status"), _ABORTED)
async def test_job_finished_still_closes_record_when_job_result_never_finished(
    tmp_path, state, aborted_status
):
    """结果先于 job_result 观察者落库：观察者中途被取消时记录仍是运行态，要照常收尾。"""

    tracker = GenerationTracker(tmp_path, 30)
    projection = TrackerProjection(tracker, _archive_ok)
    try:
        await projection.job_accepted(_accepted_job("job-open"))

        await projection.job_finished(
            _fake_job(
                "job-open",
                state,
                result=GenerationResult(artifacts=("https://img.test/a.png",)),
                error_code="x",
                error_message="原因",
            )
        )

        assert tracker.get("job-open")["status"] == aborted_status
    finally:
        await tracker.close()


@pytest.mark.asyncio
async def test_successful_job_finished_retries_timed_out_result_projection(tmp_path):
    tracker = GenerationTracker(tmp_path, 30)
    projection = TrackerProjection(tracker, _archive_ok)
    try:
        await projection.job_accepted(_accepted_job("job-success-open"))
        await projection.job_finished(
            _fake_job(
                "job-success-open",
                JobState.SUCCEEDED,
                result=GenerationResult(artifacts=("https://img.test/a.png",)),
            )
        )
        record = tracker.get("job-success-open")
        assert record["status"] == "succeeded"
        assert record["images"] == ["gallery-0.png"]
    finally:
        await tracker.close()


def _studio_job(job_id: str, count: int = 2):
    job = _accepted_job(job_id)
    job.request.source = "webui"
    job.request.image_count = count
    job.request.metadata = {"completion_policy": "studio"}
    return job


@pytest.mark.asyncio
async def test_projection_reports_fill_progress(tmp_path):
    from tl.generation_scheduler import emit_generation_count

    tracker = GenerationTracker(tmp_path, 30)
    projection = TrackerProjection(tracker)
    job = _accepted_job("job-progress")
    try:
        await projection.job_accepted(job)
        with projection.runtime_scope(job):
            await emit_generation_count(2, {"provider": "legacy", "retry_count": 1})
        record = tracker.get(job.job_id)
        assert record["generated_images"] == 2
        assert record["stats"]["provider"] == "legacy"
        assert record["stats"]["retry_count"] == 1
    finally:
        await tracker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("archived", "expected"),
    [
        ([], "failed"),
        (["one.png"], "partial_success"),
        (["one.png", "two.png"], "succeeded"),
    ],
)
async def test_studio_completion_policy(tmp_path, archived, expected):
    tracker = GenerationTracker(tmp_path, 30)

    async def archive(*args, **kwargs):
        return list(archived)

    projection = TrackerProjection(tracker, archive)
    job = _studio_job("job-studio")
    result = GenerationResult(
        artifacts=("https://img.test/a.png",),
        text="文本",
        provider="legacy",
        model="auto",
    )
    try:
        await projection.job_accepted(job)
        await projection.job_result(job, result)
        record = tracker.get(job.job_id)
        assert record["status"] == expected
        assert record["source_urls"] == ["https://img.test/a.png"]
        assert record["generated_images"] == len(archived)
        if expected == "failed":
            assert record["error"] == "生成结果归档失败"
        else:
            assert record["stats"]["provider"] == "legacy"
    finally:
        await tracker.close()


@pytest.mark.asyncio
async def test_studio_completion_truncates_to_requested_count(tmp_path):
    tracker = GenerationTracker(tmp_path, 30)

    async def archive(*args, **kwargs):
        return ["one.png", "two.png", "three.png"]

    projection = TrackerProjection(tracker, archive)
    job = _studio_job("job-studio-limit", 2)
    try:
        await projection.job_accepted(job)
        await projection.job_result(
            job, GenerationResult(artifacts=("https://img.test/a.png",))
        )
        record = tracker.get(job.job_id)
        assert record["status"] == "succeeded"
        assert record["images"] == ["one.png", "two.png"]
        assert record["generated_images"] == 2
    finally:
        await tracker.close()


@pytest.mark.asyncio
async def test_missing_gallery_lease_retries_without_reference_names(tmp_path):
    tracker = GenerationTracker(tmp_path, 30)
    projection = TrackerProjection(tracker)
    job = _studio_job("job-missing-reference", 1)
    job.request.metadata["reference_names"] = ["missing.png"]
    try:
        await projection.job_accepted(job)
        record = tracker.get(job.job_id)
        assert record is not None
        assert record["reference_names"] == []
    finally:
        await tracker.close()


@pytest.mark.asyncio
async def test_studio_batch_parent_uses_child_archive_status_and_sources(tmp_path):
    tracker = GenerationTracker(tmp_path, 30)

    async def archive(*args, **kwargs):
        return []

    projection = TrackerProjection(tracker, archive)
    parent = _studio_job("studio-parent", 1)
    parent.request.metadata["batch_parent"] = True
    child = _studio_job("studio-child", 1)
    child.parent_job_id = parent.job_id
    result = GenerationResult(artifacts=("https://img.test/a.png",))
    parent_result = GenerationResult(
        artifacts=result.artifacts,
        stats={
            "children": [
                {
                    "job_id": child.job_id,
                    "state": "succeeded",
                    "requested_images": 1,
                    "generated_images": 1,
                    "error_code": None,
                }
            ]
        },
    )
    try:
        await projection.job_accepted(parent)
        await projection.job_accepted(child)
        await projection.job_result(child, result)
        await projection.child_finished(parent, child, result)
        await projection.job_result(parent, parent_result)

        assert tracker.get(child.job_id)["status"] == "failed"
        parent_record = tracker.get(parent.job_id)
        assert parent_record["status"] == "partial_success"
        assert parent_record["generated_images"] == 1
        assert parent_record["source_urls"] == ["https://img.test/a.png"]
    finally:
        await tracker.close()
