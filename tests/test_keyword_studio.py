"""关键词触发：Studio 配置服务与生成器注入钩子。"""

from __future__ import annotations

from typing import Any

import pytest

from tl.image_generator import ImageGenerator
from tl.keyword_trigger import KeywordTriggerService
from tl.studio_keywords import StudioKeywordService
from tl.web_studio_service import StudioServiceError

_REF = "ref-aaaaaaaaaaaa"
_REF_B = "ref-bbbbbbbbbbbb"
_PATH = "/data/references/ref-aaaaaaaaaaaa.png"
_PATH_B = "/data/references/ref-bbbbbbbbbbbb.png"


def _resolve(ref_ids=frozenset({_REF, _REF_B})):
    def resolve(ref_id: str) -> dict[str, Any] | None:
        if ref_id in ref_ids:
            return {"path": f"/data/references/{ref_id}.png", "label": f"标签{ref_id}"}
        return None

    return resolve


class _RawConfig(dict):
    """带宿主保存接口的假配置对象。"""

    def __init__(self, section: dict[str, Any]) -> None:
        super().__init__({"keyword_trigger": section})
        self.saved: list[dict[str, Any]] = []

    async def save_config_async(self, update: dict[str, Any]) -> bool:
        self.update(update)
        self.saved.append(update)
        return True


class _Library:
    def __init__(self, ids=frozenset({_REF, _REF_B})) -> None:
        self.ids = set(ids)

    def get(self, ref_id: str):
        return {"label": f"标签{ref_id}"} if ref_id in self.ids else None


def _service(raw: _RawConfig, library=_Library(), trigger=None):
    config = type(
        "Cfg", (), {"keyword_trigger": dict(raw.get("keyword_trigger") or {})}
    )()
    trigger = trigger or KeywordTriggerService(_resolve())
    trigger.refresh(raw.get("keyword_trigger") or {})
    return (
        StudioKeywordService(
            raw,
            config,
            library,
            trigger,
            lambda: 5,
            "/tmp/keyword-test-data",
        ),
        trigger,
    )


def _binding(words=("水彩",), ref_ids=(_REF,), append="", enabled=True):
    return {
        "name": "画风",
        "words": list(words),
        "ref_ids": list(ref_ids),
        "prompt_append": append,
        "enabled": enabled,
    }


# ---------- 页面数据 ----------


def test_get_keywords_marks_missing_refs():
    raw = _RawConfig(
        {
            "mode": "command",
            "bindings": [_binding(ref_ids=(_REF, "ref-cccccccccccc"))],
        }
    )
    service, _ = _service(raw, _Library(ids={_REF}))
    payload = service.get_keywords()
    assert payload["settings"]["mode"] == "command"
    (binding,) = payload["bindings"]
    assert binding["refs"][0]["missing"] is False
    assert binding["refs"][1]["missing"] is True


# ---------- 保存校验 ----------


@pytest.mark.asyncio
async def test_save_rejects_one_sided_wrapper():
    raw = _RawConfig({"mode": "command", "bindings": []})
    service, _ = _service(raw)
    with pytest.raises(StudioServiceError):
        await service.save_keywords(
            {
                "revision": service.get_keywords()["revision"],
                "settings": {"mode": "auto", "wrapper_left": "#", "wrapper_right": ""},
                "bindings": [],
            }
        )


@pytest.mark.asyncio
async def test_save_rejects_unknown_ref_id():
    raw = _RawConfig({"mode": "command", "bindings": []})
    service, _ = _service(raw)
    with pytest.raises(StudioServiceError):
        await service.save_keywords(
            {
                "revision": service.get_keywords()["revision"],
                "settings": {"mode": "command"},
                "bindings": [_binding(ref_ids=("ref-zzzzzzzzzzzz",))],
            }
        )


@pytest.mark.asyncio
async def test_save_rejects_binding_without_words():
    raw = _RawConfig({"mode": "command", "bindings": []})
    service, _ = _service(raw)
    with pytest.raises(StudioServiceError):
        await service.save_keywords(
            {
                "revision": service.get_keywords()["revision"],
                "settings": {"mode": "command"},
                "bindings": [{"name": "x", "ref_ids": []}],
            }
        )


@pytest.mark.asyncio
async def test_save_keeps_previously_configured_missing_refs():
    raw = _RawConfig(
        {
            "mode": "command",
            "bindings": [_binding(ref_ids=(_REF, "ref-cccccccccccc"))],
        }
    )
    service, _ = _service(raw, _Library(ids={_REF}))
    payload = await service.save_keywords(
        {
            "revision": service.get_keywords()["revision"],
            "settings": {"mode": "command"},
            "bindings": [_binding(ref_ids=(_REF, "ref-cccccccccccc"))],
        }
    )
    assert raw.saved, "宿主保存接口应被调用"
    assert payload["bindings"][0]["refs"][1]["missing"] is True


@pytest.mark.asyncio
async def test_save_roundtrip_writes_template_key_and_refreshes_runtime():
    raw = _RawConfig({"mode": "command", "bindings": []})
    service, trigger = _service(raw)
    payload = await service.save_keywords(
        {
            "revision": service.get_keywords()["revision"],
            "settings": {
                "mode": "auto",
                "wrapper_left": "#",
                "wrapper_right": "#",
                "max_refs": 2,
            },
            "bindings": [_binding(words=("水彩", "彩铅"), append="转为水彩")],
        }
    )
    (update,) = raw.saved
    section = update["keyword_trigger"]
    assert section["mode"] == "auto"
    assert section["bindings"][0]["__template_key"] == "binding"
    assert payload["settings"]["max_refs"] == 2
    # 保存即刷新运行时：auto 生效且注入路径可解析
    assert trigger.mode == "auto"
    expansion = trigger.expand_auto("来一张 #水彩#", slots=5)
    assert expansion is not None
    assert expansion.refs[0]["path"] == _PATH
    assert expansion.prompt_append == "转为水彩"


@pytest.mark.asyncio
async def test_save_conflict_returns_409():
    raw = _RawConfig({"mode": "command", "bindings": []})
    service, _ = _service(raw)
    with pytest.raises(StudioServiceError) as exc_info:
        await service.save_keywords(
            {
                "revision": "stale",
                "settings": {"mode": "command"},
                "bindings": [],
            }
        )
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_save_failure_rolls_back_runtime_section():
    class _Broken(_RawConfig):
        async def save_config_async(self, update):
            raise OSError("disk full")

    raw = _Broken({"mode": "command", "bindings": []})
    service, _ = _service(raw)
    with pytest.raises(StudioServiceError):
        await service.save_keywords(
            {
                "revision": service.get_keywords()["revision"],
                "settings": {"mode": "command"},
                "bindings": [_binding()],
            }
        )
    assert raw.get("keyword_trigger") == {"mode": "command", "bindings": []}


# ---------- 预演 ----------


def test_preview_shares_matcher_with_runtime():
    raw = _RawConfig(
        {
            "mode": "command",
            "max_refs": 1,
            "bindings": [
                _binding(words=("水彩",), ref_ids=(_REF, _REF_B), append="转为水彩")
            ],
        }
    )
    service, _ = _service(raw)
    result = service.preview("把图改成水彩", manual_count=0)
    assert result["hits"][0]["words"] == ["水彩"]
    assert [item["id"] for item in result["refs"]] == [_REF]
    assert result["dropped"] == [{"binding": "画风", "id": _REF_B}]
    assert result["prompt_append"] == "转为水彩"
    assert "path" not in result["refs"][0]


def test_preview_without_hits_is_empty():
    raw = _RawConfig({"mode": "command", "bindings": [_binding()]})
    service, _ = _service(raw)
    assert service.preview("一张猫", manual_count=0)["hits"] == []


# ---------- 生成器注入钩子 ----------


class _V2Service:
    async def prepare_references(self, refs, requester=None, **_):
        return list(refs)


def _generator(trigger):
    return ImageGenerator(
        context=None,
        api_client=object(),
        max_reference_images=5,
        keyword_trigger=trigger,
    )


class _Event:
    unified_msg_origin = "platform:FriendMessage:user-1"

    def get(self, name, default=None):
        return getattr(self, name, default)


@pytest.mark.asyncio
async def test_prepare_v2_request_injects_keyword_refs_and_prompt():
    trigger = KeywordTriggerService(_resolve())
    trigger.refresh(
        {
            "mode": "auto",
            "bindings": [_binding(words=("水彩",), append="转为水彩")],
        }
    )
    generator = _generator(trigger)
    request = await generator.prepare_v2_request(
        event=_Event(),
        v2_service=_V2Service(),
        prompt="画一张水彩猫",
        reference_images=[],
        avatar_reference=[],
        deadline_seconds=10,
    )
    assert list(request.reference_images) == [_PATH]
    assert request.prompt.endswith("转为水彩")
    assert request.prompt.startswith("画一张水彩猫")


@pytest.mark.asyncio
async def test_prepare_v2_request_skips_when_already_expanded():
    trigger = KeywordTriggerService(_resolve())
    trigger.refresh(
        {
            "mode": "auto",
            "bindings": [_binding(words=("水彩",), append="转为水彩")],
        }
    )
    generator = _generator(trigger)
    request = await generator.prepare_v2_request(
        event=_Event(),
        v2_service=_V2Service(),
        prompt="画一张水彩猫",
        reference_images=[],
        avatar_reference=[],
        deadline_seconds=10,
        keyword_expanded=True,
    )
    assert list(request.reference_images) == []
    assert request.prompt == "画一张水彩猫"


# ---------- 审查修复：保存上限与原始文本匹配 ----------


@pytest.mark.asyncio
async def test_save_rejects_too_many_bindings():
    raw = _RawConfig({"mode": "command", "bindings": []})
    service, _ = _service(raw)
    bindings = [_binding(words=(f"词{index}",), ref_ids=()) for index in range(33)]
    with pytest.raises(StudioServiceError):
        await service.save_keywords(
            {
                "revision": service.get_keywords()["revision"],
                "settings": {"mode": "command"},
                "bindings": bindings,
            }
        )


@pytest.mark.asyncio
async def test_save_rejects_prompt_append_total_overflow():
    from tl.keyword_trigger import MAX_PROMPT_APPEND_TOTAL

    raw = _RawConfig({"mode": "command", "bindings": []})
    service, _ = _service(raw)
    huge = "a" * (MAX_PROMPT_APPEND_TOTAL // 2 + 10)
    bindings = [_binding(words=(f"词{index}",), append=huge) for index in range(5)]
    with pytest.raises(StudioServiceError):
        await service.save_keywords(
            {
                "revision": service.get_keywords()["revision"],
                "settings": {"mode": "command"},
                "bindings": bindings,
            }
        )


@pytest.mark.asyncio
async def test_prepare_v2_request_matches_raw_text_not_rendered():
    trigger = KeywordTriggerService(_resolve())
    trigger.refresh(
        {
            "mode": "auto",
            "bindings": [_binding(words=("水彩",), append="转为水彩")],
        }
    )
    generator = _generator(trigger)
    # 渲染产物不含关键词，原始文本包含：匹配必须用原始文本
    request = await generator.prepare_v2_request(
        event=_Event(),
        v2_service=_V2Service(),
        prompt="图像生成任务：一只可爱的猫",
        reference_images=[],
        avatar_reference=[],
        deadline_seconds=10,
        keyword_match_text="画一张水彩猫",
    )
    assert list(request.reference_images) == [_PATH]
    assert request.prompt.startswith("图像生成任务：一只可爱的猫")


# ---------- 上传文件与地址 ----------


@pytest.mark.asyncio
async def test_save_persists_files_and_addresses_and_returns_state(tmp_path):
    raw = _RawConfig({"bindings": []})
    service, _ = _service(raw)
    service.data_dir = tmp_path
    rel = "files/keyword_trigger/bindings/templates/binding/ref_files/a.png"
    addresses = ["https://example.com/a.png", str(tmp_path / "x.png")]
    result = await service.save_keywords(
        {
            "revision": service._revision(),
            "settings": {"mode": "command"},
            "bindings": [
                {**_binding(), "ref_files": [rel], "ref_addresses": addresses}
            ],
        }
    )
    saved = raw["keyword_trigger"]["bindings"][0]
    assert saved["ref_files"] == [rel]
    assert saved["ref_addresses"] == addresses
    binding = result["bindings"][0]
    assert binding["files"][0]["path"] == rel
    assert binding["files"][0]["missing"] is True
    assert binding["files"][0]["gen"].startswith("gen:kw-")
    assert {item["kind"] for item in binding["addresses"]} == {"url", "path"}
    assert all(item["gen"].startswith("gen:kw-") for item in binding["addresses"])


@pytest.mark.asyncio
async def test_save_rejects_invalid_file_and_address():
    raw = _RawConfig({"bindings": []})
    service, _ = _service(raw)
    base = {"revision": service._revision(), "settings": {"mode": "command"}}
    with pytest.raises(StudioServiceError):
        await service.save_keywords(
            {**base, "bindings": [{**_binding(), "ref_files": ["files/../escape.png"]}]}
        )
    with pytest.raises(StudioServiceError):
        await service.save_keywords(
            {
                **base,
                "bindings": [{**_binding(), "ref_addresses": ["relative/path.png"]}],
            }
        )


@pytest.mark.asyncio
async def test_file_upload_list_and_delete_guard(tmp_path):
    raw = _RawConfig({"bindings": []})
    service, _ = _service(raw)
    service.data_dir = tmp_path
    result = await service.save_file("../../evil.png", b"data")
    assert result["path"].endswith("/evil.png")
    with pytest.raises(StudioServiceError):
        await service.save_file("evil.exe", b"data")
    rel = result["path"]
    assert rel in service.list_files()["files"]
    await service.save_keywords(
        {
            "revision": service._revision(),
            "settings": {"mode": "command"},
            "bindings": [{**_binding(), "ref_files": [rel]}],
        }
    )
    with pytest.raises(StudioServiceError) as exc:
        await service.delete_file(rel)
    assert exc.value.status_code == 409
    await service.save_keywords(
        {
            "revision": service._revision(),
            "settings": {"mode": "command"},
            "bindings": [],
        }
    )
    await service.delete_file(rel)
    assert rel not in service.list_files()["files"]
