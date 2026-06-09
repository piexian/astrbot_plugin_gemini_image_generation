from __future__ import annotations

import importlib

import pytest

from tl.api.agnes_ai import AgnesAIProvider
from tl.api_types import ApiRequestConfig


class _FakeClient:
    agnes_ai_settings: dict = {}

    def __init__(self) -> None:
        self.normalized: list[tuple[str, str]] = []

    async def _normalize_image_input(
        self, image_input: str, *, image_input_mode: str = "force_base64"
    ) -> tuple[str, str]:
        self.normalized.append((image_input, image_input_mode))
        return "image/png", "BASE64DATA"

    def _request_has_proxy(self, request_config) -> bool:  # noqa: ANN001
        return False

    def _request_http_proxy(self, request_config) -> None:  # noqa: ANN001
        return None


@pytest.mark.asyncio
async def test_agnes_ai_text_to_image_url_payload() -> None:
    provider = AgnesAIProvider()
    config = ApiRequestConfig(
        model="",
        prompt="draw a cat",
        api_type="agnes_ai",
        api_key="test-key",
        resolution="1K",
        aspect_ratio="4:3",
        provider_settings={
            "model": "agnes-image-2.1-flash",
            "response_format": "url",
        },
    )

    request = await provider.build_request(client=_FakeClient(), config=config)

    assert request.url == "https://apihub.agnes-ai.com/v1/images/generations"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert request.payload == {
        "model": "agnes-image-2.1-flash",
        "prompt": "draw a cat",
        "size": "1024x768",
        "extra_body": {"response_format": "url"},
    }


@pytest.mark.asyncio
async def test_agnes_ai_custom_proxy_path_is_preserved() -> None:
    provider = AgnesAIProvider()
    config = ApiRequestConfig(
        model="agnes-image-2.1-flash",
        prompt="draw a cat",
        api_type="agnes_ai",
        api_key="test-key",
        provider_settings={
            "api_base": "https://my-proxy.com/custom/path/v1",
            "response_format": "url",
        },
    )

    request = await provider.build_request(client=_FakeClient(), config=config)

    assert request.url == (
        "https://my-proxy.com/custom/path/v1/images/generations"
    )


@pytest.mark.asyncio
async def test_agnes_ai_full_endpoint_base_is_trimmed() -> None:
    provider = AgnesAIProvider()
    config = ApiRequestConfig(
        model="agnes-image-2.1-flash",
        prompt="draw a cat",
        api_type="agnes_ai",
        api_key="test-key",
        provider_settings={
            "api_base": "https://my-proxy.com/custom/path/v1/images/generations",
            "response_format": "url",
        },
    )

    request = await provider.build_request(client=_FakeClient(), config=config)

    assert request.url == (
        "https://my-proxy.com/custom/path/v1/images/generations"
    )


@pytest.mark.asyncio
async def test_agnes_ai_v1_prefixed_proxy_path_is_preserved() -> None:
    provider = AgnesAIProvider()
    config = ApiRequestConfig(
        model="agnes-image-2.1-flash",
        prompt="draw a cat",
        api_type="agnes_ai",
        api_key="test-key",
        provider_settings={
            "api_base": "https://my-proxy.com/v1/custom",
            "response_format": "url",
        },
    )

    request = await provider.build_request(client=_FakeClient(), config=config)

    assert request.url == "https://my-proxy.com/v1/custom/images/generations"


@pytest.mark.asyncio
async def test_agnes_ai_v1beta_proxy_path_is_preserved() -> None:
    provider = AgnesAIProvider()
    config = ApiRequestConfig(
        model="agnes-image-2.1-flash",
        prompt="draw a cat",
        api_type="agnes_ai",
        api_key="test-key",
        provider_settings={
            "api_base": "https://my-proxy.com/v1beta",
            "response_format": "url",
        },
    )

    request = await provider.build_request(client=_FakeClient(), config=config)

    assert request.url == "https://my-proxy.com/v1beta/images/generations"


@pytest.mark.asyncio
async def test_agnes_ai_text_to_image_b64_uses_return_base64() -> None:
    provider = AgnesAIProvider()
    config = ApiRequestConfig(
        model="agnes-image-2.0-flash",
        prompt="draw a cat",
        api_type="agnes_ai",
        api_key="test-key",
        resolution="1024×768",
        provider_settings={"response_format": "b64_json"},
    )

    request = await provider.build_request(client=_FakeClient(), config=config)

    assert request.payload["model"] == "agnes-image-2.0-flash"
    assert request.payload["size"] == "1024x768"
    assert request.payload["return_base64"] is True
    assert "extra_body" not in request.payload
    assert "response_format" not in request.payload


@pytest.mark.asyncio
async def test_agnes_ai_reference_payload_uses_extra_body_image() -> None:
    provider = AgnesAIProvider()
    client = _FakeClient()
    config = ApiRequestConfig(
        model="",
        prompt="edit this",
        api_type="agnes_ai",
        api_key="test-key",
        resolution="1K",
        aspect_ratio="1:1",
        reference_images=["/tmp/ref.png"],
        image_input_mode="force_base64",
        provider_settings={
            "model": "agnes-image-2.0-flash",
            "response_format": "b64_json",
            "reference_image_mode": "base64",
        },
    )

    request = await provider.build_request(client=client, config=config)

    assert client.normalized == [("/tmp/ref.png", "force_base64")]
    assert request.payload["model"] == "agnes-image-2.0-flash"
    assert request.payload["size"] == "1024x1024"
    assert request.payload["extra_body"] == {
        "image": ["data:image/png;base64,BASE64DATA"],
        "response_format": "b64_json",
    }


@pytest.mark.asyncio
async def test_agnes_ai_reference_payload_omits_size_when_suppressed() -> None:
    provider = AgnesAIProvider()
    client = _FakeClient()
    config = ApiRequestConfig(
        model="agnes-image-2.1-flash",
        prompt="edit this",
        api_type="agnes_ai",
        api_key="test-key",
        reference_images=["/tmp/ref.png"],
        image_input_mode="force_base64",
        suppress_resolution=True,
        provider_settings={
            "response_format": "url",
            "reference_image_mode": "base64",
        },
    )

    request = await provider.build_request(client=client, config=config)

    assert "size" not in request.payload
    assert request.payload["extra_body"] == {
        "image": ["data:image/png;base64,BASE64DATA"],
        "response_format": "url",
    }


@pytest.mark.asyncio
async def test_agnes_ai_parse_url_response() -> None:
    provider = AgnesAIProvider()

    (
        image_urls,
        image_paths,
        text_content,
        thought_signature,
    ) = await provider.parse_response(
        client=_FakeClient(),
        response_data={
            "created": 1780000000,
            "data": [
                {
                    "url": "https://storage.example/image.png",
                    "revised_prompt": "draw a cat",
                }
            ],
        },
        session=None,
        http_status=200,
    )

    assert image_urls == ["https://storage.example/image.png"]
    assert image_paths == []
    assert text_content == "修订提示词: draw a cat"
    assert thought_signature is None


@pytest.mark.asyncio
async def test_agnes_ai_parse_b64_response(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = AgnesAIProvider()
    saved_calls: list[tuple[str, str]] = []

    async def _save_base64_image(b64_data: str, image_format: str = "png") -> str:
        saved_calls.append((b64_data, image_format))
        return "/tmp/generated.png"

    agnes_ai_module = importlib.import_module("tl.api.agnes_ai")
    monkeypatch.setattr(agnes_ai_module, "save_base64_image", _save_base64_image)

    (
        image_urls,
        image_paths,
        text_content,
        thought_signature,
    ) = await provider.parse_response(
        client=_FakeClient(),
        response_data={"data": [{"b64_json": "BASE64DATA"}]},
        session=None,
        http_status=200,
    )

    assert saved_calls == [("BASE64DATA", "png")]
    assert image_urls == ["/tmp/generated.png"]
    assert image_paths == ["/tmp/generated.png"]
    assert text_content is None
    assert thought_signature is None
