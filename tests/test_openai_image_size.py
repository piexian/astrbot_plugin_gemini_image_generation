from __future__ import annotations

import pytest

from tl.api_types import ApiRequestConfig
from tl.openai_image_size import resolve_openai_custom_size, validate_custom_size


def test_validate_custom_size_reports_real_constraint_after_normalization() -> None:
    with pytest.raises(ValueError, match="16 的倍数"):
        validate_custom_size("2048×1080")


def test_resolve_openai_custom_size_invalid_custom_size_raises_value_error() -> None:
    settings = {"size_mode": "custom", "custom_size": "2048×1080"}

    with pytest.raises(ValueError, match="16 的倍数"):
        resolve_openai_custom_size(
            None,
            None,
            None,
            settings,
        )


def test_build_candidate_config_drops_reference_images_when_disabled() -> None:
    from tl.tl_api import GeminiAPIClient

    candidate = type(
        "Candidate",
        (),
        {
            "id": "google#1",
            "api_type": "google",
            "model": "gemini-3-pro-image-preview",
            "api_base": "",
            "settings": {"max_reference_images": 0},
        },
    )()
    client = GeminiAPIClient(["key"])
    config = ApiRequestConfig(
        model="",
        prompt="test",
        api_type="",
        reference_images=["a", "b"],
    )

    candidate_config = client._build_candidate_config(config, candidate)

    assert candidate_config.reference_images is None
