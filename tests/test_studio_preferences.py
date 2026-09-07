from __future__ import annotations

import asyncio
import json
from http.cookies import SimpleCookie
from types import SimpleNamespace

import pytest

from tests.test_web_api import _api
from tests.test_web_studio_service import _config
from tl.studio_preferences import StudioPreferencesStore
from tl.web_studio_service import WebStudioService


@pytest.mark.asyncio
async def test_preferences_are_browser_specific_and_reject_stale_writes(tmp_path):
    store = StudioPreferencesStore(tmp_path)
    one, two = "a" * 32, "b" * 32
    await asyncio.gather(
        store.save(one, {"selection": "new"}, 20),
        store.save(one, {"selection": "old"}, 10),
        store.save(two, {"selection": "other"}, 30),
    )
    assert (await store.load(one))["preferences"] == {"selection": "new"}
    assert (await store.load(two))["preferences"] == {"selection": "other"}
    assert (await StudioPreferencesStore(tmp_path).load(one))["revision"] == 20


@pytest.mark.asyncio
async def test_preferences_api_sets_scoped_http_only_cookie_and_sanitizes_values(
    tmp_path, monkeypatch
):
    import tl.web_api as module

    api = _api(tmp_path, monkeypatch)
    api.service = WebStudioService(None, api.tracker, _config(), tmp_path)
    candidate = api.service.config.provider_candidates[0]
    path = "/api/v1/plugins/extensions/astrbot_plugin_gemini_image_generation/webui/preferences"
    request = SimpleNamespace(
        cookies={},
        path=path,
        method="GET",
        _request=SimpleNamespace(url=SimpleNamespace(scheme="https")),
    )
    monkeypatch.setattr(module, "request", request)
    response = await api.preferences()
    cookie = SimpleCookie(response.headers["set-cookie"])["gemini_studio_browser"]
    assert cookie["path"] == path
    assert cookie["httponly"]
    assert cookie["secure"]
    assert cookie["max-age"] == "31536000"
    identity = {
        "candidate_id": candidate.id,
        "provider": candidate.api_type,
        "model": candidate.model,
    }
    state = {
        "selected": identity,
        "candidates": {
            candidate.id: {
                **identity,
                "generation_settings": {
                    "enable_grounding": False,
                    "api_base": "secret-url",
                    "api_keys": ["secret-key"],
                },
                "image_count": 2,
            }
        },
    }

    async def body(default=None):
        return {"revision": 100, "preferences": state}

    request.cookies = {"gemini_studio_browser": cookie.value}
    request.method = "POST"
    request.json = body
    saved = await api.preferences()
    assert saved.status_code == 200
    assert "secret-" not in saved.body.decode()
    request.method = "GET"
    restored = json.loads((await api.preferences()).body)["data"]
    assert restored["preferences"]["selected"] == identity
    assert restored["preferences"]["candidates"][candidate.id][
        "generation_settings"
    ] == {"enable_grounding": False}
    request.cookies = {"gemini_studio_browser": "c" * 32}
    other = json.loads((await api.preferences()).body)["data"]
    assert other["preferences"]["candidates"] == {}
    assert (
        "secret-"
        not in next((tmp_path / "webui_preferences").glob("*.json")).read_text()
    )


@pytest.mark.asyncio
async def test_preferences_api_rejects_invalid_revision(tmp_path, monkeypatch):
    import tl.web_api as module

    api = _api(tmp_path, monkeypatch)
    api.service = WebStudioService(None, api.tracker, _config(), tmp_path)

    async def body(default=None):
        return {"revision": True, "preferences": {}}

    monkeypatch.setattr(
        module,
        "request",
        SimpleNamespace(
            cookies={},
            path="/preferences",
            method="POST",
            json=body,
            _request=SimpleNamespace(url=SimpleNamespace(scheme="http")),
        ),
    )
    assert (await api.preferences()).status_code == 400
    assert not (tmp_path / "webui_preferences").exists()
