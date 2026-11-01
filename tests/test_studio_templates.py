"""提示词广场后端：模板增删改、导入导出、预览图与 Web API 守卫。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tl.prompt_templates import PromptTemplateStore
from tl.studio_templates import StudioTemplateService, _public_record
from tl.web_api import WebStudioAPI
from tl.web_studio_service import StudioServiceError

PREFIX = "/astrbot_plugin_gemini_image_generation/webui"


def _json_response(data=None, *, status_code=200, headers=None):
    from starlette.responses import JSONResponse

    return JSONResponse(data or {}, status_code=status_code, headers=headers)


def _error_response(message, *, status_code=400, data=None, headers=None):
    return _json_response(
        {"status": "error", "message": message, "data": data},
        status_code=status_code,
        headers=headers,
    )


def _file_response(path, *, filename=None, content_type=None, headers=None):
    from starlette.responses import FileResponse

    return FileResponse(
        path, filename=filename, media_type=content_type, headers=headers
    )


def _template(**overrides):
    payload = {
        "name": "我的模板",
        "content": "画一只{{input}}的猫",
        "ai_description": "一句话说明",
        "defaults": {"resolution": "2K", "aspect_ratio": "16:9"},
        "reference": "optional",
        "tags": ["测试"],
    }
    payload.update(overrides)
    return payload


def _package(*templates):
    return {
        "format": "gemini-image-templates",
        "version": 1,
        "templates": list(templates),
    }


def _service(tmp_path: Path) -> StudioTemplateService:
    store = PromptTemplateStore(tmp_path)
    gallery = tmp_path / "gallery"
    gallery.mkdir(parents=True, exist_ok=True)
    return StudioTemplateService(store, tmp_path, gallery_dir=gallery)


@pytest.mark.asyncio
async def test_list_includes_builtin_and_marks_origin(tmp_path):
    service = _service(tmp_path)
    records = service.list_templates()
    builtins = [item for item in records if item["builtin"]]
    assert builtins
    assert all(item["origin"] == "builtin" for item in builtins)
    assert all(item["origin"] == "local" for item in records if not item["builtin"])


@pytest.mark.asyncio
async def test_create_update_and_delete(tmp_path):
    service = _service(tmp_path)
    created = await service.save({"action": "create", "template": _template()})
    assert created["origin"] == "local"
    assert created["defaults"] == {"resolution": "2K", "aspect_ratio": "16:9"}

    updated = await service.save(
        {
            "action": "update",
            "id": created["id"],
            "changes": {"name": "改过的名字", "reference": "required"},
        }
    )
    assert updated["name"] == "改过的名字"
    assert updated["reference"] == "required"

    deleted = await service.save({"action": "delete", "id": created["id"]})
    assert deleted == {"id": created["id"]}
    assert service.get(created["id"]) is None


@pytest.mark.asyncio
async def test_builtin_cannot_be_deleted_but_can_restore(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(Exception):
        await service.save({"action": "delete", "id": "poster"})
    changed = await service.save(
        {"action": "update", "id": "poster", "changes": {"name": "海报改名"}}
    )
    assert changed["name"] == "海报改名"
    restored = await service.save({"action": "restore", "id": "poster"})
    assert restored["name"] != "海报改名"


@pytest.mark.asyncio
async def test_set_ai_enabled_rejects_non_boolean(tmp_path):
    service = _service(tmp_path)
    created = await service.save({"action": "create", "template": _template()})
    with pytest.raises(Exception):
        await service.save(
            {"action": "set_ai_enabled", "id": created["id"], "ai_enabled": "yes"}
        )
    disabled = await service.save(
        {"action": "set_ai_enabled", "id": created["id"], "ai_enabled": False}
    )
    assert disabled["ai_enabled"] is False


@pytest.mark.asyncio
async def test_unknown_action_is_rejected(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(StudioServiceError) as error:
        await service.save({"action": "explode"})
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_missing_id_is_rejected(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(StudioServiceError) as error:
        await service.save({"action": "delete"})
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_import_export_roundtrip(tmp_path):
    service = _service(tmp_path)
    source = _service(tmp_path / "other")
    await source.save({"action": "create", "template": _template(name="导出模板")})
    package = source.export_package()

    result = await service.import_package(package)
    assert result["imported"] >= 1
    names = {item["name"] for item in service.list_templates()}
    assert "导出模板" in names


@pytest.mark.asyncio
async def test_import_rejects_bad_format(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(Exception):
        await service.import_package({"format": "nope", "version": 1, "templates": []})


@pytest.mark.asyncio
async def test_import_requires_object(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(StudioServiceError) as error:
        await service.import_package(["not", "an", "object"])
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_save_asset_from_gallery_and_read_back(tmp_path):
    service = _service(tmp_path)
    (service.gallery_dir / "sample.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    name = await service.save_asset_from_gallery("sample.png")
    assert name.endswith(".png")
    assert service.asset_file(name).is_file()


@pytest.mark.asyncio
async def test_save_asset_rejects_traversal_and_missing(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(StudioServiceError) as bad_name:
        await service.save_asset_from_gallery("../secret.png")
    assert bad_name.value.status_code == 400
    with pytest.raises(StudioServiceError) as missing:
        await service.save_asset_from_gallery("nothing.png")
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_save_asset_rejects_non_image_suffix(tmp_path):
    service = _service(tmp_path)
    (service.gallery_dir / "notes.txt").write_bytes(b"hello")
    with pytest.raises(StudioServiceError) as error:
        await service.save_asset_from_gallery("notes.txt")
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_drop_asset_ignores_unsafe_names(tmp_path):
    service = _service(tmp_path)
    await service.drop_asset("../outside.png")
    await service.drop_asset("")
    await service.drop_asset(None)


def test_public_record_exposes_expected_fields(tmp_path):
    service = _service(tmp_path)
    record = _public_record(service.store.get("poster"))
    assert set(record) >= {
        "id",
        "name",
        "content",
        "reference",
        "ai_enabled",
        "origin",
        "builtin",
    }


class _Context:
    def __init__(self) -> None:
        self.registered_web_apis = []

    def register_web_api(self, route, handler, methods, desc) -> None:
        for index, entry in enumerate(self.registered_web_apis):
            if entry[0] == route and entry[2] == methods:
                self.registered_web_apis[index] = (route, handler, methods, desc)
                return
        self.registered_web_apis.append((route, handler, methods, desc))


def _api(tmp_path, monkeypatch, service=None):
    import tl.web_api as module
    from tl.generation_tracker import GenerationTracker

    monkeypatch.setattr(module, "WEB_API_AVAILABLE", True)
    monkeypatch.setattr(module, "json_response", _json_response)
    monkeypatch.setattr(module, "error_response", _error_response)
    monkeypatch.setattr(module, "file_response", _file_response)
    api = WebStudioAPI(
        GenerationTracker(tmp_path, 20),
        SimpleNamespace(),
        templates_service=service,
    )
    return api


def _request(*, method="GET", body=None, username="admin", query=None):
    payload = json.dumps(body or {}, ensure_ascii=False).encode()

    async def parse(default=None):
        return json.loads(payload)

    return SimpleNamespace(
        method=method,
        headers={},
        cookies={},
        query=query or {},
        username=username,
        json=AsyncMock(side_effect=parse),
        _request=SimpleNamespace(_body=payload, _receive=None),
    )


def test_template_routes_registered(tmp_path, monkeypatch):
    import tl.web_api as module

    api = _api(tmp_path, monkeypatch, service=_service(tmp_path))
    context = _Context()
    entries = api.register(context)
    routes = {route for route, *_ in entries}
    assert f"{PREFIX}/templates" in routes
    assert f"{PREFIX}/templates/import" in routes
    assert f"{PREFIX}/templates/export" in routes
    assert f"{PREFIX}/templates/asset" in routes
    assert f"{PREFIX}/template_asset/<name>" in routes
    assert module._PANEL_ONLY_METHODS["/templates"] == {"GET", "POST"}
    assert module._PANEL_ONLY_METHODS["/templates/import"] == {"POST"}


@pytest.mark.asyncio
async def test_templates_endpoint_lists_and_writes(tmp_path, monkeypatch):
    import tl.web_api as module

    service = _service(tmp_path)
    api = _api(tmp_path, monkeypatch, service=service)
    module.request = _request(method="GET")
    listed = await api.templates()
    assert json.loads(listed.body)["status"] == "ok"
    assert json.loads(listed.body)["data"]["templates"]

    module.request = _request(
        method="POST", body={"action": "create", "template": _template(name="接口模板")}
    )
    created = await api.templates()
    assert json.loads(created.body)["data"]["name"] == "接口模板"


@pytest.mark.asyncio
async def test_templates_endpoint_reports_missing_service(tmp_path, monkeypatch):
    import tl.web_api as module

    api = _api(tmp_path, monkeypatch, service=None)
    module.request = _request(method="GET")
    response = await api.templates()
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_templates_endpoint_rejects_invalid_json(tmp_path, monkeypatch):
    import tl.web_api as module

    api = _api(tmp_path, monkeypatch, service=_service(tmp_path))

    async def bad(default=None):
        return default

    module.request = SimpleNamespace(
        method="POST", headers={}, username="admin", json=AsyncMock(side_effect=bad)
    )
    response = await api.templates()
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_import_export_endpoints(tmp_path, monkeypatch):
    import tl.web_api as module

    service = _service(tmp_path)
    api = _api(tmp_path, monkeypatch, service=service)
    module.request = _request(
        method="POST", body=_package({**_template(name="包模板"), "id": "pkg-one"})
    )
    imported = await api.templates_import()
    # 默认导入会重新分配本地 id，内容按名称比对。
    assert json.loads(imported.body)["data"]["imported"] == 1

    module.request = _request(method="GET")
    exported = await api.templates_export()
    package = json.loads(exported.body)["data"]
    assert package["format"] == "gemini-image-templates"
    assert any(item["name"] == "包模板" for item in package["templates"])


@pytest.mark.asyncio
async def test_import_keep_ids_preserves_package_ids(tmp_path, monkeypatch):
    import tl.web_api as module

    service = _service(tmp_path)
    api = _api(tmp_path, monkeypatch, service=service)
    module.request = _request(
        method="POST",
        body={
            **_package({**_template(name="保留 id"), "id": "kept-one"}),
            "keep_ids": True,
        },
    )
    result = json.loads((await api.templates_import()).body)["data"]
    assert result["imported"] == 1
    assert result["results"][0]["id"] == "kept-one"


@pytest.mark.asyncio
async def test_import_endpoint_rejects_bad_package(tmp_path, monkeypatch):
    import tl.web_api as module

    api = _api(tmp_path, monkeypatch, service=_service(tmp_path))
    module.request = _request(method="POST", body={"format": "wrong"})
    response = await api.templates_import()
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_asset_endpoints(tmp_path, monkeypatch):
    import tl.web_api as module

    service = _service(tmp_path)
    (service.gallery_dir / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    api = _api(tmp_path, monkeypatch, service=service)

    module.request = _request(method="POST", body={"image_name": "shot.png"})
    saved = await api.template_asset()
    name = json.loads(saved.body)["data"]["name"]

    module.request = _request(method="GET")
    fetched = await api.template_asset_file(name)
    assert fetched.status_code == 200

    module.request = _request(method="GET")
    traversal = await api.template_asset_file("../secret")
    assert traversal.status_code == 400


@pytest.mark.asyncio
async def test_delete_through_api_drops_asset(tmp_path, monkeypatch):
    import tl.web_api as module

    service = _service(tmp_path)
    (service.gallery_dir / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    api = _api(tmp_path, monkeypatch, service=service)
    module.request = _request(method="POST", body={"image_name": "shot.png"})
    asset = json.loads((await api.template_asset()).body)["data"]["name"]
    module.request = _request(
        method="POST",
        body={
            "action": "create",
            "template": _template(name="带预览", preview=asset),
        },
    )
    created = json.loads((await api.templates()).body)["data"]
    module.request = _request(
        method="POST", body={"action": "delete", "id": created["id"]}
    )
    await api.templates()
    assert not service.asset_file.__self__.asset_dir.joinpath(asset).exists()
