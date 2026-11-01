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


class _FakeHubSource:
    """替代在线客户端，记录调用并返回可控结果。"""

    def __init__(self, base_url="https://image-hub.alxy.app", api_key=None):
        self.base_url = base_url
        self.api_key = api_key
        self.source_name = "image-hub.alxy.app"
        self.calls: list = []
        self.search_results = []
        self.search_error = None
        self.closed = False

    async def search(self, query, *, tag=None, cursor=None, limit=20):
        from tl.prompt_templates import SearchPage

        self.calls.append(("search", query, tag))
        if self.search_error:
            raise self.search_error
        return SearchPage(templates=list(self.search_results), next_cursor=None)

    async def catalog_version(self):
        from tl.prompt_templates import CatalogVersion

        return CatalogVersion(hash="h", count=0, updated_at="", size=0)

    async def download_catalog(self, etag=None):
        items = [self.catalog_item] if getattr(self, "catalog_item", None) else []
        return ('W/"h"', {"updated_at": "", "templates": items})

    async def request_key(self, email):
        self.calls.append(("request_key", email))

    async def key_status(self):
        self.calls.append(("key_status",))
        return {"scopes": ["read", "write"]}

    async def revoke_key(self):
        self.calls.append(("revoke_key",))

    async def upload(self, template):
        self.calls.append(("upload", template.id))
        return template

    async def close(self):
        self.closed = True


def _hub_service(tmp_path, *, mode="cache", source=None):
    service = _service(tmp_path)
    service.reconfigure_hub(mode=mode, url="", key="")
    if source is not None:
        service.hub_source = source
        service.hub_cache.reconfigure(source=source, mode=mode)
    return service


def test_hub_reconfigure_off_clears_source(tmp_path):
    service = _hub_service(tmp_path, source=_FakeHubSource())
    service.reconfigure_hub(mode="off")
    assert service.hub_source is None
    assert not service.hub_cache.enabled
    assert service.hub_state()["mode"] == "off"


def test_hub_state_reports_mode_and_cached_hash(tmp_path):
    service = _hub_service(tmp_path, mode="online", source=_FakeHubSource())
    state = service.hub_state()
    assert state["mode"] == "online"
    assert state["live_search"] is True
    assert state["url"] == "https://image-hub.alxy.app"


@pytest.mark.asyncio
async def test_hub_search_uses_cache_only_in_cache_mode(tmp_path):
    source = _FakeHubSource()
    source.catalog_item = _online_template()
    service = _hub_service(tmp_path, mode="cache", source=source)
    await service.sync_hub(force=True)

    result = await service.hub_search("猫")
    assert result["templates"][0]["found_via"] == "cache"
    assert source.calls == []


@pytest.mark.asyncio
async def test_hub_search_merges_online_results_in_online_mode(tmp_path):
    from tl.prompt_templates import PromptTemplate, RemoteOrigin
    from tl.prompt_templates.models import utc_now

    source = _FakeHubSource()
    service = _hub_service(tmp_path, mode="online", source=source)
    source.search_results = [
        PromptTemplate(
            id="online-one",
            name="在线模板",
            content="画 {{input}}",
            reference="none",
            remote=RemoteOrigin(source="image-hub.alxy.app", remote_id="online-one"),
            created_at=utc_now(),
            updated_at=utc_now(),
        )
    ]
    result = await service.hub_search("在线")
    assert [item["found_via"] for item in result["templates"]] == ["online"]
    assert source.calls == [("search", "在线", None)]


@pytest.mark.asyncio
async def test_hub_search_reports_notice_on_source_error(tmp_path):
    from tl.prompt_templates import SourceUnavailable

    source = _FakeHubSource()
    source.search_error = SourceUnavailable("down")
    service = _hub_service(tmp_path, mode="online", source=source)
    result = await service.hub_search("猫")
    assert "不可用" in result["notice"]


def _online_template():
    return {
        "id": "id0aaaaaaa",
        "name": "在线猫",
        "content": "画一只猫 {{input}}",
        "ai_description": "猫模板",
        "reference": "none",
        "tags": ["动物"],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_hub_import_creates_local_copy(tmp_path):
    source = _FakeHubSource()

    class _CatalogSource(_FakeHubSource):
        async def download_catalog(self, etag=None):
            return ('W/"hh"', {"updated_at": "", "templates": [_online_template()]})

    source = _CatalogSource()
    service = _hub_service(tmp_path, source=source)
    await service.sync_hub(force=True)

    result = await service.hub_import(["id0aaaaaaa"])
    assert result["imported"] == 1
    names = {item["name"] for item in service.list_templates()}
    assert "在线猫" in names


@pytest.mark.asyncio
async def test_hub_import_rejects_empty_and_missing(tmp_path):
    service = _hub_service(tmp_path, source=_FakeHubSource())
    with pytest.raises(StudioServiceError):
        await service.hub_import([])
    with pytest.raises(StudioServiceError):
        await service.hub_import([str(i) for i in range(101)])
    result = await service.hub_import(["missing"])
    assert result["failed"] == 1


@pytest.mark.asyncio
async def test_hub_key_operations_go_through_source(tmp_path):
    source = _FakeHubSource()
    service = _hub_service(tmp_path, source=source)
    assert (await service.hub_key_request("a@b.com"))["status"] == "sent"
    assert (await service.hub_key_check())["key"]["scopes"] == ["read", "write"]
    assert (await service.hub_key_revoke())["status"] == "revoked"
    assert [call[0] for call in source.calls] == [
        "request_key",
        "key_status",
        "revoke_key",
    ]


@pytest.mark.asyncio
async def test_hub_operations_require_source(tmp_path):
    service = _hub_service(tmp_path, mode="off")
    with pytest.raises(StudioServiceError):
        await service.hub_key_check()


@pytest.mark.asyncio
async def test_hub_upload_maps_source_errors(tmp_path):
    from tl.prompt_templates import SourceAuthError, SourceRejected

    service = _hub_service(tmp_path, source=_FakeHubSource())
    created = await service.save(
        {"action": "create", "template": _template(name="待上传")}
    )

    preview = service.hub_upload_preview(created["id"])
    assert preview["content"] == created["content"]
    assert "preview" not in preview

    uploaded = await service.hub_upload(created["id"])
    assert uploaded["name"] == "待上传"

    source = _FakeHubSource()

    async def reject(template):
        raise SourceRejected("名称重复")

    source.upload = reject
    service.hub_source = source
    with pytest.raises(StudioServiceError) as error:
        await service.hub_upload(created["id"])
    assert error.value.status_code == 400

    async def unauthorized(template):
        raise SourceAuthError("bad key")

    source.upload = unauthorized
    with pytest.raises(StudioServiceError) as auth:
        await service.hub_upload(created["id"])
    assert auth.value.status_code == 403


@pytest.mark.asyncio
async def test_hub_upload_preview_missing_template(tmp_path):
    service = _hub_service(tmp_path, source=_FakeHubSource())
    with pytest.raises(StudioServiceError) as error:
        service.hub_upload_preview("nope")
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_close_closes_hub_source(tmp_path):
    source = _FakeHubSource()
    service = _hub_service(tmp_path, source=source)
    await service.close()
    assert source.closed
    assert service.hub_source is None


# --- 在线路由 ---


def _hub_api(tmp_path, monkeypatch, service, *, method="GET", body=None, query=None):
    import tl.web_api as module

    api = _api(tmp_path, monkeypatch, service=service)
    module.request = _request(method=method, body=body, query=query)
    return api


@pytest.mark.asyncio
async def test_hub_routes_registered(tmp_path, monkeypatch):
    import tl.web_api as module

    api = _api(tmp_path, monkeypatch, service=_hub_service(tmp_path))
    context = _Context()
    routes = {route for route, *_ in api.register(context)}
    assert f"{PREFIX}/templates/hub" in routes
    assert f"{PREFIX}/templates/hub/sync" in routes
    assert f"{PREFIX}/templates/hub/import" in routes
    assert f"{PREFIX}/templates/hub/key" in routes
    assert module._PANEL_ONLY_METHODS["/templates/hub/key"] == {"GET", "POST", "DELETE"}


@pytest.mark.asyncio
async def test_hub_endpoint_search(tmp_path, monkeypatch):
    service = _hub_service(tmp_path, source=_FakeHubSource())
    api = _hub_api(tmp_path, monkeypatch, service, query={"q": "猫", "limit": "10"})
    response = await api.templates_hub()
    assert json.loads(response.body)["status"] == "ok"


@pytest.mark.asyncio
async def test_hub_endpoint_upload_and_preview(tmp_path, monkeypatch):
    service = _hub_service(tmp_path, source=_FakeHubSource())
    created = await service.save(
        {"action": "create", "template": _template(name="上传用")}
    )
    api = _hub_api(
        tmp_path,
        monkeypatch,
        service,
        method="POST",
        body={"action": "upload", "id": created["id"]},
    )
    uploaded = await api.templates_hub()
    assert json.loads(uploaded.body)["data"]["name"] == "上传用"

    api = _hub_api(
        tmp_path,
        monkeypatch,
        service,
        method="POST",
        body={"action": "preview", "id": created["id"]},
    )
    preview = await api.templates_hub()
    assert json.loads(preview.body)["data"]["content"] == created["content"]

    api = _hub_api(tmp_path, monkeypatch, service, method="POST", body={"action": "x"})
    assert (await api.templates_hub()).status_code == 400


@pytest.mark.asyncio
async def test_hub_sync_and_import_endpoints(tmp_path, monkeypatch):
    service = _hub_service(tmp_path, source=_FakeHubSource())
    api = _hub_api(tmp_path, monkeypatch, service, method="POST", body={"force": True})
    synced = await api.templates_hub_sync()
    assert json.loads(synced.body)["data"]["mode"] == "cache"

    api = _hub_api(
        tmp_path, monkeypatch, service, method="POST", body={"ids": ["missing"]}
    )
    imported = await api.templates_hub_import()
    assert json.loads(imported.body)["data"]["failed"] == 1

    api = _hub_api(tmp_path, monkeypatch, service, method="POST", body={"ids": []})
    import tl.web_api as module

    async def bad(default=None):
        return default

    module.request.json = AsyncMock(side_effect=bad)
    assert (await api.templates_hub_import()).status_code == 400


@pytest.mark.asyncio
async def test_hub_key_endpoints(tmp_path, monkeypatch):
    service = _hub_service(tmp_path, source=_FakeHubSource())
    api = _hub_api(tmp_path, monkeypatch, service, method="GET")
    assert json.loads((await api.templates_hub_key()).body)["status"] == "ok"

    api = _hub_api(tmp_path, monkeypatch, service, method="DELETE")
    assert (
        json.loads((await api.templates_hub_key()).body)["data"]["status"] == "revoked"
    )

    api = _hub_api(
        tmp_path, monkeypatch, service, method="POST", body={"email": "a@b.com"}
    )
    assert json.loads((await api.templates_hub_key()).body)["data"]["status"] == "sent"


@pytest.mark.asyncio
async def test_hub_endpoints_report_missing_service(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch, service=None)
    import tl.web_api as module

    module.request = _request(method="GET")
    assert (await api.templates_hub()).status_code == 503
    module.request = _request(method="GET")
    assert (await api.templates_hub_key()).status_code == 503


@pytest.mark.asyncio
async def test_save_hub_key_writes_plugin_config(tmp_path):
    saved: dict = {}

    class RawConfig:
        async def save_config_async(self, update):
            saved.update(update)

    service = _service(tmp_path)
    service.raw_config = RawConfig()
    result = await service.save_hub_key("iph_secret_value")
    assert saved["template_hub_key"] == "iph_secret_value"
    assert result["has_key"] is True


@pytest.mark.asyncio
async def test_save_hub_key_empty_clears(tmp_path):
    saved: dict = {}

    class RawConfig:
        async def save_config_async(self, update):
            saved.update(update)

    service = _service(tmp_path)
    service.raw_config = RawConfig()
    await service.save_hub_key("iph_x")
    result = await service.save_hub_key("")
    assert saved["template_hub_key"] == ""
    assert result["has_key"] is False


@pytest.mark.asyncio
async def test_save_hub_key_without_host_interface_raises(tmp_path):
    service = _service(tmp_path)
    service.raw_config = None
    with pytest.raises(StudioServiceError) as error:
        await service.save_hub_key("iph_x")
    assert error.value.status_code == 503


def test_mask_key_never_reveals_full_value():
    from tl.studio_templates import _mask_key

    masked = _mask_key("iph_abcdefghijklmnop")
    assert "abcdefghijklmnop" not in masked
    assert masked.startswith("iph_")
    assert masked.endswith("mnop")
    assert _mask_key("short") == "*****"


@pytest.mark.asyncio
async def test_hub_key_get_returns_mask_not_secret(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service._hub_key = "iph_abcdefghijklmnop"
    api = _hub_api(tmp_path, monkeypatch, service, method="GET")
    data = json.loads((await api.templates_hub_key()).body)["data"]
    assert data["configured"] is True
    assert "abcdefghijklmnop" not in data["masked"]


@pytest.mark.asyncio
async def test_hub_key_test_requires_key(tmp_path):
    service = _service(tmp_path)
    service._hub_key = ""
    with pytest.raises(StudioServiceError) as error:
        await service.hub_key_test()
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_hub_report_requires_reason(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(StudioServiceError) as error:
        await service.hub_report("abcdefghijk1", "  ")
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_hub_report_requires_identifier(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(StudioServiceError) as error:
        await service.hub_report("", "不合适")
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_hub_report_sends_reason(tmp_path, monkeypatch):
    calls: list = []

    class Source:
        async def report(self, identifier, reason):
            calls.append((identifier, reason))

    service = _service(tmp_path)
    monkeypatch.setattr(service, "_with_source", lambda: Source())
    result = await service.hub_report("abcdefghijk1", "  不合适  ")
    assert result == {"status": "reported"}
    assert calls == [("abcdefghijk1", "不合适")]


@pytest.mark.asyncio
async def test_hub_mine_requires_key(tmp_path):
    service = _service(tmp_path)
    service._hub_key = ""
    with pytest.raises(StudioServiceError) as error:
        await service.hub_mine()
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_hub_mine_returns_records(tmp_path, monkeypatch):
    class Source:
        async def my_templates(self, *, limit=50):
            assert limit == 50
            from tl.prompt_templates import validate_template_data

            item = validate_template_data(
                {
                    "id": "abcdefghijk1",
                    "name": "我的模板",
                    "content": "画一只 {{input}}",
                    "tags": ["测试"],
                    "reference": "none",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                    "origin": "remote",
                },
                origin="remote",
            )
            return [item]

    service = _service(tmp_path)
    service._hub_key = "iph_x"
    monkeypatch.setattr(service, "_with_source", lambda: Source())
    result = await service.hub_mine(limit=50)
    assert len(result["templates"]) == 1


@pytest.mark.asyncio
async def test_hub_tags_returns_list(tmp_path, monkeypatch):
    class Source:
        async def tags(self, *, query="", limit=20):
            assert query == ""
            return [{"tag": "摄影", "count": 5}]

    service = _service(tmp_path)
    monkeypatch.setattr(service, "_with_source", lambda: Source())
    result = await service.hub_tags()
    assert result == {"tags": [{"tag": "摄影", "count": 5}]}


@pytest.mark.asyncio
async def test_hub_update_rejects_unknown_fields(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(StudioServiceError) as error:
        await service.hub_update("abcdefghijk1", {"id": "x", "owner": "me"})
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_hub_update_requires_local_source(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(StudioServiceError) as error:
        await service.hub_update("abcdefghijk1", {"name": "新名字"})
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_hub_delete_requires_identifier(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(StudioServiceError) as error:
        await service.hub_delete("")
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_hub_delete_calls_source(tmp_path, monkeypatch):
    calls = []

    class Source:
        async def delete(self, identifier):
            calls.append(identifier)

    service = _service(tmp_path)
    monkeypatch.setattr(service, "_with_source", lambda: Source())
    assert await service.hub_delete("abcdefghijk1") == {"status": "deleted"}
    assert calls == ["abcdefghijk1"]
