"""tests for tl/api/xai.py — grok-imagine-image-2.0 对齐"""

from __future__ import annotations

import pytest

from tl.api.xai import XAIProvider
from tl.api_types import ApiRequestConfig


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
    async def _normalize_reference_image_input(self, image_input, image_input_mode):  # noqa: ANN001
        return "image/png", "QUJD"

    def _request_has_proxy(self, request_config) -> bool:  # noqa: ANN001
        return False

    def _request_http_proxy(self, request_config) -> None:  # noqa: ANN001
        return None


@pytest.mark.asyncio
async def test_default_model_is_image_2_0() -> None:
    provider = XAIProvider()
    request = await provider.build_request(
        client=_FakeClient(), config=_make_config(provider_settings={})
    )
    assert request.payload["model"] == "grok-imagine-image-2.0"
    assert request.url.endswith("/v1/images/generations")


def test_edit_max_three_images() -> None:
    import tl.api.xai as xai_module

    # 现行官方文档：多图编辑最多 5 张源图
    assert xai_module._MAX_EDIT_IMAGES == 5


@pytest.mark.asyncio
async def test_image_count_clamped_to_ten() -> None:
    provider = XAIProvider()
    request = await provider.build_request(
        client=_FakeClient(),
        config=_make_config(provider_settings={"n": 15}),
    )
    assert request.payload["n"] == 10
