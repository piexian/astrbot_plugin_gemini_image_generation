from __future__ import annotations

import pytest

from tl.api.openai_images import _resolve_size_value
from tl.api_types import ApiRequestConfig
from tl.openai_image_size import (
    CUSTOM_SIZE_DEFAULT,
    derive_custom_size_from_preset_params,
    normalize_size_mode,
    resolve_openai_custom_size,
    validate_custom_size,
)


def test_validate_custom_size_accepts_multiplication_sign() -> None:
    assert validate_custom_size(" 2048 × 1152 ") == "2048x1152"


def test_validate_custom_size_reports_real_constraint_after_normalization() -> None:
    with pytest.raises(ValueError, match="16 的倍数"):
        validate_custom_size("2048×1080")


def test_normalize_size_mode_accepts_custom() -> None:
    assert normalize_size_mode("custom") == "custom"


def test_resolve_size_value_uses_config_custom_size_for_preset_resolution() -> None:
    settings = {"size_mode": "custom", "custom_size": "2048×1152"}

    assert _resolve_size_value("gpt-image-2", "2K", settings) == "2048x1152"


def test_resolve_size_value_skips_custom_size_when_resolution_suppressed() -> None:
    settings = {"size_mode": "custom", "custom_size": "2048×1152"}

    assert (
        _resolve_size_value(
            "gpt-image-2",
            None,
            settings,
            ref_image_dims=(1600, 900),
            suppress_resolution=True,
        )
        is None
    )


def test_resolve_size_value_uses_explicit_custom_size_when_it_is_wxh() -> None:
    settings = {"size_mode": "custom", "custom_size": "1024x1024"}

    assert _resolve_size_value("gpt-image-2", "2048×1152", settings) == "2048x1152"


def test_derive_custom_size_from_preset_params() -> None:
    assert derive_custom_size_from_preset_params("2K", "16:9") == "2048x1152"


def test_derive_custom_size_from_1k_widescreen_preset() -> None:
    assert derive_custom_size_from_preset_params("1K", "16:9") == "1280x720"


def test_resolve_openai_custom_size_from_preset_params() -> None:
    settings = {"size_mode": "custom", "custom_size": "1024×1024"}

    assert (
        resolve_openai_custom_size(
            None,
            "2K",
            "16:9",
            settings,
        )
        == "2048x1152"
    )


def test_resolve_openai_custom_size_falls_back_to_config() -> None:
    settings = {"size_mode": "custom", "custom_size": "1536×1024"}

    assert (
        resolve_openai_custom_size(
            None,
            None,
            None,
            settings,
        )
        == "1536x1024"
    )


def test_resolve_openai_custom_size_invalid_custom_size_raises_value_error() -> None:
    settings = {"size_mode": "custom", "custom_size": "2048×1080"}

    with pytest.raises(ValueError, match="16 的倍数"):
        resolve_openai_custom_size(
            None,
            None,
            None,
            settings,
        )


def test_resolve_openai_custom_size_from_explicit_size() -> None:
    settings = {"size_mode": "custom", "custom_size": "1024x1024"}

    assert (
        resolve_openai_custom_size(
            "1536×1024",
            "2K",
            "16:9",
            settings,
        )
        == "1536x1024"
    )


def test_derive_custom_size_rejects_invalid_preset_combo_inputs() -> None:
    with pytest.raises(ValueError, match="aspect_ratio 仅支持"):
        derive_custom_size_from_preset_params("2K", "17:10")


def test_resolve_size_value_in_preset_mode_keeps_original_mapping() -> None:
    settings = {"size_mode": "preset"}

    assert _resolve_size_value("gpt-image-2", "2K", settings) == "1536x1024"


def test_client_custom_mode_uses_config_size_when_no_request_override() -> None:
    from tl.tl_api import GeminiAPIClient

    candidate = type(
        "Candidate",
        (),
        {
            "id": "openai_images#1",
            "api_type": "openai_images",
            "model": "gpt-image-1",
            "api_base": "",
            "settings": {
                "size_mode": "custom",
                "custom_size": "1536x1024",
                "resolution": "4K",
                "aspect_ratio": "16:9",
            },
        },
    )()
    client = GeminiAPIClient(["key"])
    config = ApiRequestConfig(model="", prompt="test", api_type="")

    candidate_config = client._build_candidate_config(config, candidate)

    assert candidate_config.resolution == "1536x1024"
    assert candidate_config.aspect_ratio == ""


def test_client_custom_mode_derives_size_from_request_override() -> None:
    from tl.tl_api import GeminiAPIClient

    candidate = type(
        "Candidate",
        (),
        {
            "id": "openai_images#1",
            "api_type": "openai_images",
            "model": "gpt-image-1",
            "api_base": "",
            "settings": {
                "size_mode": "custom",
                "custom_size": "1536x1024",
            },
        },
    )()
    client = GeminiAPIClient(["key"])
    config = ApiRequestConfig(
        model="",
        prompt="test",
        api_type="",
        resolution="2K",
        aspect_ratio="16:9",
    )

    candidate_config = client._build_candidate_config(config, candidate)

    assert candidate_config.resolution == "2048x1152"
    assert candidate_config.aspect_ratio == ""


def test_client_custom_mode_falls_back_to_config_size_when_override_invalid() -> None:
    from tl.tl_api import GeminiAPIClient

    candidate = type(
        "Candidate",
        (),
        {
            "id": "openai_images#1",
            "api_type": "openai_images",
            "model": "gpt-image-1",
            "api_base": "",
            "settings": {
                "size_mode": "custom",
                "custom_size": "1536x1024",
            },
        },
    )()
    client = GeminiAPIClient(["key"])
    config = ApiRequestConfig(
        model="",
        prompt="test",
        api_type="",
        resolution="2K",
        aspect_ratio="17:10",
    )

    candidate_config = client._build_candidate_config(config, candidate)

    assert candidate_config.resolution == "1536x1024"
    assert candidate_config.aspect_ratio == ""


def test_client_custom_mode_falls_back_to_default_when_override_and_config_invalid() -> (
    None
):
    from tl.tl_api import GeminiAPIClient

    candidate = type(
        "Candidate",
        (),
        {
            "id": "openai_images#1",
            "api_type": "openai_images",
            "model": "gpt-image-1",
            "api_base": "",
            "settings": {
                "size_mode": "custom",
                "custom_size": "2048x1080",
            },
        },
    )()
    client = GeminiAPIClient(["key"])
    config = ApiRequestConfig(
        model="",
        prompt="test",
        api_type="",
        resolution="2K",
        aspect_ratio="17:10",
    )

    candidate_config = client._build_candidate_config(config, candidate)

    assert candidate_config.resolution == CUSTOM_SIZE_DEFAULT
    assert candidate_config.aspect_ratio == ""


def test_client_custom_mode_keeps_explicit_wxh_request_override() -> None:
    from tl.tl_api import GeminiAPIClient

    candidate = type(
        "Candidate",
        (),
        {
            "id": "openai_images#1",
            "api_type": "openai_images",
            "model": "gpt-image-1",
            "api_base": "",
            "settings": {
                "size_mode": "custom",
                "custom_size": "1024x1024",
            },
        },
    )()
    client = GeminiAPIClient(["key"])
    config = ApiRequestConfig(
        model="",
        prompt="test",
        api_type="",
        resolution="1536x1024",
    )

    candidate_config = client._build_candidate_config(config, candidate)

    assert candidate_config.resolution == "1536x1024"
    assert candidate_config.aspect_ratio == ""


def test_build_candidate_config_truncates_reference_images() -> None:
    from tl.tl_api import GeminiAPIClient

    candidate = type(
        "Candidate",
        (),
        {
            "id": "google#1",
            "api_type": "google",
            "model": "gemini-3-pro-image-preview",
            "api_base": "",
            "settings": {"max_reference_images": 2},
        },
    )()
    client = GeminiAPIClient(["key"])
    config = ApiRequestConfig(
        model="",
        prompt="test",
        api_type="",
        reference_images=["a", "b", "c", "d"],
    )

    candidate_config = client._build_candidate_config(config, candidate)

    assert candidate_config.reference_images == ["a", "b"]


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
