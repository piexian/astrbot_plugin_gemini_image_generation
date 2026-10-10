"""tests for tl/api/gemini_interactions.py — Interactions API 适配与参数门控"""

from __future__ import annotations

from typing import Any

import pytest

from tl.api.gemini_interactions import GeminiInteractionsProvider
from tl.api.registry import get_api_provider
from tl.api_types import APIError, ApiRequestConfig


class _FakeClient:
    def __init__(self):
        self.downloaded: list[str] = []

    async def _process_reference_image(self, image_input, idx, mode):
        return "image/png", image_input, False

    def _validate_b64_with_fallback(self, data, context=""):
        return data, True

    def _ensure_mime_type(self, mime):
        return mime or "image/png"

    def _find_image_urls_in_text(self, text):
        return []

    async def _download_image(self, url, session, use_cache=False, proxy=None):
        self.downloaded.append(url)
        return None, f"/tmp/dl_{len(self.downloaded)}.png"

    def _request_http_proxy(self, config):
        return None


def _make_config(**overrides) -> ApiRequestConfig:
    kwargs: dict[str, Any] = {
        "model": "",
        "prompt": "draw a cat",
        "api_type": "gemini_interactions",
        "api_key": "test-key",
        "resolution": "1K",
        "aspect_ratio": "1:1",
        "response_modalities": "IMAGE",
        "provider_settings": {"model": "gemini-3.1-flash-image"},
    }
    kwargs.update(overrides)
    return ApiRequestConfig(**kwargs)


def _make_provider() -> GeminiInteractionsProvider:
    return GeminiInteractionsProvider()


def test_registry_resolves_gemini_interactions() -> None:
    provider = get_api_provider("gemini_interactions")
    assert isinstance(provider, GeminiInteractionsProvider)
    assert provider.name == "gemini_interactions"


@pytest.mark.asyncio
async def test_missing_api_key_raises() -> None:
    provider = _make_provider()
    with pytest.raises(APIError) as exc_info:
        await provider.build_request(
            client=_FakeClient(), config=_make_config(api_key="")
        )
    assert exc_info.value.error_type == "missing_api_key"


@pytest.mark.asyncio
async def test_parse_response_failed_status() -> None:
    provider = _make_provider()
    with pytest.raises(APIError) as exc_info:
        await provider.parse_response(
            client=_FakeClient(),
            response_data={"status": "failed", "faults": [{"message": "blocked"}]},
            session=None,
        )
    assert exc_info.value.error_type == "failed"
    assert "blocked" in str(exc_info.value)


@pytest.mark.asyncio
async def test_lite_model_never_gets_grounding() -> None:
    provider = _make_provider()
    config = _make_config(
        enable_grounding=True,
        provider_settings={"model": "gemini-3.1-flash-lite-image"},
    )
    request = await provider.build_request(client=_FakeClient(), config=config)
    assert "tools" not in request.payload
