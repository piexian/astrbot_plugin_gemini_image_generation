from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tl.generation_tracker import (
    GenerationTracker,
    current_tracking_context,
    requester_from_event,
    tracking_context,
)
from tl.image_generator import ImageGenerator


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
    assert requester["user_name"].startswith("<script>")
    assert len(requester["user_name"]) <= 200


@pytest.mark.asyncio
async def test_image_generator_uses_context_parent_and_item(monkeypatch) -> None:
    class Tracker:
        def __init__(self) -> None:
            self.begin_kwargs = None
            self.completed = None

        async def begin(self, **kwargs):
            self.begin_kwargs = kwargs
            return {"job_id": "job-one"}

        async def complete(self, job_id, **kwargs):
            self.completed = (job_id, kwargs)

        async def fail(self, job_id, **kwargs):
            raise AssertionError(kwargs)

    class Client:
        async def generate_image(self, config, **kwargs):
            config.successful_provider = "google"
            config.successful_model = "image-model"
            return [], ["/tmp/generated.png"], None, None

    async def archive(urls, paths, **kwargs):
        assert kwargs["job_id"] == "job-one"
        return ["gallery.png"]

    tracker = Tracker()
    generator = ImageGenerator(
        context=None,
        api_client=Client(),
        filter_valid_fn=lambda images, source: images or [],
        tracker=tracker,
        archive_images_fn=archive,
    )
    monkeypatch.setattr("tl.image_generator.Path.exists", lambda self: True)

    with tracking_context("llm_batch", "parent", "item"):
        success, _ = await generator.generate_image_core(
            event=None,
            prompt="draw",
            reference_images=[],
            avatar_reference=[],
            is_tool_call=True,
        )

    assert success is True
    assert tracker.begin_kwargs["source"] == "llm_batch"
    assert tracker.begin_kwargs["parent_job_id"] == "parent"
    assert tracker.begin_kwargs["item_name"] == "item"
    assert tracker.completed[0] == "job-one"
    assert tracker.completed[1]["image_files"] == ["gallery.png"]


def test_import_legacy_inserts_and_dedupes(tmp_path) -> None:
    tracker = GenerationTracker(tmp_path, 20)
    record = {
        "job_id": "legacy-a",
        "parent_job_id": None,
        "item_name": None,
        "source": "legacy",
        "status": "succeeded",
        "prompt": "",
        "params": {},
        "requester": {"user_id": "", "user_name": "", "group_id": ""},
        "created_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:00+00:00",
        "duration_ms": 0,
        "requested_images": 1,
        "generated_images": 1,
        "images": ["old.png"],
        "text_content": "",
        "error": None,
        "stats": {},
    }

    assert tracker.import_legacy([dict(record)]) == 1
    # 相同图片名不重复建档
    dup = dict(record, job_id="legacy-b")
    assert tracker.import_legacy([dup]) == 0
    # 重载后仍不重复
    tracker2 = GenerationTracker(tmp_path, 20)
    assert tracker2.import_legacy([dict(record, job_id="legacy-c")]) == 0
    assert tracker2.get("legacy-a") is not None


def test_import_legacy_disabled_tracker_noop(tmp_path) -> None:
    tracker = GenerationTracker(tmp_path, 20, enabled=False)
    record = {"job_id": "legacy-x", "images": ["x.png"]}
    assert tracker.import_legacy([record]) == 0
    assert not (tmp_path / "generation_history.json").exists()
