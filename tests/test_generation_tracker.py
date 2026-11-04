from __future__ import annotations

import base64
import io
import json
from types import SimpleNamespace

import pytest

from tl.generation_tracker import (
    GenerationTracker,
    current_tracking_context,
    record_generation_success,
    requester_from_event,
    tracking_context,
)


@pytest.mark.asyncio
async def test_preview_is_transient_and_not_counted(tmp_path):
    from PIL import Image

    tracker = GenerationTracker(tmp_path, max_records=20)
    queue = tracker.subscribe()
    record = await tracker.begin(source="webui", prompt="draw", params={}, requester={})
    queue.get_nowait()
    output = io.BytesIO()
    Image.new("RGB", (1024, 1024), "red").save(output, format="PNG")
    await tracker.preview(
        record["job_id"], base64.b64encode(output.getvalue()).decode()
    )
    preview = queue.get_nowait()["data"]
    assert preview["generated_images"] == 0
    assert preview["images"] == []
    raw = base64.b64decode(preview["preview"].split(",", 1)[1])
    with Image.open(io.BytesIO(raw)) as image:
        assert image.size == (512, 512)
    assert "preview" not in tracker.path.read_text()
    await tracker.complete(
        record["job_id"], image_files=[], text_content=None, stats={}
    )
    assert "preview" not in queue.get_nowait()["data"]
    await tracker.close()


@pytest.mark.parametrize(
    ("message_type", "group_id", "expected"),
    [
        ("GroupMessage", "123456", "group"),
        ("GroupMessage", "", "group"),
        ("FriendMessage", "", "private"),
        ("OtherMessage", "", "unknown"),
        (None, "", "unknown"),
    ],
)
def test_requester_keeps_explicit_chat_type(message_type, group_id, expected):
    event = SimpleNamespace(
        get_sender_id=lambda: "10001",
        get_group_id=lambda: group_id,
        get_message_type=lambda: SimpleNamespace(value=message_type),
    )
    requester = requester_from_event(event)
    assert requester["chat_type"] == expected
    assert requester["group_id"] == group_id
    assert requester["user_id"] == "10001"


@pytest.mark.asyncio
async def test_requester_survives_sse_reload_and_group_filter(tmp_path):
    tracker = GenerationTracker(tmp_path, max_records=20)
    queue = tracker.subscribe()
    requesters = []
    for group_id, message_type in [
        ("123456", "GroupMessage"),
        ("654321", "GroupMessage"),
        ("", "FriendMessage"),
    ]:
        event = SimpleNamespace(
            get_sender_id=lambda: "10001",
            get_sender_name=lambda: "同一个用户",
            get_group_id=lambda group_id=group_id: group_id,
            get_message_type=lambda message_type=message_type: SimpleNamespace(
                value=message_type
            ),
        )
        requester = requester_from_event(event)
        requesters.append(requester)
        record = await tracker.begin(
            source="command", prompt="draw", params={}, requester=requester
        )
        assert queue.get_nowait()["data"]["requester"] == requester
        await tracker.complete(
            record["job_id"], image_files=[], text_content="done", stats={}
        )
        assert queue.get_nowait()["data"]["requester"] == requester
    await tracker.close()

    restored = GenerationTracker(tmp_path, max_records=20)
    query = {
        "page": 1,
        "size": 20,
        "keyword": "",
        "source": "",
        "group_id": "",
        "user_id": "10001",
    }
    records = restored.query_history(**query)["items"]
    assert len(records) == 3
    assert all(
        requester in [record["requester"] for record in records]
        for requester in requesters
    )
    query["group_id"] = "123456"
    filtered = restored.query_history(**query)
    assert filtered["total"] == 1
    assert filtered["items"][0]["requester"] == requesters[0]
    await restored.close()


@pytest.mark.asyncio
async def test_begin_updates_and_complete_are_persisted(tmp_path) -> None:
    tracker = GenerationTracker(tmp_path, max_records=20)

    record = await tracker.begin(
        source="command",
        prompt="draw",
        params={"resolution": "1K", "secret": "ignored"},
        requester={"user_id": "1", "user_name": "name", "group_id": "2"},
        requested_images=2,
    )
    await tracker.update(record["job_id"], generated_images=1)
    await tracker.complete(
        record["job_id"],
        image_files=["one.png", "two.png"],
        text_content="done",
        stats={"provider": "google", "api_key": "must-not-persist"},
    )

    payload = json.loads((tmp_path / "generation_history.json").read_text())
    saved = payload["jobs"][0]
    assert saved["status"] == "succeeded"
    assert saved["generated_images"] == 2
    assert saved["params"] == {
        "resolution": "1K",
        "aspect_ratio": None,
        "provider": None,
        "model": None,
        "candidate_id": None,
        "image_count": 1,
        "quality": None,
        "seed": None,
        "negative_prompt": None,
        "generation_settings": {},
        "template_id": None,
    }
    assert saved["stats"] == {
        "provider": "google",
        "model": "",
        "alias": "",
        "retry_count": 0,
    }


@pytest.mark.asyncio
async def test_tracker_debug_logs_summarize_lifecycle_and_sse(
    tmp_path, monkeypatch
) -> None:
    import tl.generation_tracker as tracker_module

    messages: list[str] = []
    monkeypatch.setattr(
        tracker_module,
        "logger",
        SimpleNamespace(debug=messages.append),
    )
    tracker = GenerationTracker(tmp_path, max_records=20)
    prompt = "甲" * 30 + "不应出现在日志"

    completed = await tracker.begin(
        source="webui",
        prompt=prompt,
        params={"provider": "google", "model": "image-model"},
        requester={},
    )
    await tracker.complete(
        completed["job_id"], image_files=["one.png"], text_content=None, stats={}
    )
    failed = await tracker.begin(
        source="command",
        prompt="失败示例",
        params={},
        requester={},
    )
    await tracker.fail(failed["job_id"], error="boom")
    queue = tracker.subscribe()
    tracker._broadcast_event({"type": "resync"})
    tracker.unsubscribe(queue)

    joined = "\n".join(messages)
    assert "甲" * 30 in joined
    assert "不应出现在日志" not in joined
    assert "状态=无->running" in joined
    assert "状态=running->succeeded" in joined
    assert "状态=running->failed" in joined
    assert "当前订阅数=1" in joined
    assert "当前订阅数=0" in joined
    assert "resync 已触发" in joined


@pytest.mark.asyncio
async def test_prune_removes_a_whole_parent_group(tmp_path) -> None:
    tracker = GenerationTracker(tmp_path, max_records=3)
    parent = await tracker.begin(
        source="webui",
        prompt="parent",
        params={},
        requester={},
    )
    children = []
    for name in ("a", "b"):
        child = await tracker.begin(
            source="webui",
            prompt=name,
            params={},
            requester={},
            parent_job_id=parent["job_id"],
            item_name=name,
        )
        children.append(child)
        await tracker.complete(
            child["job_id"], image_files=[], text_content=None, stats={}
        )
    await tracker.complete(
        parent["job_id"], image_files=[], text_content=None, stats={}
    )

    newest = await tracker.begin(
        source="command",
        prompt="new",
        params={},
        requester={},
    )

    assert tracker.get(parent["job_id"]) is None
    assert all(tracker.get(child["job_id"]) is None for child in children)
    assert tracker.get(newest["job_id"]) is not None


@pytest.mark.asyncio
async def test_restart_marks_running_records_interrupted(tmp_path) -> None:
    first = GenerationTracker(tmp_path, max_records=20)
    record = await first.begin(
        source="llm_tool",
        prompt="draw",
        params={},
        requester={},
    )

    restarted = GenerationTracker(tmp_path, max_records=20)

    assert restarted.get(record["job_id"])["status"] == "interrupted"


def test_corrupt_history_is_backed_up(tmp_path) -> None:
    path = tmp_path / "generation_history.json"
    path.write_text("not-json", encoding="utf-8")

    tracker = GenerationTracker(tmp_path, max_records=20)

    assert (
        tracker.query_history(
            page=1, size=20, keyword="", source="", group_id="", user_id=""
        )["total"]
        == 0
    )
    assert list(tmp_path.glob("generation_history.json.corrupt-*"))


def test_subscription_overflow_requests_a_resync(tmp_path) -> None:
    tracker = GenerationTracker(tmp_path, max_records=20)
    queue = tracker.subscribe()

    for index in range(205):
        tracker._broadcast({"job_id": str(index)})

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert any(event["type"] == "resync" for event in events)
    assert events[-1]["data"]["job_id"] == "204"
    tracker.unsubscribe(queue)


@pytest.mark.asyncio
async def test_delete_parent_cascades_and_tolerates_missing_files(tmp_path) -> None:
    tracker = GenerationTracker(tmp_path, max_records=20)
    parent = await tracker.begin(source="webui", prompt="p", params={}, requester={})
    child = await tracker.begin(
        source="webui",
        prompt="c",
        params={},
        requester={},
        parent_job_id=parent["job_id"],
    )
    await tracker.complete(
        child["job_id"], image_files=["missing.png"], text_content=None, stats={}
    )
    await tracker.complete(
        parent["job_id"], image_files=[], text_content=None, stats={}
    )

    result = await tracker.delete([parent["job_id"]])

    assert set(result["deleted"]) == {parent["job_id"], child["job_id"]}
    assert result["failed"] == []


def test_tracking_context_is_nested_and_restored() -> None:
    assert current_tracking_context() is None
    with tracking_context("llm_batch", parent_job_id="parent", item_name="one"):
        current = current_tracking_context()
        assert current.source == "llm_batch"
        assert current.parent_job_id == "parent"
        assert current.item_name == "one"
    assert current_tracking_context() is None


def test_requester_metadata_is_defensive_and_truncated() -> None:
    event = SimpleNamespace(
        get_sender_id=lambda: "user",
        get_sender_name=lambda: "<script>" + "x" * 300,
        message_obj=SimpleNamespace(group_id="group"),
    )

    requester = requester_from_event(event)

    assert requester["user_id"] == "user"
    assert requester["group_id"] == "group"
    assert requester["chat_type"] == "group"
    assert requester["user_name"].startswith("<script>")
    assert len(requester["user_name"]) <= 200


class _RecordingTracker:
    """记录终态写入的最小 tracker。"""

    def __init__(self) -> None:
        self.completed = None
        self.updates: list[dict] = []
        self.failed = None

    async def complete(self, job_id, **kwargs):
        self.completed = (job_id, kwargs)

    async def update(self, job_id, **changes):
        self.updates.append(changes)

    async def fail(self, job_id, **kwargs):
        self.failed = (job_id, kwargs)


@pytest.mark.asyncio
async def test_record_generation_success_archives_with_job_and_candidate() -> None:
    """完成记录把 job_id 与候选传给归档，并保留归档结果与统计。"""
    tracker = _RecordingTracker()
    archived = []

    async def archive(urls, paths, **kwargs):
        archived.append((list(urls), list(paths), kwargs))
        return ["gallery.png"]

    await record_generation_success(
        tracker,
        "job-one",
        image_urls=[],
        image_paths=["/tmp/generated.png"],
        text_content=None,
        stats={"successful_provider": "google", "successful_model": "image-model"},
        archive_images_fn=archive,
        candidate_id="cand-1",
    )

    assert archived == [
        ([], ["/tmp/generated.png"], {"candidate_id": "cand-1", "job_id": "job-one"})
    ]
    assert tracker.completed[0] == "job-one"
    assert tracker.completed[1]["image_files"] == ["gallery.png"]
    assert tracker.completed[1]["status"] == "succeeded"
    assert tracker.completed[1]["stats"]["successful_model"] == "image-model"
    assert tracker.failed is None


@pytest.mark.asyncio
async def test_record_generation_success_archive_failure_downgrades_to_partial() -> (
    None
):
    """归档异常（如下载超时）不能把已发送的生成结果记为失败。"""

    async def broken_archive(urls, paths, **kwargs):
        raise TimeoutError()  # aiohttp 下载超时的 str() 为空

    tracker = _RecordingTracker()

    await record_generation_success(
        tracker,
        "job-one",
        image_urls=["https://cdn.example/a.png"],
        image_paths=[],
        text_content=None,
        stats={
            "successful_provider": "openai_images",
            "successful_model": "gpt-image-2",
        },
        archive_images_fn=broken_archive,
    )

    assert tracker.completed[0] == "job-one"
    assert tracker.completed[1]["status"] == "partial_success"
    assert tracker.completed[1]["image_files"] == []
    assert tracker.completed[1]["stats"]["successful_provider"] == "openai_images"
    assert tracker.failed is None
    (update,) = tracker.updates
    assert "画廊归档不完整（0/1）" in update["error"]
    assert "TimeoutError" in update["error"]


@pytest.mark.asyncio
async def test_record_generation_success_partial_archive_marks_partial_success() -> (
    None
):
    """归档返回空列表（静默下载失败）同样降级为部分成功而非失败。"""

    async def empty_archive(urls, paths, **kwargs):
        return []

    tracker = _RecordingTracker()

    await record_generation_success(
        tracker,
        "job-one",
        image_urls=["https://cdn.example/a.png"],
        image_paths=[],
        text_content=None,
        stats={"successful_provider": "google", "successful_model": "image-model"},
        archive_images_fn=empty_archive,
    )

    assert tracker.completed[1]["status"] == "partial_success"
    assert tracker.failed is None
    assert "画廊归档不完整（0/1）" in tracker.updates[0]["error"]


@pytest.mark.asyncio
async def test_record_generation_success_without_archive_fn_keeps_succeeded() -> None:
    """未接入归档函数时（无 Studio）语义不变，仍记为成功。"""
    tracker = _RecordingTracker()

    await record_generation_success(
        tracker,
        "job-one",
        image_urls=[],
        image_paths=["/tmp/generated.png"],
        text_content=None,
        stats={"successful_provider": "google", "successful_model": "image-model"},
    )

    assert tracker.completed[1]["status"] == "succeeded"
    assert tracker.completed[1]["image_files"] == []
    assert tracker.updates == []


@pytest.mark.asyncio
async def test_record_generation_success_records_persistent_source_urls() -> None:
    """归档失败时源 URL 仍写入记录，便于事后取回图片。"""

    async def broken_archive(urls, paths, **kwargs):
        raise TimeoutError()

    tracker = _RecordingTracker()

    await record_generation_success(
        tracker,
        "job-one",
        image_urls=["https://cdn.example/a.png", "/tmp/local.png"],
        image_paths=[],
        text_content=None,
        stats={
            "successful_provider": "openai_images",
            "successful_model": "gpt-image-2",
        },
        archive_images_fn=broken_archive,
    )

    kwargs = tracker.completed[1]
    assert kwargs["status"] == "partial_success"
    assert kwargs["source_urls"] == ["https://cdn.example/a.png"]


@pytest.mark.asyncio
@pytest.mark.parametrize("job_id", ["job-1", "a", "A-b_C.d", "0" * 128])
async def test_begin_accepts_valid_explicit_job_id(tmp_path, job_id):
    tracker = GenerationTracker(tmp_path, 10)
    try:
        record = await tracker.begin(
            job_id=job_id,
            source="command",
            prompt="draw",
            params={},
            requester={},
        )
        assert record["job_id"] == job_id
    finally:
        await tracker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "job_id",
    ["", " ", "job 1", "job/1", "job:1", "job#1", "a" * 129, 123, ["job"]],
)
async def test_begin_rejects_invalid_explicit_job_id(tmp_path, job_id):
    tracker = GenerationTracker(tmp_path, 10)
    try:
        with pytest.raises(ValueError):
            await tracker.begin(
                job_id=job_id,
                source="command",
                prompt="draw",
                params={},
                requester={},
            )
    finally:
        await tracker.close()


@pytest.mark.asyncio
async def test_begin_rejects_duplicate_explicit_job_id(tmp_path):
    tracker = GenerationTracker(tmp_path, 10)
    try:
        await tracker.begin(
            job_id="job-dup",
            source="command",
            prompt="draw",
            params={},
            requester={},
        )
        with pytest.raises(ValueError):
            await tracker.begin(
                job_id="job-dup",
                source="command",
                prompt="again",
                params={},
                requester={},
            )
        # 不给 job_id 时仍按原方式生成，不受显式 id 影响。
        record = await tracker.begin(
            source="command", prompt="draw", params={}, requester={}
        )
        assert record["job_id"].startswith("job-")
        assert record["job_id"] != "job-dup"
    finally:
        await tracker.close()
