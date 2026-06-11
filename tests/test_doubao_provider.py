from __future__ import annotations

import pytest

from tl.api.doubao import DoubaoProvider
from tl.api_types import ApiRequestConfig
from tl.provider_hooks import (
    DOUBAO_SEQUENTIAL_IMAGES_MIN,
    normalize_doubao_settings,
)


@pytest.mark.asyncio
async def test_doubao_payload_defaults_to_current_seedream_model() -> None:
    payload = await DoubaoProvider()._prepare_payload(
        client=object(),
        config=ApiRequestConfig(model="", prompt="draw", api_type="doubao"),
        doubao_settings={},
    )

    assert payload["model"] == "doubao-seedream-5-0-260128"


@pytest.mark.asyncio
async def test_doubao_custom_size_payload_uses_official_size_field() -> None:
    payload = await DoubaoProvider()._prepare_payload(
        client=object(),
        config=ApiRequestConfig(
            model="",
            prompt="draw",
            api_type="doubao",
            resolution="4K",
            seed=123,
        ),
        doubao_settings={
            "endpoint_id": "doubao-seedream-5-0-lite",
            "size": "3K",
            "size_mode": "custom",
            "custom_size": "2304×1728",
            "watermark": False,
        },
    )

    assert payload["model"] == "doubao-seedream-5-0-lite"
    assert payload["size"] == "2304x1728"
    assert "seed" not in payload
    assert "size_mode" not in payload
    assert "custom_size" not in payload
    assert "default_size" not in payload
    assert "resolution" not in payload
    assert "aspect_ratio" not in payload


@pytest.mark.asyncio
async def test_doubao_preset_size_payload_uses_size_setting() -> None:
    payload = await DoubaoProvider()._prepare_payload(
        client=object(),
        config=ApiRequestConfig(
            model="doubao-seedream-5-0-lite",
            prompt="draw",
            api_type="doubao",
            resolution="4K",
        ),
        doubao_settings={
            "size": "3K",
            "size_mode": "preset",
            "sequential_image_generation": "auto",
            "sequential_max_images": 1,
        },
    )

    assert payload["size"] == "3K"
    assert payload["sequential_image_generation_options"] == {"max_images": 1}


def test_doubao_normalizer_accepts_official_min_sequential_images() -> None:
    settings = {"sequential_max_images": "1"}

    normalize_doubao_settings(settings)

    assert DOUBAO_SEQUENTIAL_IMAGES_MIN == 1
    assert settings["sequential_max_images"] == 1
