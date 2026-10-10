from __future__ import annotations

import pytest

from tl.api.dashscope import DashScopeProvider
from tl.api_types import APIError, ApiRequestConfig


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
