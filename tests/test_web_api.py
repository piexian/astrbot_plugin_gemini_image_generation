from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest
from starlette.responses import FileResponse, JSONResponse, StreamingResponse

from tl.generation_tracker import GenerationTracker
from tl.web_api import WebStudioAPI
from tl.web_studio_service import StudioServiceError


def _json_response(data=None, *, status_code=200, headers=None):
    return JSONResponse(data or {}, status_code=status_code, headers=headers)


def _error_response(message, *, status_code=400, data=None, headers=None):
    return _json_response(
        {"status": "error", "message": message, "data": data},
        status_code=status_code,
        headers=headers,
    )


def _file_response(path, *, filename=None, content_type=None, headers=None):
    return FileResponse(
        path, filename=filename, media_type=content_type, headers=headers
    )


def _stream_response(
    content, *, content_type="text/event-stream", status_code=200, headers=None
):
    return StreamingResponse(
        content, media_type=content_type, status_code=status_code, headers=headers
    )


class _Context:
    def __init__(self) -> None:
        self.registered_web_apis = []

    def register_web_api(self, route, handler, methods, desc) -> None:
        for index, entry in enumerate(self.registered_web_apis):
            if entry[0] == route and entry[2] == methods:
                self.registered_web_apis[index] = (route, handler, methods, desc)
                return
        self.registered_web_apis.append((route, handler, methods, desc))


class _Service:
    gallery_dir = None

    def capabilities(self):
        return {"models": []}

    async def generate(self, payload, requester=None):
        return {"job_id": "job-one"}

    async def save_uploads(self, files):
        return ["upload.png"]

    async def gallery_image_base64(self, name, *, thumbnail=False):
        return {"mime": "image/jpeg", "b64": "aW1hZ2U="}


def _api(tmp_path, monkeypatch) -> WebStudioAPI:
    import tl.web_api as module

    monkeypatch.setattr(module, "WEB_API_AVAILABLE", True)
    monkeypatch.setattr(module, "json_response", _json_response)
    monkeypatch.setattr(module, "error_response", _error_response)
    monkeypatch.setattr(module, "file_response", _file_response)
    monkeypatch.setattr(module, "stream_response", _stream_response)
    return WebStudioAPI(GenerationTracker(tmp_path, 20), _Service())


def _match(entries, subpath: str, method: str):
    request_path = f"/{subpath.lstrip('/')}"
    for route, handler, methods, _desc in entries:
        chunks = []
        position = 0
        for match in re.finditer(r"<(?:(path):)?([A-Za-z_][A-Za-z0-9_]*)>", route):
            chunks.append(re.escape(route[position : match.start()]))
            name = match.group(2)
            chunks.append(f"(?P<{name}>.*)" if match.group(1) else f"(?P<{name}>[^/]+)")
            position = match.end()
        chunks.append(re.escape(route[position:]))
        matched = re.fullmatch("".join(chunks), request_path)
        if method.upper() in methods and matched:
            return handler, matched.groupdict()
    return None


def test_routes_include_plugin_name_and_match_full_path(tmp_path, monkeypatch) -> None:
    api = _api(tmp_path, monkeypatch)
    context = _Context()

    entries = api.register(context)

    assert entries
    assert all(
        route.startswith("/astrbot_plugin_gemini_image_generation/webui/")
        for route, *_ in entries
    )
    assert _match(
        context.registered_web_apis,
        "astrbot_plugin_gemini_image_generation/webui/history/job-1",
        "GET",
    )
    assert _match(
        context.registered_web_apis,
        "astrbot_plugin_gemini_image_generation/webui/image_b64/image.jpg",
        "GET",
    )


def test_reload_replaces_routes_and_unregister_is_identity_safe(
    tmp_path, monkeypatch
) -> None:
    context = _Context()
    first = _api(tmp_path / "one", monkeypatch)
    first_entries = first.register(context)
    second = _api(tmp_path / "two", monkeypatch)
    second_entries = second.register(context)

    first.unregister(context, first_entries)
    assert len(context.registered_web_apis) == len(second_entries)
    second.unregister(context, second_entries)
    assert context.registered_web_apis == []


@pytest.mark.asyncio
async def test_success_and_error_use_astrbot_envelopes(tmp_path, monkeypatch) -> None:
    api = _api(tmp_path, monkeypatch)

    success = await api.jobs()
    api._is_closed = lambda: True
    closed = await api.jobs()

    assert json.loads(success.body) == {"status": "ok", "data": []}
    assert json.loads(closed.body)["status"] == "error"
    assert closed.status_code == 503


@pytest.mark.asyncio
async def test_jobs_stream_logs_snapshot_frame(tmp_path, monkeypatch) -> None:
    import tl.web_api as module

    messages: list[str] = []
    monkeypatch.setattr(module, "logger", SimpleNamespace(debug=messages.append))
    api = _api(tmp_path, monkeypatch)

    response = await api.jobs_stream()
    first_frame = await anext(response.body_iterator)
    await response.body_iterator.aclose()

    assert "snapshot" in str(first_frame)
    assert messages == ["[WebUI] SSE 快照帧发送: 记录数=0"]


@pytest.mark.asyncio
async def test_generate_rejects_invalid_json_and_nan(tmp_path, monkeypatch) -> None:
    import tl.web_api as module

    api = _api(tmp_path, monkeypatch)
    module.request = SimpleNamespace(json=lambda default=None: _async_value(default))
    invalid = await api.generate()

    module.request = SimpleNamespace(
        json=lambda default=None: _async_value(
            {"prompt": "draw", "value": float("nan")}
        )
    )
    nan = await api.generate()

    assert invalid.status_code == 400
    assert json.loads(nan.body)["status"] == "error"


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_service_errors_are_exposed_with_status(tmp_path, monkeypatch) -> None:
    api = _api(tmp_path, monkeypatch)

    async def reject(payload, requester=None):
        raise StudioServiceError("busy", status_code=429)

    api.service.generate = reject
    import tl.web_api as module

    module.request = SimpleNamespace(
        json=lambda default=None: _async_value({"prompt": "draw"}), username="admin"
    )
    response = await api.generate()

    assert response.status_code == 429
    assert json.loads(response.body)["status"] == "error"


@pytest.mark.asyncio
async def test_image_rejects_path_traversal(tmp_path, monkeypatch) -> None:
    api = _api(tmp_path, monkeypatch)

    response = await api.image("../secret")

    assert response.status_code == 400
    assert json.loads(response.body)["status"] == "error"


@pytest.mark.asyncio
async def test_image_b64_uses_standard_envelope_and_thumb_query(
    tmp_path, monkeypatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    received = {}

    async def load(name, *, thumbnail=False):
        received.update(name=name, thumbnail=thumbnail)
        return {"mime": "image/jpeg", "b64": "aW1hZ2U="}

    api.service.gallery_image_base64 = load
    import tl.web_api as module

    module.request = SimpleNamespace(query={"thumb": "1"})
    response = await api.image_b64("image.jpg")

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "status": "ok",
        "data": {"mime": "image/jpeg", "b64": "aW1hZ2U="},
    }
    assert received == {"name": "image.jpg", "thumbnail": True}


@pytest.mark.asyncio
async def test_image_b64_rejects_path_traversal(tmp_path, monkeypatch) -> None:
    api = _api(tmp_path, monkeypatch)

    response = await api.image_b64("../secret")

    assert response.status_code == 400
    assert json.loads(response.body)["status"] == "error"
