from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.responses import JSONResponse

from tl.web_api import WebStudioAPI
from tl.web_studio_service import StudioServiceError


@pytest.fixture
def api(monkeypatch):
    import tl.web_api as module

    def error(message, *, status_code=400, data=None):
        return JSONResponse(
            {"status": "error", "message": message, "data": data},
            status_code=status_code,
        )

    monkeypatch.setattr(
        module, "json_response", lambda value, **kwargs: JSONResponse(value, **kwargs)
    )
    monkeypatch.setattr(module, "error_response", error)
    monkeypatch.setattr(
        module,
        "request",
        SimpleNamespace(
            username="admin",
            method="GET",
            query={},
            headers={},
            json=AsyncMock(return_value={}),
        ),
    )
    limits = SimpleNamespace(
        load_limits=AsyncMock(return_value={"revision": "v1"}),
        save_limits=AsyncMock(return_value={"revision": "v2"}),
        sessions=AsyncMock(return_value={"sessions": [], "total": 0}),
    )
    return WebStudioAPI(None, None, limits_service=limits)


@pytest.mark.asyncio
async def test_limits_auth_and_envelope(api, monkeypatch):
    import tl.web_api as module

    response = await api.limits()
    assert json.loads(response.body) == {"status": "ok", "data": {"revision": "v1"}}
    monkeypatch.setattr(module.request, "username", "")
    assert (await api.limits()).status_code == 401
    assert (await api.sessions()).status_code == 401
    api.limits_service.load_limits.assert_awaited_once()


@pytest.mark.asyncio
async def test_limits_post_preserves_conflict_and_body_limit(api, monkeypatch):
    import tl.web_api as module

    monkeypatch.setattr(module.request, "method", "POST")
    api.limits_service.save_limits.side_effect = StudioServiceError(
        "stale", status_code=409
    )
    assert (await api.limits()).status_code == 409
    monkeypatch.setattr(
        module.request, "headers", {"content-length": str(3 * 1024 * 1024)}
    )
    assert (await api.limits()).status_code == 413
    api.limits_service.save_limits.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_query_is_bounded_and_closed_service_cannot_save(
    api, monkeypatch
):
    import tl.web_api as module

    monkeypatch.setattr(module.request, "query", {"page_size": "10000000"})
    assert (await api.sessions()).status_code == 400
    api.limits_service.sessions.assert_not_awaited()
    monkeypatch.setattr(module.request, "query", {"page": "2", "search": "abc"})
    assert (await api.sessions()).status_code == 200
    api.limits_service.sessions.assert_awaited_once_with(
        page=2, page_size=20, search="abc", message_type="all", platform=""
    )
    api._web_closed = True
    assert (await api.limits()).status_code == 503
    api.limits_service.save_limits.assert_not_awaited()
