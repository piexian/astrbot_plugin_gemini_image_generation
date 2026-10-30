"""WebUI 路由守卫与历史源链接脱敏。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_web_api import _api, _Context, _match
from tl.generation_tracker import GenerationTracker


def route(api, path, method):
    context = _Context()
    api.register(context)
    matched = _match(
        context.registered_web_apis,
        f"astrbot_plugin_gemini_image_generation/webui{path}",
        method,
    )
    assert matched is not None
    return matched


def set_request(
    monkeypatch, *, username, method, headers=None, query=None, payload=None
):
    import tl.web_api as module

    raw = SimpleNamespace(
        _body=b"",
        _receive=None,
        url=SimpleNamespace(scheme="https"),
    )
    adapter = SimpleNamespace(
        username=username,
        method=method,
        headers=headers or {},
        query=query or {},
        cookies={},
        path="/",
        _request=raw,
        json=AsyncMock(return_value=payload),
    )
    monkeypatch.setattr(module, "request", adapter)
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "method", "kwargs"),
    [
        ("/history", "GET", {}),
        ("/image/example.png", "GET", {"name": "example.png"}),
        ("/generate", "POST", {}),
        ("/history/delete", "POST", {}),
        ("/providers", "GET", {}),
        ("/providers/models", "POST", {}),
    ],
)
async def test_routes_require_host_identity(
    tmp_path, monkeypatch, path, method, kwargs
):
    api = _api(tmp_path, monkeypatch)
    handler, route_kwargs = route(api, path, method)
    set_request(monkeypatch, username="", method=method)
    response = await handler(**route_kwargs)
    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/providers", "GET"),
        ("/providers/models", "POST"),
        ("/history/delete", "POST"),
        ("/upload", "POST"),
    ],
)
async def test_api_key_cannot_use_panel_only_routes(
    tmp_path, monkeypatch, path, method
):
    api = _api(tmp_path, monkeypatch)
    handler, kwargs = route(api, path, method)
    set_request(monkeypatch, username="api_key:key-1", method=method)
    response = await handler(**kwargs)
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/generate", "POST"),
        ("/history/delete", "POST"),
        ("/providers", "POST"),
        ("/providers/models", "POST"),
    ],
)
async def test_cross_site_post_is_rejected(tmp_path, monkeypatch, path, method):
    api = _api(tmp_path, monkeypatch)
    handler, kwargs = route(api, path, method)
    set_request(
        monkeypatch,
        username="admin",
        method=method,
        headers={"Host": "dashboard.local", "Origin": "https://evil.local"},
    )
    response = await handler(**kwargs)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_fetch_metadata_cross_site_post_is_rejected(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch)
    handler, kwargs = route(api, "/generate", "POST")
    set_request(
        monkeypatch,
        username="admin",
        method="POST",
        headers={"Host": "dashboard.local", "Sec-Fetch-Site": "cross-site"},
    )
    response = await handler(**kwargs)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_api_key_can_read_history_and_generate(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch)
    history, history_kwargs = route(api, "/history", "GET")
    set_request(monkeypatch, username="api_key:key-1", method="GET", query={})
    assert (await history(**history_kwargs)).status_code == 200

    generate, generate_kwargs = route(api, "/generate", "POST")
    set_request(
        monkeypatch,
        username="api_key:key-1",
        method="POST",
        payload={"prompt": "draw"},
    )
    response = await generate(**generate_kwargs)
    assert response.status_code == 202
    assert json.loads(response.body)["data"]["job_id"] == "job-one"


@pytest.mark.asyncio
async def test_same_origin_panel_provider_read_passes(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch)
    api.providers_service = SimpleNamespace(
        get_config=AsyncMock(return_value={"entries": []})
    )
    handler, kwargs = route(api, "/providers", "GET")
    set_request(monkeypatch, username="admin", method="GET")
    response = await handler(**kwargs)
    assert response.status_code == 200
    assert json.loads(response.body)["data"] == {"entries": []}


@pytest.mark.asyncio
async def test_image_filename_traversal_is_still_rejected(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch)
    handler, kwargs = route(api, "/image/..%2Fsecret.png", "GET")
    set_request(monkeypatch, username="admin", method="GET")
    response = await handler(**kwargs)
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_source_urls_are_redacted_on_write_and_api_reads(tmp_path, monkeypatch):
    tracker = GenerationTracker(tmp_path, 20)
    raw = "https://user:pass@example.com/image.png?token=secret#fragment"
    record = await tracker.begin(source="webui", prompt="draw", params={}, requester={})
    await tracker.complete(
        record["job_id"],
        image_files=[],
        text_content="text",
        stats={},
        status="partial_success",
        source_urls=[raw],
    )
    safe = "https://example.com/image.png"
    assert tracker.get(record["job_id"])["source_urls"] == [safe]

    # 模拟旧版本磁盘记录：读取响应脱敏，但不回写原记录。
    tracker._jobs[record["job_id"]]["source_urls"] = [raw]
    api = _api(tmp_path, monkeypatch)
    old_api_tracker = api.tracker
    api.tracker = tracker
    api.service.reference_status = lambda names: {
        "available_reference_names": [],
        "missing_reference_names": [],
    }
    set_request(monkeypatch, username="admin", method="GET", query={})
    try:
        jobs = await api.jobs()
        history = await api.history()
        detail = await api.history_detail(record["job_id"])
        for response in (jobs, history, detail):
            body = response.body.decode()
            assert safe in body
            assert "token=secret" not in body
            assert "user:pass" not in body
        assert tracker.get(record["job_id"])["source_urls"] == [raw]
    finally:
        await tracker.close()
        await old_api_tracker.close()
