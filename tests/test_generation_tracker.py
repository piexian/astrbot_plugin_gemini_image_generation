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


@pytest.mark.asyncio
async def test_image_generator_archive_failure_records_partial_success(
    monkeypatch,
) -> None:
    """归档异常（如下载超时）不能把已发送的生成结果记为失败。"""

    class Tracker:
        def __init__(self) -> None:
            self.completed = None
            self.updates: list[dict] = []

        async def begin(self, **kwargs):
            return {"job_id": "job-one"}

        async def complete(self, job_id, **kwargs):
            self.completed = (job_id, kwargs)

        async def update(self, job_id, **changes):
            self.updates.append(changes)

        async def fail(self, job_id, **kwargs):
            raise AssertionError(kwargs)

    class Client:
        async def generate_image(self, config, **kwargs):
            config.successful_provider = "openai_images"
            config.successful_model = "gpt-image-2"
            return [], ["/tmp/generated.png"], None, None

    async def broken_archive(urls, paths, **kwargs):
        raise TimeoutError()  # aiohttp 下载超时的 str() 为空

    tracker = Tracker()
    generator = ImageGenerator(
        context=None,
        api_client=Client(),
        filter_valid_fn=lambda images, source: images or [],
        tracker=tracker,
        archive_images_fn=broken_archive,
    )
    monkeypatch.setattr("tl.image_generator.Path.exists", lambda self: True)

    success, _ = await generator.generate_image_core(
        event=None,
        prompt="draw",
        reference_images=[],
        avatar_reference=[],
        is_tool_call=True,
    )

    assert success is True
    assert tracker.completed[1]["status"] == "partial_success"
    assert tracker.completed[1]["image_files"] == []
    assert tracker.completed[1]["stats"]["successful_provider"] == "openai_images"
    assert len(tracker.updates) == 1
    assert "画廊归档不完整（0/1）" in tracker.updates[0]["error"]
    assert "TimeoutError" in tracker.updates[0]["error"]


@pytest.mark.asyncio
async def test_image_generator_partial_archive_marks_partial_success(
    monkeypatch,
) -> None:
    """归档返回空列表（静默下载失败）同样降级为部分成功而非失败。"""

    class Tracker:
        def __init__(self) -> None:
            self.completed = None
            self.updates: list[dict] = []

        async def begin(self, **kwargs):
            return {"job_id": "job-one"}

        async def complete(self, job_id, **kwargs):
            self.completed = (job_id, kwargs)

        async def update(self, job_id, **changes):
            self.updates.append(changes)

        async def fail(self, job_id, **kwargs):
            raise AssertionError(kwargs)

    class Client:
        async def generate_image(self, config, **kwargs):
            config.successful_provider = "google"
            config.successful_model = "image-model"
            return ["https://cdn.example/a.png"], [], None, None

    async def empty_archive(urls, paths, **kwargs):
        return []

    tracker = Tracker()
    generator = ImageGenerator(
        context=None,
        api_client=Client(),
        filter_valid_fn=lambda images, source: images or [],
        tracker=tracker,
        archive_images_fn=empty_archive,
    )
    monkeypatch.setattr("tl.image_generator.Path.exists", lambda self: True)

    success, _ = await generator.generate_image_core(
        event=None,
        prompt="draw",
        reference_images=[],
        avatar_reference=[],
        is_tool_call=True,
    )

    assert success is True
    assert tracker.completed[1]["status"] == "partial_success"
    assert "画廊归档不完整（0/1）" in tracker.updates[0]["error"]


@pytest.mark.asyncio
async def test_image_generator_without_archive_fn_keeps_succeeded(
    monkeypatch,
) -> None:
    """未接入归档函数时（无 Studio）语义不变，仍记为成功。"""

    class Tracker:
        def __init__(self) -> None:
            self.completed = None

        async def begin(self, **kwargs):
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

    tracker = Tracker()
    generator = ImageGenerator(
        context=None,
        api_client=Client(),
        filter_valid_fn=lambda images, source: images or [],
        tracker=tracker,
    )
    monkeypatch.setattr("tl.image_generator.Path.exists", lambda self: True)

    success, _ = await generator.generate_image_core(
        event=None,
        prompt="draw",
        reference_images=[],
        avatar_reference=[],
        is_tool_call=True,
    )

    assert success is True
    assert tracker.completed[1]["status"] == "succeeded"
    assert tracker.completed[1]["image_files"] == []


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


@pytest.mark.asyncio
async def test_source_urls_sanitized_on_begin_complete_and_update(tmp_path) -> None:
    """持久源链接随任务记录；临时缓存/非 http/超长/重复项被过滤，上限 20 条。"""
    tracker = GenerationTracker(tmp_path, 20)
    record = await tracker.begin(
        source="command", prompt="draw", params={}, requester={}, requested_images=1
    )
    job_id = record["job_id"]
    assert record["source_urls"] == []

    urls = [
        "https://cdn.example/a.png",
        "https://cdn.example/a.png",
        "https://gw.example/images/users-1/cache.png",
        "https://gw.example/temp/image/x.png",
        "file:///tmp/a.png",
        "ftp://cdn.example/a.png",
        123,
        None,
        "https://cdn.example/" + "q" * 2100,
        *[f"https://cdn.example/{index}.png" for index in range(30)],
    ]
    await tracker.complete(
        job_id, image_files=[], text_content=None, stats={}, source_urls=urls
    )
    saved = tracker.get(job_id)["source_urls"]
    assert saved[0] == "https://cdn.example/a.png"
    assert len(saved) == 20
    assert all(
        isinstance(url, str)
        and url.startswith(("http://", "https://"))
        and len(url) <= 2048
        for url in saved
    )
    assert not any("/images/users-" in url or "/temp/image/" in url for url in saved)

    await tracker.update(
        job_id, source_urls=["https://keep.example/b.png", "not-a-url"]
    )
    assert tracker.get(job_id)["source_urls"] == ["https://keep.example/b.png"]
    await tracker.update(job_id, source_urls="https://wrong.example/type.png")
    assert tracker.get(job_id)["source_urls"] == []
    await tracker.close()


def test_legacy_records_without_source_urls_still_load(tmp_path) -> None:
    """旧历史文件没有 source_urls 字段，加载与更新不受影响。"""
    history = tmp_path / "generation_history.json"
    history.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "job_id": "legacy-no-urls",
                        "status": "succeeded",
                        "images": [],
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    tracker = GenerationTracker(tmp_path, 20)
    record = tracker.get("legacy-no-urls")
    assert record is not None
    assert "source_urls" not in record


@pytest.mark.asyncio
async def test_image_generator_records_persistent_source_urls(monkeypatch) -> None:
    """归档失败时源 URL 仍写入记录，便于事后取回图片。"""

    class Tracker:
        def __init__(self) -> None:
            self.completed = None
            self.updates: list[dict] = []

        async def begin(self, **kwargs):
            return {"job_id": "job-one"}

        async def complete(self, job_id, **kwargs):
            self.completed = (job_id, kwargs)

        async def update(self, job_id, **changes):
            self.updates.append(changes)

        async def fail(self, job_id, **kwargs):
            raise AssertionError(kwargs)

    class Client:
        async def generate_image(self, config, **kwargs):
            config.successful_provider = "openai_images"
            config.successful_model = "gpt-image-2"
            return ["https://cdn.example/a.png", "/tmp/local.png"], [], None, None

    async def broken_archive(urls, paths, **kwargs):
        raise TimeoutError()

    tracker = Tracker()
    generator = ImageGenerator(
        context=None,
        api_client=Client(),
        filter_valid_fn=lambda images, source: images or [],
        tracker=tracker,
        archive_images_fn=broken_archive,
    )
    monkeypatch.setattr("tl.image_generator.Path.exists", lambda self: True)

    success, _ = await generator.generate_image_core(
        event=None,
        prompt="draw",
        reference_images=[],
        avatar_reference=[],
        is_tool_call=True,
    )

    assert success is True
    kwargs = tracker.completed[1]
    assert kwargs["status"] == "partial_success"
    assert kwargs["source_urls"] == ["https://cdn.example/a.png"]
