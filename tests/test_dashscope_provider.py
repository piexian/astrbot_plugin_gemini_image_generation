from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tl.api.dashscope import (
    DashScopeProvider,
    _coerce_extend_mode,
    _coerce_seed,
    _parse_color_palette,
)
from tl.api.size_utils import clamp_side, size_from_budget
from tl.api_types import APIError, ApiRequestConfig
from tl.provider_capabilities import dashscope_capability


class _FakeClient:
    dashscope_settings: dict = {}

    def __init__(self, download_error: bool = False) -> None:
        self.normalized: list[tuple[str, str]] = []
        self.downloaded: list[str] = []
        self.download_error = download_error

    async def _normalize_reference_image_input(
        self, image_input: str, *, image_input_mode: str = "force_base64"
    ) -> tuple[str, str]:
        self.normalized.append((image_input, image_input_mode))
        return "image/png", "BASE64"

    def _request_has_proxy(self, request_config) -> bool:  # noqa: ANN001
        return False

    def _request_http_proxy(self, request_config) -> None:  # noqa: ANN001
        return None

    async def _download_image(self, url, session, use_cache=False, proxy=None):  # noqa: ANN001
        self.downloaded.append(url)
        if self.download_error:
            raise RuntimeError("boom")
        return url, f"/tmp/dashscope_{len(self.downloaded)}.png"


def _make_config(**overrides) -> ApiRequestConfig:
    kwargs: dict = {
        "model": "",
        "prompt": "draw a cat",
        "api_type": "dashscope",
        "api_key": "test-key",
        "resolution": "2K",
        "aspect_ratio": "1:1",
        "provider_settings": {"model": "wan2.7-image-pro"},
    }
    kwargs.update(overrides)
    return ApiRequestConfig(**kwargs)


@pytest.mark.asyncio
async def test_dashscope_4k_edit_downgrades_to_2k() -> None:
    """4K 仅 wan2.7-image-pro 文生图支持；带参考图时降为 2K。"""
    provider = DashScopeProvider()
    request = await provider.build_request(
        client=_FakeClient(),
        config=_make_config(
            resolution="4K",
            aspect_ratio="1:1",
            reference_images=["https://example.com/a.png"],
        ),
    )
    assert request.payload["parameters"]["size"] == "2048*2048"


@pytest.mark.asyncio
async def test_dashscope_endpoint_mode_token_plan() -> None:
    provider = DashScopeProvider()
    request = await provider.build_request(
        client=_FakeClient(),
        config=_make_config(
            provider_settings={
                "model": "wan2.7-image-pro",
                "endpoint_mode": "token_plan",
            }
        ),
    )
    assert request.url == (
        "https://token-plan.cn-beijing.maas.aliyuncs.com"
        "/api/v1/services/aigc/multimodal-generation/generation"
    )


@pytest.mark.asyncio
async def test_dashscope_reference_images_capped_at_nine() -> None:
    provider = DashScopeProvider()
    request = await provider.build_request(
        client=_FakeClient(),
        config=_make_config(
            reference_images=[f"/tmp/photo_{i}.jpg" for i in range(10)],
            image_input_mode="force_base64",
        ),
    )
    content = request.payload["input"]["messages"][0]["content"]
    image_items = [item for item in content if "image" in item]
    assert len(image_items) == 9


@pytest.mark.asyncio
async def test_dashscope_parse_data_inspection_not_retryable() -> None:
    provider = DashScopeProvider()
    with pytest.raises(APIError) as excinfo:
        await provider.parse_response(
            client=_FakeClient(),
            response_data={"code": "DataInspectionFailed", "message": "内容拦截"},
            session=None,
            http_status=400,
        )
    assert excinfo.value.retryable is False
    assert excinfo.value.error_code == "DataInspectionFailed"


@pytest.mark.asyncio
async def test_dashscope_z_image_reference_raises() -> None:
    provider = DashScopeProvider()
    config = _make_config(
        reference_images=["https://example.com/a.png"],
        provider_settings={"model": "z-image-turbo"},
    )
    with pytest.raises(Exception) as exc_info:
        await provider._prepare_image_values(client=_FakeClient(), config=config)
    assert getattr(exc_info.value, "retryable", True) is False


async def _build_params(**overrides) -> dict:
    request = await DashScopeProvider().build_request(
        client=_FakeClient(), config=_make_config(**overrides)
    )
    return request.payload["parameters"]


@pytest.mark.asyncio
async def test_dashscope_default_endpoint_domain() -> None:
    request = await DashScopeProvider().build_request(
        client=_FakeClient(), config=_make_config()
    )
    assert request.url == (
        "https://maas.qianwenaiapi.com"
        "/api/v1/services/aigc/multimodal-generation/generation"
    )


@pytest.mark.asyncio
async def test_dashscope_z_image_sends_prompt_extend() -> None:
    params = await _build_params(
        provider_settings={"model": "z-image-turbo", "prompt_extend": False}
    )
    assert params["prompt_extend"] is False
    assert "watermark" not in params
    assert "negative_prompt" not in params


@pytest.mark.asyncio
async def test_dashscope_4k_sequential_downgrades_to_2k() -> None:
    params = await _build_params(
        resolution="4K",
        provider_settings={"model": "wan2.7-image-pro", "enable_sequential": True},
    )
    assert params["size"] == "2048*2048"
    assert params["enable_sequential"] is True
    assert "thinking_mode" not in params


@pytest.mark.asyncio
async def test_dashscope_max_reference_images_setting_honored() -> None:
    request = await DashScopeProvider().build_request(
        client=_FakeClient(),
        config=_make_config(
            reference_images=[f"/tmp/p_{i}.jpg" for i in range(5)],
            image_input_mode="force_base64",
            provider_settings={"model": "wan2.7-image-pro", "max_reference_images": 3},
        ),
    )
    content = request.payload["input"]["messages"][0]["content"]
    assert len([item for item in content if "image" in item]) == 3


@pytest.mark.asyncio
async def test_dashscope_qwen3_reference_cap_overrides_setting() -> None:
    request = await DashScopeProvider().build_request(
        client=_FakeClient(),
        config=_make_config(
            reference_images=[f"/tmp/p_{i}.jpg" for i in range(5)],
            image_input_mode="force_base64",
            provider_settings={"model": "qwen-image-3.0", "max_reference_images": 9},
        ),
    )
    content = request.payload["input"]["messages"][0]["content"]
    assert len([item for item in content if "image" in item]) == 3


@pytest.mark.asyncio
async def test_dashscope_seed_sent_and_clamped() -> None:
    assert (
        await _build_params(provider_settings={"model": "wan2.7-image", "seed": 42})
    )["seed"] == 42
    assert "seed" not in await _build_params(
        provider_settings={"model": "wan2.7-image", "seed": 0}
    )
    assert (
        await _build_params(
            provider_settings={"model": "wan2.7-image", "seed": 2147483648}
        )
    )["seed"] == 2147483647


@pytest.mark.asyncio
async def test_dashscope_qwen3_thinking_params_gated_by_prompt_extend() -> None:
    on = await _build_params(
        provider_settings={
            "model": "qwen-image-3.0",
            "prompt_extend": True,
            "prompt_extend_mode": "agent",
            "enable_thinking": False,
        }
    )
    assert on["prompt_extend_mode"] == "agent"
    assert on["enable_thinking"] is False
    off = await _build_params(
        provider_settings={"model": "qwen-image-3.0", "prompt_extend": False}
    )
    assert "prompt_extend_mode" not in off
    assert "enable_thinking" not in off


@pytest.mark.asyncio
async def test_dashscope_qwen3_agent_mode_falls_back_for_edit() -> None:
    params = await _build_params(
        reference_images=["https://example.com/a.png"],
        provider_settings={
            "model": "qwen-image-3.0",
            "prompt_extend": True,
            "prompt_extend_mode": "agent",
        },
    )
    assert params["prompt_extend_mode"] == "direct"


@pytest.mark.asyncio
async def test_dashscope_qwen21_gating() -> None:
    params = await _build_params(
        provider_settings={
            "model": "qwen-image-2.1-pro",
            "n": 8,
            "negative_prompt": "blurry",
            "prompt_extend": True,
        }
    )
    assert params["n"] == 6
    assert "negative_prompt" not in params
    assert params["prompt_extend_mode"] == "direct"
    assert params["enable_thinking"] is True


@pytest.mark.asyncio
async def test_dashscope_color_palette_valid_and_invalid() -> None:
    palette = json.dumps(
        [
            {"hex": "#C2D1E6", "ratio": "50%"},
            {"hex": "#B5C8DB", "ratio": "30%"},
            {"hex": "#636574", "ratio": "20%"},
        ]
    )
    ok = await _build_params(
        provider_settings={"model": "wan2.7-image-pro", "color_palette": palette}
    )
    assert ok["color_palette"][0] == {"hex": "#C2D1E6", "ratio": "50%"}
    bad = await _build_params(
        provider_settings={
            "model": "wan2.7-image-pro",
            "color_palette": json.dumps(
                [{"hex": "#FFFFFF", "ratio": "50%"}, {"hex": "#000000", "ratio": "10%"}]
            ),
        }
    )
    assert "color_palette" not in bad
    seq = await _build_params(
        provider_settings={
            "model": "wan2.7-image-pro",
            "enable_sequential": True,
            "color_palette": palette,
        }
    )
    assert "color_palette" not in seq


def test_dashscope_capability_qwen21() -> None:
    candidate = SimpleNamespace(
        model="qwen-image-2.1-pro",
        settings={"model": "qwen-image-2.1-pro"},
        supports_image_edit=True,
    )
    profile = dashscope_capability(candidate)
    assert profile["native_batch_limit"] == 6
    assert "negative_prompt" not in profile["parameters"]
    assert "seed" in profile["parameters"]


def test_dashscope_seed_and_palette_helpers() -> None:
    assert _coerce_seed(0) is None
    assert _coerce_seed("abc") is None
    assert _coerce_seed(7) == 7
    assert _coerce_extend_mode("agent", has_reference=True) == "direct"
    assert _coerce_extend_mode("bogus", has_reference=False) == "direct"
    assert _parse_color_palette("") is None
    assert _parse_color_palette("not json") is None


def test_size_utils_clamp_and_budget() -> None:
    assert clamp_side(2688.4, min_side=512, max_side=2048, align=16) == 2048
    assert clamp_side(100, min_side=512, max_side=4096, align=16) == 512
    w, h = size_from_budget(2048 * 2048, 1, 1)
    assert (w, h) == (2048, 2048)
