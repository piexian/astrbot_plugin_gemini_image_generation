"""TrackerProjection：v2 调度生命周期投影到生成历史的端到端与钩子级测试。"""

from __future__ import annotations

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
