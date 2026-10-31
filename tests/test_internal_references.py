"""内部引用语法、解析、记录字段和对外输出。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from tl.generation_tracker import (
    GenerationTracker,
    ReferenceImageUnavailableError,
    requester_from_event,
    without_session_origin,
)
from tl.internal_references import (
    InternalReferenceError,
    ReferenceResolver,
    parse_references,
)
from tl.plugin_config import ConfigLoader
from tl.plugin_service import ImageGenerationService


def _png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2, 2), (255, 0, 0)).save(path)
    return path


def _settings(**overrides):
    values = {
        "cross_session": False,
        "history_enabled": True,
        "max_reference_images": 6,
        "allowed_dirs": [],
        "global_paths": False,
    }
    values.update(overrides)
    return values


class _Images:
    def __init__(self, images):
        self.images = images
        self.calls = []

    async def fetch_images_from_event(self, event, include_at_avatars=False):
        self.calls.append(include_at_avatars)
        return list(self.images), []


class _Avatars:
    def __init__(self, images=None, fail=False):
        self.images = images or {}
        self.fail = fail
        self.calls = []

    async def download_user_avatar(self, user_id, event):
        self.calls.append(user_id)
        if self.fail:
            return None
        return self.images.get(user_id, f"avatar:{user_id}")


class _At:
    def __init__(self, qq, **flags):
        self.qq = qq
        for name, value in flags.items():
            setattr(self, name, value)


def _event(umo="session:a", sender="100", messages=None, self_id="bot"):
    return SimpleNamespace(
        unified_msg_origin=umo,
        get_sender_id=lambda: sender,
        get_self_id=lambda: self_id,
        get_messages=lambda: list(messages or []),
    )


def _resolver(tracker, tmp_path, **options):
    holder = _settings(allowed_dirs=[str(tmp_path)], **options)
    return ReferenceResolver(
        image_handler=_Images(["msg"]),
        avatar_handler=_Avatars(),
        generation_tracker=tracker,
        settings=lambda: holder,
    ), holder


async def _record(tracker, *, umo, user="100", status="succeeded", images=("a.png",)):
    names = []
    for name in images:
        _png(tracker.gallery_dir / name)
        names.append(name)
    record = await tracker.begin(
        source="command",
        prompt="一只猫",
        params={},
        requester={
            "user_id": user,
            "user_name": "用户",
            "group_id": "",
            "chat_type": "private",
            "umo": umo,
        },
    )
    if status == "failed":
        await tracker.fail(record["job_id"], error="失败")
    else:
        await tracker.complete(
            record["job_id"],
            image_files=names,
            text_content=None,
            stats={},
            status=status,
        )
    return record["job_id"]


def test_syntax_accepts_each_form_and_keeps_order():
    specs = parse_references(
        [
            " current ",
            "avatar:sender",
            "avatar:mentioned",
            "avatar:User_1.2",
            "gen:last",
            "gen:job-1",
            "gen:job-1#2",
            "/tmp/a.png",
            "file:///tmp/a.png",
            r"C:\pictures\a.png",
            "current",
        ]
    )
    assert [item.kind for item in specs] == [
        "current",
        "avatar_sender",
        "avatar_mentioned",
        "avatar_user",
        "gen_last",
        "gen_job",
        "gen_job",
        "local",
        "local",
        "local",
    ]
    assert specs[3].user_id == "User_1.2"
    assert specs[6].index == 2
    assert len(specs) == 10


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "Current",
        "foo:bar",
        "avatar:",
        "avatar:bad id",
        "gen:",
        "gen:job#0",
        "gen:job#-1",
        "gen:job#abc",
        "gen:job#21",
        "gen:bad id",
        "gen:../secret",
        "pictures/a.png",
        "~/a.png",
        "/tmp/../etc/passwd",
        r"C:\temp\..\secret.png",
    ],
)
def test_syntax_rejects_invalid_forms(value):
    with pytest.raises(InternalReferenceError) as caught:
        parse_references([value])
    assert caught.value.code == "invalid_request"
    if value.strip():
        assert value.strip()[:32] in caught.value.message


def test_syntax_limits_item_count_and_length():
    with pytest.raises(InternalReferenceError, match="16"):
        parse_references(["current"] * 17)
    with pytest.raises(InternalReferenceError, match="512"):
        parse_references(["/" + "a" * 512])


@pytest.mark.asyncio
async def test_current_reads_event_images_without_cache(tmp_path):
    tracker = GenerationTracker(tmp_path / "history", 20)
    images = _Images(["one", "two"])
    resolver = ReferenceResolver(
        image_handler=images,
        avatar_handler=_Avatars(),
        generation_tracker=tracker,
        settings=lambda: _settings(),
    )
    resolved = await resolver.resolve(parse_references(["current"]), _event())
    assert resolved.message_images == ["one", "two"]
    assert images.calls == [False]
    with pytest.raises(InternalReferenceError, match="当前消息没有图片"):
        images.images = []
        await resolver.resolve(parse_references(["current"]), _event())
    await tracker.close()


@pytest.mark.asyncio
async def test_avatar_forms_exclude_auto_and_bot_mentions():
    avatars = _Avatars()
    resolver = ReferenceResolver(
        image_handler=_Images([]),
        avatar_handler=avatars,
        generation_tracker=SimpleNamespace(enabled=True),
        settings=lambda: _settings(),
    )
    event = _event(
        sender="100",
        self_id="bot",
        messages=[_At("bot"), _At("200", is_auto=True), _At("300")],
    )
    mentioned = await resolver.resolve(parse_references(["avatar:mentioned"]), event)
    assert avatars.calls == ["300"]
    assert mentioned.avatar_reference == ["avatar:300"]
    sender = await resolver.resolve(parse_references(["avatar:sender"]), event)
    assert sender.avatar_reference == ["avatar:100"]
    selected = await resolver.resolve(parse_references(["avatar:100"]), event)
    assert selected.avatar_reference == ["avatar:100"]
    with pytest.raises(InternalReferenceError, match="无权引用") as caught:
        await resolver.resolve(parse_references(["avatar:999"]), event)
    assert caught.value.code == "reference_access_denied"
    avatars.fail = True
    with pytest.raises(InternalReferenceError, match="头像获取失败"):
        await resolver.resolve(parse_references(["avatar:sender"]), event)


@pytest.mark.asyncio
async def test_generation_scope_rules(tmp_path):
    tracker = GenerationTracker(tmp_path / "history", 20)
    own = await _record(
        tracker, umo="session:a", user="100", images=("own.png", "second.png")
    )
    other = await _record(tracker, umo="session:b", user="200", images=("other.png",))
    failed = await _record(tracker, umo="session:a", status="failed", images=())
    old = await _record(tracker, umo="session:a", images=("old.png",))
    tracker._jobs[old]["requester"].pop("umo")
    resolver, settings = _resolver(tracker, tmp_path / "allowed")
    event = _event("session:a")

    own_image = await resolver.resolve(parse_references([f"gen:{own}"]), event)
    assert own_image.gallery_names == ["own.png"]
    assert "own.png" not in json.dumps(own_image.sources)
    second = await resolver.resolve(parse_references([f"gen:{own}#2"]), event)
    assert second.gallery_names == ["second.png"]
    with pytest.raises(InternalReferenceError, match="没有对应的图片"):
        await resolver.resolve(parse_references([f"gen:{own}#3"]), event)

    with pytest.raises(InternalReferenceError, match="无权引用") as denied:
        await resolver.resolve(parse_references([f"gen:{other}"]), event)
    assert denied.value.code == "reference_access_denied"
    assert "session:b" not in denied.value.message

    await tracker.set_shared(other, True, user_id="200", umo="session:b")
    shared = await resolver.resolve(parse_references([f"gen:{other}"]), event)
    assert shared.gallery_names == ["other.png"]
    await tracker.set_shared(other, False, user_id="200", umo="session:b")
    settings["cross_session"] = True
    opened = await resolver.resolve(parse_references([f"gen:{other}"]), event)
    assert opened.gallery_names == ["other.png"]
    settings["cross_session"] = False

    with pytest.raises(InternalReferenceError, match="无权引用"):
        await resolver.resolve(parse_references([f"gen:{old}"]), event)
    with pytest.raises(InternalReferenceError, match="还不能引用"):
        await resolver.resolve(parse_references([f"gen:{failed}"]), event)

    settings["history_enabled"] = False
    with pytest.raises(
        InternalReferenceError, match="参考图片不存在或正在清理"
    ) as closed:
        await resolver.resolve(parse_references(["gen:last"]), event)
    assert closed.value.code == "reference_image_unavailable"
    settings["history_enabled"] = True
    (tracker.gallery_dir / "own.png").unlink()
    with pytest.raises(InternalReferenceError) as missing:
        await resolver.resolve(parse_references([f"gen:{own}"]), event)
    assert missing.value.code == "reference_image_unavailable"
    await tracker.close()


@pytest.mark.asyncio
async def test_gen_last_uses_latest_success_in_current_session(tmp_path):
    tracker = GenerationTracker(tmp_path / "history", 20)
    await _record(tracker, umo="session:a", images=("first.png",))
    await _record(tracker, umo="session:b", images=("other.png",))
    latest = await _record(tracker, umo="session:a", images=("latest.png",))
    resolver, _settings_holder = _resolver(tracker, tmp_path)
    resolved = await resolver.resolve(
        parse_references(["gen:last"]), _event("session:a")
    )
    assert resolved.gallery_names == ["latest.png"]
    assert latest
    tracker._pending_image_deletions.add("latest.png")
    with pytest.raises(InternalReferenceError) as caught:
        await resolver.resolve(parse_references(["gen:last"]), _event("session:a"))
    assert caught.value.code == "reference_image_unavailable"
    await tracker.close()


@pytest.mark.asyncio
async def test_local_path_is_guarded_separately_from_gallery(tmp_path):
    allowed = tmp_path / "allowed"
    image = _png(allowed / "local.png")
    outside = _png(tmp_path / "outside.png")
    tracker = GenerationTracker(tmp_path / "history", 20)
    job_id = await _record(tracker, umo="session:a", images=("gallery.png",))
    resolver, _holder = _resolver(tracker, allowed)
    resolved = await resolver.resolve(
        parse_references([str(image), f"gen:{job_id}"]), _event()
    )
    assert resolved.user_files and resolved.user_files[0].startswith("file:")
    assert resolved.gallery_names == ["gallery.png"]
    assert resolved.gallery_files[0] not in resolved.user_files
    assert all(
        "gallery" not in item["label"] or item["kind"] == "gallery"
        for item in resolved.sources
    )
    with pytest.raises(InternalReferenceError, match="本地路径不可用") as caught:
        await resolver.resolve(parse_references([str(outside)]), _event())
    assert caught.value.code == "path_policy_rejected"
    await tracker.close()


@pytest.mark.asyncio
async def test_mixed_failure_and_image_limit_reject_the_whole_request(tmp_path):
    tracker = GenerationTracker(tmp_path / "history", 20)
    images = _Images(["one", "two"])
    resolver = ReferenceResolver(
        image_handler=images,
        avatar_handler=_Avatars(),
        generation_tracker=tracker,
        settings=lambda: _settings(max_reference_images=1),
    )
    with pytest.raises(InternalReferenceError, match="最多 1 张"):
        await resolver.resolve(parse_references(["current"]), _event())
    with pytest.raises(InternalReferenceError, match="gen:missing") as caught:
        await resolver.resolve(parse_references(["current", "gen:missing"]), _event())
    assert caught.value.token == "gen:missing"
    await tracker.close()


@pytest.mark.asyncio
async def test_missing_reference_name_still_rejects_begin(tmp_path):
    tracker = GenerationTracker(tmp_path / "history", 20)
    with pytest.raises(
        ReferenceImageUnavailableError, match="参考图片不存在或正在清理"
    ):
        await tracker.begin(
            source="command",
            prompt="draw",
            params={},
            requester={"user_id": "100"},
            reference_names=["missing.png"],
        )
    await tracker.close()


@pytest.mark.asyncio
async def test_share_permissions_persist_and_old_records_default_false(tmp_path):
    tracker = GenerationTracker(tmp_path / "history", 20)
    job_id = await _record(tracker, umo="session:a", user="100")
    with pytest.raises(ValueError, match="请求者本人"):
        await tracker.set_shared(job_id, True, user_id="200", umo="session:a")
    with pytest.raises(ValueError, match="请求者本人"):
        await tracker.set_shared(job_id, True, user_id="", umo="session:a")
    running = await tracker.begin(
        source="command",
        prompt="draw",
        params={},
        requester={"user_id": "100", "umo": "session:a"},
    )
    with pytest.raises(ValueError, match="已完成且有图片"):
        await tracker.set_shared(
            running["job_id"], True, user_id="100", umo="session:a"
        )
    shared = await tracker.set_shared(job_id, True, user_id="100", umo="session:a")
    assert shared["shared"] is True and shared["shared_at"]
    await tracker.close()

    restored = GenerationTracker(tmp_path / "history", 20)
    assert restored.get(job_id)["shared"] is True
    await restored.set_shared(job_id, False, user_id="100", umo="session:a")
    assert restored.get(job_id)["shared_at"] is None
    payload = {
        "jobs": [
            {
                "job_id": "legacy",
                "status": "succeeded",
                "requester": {"user_id": "100"},
                "images": ["legacy.png"],
            }
        ]
    }
    (tmp_path / "old" / "generation_history.json").parent.mkdir()
    (tmp_path / "old" / "generation_history.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    old = GenerationTracker(tmp_path / "old", 20)
    assert old.get("legacy")["shared"] is False
    assert old.get("legacy")["requester"] == {"user_id": "100"}
    await restored.close()
    await old.close()


def test_public_outputs_drop_session_origin():
    record = {
        "requester": {"user_id": "100", "umo": "platform:group:1"},
        "shared": True,
    }
    assert "platform:group:1" not in json.dumps(without_session_origin(record))
    assert without_session_origin(record)["shared"] is True

    service = ImageGenerationService.__new__(ImageGenerationService)
    service._state = "ready"

    class _Manager:
        async def get_for_plugin(self, task_id, plugin_id):
            return {"task_id": task_id, "requester": dict(record["requester"])}

    service.plugin = SimpleNamespace(background_task_manager=_Manager())
    public = __import__("asyncio").run(service.get_task("img-1", plugin_id="plugin"))
    assert public["requester"] == {"user_id": "100"}


def test_reference_cross_session_loads_and_updates_from_config():
    assert ConfigLoader({}).load().reference_cross_session is False
    enabled = ConfigLoader(
        {"service_settings": {"reference_cross_session": True}}
    ).load()
    assert enabled.reference_cross_session is True
    disabled = ConfigLoader(
        {"service_settings": {"reference_cross_session": "false"}}
    ).load()
    assert disabled.reference_cross_session is False
    schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
    item = schema["service_settings"]["items"]["reference_cross_session"]
    assert item["type"] == "bool" and item["default"] is False


def test_requester_from_event_includes_bounded_session_origin():
    event = SimpleNamespace(
        unified_msg_origin="platform:group:" + "1" * 1100,
        get_sender_id=lambda: "100",
        get_sender_name=lambda: "用户",
        get_group_id=lambda: "",
        get_message_type=lambda: SimpleNamespace(value="FriendMessage"),
    )
    requester = requester_from_event(event)
    assert requester["umo"] == event.unified_msg_origin[:1024]
    bare = SimpleNamespace(
        get_sender_id=lambda: "100",
        get_sender_name=lambda: "用户",
        get_group_id=lambda: "",
        get_message_type=lambda: SimpleNamespace(value="FriendMessage"),
    )
    assert "umo" not in requester_from_event(bare)


@pytest.mark.parametrize("token", ["gen:last#2", "gen:last#abc"])
def test_gen_last_rejects_any_index(token):
    with pytest.raises(InternalReferenceError, match="gen:last 不带序号") as caught:
        parse_references([token])
    assert caught.value.code == "invalid_request"


@pytest.mark.asyncio
async def test_gen_last_ignores_records_without_session_origin(tmp_path):
    tracker = GenerationTracker(tmp_path / "history", 20)
    old = await _record(tracker, umo="session:a", images=("old.png",))
    tracker._jobs[old]["requester"].pop("umo")
    resolver, _holder = _resolver(tracker, tmp_path)
    with pytest.raises(InternalReferenceError, match="本会话没有可引用的生成"):
        await resolver.resolve(parse_references(["gen:last"]), _event(""))
    with pytest.raises(InternalReferenceError, match="本会话没有可引用的生成"):
        await resolver.resolve(parse_references(["gen:last"]), _event("session:a"))
    await tracker.close()


@pytest.mark.asyncio
async def test_partial_success_can_be_referenced_and_shared(tmp_path):
    tracker = GenerationTracker(tmp_path / "history", 20)
    partial = await _record(
        tracker,
        umo="session:a",
        user="100",
        status="partial_success",
        images=("only.png",),
    )
    cancelled = await _record(tracker, umo="session:a", images=("cancelled.png",))
    tracker._jobs[cancelled]["status"] = "cancelled"
    failed = await _record(tracker, umo="session:a", status="failed", images=())
    resolver, _holder = _resolver(tracker, tmp_path)
    event = _event("session:a")

    resolved = await resolver.resolve(parse_references([f"gen:{partial}"]), event)
    assert resolved.gallery_names == ["only.png"]
    latest = await resolver.resolve(parse_references(["gen:last"]), event)
    assert latest.gallery_names == ["only.png"]
    with pytest.raises(InternalReferenceError, match="没有对应的图片"):
        await resolver.resolve(parse_references([f"gen:{partial}#2"]), event)
    shared = await tracker.set_shared(partial, True, user_id="100", umo="session:a")
    assert shared["shared"] is True
    with pytest.raises(InternalReferenceError, match="还不能引用"):
        await resolver.resolve(parse_references([f"gen:{cancelled}"]), event)
    with pytest.raises(InternalReferenceError, match="还不能引用"):
        await resolver.resolve(parse_references([f"gen:{failed}"]), event)
    with pytest.raises(ValueError, match="已完成且有图片"):
        await tracker.set_shared(cancelled, True, user_id="100", umo="session:a")
    await tracker.close()
