from __future__ import annotations

import pytest

from tl.api.agnes_ai import AgnesAIProvider
from tl.api_types import ApiRequestConfig


class _FakeClient:
    agnes_ai_settings: dict = {}

    def __init__(self) -> None:
        self.normalized: list[tuple[str, str]] = []

    async def _normalize_reference_image_input(
        self, image_input: str, *, image_input_mode: str = "force_base64"
    ) -> tuple[str, str]:
        self.normalized.append((image_input, image_input_mode))
        return "image/png", "BASE64DATA"

    def _request_has_proxy(self, request_config) -> bool:  # noqa: ANN001
        return False

    def _request_http_proxy(self, request_config) -> None:  # noqa: ANN001
        return None


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

    assert request.url == ("https://my-proxy.com/custom/path/v1/images/generations")


@pytest.mark.asyncio
async def test_agnes_ai_default_model_is_25_flash() -> None:
    provider = AgnesAIProvider()
    config = ApiRequestConfig(
        model="",
        prompt="draw a cat",
        api_type="agnes_ai",
        api_key="test-key",
        resolution="1K",
        provider_settings={"response_format": "url"},
    )

    request = await provider.build_request(client=_FakeClient(), config=config)

    assert request.payload["model"] == "agnes-image-2.5-flash"


@pytest.mark.asyncio
async def test_agnes_ai_3k_tier_and_wide_ratio_passthrough() -> None:
    provider = AgnesAIProvider()
    config = ApiRequestConfig(
        model="agnes-image-2.5-flash",
        prompt="draw a cat",
        api_type="agnes_ai",
        api_key="test-key",
        resolution="3K",
        aspect_ratio="21:9",
        provider_settings={"response_format": "url"},
    )

    request = await provider.build_request(client=_FakeClient(), config=config)

    assert request.payload["size"] == "3K"
    assert request.payload["ratio"] == "21:9"
