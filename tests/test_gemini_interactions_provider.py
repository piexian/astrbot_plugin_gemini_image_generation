"""tests for tl/api/gemini_interactions.py — Interactions API 适配与参数门控"""

from __future__ import annotations

from typing import Any

import pytest

from tl.api.gemini_interactions import GeminiInteractionsProvider
from tl.api.registry import get_api_provider
from tl.api_types import APIError, ApiRequestConfig


class _FakeClient:
    async def _process_reference_image(self, image_input, idx, mode):
        return "image/png", image_input, False

    def _validate_b64_with_fallback(self, data, context=""):
        return data, True

    def _ensure_mime_type(self, mime):
        return mime or "image/png"

    def _find_image_urls_in_text(self, text):
        return []


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
async def test_build_request_default_url_and_payload(monkeypatch) -> None:
    provider = _make_provider()
    request = await provider.build_request(client=_FakeClient(), config=_make_config())
    assert request.url == (
        "https://generativelanguage.googleapis.com/v1beta/interactions"
    )
    assert request.headers["x-goog-api-key"] == "test-key"
    assert request.payload["model"] == "gemini-3.1-flash-image"
    assert request.payload["store"] is False
    assert request.payload["input"] == [{"type": "text", "text": "draw a cat"}]
    assert request.payload["response_format"] == {
        "type": "image",
        "image_size": "1K",
        "aspect_ratio": "1:1",
    }
    assert "tools" not in request.payload


@pytest.mark.asyncio
async def test_build_request_api_base_version_prefix() -> None:
    provider = _make_provider()
    request = await provider.build_request(
        client=_FakeClient(),
        config=_make_config(api_base="https://proxy.example.com"),
    )
    assert request.url == "https://proxy.example.com/v1beta/interactions"

    request = await provider.build_request(
        client=_FakeClient(),
        config=_make_config(api_base="https://proxy.example.com/v1beta/"),
    )
    assert request.url == "https://proxy.example.com/v1beta/interactions"


@pytest.mark.asyncio
async def test_lite_model_downgrades_resolution_to_1k() -> None:
    provider = _make_provider()
    config = _make_config(
        resolution="4K",
        provider_settings={"model": "gemini-3.1-flash-lite-image"},
    )
    request = await provider.build_request(client=_FakeClient(), config=config)
    assert request.payload["response_format"]["image_size"] == "1K"


@pytest.mark.asyncio
async def test_extreme_ratio_gated_by_model_tier() -> None:
    provider = _make_provider()
    request = await provider.build_request(
        client=_FakeClient(), config=_make_config(aspect_ratio="1:4")
    )
    assert request.payload["response_format"]["aspect_ratio"] == "1:4"

    request = await provider.build_request(
        client=_FakeClient(),
        config=_make_config(
            aspect_ratio="1:4",
            provider_settings={"model": "gemini-3-pro-image"},
        ),
    )
    assert "aspect_ratio" not in request.payload["response_format"]


@pytest.mark.asyncio
async def test_reference_images_become_image_blocks() -> None:
    provider = _make_provider()
    config = _make_config(
        reference_images=["aGVsbG8=", "aGVsbG8y"],
        response_modalities="TEXT_IMAGE",
    )
    request = await provider.build_request(client=_FakeClient(), config=config)
    image_blocks = [
        block for block in request.payload["input"] if block["type"] == "image"
    ]
    assert len(image_blocks) == 2
    assert image_blocks[0]["mime_type"] == "image/png"
    assert request.payload["response_format"][0] == {"type": "text"}


@pytest.mark.asyncio
async def test_grounding_and_thinking_level() -> None:
    provider = _make_provider()
    config = _make_config(
        enable_grounding=True,
        provider_settings={
            "model": "gemini-3.1-flash-image",
            "image_search": True,
            "thinking_level": "HIGH",
        },
    )
    request = await provider.build_request(client=_FakeClient(), config=config)
    assert request.payload["tools"] == [
        {"type": "google_search", "search_types": ["web_search", "image_search"]}
    ]
    assert request.payload["generation_config"]["thinking_level"] == "high"


@pytest.mark.asyncio
async def test_invalid_thinking_level_ignored() -> None:
    provider = _make_provider()
    config = _make_config(
        provider_settings={"model": "gemini-3.1-flash-image", "thinking_level": "max"}
    )
    request = await provider.build_request(client=_FakeClient(), config=config)
    assert "thinking_level" not in request.payload.get("generation_config", {})


@pytest.mark.asyncio
async def test_safety_settings_not_sent() -> None:
    provider = _make_provider()
    config = _make_config(safety_settings={"category": "HARM"})
    request = await provider.build_request(client=_FakeClient(), config=config)
    assert "safetySettings" not in request.payload
    assert "safety_settings" not in request.payload


@pytest.mark.asyncio
async def test_missing_api_key_raises() -> None:
    provider = _make_provider()
    with pytest.raises(APIError) as exc_info:
        await provider.build_request(
            client=_FakeClient(), config=_make_config(api_key="")
        )
    assert exc_info.value.error_type == "missing_api_key"


@pytest.mark.asyncio
async def test_empty_prompt_raises() -> None:
    provider = _make_provider()
    with pytest.raises(APIError) as exc_info:
        await provider.build_request(
            client=_FakeClient(), config=_make_config(prompt="  ")
        )
    assert exc_info.value.error_type == "empty_prompt"


def _completed_response(steps: list[dict]) -> dict:
    return {"status": "completed", "steps": steps}


@pytest.mark.asyncio
async def test_parse_response_model_output(monkeypatch) -> None:
    async def _fake_save(data, fmt):
        return f"/tmp/fake.{fmt}"

    monkeypatch.setattr("tl.api.gemini_interactions.save_base64_image", _fake_save)
    provider = _make_provider()
    urls, paths, text, _ = await provider.parse_response(
        client=_FakeClient(),
        response_data=_completed_response(
            [
                {"type": "thought", "summary": [{"type": "text", "text": "thinking"}]},
                {
                    "type": "model_output",
                    "content": [
                        {"type": "text", "text": "a cat"},
                        {"type": "image", "mime_type": "image/png", "data": "abc"},
                    ],
                },
            ]
        ),
        session=None,
    )
    assert urls == ["/tmp/fake.png"]
    assert paths == ["/tmp/fake.png"]
    assert text == "a cat"


@pytest.mark.asyncio
async def test_parse_response_thought_image_fallback(monkeypatch) -> None:
    async def _fake_save(data, fmt):
        return f"/tmp/fake.{fmt}"

    monkeypatch.setattr("tl.api.gemini_interactions.save_base64_image", _fake_save)
    provider = _make_provider()
    urls, _, _, _ = await provider.parse_response(
        client=_FakeClient(),
        response_data=_completed_response(
            [
                {
                    "type": "thought",
                    "summary": [
                        {"type": "image", "mime_type": "image/jpeg", "data": "abc"}
                    ],
                }
            ]
        ),
        session=None,
    )
    assert urls == ["/tmp/fake.jpeg"]


@pytest.mark.asyncio
async def test_parse_response_error_payload() -> None:
    provider = _make_provider()
    with pytest.raises(APIError) as exc_info:
        await provider.parse_response(
            client=_FakeClient(),
            response_data={"error": {"code": 400, "message": "bad request"}},
            session=None,
            http_status=400,
        )
    assert exc_info.value.error_type == "api_error"
    assert exc_info.value.retryable is False


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
async def test_parse_response_text_only_triggers_retry() -> None:
    provider = _make_provider()
    with pytest.raises(APIError) as exc_info:
        await provider.parse_response(
            client=_FakeClient(),
            response_data=_completed_response(
                [{"type": "model_output", "content": [{"type": "text", "text": "hi"}]}]
            ),
            session=None,
        )
    assert exc_info.value.error_type == "no_image_retry"


@pytest.mark.asyncio
async def test_parse_response_empty_raises_invalid() -> None:
    provider = _make_provider()
    with pytest.raises(APIError) as exc_info:
        await provider.parse_response(
            client=_FakeClient(), response_data={"status": "completed"}, session=None
        )
    assert exc_info.value.error_type == "invalid_response"
