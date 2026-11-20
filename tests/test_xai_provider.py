"""tests for tl/api/xai.py — grok-imagine-image-2.0 对齐（deferred/user/MIME 白名单）"""

from __future__ import annotations

import base64
import io
import json

import pytest
from PIL import Image

from tl.api.xai import XAIProvider
from tl.api_types import APIError, ApiRequestConfig


def _make_config(**overrides) -> ApiRequestConfig:
    kwargs: dict = {
        "model": "",
        "prompt": "draw a cat",
        "api_type": "xai",
        "api_key": "test-key",
        "resolution": "1K",
        "aspect_ratio": "1:1",
        "provider_settings": {"model": "grok-imagine-image-2.0"},
    }
    kwargs.update(overrides)
    return ApiRequestConfig(**kwargs)


class _FakeClient:
    def __init__(self, normalize_result: tuple = ("image/png", "QUJD")):
        self._normalize_result = normalize_result

    async def _normalize_reference_image_input(self, image_input, image_input_mode):  # noqa: ANN001
        return self._normalize_result

    def _request_has_proxy(self, request_config) -> bool:  # noqa: ANN001
        return False

    def _request_http_proxy(self, request_config) -> None:  # noqa: ANN001
        return None


class _FakePollResponse:
    def __init__(self, body: str, status: int = 200):
        self._body = body
        self.status = status

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> _FakePollResponse:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """按顺序返回响应体；最后一个响应体会被重复返回。"""

    def __init__(self, bodies: list):
        self._bodies = bodies
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs) -> _FakePollResponse:  # noqa: ANN003
        self.calls.append({"url": url, **kwargs})
        item = self._bodies.pop(0) if len(self._bodies) > 1 else self._bodies[0]
        status, body = item if isinstance(item, tuple) else (200, item)
        return _FakePollResponse(body, status)


def _image_b64(fmt: str) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (200, 30, 30)).save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode()


def _done_body(url: str = "https://imgen.x.ai/tmp/a.jpeg") -> str:
    return json.dumps(
        {
            "request_id": "req-1",
            "status": "done",
            "data": [{"url": url, "mime_type": "image/jpeg"}],
            "usage": {"cost_in_usd_ticks": 400000000},
        }
    )


@pytest.mark.asyncio
async def test_default_model_is_image_2_0() -> None:
    provider = XAIProvider()
    request = await provider.build_request(
        client=_FakeClient(), config=_make_config(provider_settings={})
    )
    assert request.payload["model"] == "grok-imagine-image-2.0"
    assert request.url.endswith("/v1/images/generations")


def test_edit_max_five_images() -> None:
    import tl.api.xai as xai_module

    # 官方多图编辑最多 5 张源图
    assert xai_module._MAX_EDIT_IMAGES == 5


@pytest.mark.asyncio
async def test_image_count_clamped_to_ten() -> None:
    provider = XAIProvider()
    request = await provider.build_request(
        client=_FakeClient(),
        config=_make_config(provider_settings={"n": 15}),
    )
    assert request.payload["n"] == 10


@pytest.mark.asyncio
async def test_user_default_is_astrbot() -> None:
    request = await XAIProvider().build_request(
        client=_FakeClient(), config=_make_config()
    )
    assert request.payload["user"] == "AstrBot"


@pytest.mark.asyncio
async def test_user_custom_value_sent() -> None:
    request = await XAIProvider().build_request(
        client=_FakeClient(),
        config=_make_config(
            provider_settings={"model": "grok-imagine-image-2.0", "user": "my-app"}
        ),
    )
    assert request.payload["user"] == "my-app"


@pytest.mark.asyncio
async def test_user_empty_not_sent() -> None:
    request = await XAIProvider().build_request(
        client=_FakeClient(),
        config=_make_config(
            provider_settings={"model": "grok-imagine-image-2.0", "user": ""}
        ),
    )
    assert "user" not in request.payload


@pytest.mark.asyncio
async def test_edits_payload_carries_user_and_deferred() -> None:
    config = _make_config(
        reference_images=["data:image/png;base64,QUJD"],
        request_source="llm_tool",
    )
    request = await XAIProvider().build_request(client=_FakeClient(), config=config)
    assert request.url.endswith("/v1/images/edits")
    assert request.payload["image"] == {
        "type": "image_url",
        "url": "data:image/png;base64,QUJD",
    }
    assert request.payload["user"] == "AstrBot"
    assert request.payload["deferred"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "settings_extra", "expected"),
    [
        ("llm_tool", {}, True),
        ("llm_batch", {}, True),
        ("command", {}, False),
        ("webui", {}, False),
        (None, {}, False),
        ("llm_tool", {"response_format": "url"}, True),
        ("llm_tool", {"response_format": "b64_json"}, False),
        ("llm_tool", {"deferred": False}, False),
    ],
)
async def test_deferred_gating(source, settings_extra, expected) -> None:
    settings = {"model": "grok-imagine-image-2.0", **settings_extra}
    config = _make_config(provider_settings=settings, request_source=source)
    request = await XAIProvider().build_request(client=_FakeClient(), config=config)
    assert request.payload.get("deferred", False) is expected


@pytest.mark.asyncio
async def test_resolution_1_5k_passed() -> None:
    request = await XAIProvider().build_request(
        client=_FakeClient(), config=_make_config(resolution="1.5K")
    )
    assert request.payload["resolution"] == "1.5k"


@pytest.mark.asyncio
async def test_resolution_4k_downgrades_to_2k() -> None:
    request = await XAIProvider().build_request(
        client=_FakeClient(), config=_make_config(resolution="4K")
    )
    assert request.payload["resolution"] == "2k"


@pytest.mark.asyncio
async def test_gif_data_uri_transcoded_to_png() -> None:
    uri = f"data:image/gif;base64,{_image_b64('GIF')}"
    result = await XAIProvider()._to_image_url(_FakeClient(), _make_config(), uri)
    assert result.startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_heic_data_uri_transcoded_to_png() -> None:
    # 白名单判定只看声明 MIME；字节用 PNG 充当，转码路径经 PIL 重编码
    uri = f"data:image/heic;base64,{_image_b64('PNG')}"
    result = await XAIProvider()._to_image_url(_FakeClient(), _make_config(), uri)
    assert result.startswith("data:image/png;base64,")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fmt", "mime"),
    [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")],
)
async def test_supported_mime_passthrough(fmt, mime) -> None:
    uri = f"data:{mime};base64,{_image_b64(fmt)}"
    result = await XAIProvider()._to_image_url(_FakeClient(), _make_config(), uri)
    assert result == uri


@pytest.mark.asyncio
async def test_deferred_ack_polled_until_done() -> None:
    pending = json.dumps({"request_id": "req-1", "status": "pending"})
    session = _FakeSession([(202, pending), _done_body()])
    config = _make_config(
        provider_settings={
            "model": "grok-imagine-image-2.0",
            "poll_interval": 0.001,
        }
    )
    urls, paths, _text, _sig = await XAIProvider().parse_response(
        client=_FakeClient(),
        response_data={"request_id": "req-1"},
        session=session,
        api_base="https://api.x.ai",
        http_status=200,
        request_config=config,
    )
    assert urls == ["https://imgen.x.ai/tmp/a.jpeg"]
    assert paths == []
    assert session.calls[0]["url"] == "https://api.x.ai/v1/images/req-1"
    assert session.calls[0]["headers"]["Authorization"] == "Bearer test-key"


@pytest.mark.asyncio
async def test_deferred_poll_url_with_v1_base() -> None:
    session = _FakeSession([_done_body()])
    config = _make_config(
        provider_settings={"model": "grok-imagine-image-2.0", "poll_interval": 0.001}
    )
    await XAIProvider().parse_response(
        client=_FakeClient(),
        response_data={"request_id": "req-1"},
        session=session,
        api_base="https://api.x.ai/v1",
        http_status=200,
        request_config=config,
    )
    assert session.calls[0]["url"] == "https://api.x.ai/v1/images/req-1"


@pytest.mark.asyncio
async def test_deferred_failed_raises_non_retryable() -> None:
    failed = json.dumps(
        {
            "request_id": "req-1",
            "status": "failed",
            "error": {"code": "failed_precondition", "message": "content policy"},
        }
    )
    session = _FakeSession([failed])
    config = _make_config(
        provider_settings={"model": "grok-imagine-image-2.0", "poll_interval": 0.001}
    )
    with pytest.raises(APIError) as exc_info:
        await XAIProvider().parse_response(
            client=_FakeClient(),
            response_data={"request_id": "req-1"},
            session=session,
            api_base="https://api.x.ai",
            http_status=200,
            request_config=config,
        )
    assert exc_info.value.retryable is False
    assert "content policy" in str(exc_info.value)


@pytest.mark.asyncio
async def test_deferred_poll_timeout_does_not_resubmit() -> None:
    pending = json.dumps({"request_id": "req-1", "status": "pending"})
    session = _FakeSession([(202, pending)])
    config = _make_config(
        provider_settings={
            "model": "grok-imagine-image-2.0",
            "poll_interval": 0.001,
            "poll_timeout": 0.05,
        }
    )
    with pytest.raises(APIError) as exc_info:
        await XAIProvider().parse_response(
            client=_FakeClient(),
            response_data={"request_id": "req-1"},
            session=session,
            api_base="https://api.x.ai",
            http_status=200,
            request_config=config,
        )
    assert exc_info.value.retryable is False
    assert exc_info.value.error_type == "timeout"


@pytest.mark.asyncio
async def test_deferred_poll_404_raises_non_retryable() -> None:
    session = _FakeSession([(404, "{}")])
    config = _make_config(
        provider_settings={"model": "grok-imagine-image-2.0", "poll_interval": 0.001}
    )
    with pytest.raises(APIError) as exc_info:
        await XAIProvider().parse_response(
            client=_FakeClient(),
            response_data={"request_id": "req-1"},
            session=session,
            api_base="https://api.x.ai",
            http_status=200,
            request_config=config,
        )
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_sync_response_skips_polling() -> None:
    session = _FakeSession([_done_body()])
    config = _make_config()
    urls, _paths, _text, _sig = await XAIProvider().parse_response(
        client=_FakeClient(),
        response_data={
            "data": [{"url": "https://imgen.x.ai/tmp/sync.jpeg"}],
            "usage": {"cost_in_usd_ticks": 400000000},
        },
        session=session,
        api_base="https://api.x.ai",
        http_status=200,
        request_config=config,
    )
    assert urls == ["https://imgen.x.ai/tmp/sync.jpeg"]
    assert session.calls == []
