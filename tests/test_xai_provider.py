"""tests for tl/api/xai.py — grok-imagine-image-2.0 对齐"""

from __future__ import annotations

import pytest

from tl.api.xai import XAIProvider
from tl.api_types import ApiRequestConfig
from tl.provider_capabilities import candidate_capability


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


def test_quality_whitelist_and_high_downgrade() -> None:
    assert XAIProvider._normalize_quality("low") == "low"
    assert XAIProvider._normalize_quality("HIGH") == "medium"
    assert XAIProvider._normalize_quality("extreme") is None
    assert XAIProvider._normalize_quality(None) is None


def test_aspect_ratio_whitelist() -> None:
    assert XAIProvider._normalize_aspect_ratio("20:9") == "20:9"
    assert XAIProvider._normalize_aspect_ratio("auto") == "auto"
    assert XAIProvider._normalize_aspect_ratio("4:5") is None
    assert XAIProvider._normalize_aspect_ratio("21:9") is None
    assert XAIProvider._normalize_aspect_ratio("") is None


def test_edit_max_three_images() -> None:
    import tl.api.xai as xai_module

    assert xai_module._MAX_EDIT_IMAGES == 3


@pytest.mark.asyncio
async def test_edits_image_shape_single_and_multi() -> None:
    provider = XAIProvider()

    single = await provider._prepare_edits_payload(
        client=_FakeClient(),
        config=_make_config(reference_images=["https://example.com/a.png"]),
        settings={"model": "grok-imagine-image-2.0"},
    )
    # 默认 force_base64：URL 参考图同样内联为 data URI
    assert single["image"] == {
        "type": "image_url",
        "url": "data:image/png;base64,QUJD",
    }
    assert "images" not in single
    # 单图编辑不发送 aspect_ratio，比例跟随输入图
    assert "aspect_ratio" not in single

    multi = await provider._prepare_edits_payload(
        client=_FakeClient(),
        config=_make_config(
            reference_images=[
                "https://example.com/a.png",
                "data:image/png;base64,QUJD",
            ],
            aspect_ratio="16:9",
        ),
        settings={"model": "grok-imagine-image-2.0"},
    )
    assert len(multi["images"]) == 2
    assert multi["images"][0]["type"] == "image_url"
    assert multi["aspect_ratio"] == "16:9"


@pytest.mark.asyncio
async def test_edits_over_limit_truncates() -> None:
    """超过 3 张参考图时截断前 3 张，与其他 provider 截断约定一致。"""
    provider = XAIProvider()
    config = _make_config(
        reference_images=[f"https://example.com/{i}.png" for i in range(4)]
    )
    payload = await provider._prepare_edits_payload(
        client=_FakeClient(), config=config, settings={}
    )
    assert len(payload["images"]) == 3


@pytest.mark.asyncio
async def test_image_count_clamped_to_ten() -> None:
    provider = XAIProvider()
    request = await provider.build_request(
        client=_FakeClient(),
        config=_make_config(provider_settings={"n": 15}),
    )
    assert request.payload["n"] == 10


def test_capability_quality_enum_excludes_high() -> None:
    class _Candidate:
        api_type = "xai"
        settings: dict = {}
        supports_image_edit = True
        model_alias = None

        @property
        def model(self) -> str:
            return "grok-imagine-image-2.0"

    params = candidate_capability(_Candidate())["parameters"]
    assert params["quality"]["enum"] == ["low", "medium"]
