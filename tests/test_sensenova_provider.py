"""tests for tl/api/sensenova.py — u1.5-lite 适配与图生图防护"""

from __future__ import annotations

import pytest

from tl.api.sensenova import SenseNovaProvider, _resolve_u15_size
from tl.api_types import ApiRequestConfig


def _make_config(**overrides) -> ApiRequestConfig:
    kwargs: dict = {
        "model": "",
        "prompt": "画一只猫",
        "api_type": "sensenova",
        "api_key": "test-key",
        "resolution": "2K",
        "aspect_ratio": "1:1",
        "provider_settings": {"model": "sensenova-u1.5-lite"},
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
async def test_default_model_is_u15_lite() -> None:
    provider = SenseNovaProvider()
    config = _make_config(provider_settings={})
    request = await provider.build_request(client=_FakeClient(), config=config)
    assert request.payload["model"] == "sensenova-u1.5-lite"
    assert request.url.endswith("/v1/images/generations")


@pytest.mark.asyncio
async def test_u15_size_4k_preserves_aspect_ratio() -> None:
    """4K 非方形请求单边触顶后按比例重算另一边，比例不得失真。"""
    assert _resolve_u15_size(resolution="4K", aspect_ratio="16:9") == "4096x2304"
    assert _resolve_u15_size(resolution="4K", aspect_ratio="9:16") == "2304x4096"
    assert _resolve_u15_size(resolution="4K", aspect_ratio="4:3") == "4096x3072"
    assert _resolve_u15_size(resolution="4K", aspect_ratio="3:4") == "3072x4096"
    assert _resolve_u15_size(resolution="4K", aspect_ratio="1:1") == "4096x4096"
    for ratio, expected in (("3:1", 3.0), ("1:3", 1 / 3)):
        size = _resolve_u15_size(resolution="4K", aspect_ratio=ratio)
        width, height = (int(part) for part in size.split("x"))
        assert width % 32 == 0 and height % 32 == 0
        assert 512 <= width <= 4096 and 512 <= height <= 4096
        assert abs(width / height - expected) < 0.05


@pytest.mark.asyncio
async def test_u1_fast_with_reference_raises() -> None:
    provider = SenseNovaProvider()
    config = _make_config(
        provider_settings={"model": "sensenova-u1-fast"},
        reference_images=["https://example.com/a.png"],
    )
    with pytest.raises(Exception) as exc_info:
        await provider.build_request(client=_FakeClient(), config=config)
    assert getattr(exc_info.value, "retryable", True) is False
