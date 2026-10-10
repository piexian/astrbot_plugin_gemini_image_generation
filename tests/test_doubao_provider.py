from __future__ import annotations

import pytest

from tl.api.doubao import DoubaoProvider
from tl.api_types import ApiRequestConfig
from tl.provider_hooks import (
    DOUBAO_SEQUENTIAL_IMAGES_MAX,
    DOUBAO_SEQUENTIAL_IMAGES_MIN,
    normalize_doubao_settings,
)


@pytest.mark.asyncio
async def test_doubao_seedream_5_pro_limits_reference_images_to_ten() -> None:
    references = [f"https://example.com/reference-{index}.png" for index in range(11)]
    payload = await DoubaoProvider()._prepare_payload(
        client=object(),
        config=ApiRequestConfig(
            model="doubao-seedream-5.0-pro",
            prompt="edit",
            api_type="doubao",
            reference_images=references,
            image_input_mode="auto",
        ),
        doubao_settings={"endpoint_id": "doubao-seedream-5.0-pro"},
    )

    assert isinstance(payload["image"], list)


@pytest.mark.asyncio
async def test_doubao_default_endpoint_mode_uses_official_path() -> None:
    request = await DoubaoProvider().build_request(
        client=object(),
        config=ApiRequestConfig(
            model="doubao-seedream-5-0-260128",
            prompt="draw",
            api_type="doubao",
            api_key="official-key",
            provider_settings={
                "api_base": "https://ark.cn-beijing.volces.com",
            },
        ),
    )

    assert request.url == (
        "https://ark.cn-beijing.volces.com/api/v3/images/generations"
    )


@pytest.mark.asyncio
async def test_doubao_plan_mode_reuses_existing_full_api_base() -> None:
    request = await DoubaoProvider().build_request(
        client=object(),
        config=ApiRequestConfig(
            model="doubao-seedream-5.0-lite",
            prompt="draw",
            api_type="doubao",
            api_key="agent-plan-key",
            provider_settings={
                "api_base": "https://ark.cn-beijing.volces.com/api/v3",
                "endpoint_mode": "plan",
            },
        ),
    )

    assert request.url == (
        "https://ark.cn-beijing.volces.com/api/plan/v3/images/generations"
    )


def test_doubao_normalizer_rejects_too_many_sequential_images() -> None:
    settings = {"sequential_max_images": str(DOUBAO_SEQUENTIAL_IMAGES_MAX + 1)}

    with pytest.raises(ValueError) as exc_info:
        normalize_doubao_settings(settings)

    message = str(exc_info.value)
    assert "sequential_max_images" in message
    assert "必须在" in message
    assert str(DOUBAO_SEQUENTIAL_IMAGES_MIN) in message
    assert str(DOUBAO_SEQUENTIAL_IMAGES_MAX) in message
