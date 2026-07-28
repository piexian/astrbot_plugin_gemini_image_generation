from __future__ import annotations

import pytest

from tl.api_types import ApiRequestConfig
from tl.plugin_config import ConfigLoader, ProviderCandidate
from tl.provider_capabilities import (
    IMAGE_TO_IMAGE,
    apply_request_overrides,
    candidate_capability,
    routing_mode,
    select_candidates,
)


def _candidate(
    api_type: str,
    model: str,
    *,
    alias: str | None = None,
    supports_edit: bool = True,
    **settings,
) -> ProviderCandidate:
    model_field = "endpoint_id" if api_type == "doubao" else "model"
    values = {model_field: model, "api_keys": ["key"], **settings}
    return ProviderCandidate(
        id=f"{api_type}#1",
        api_type=api_type,
        settings=values,
        supports_image_edit=supports_edit,
        model_alias=alias,
    )


def test_select_candidates_preserves_polling_order_for_shared_alias() -> None:
    candidates = [
        _candidate("xai", "grok-imagine-image", alias="fast"),
        _candidate("openai_images", "gpt-image-1", alias="fast"),
        _candidate("sensenova", "sensenova-u1-fast", supports_edit=False),
    ]

    selected = select_candidates(candidates, model="fast")

    assert [candidate.api_type for candidate in selected] == ["xai", "openai_images"]
    assert routing_mode(model="fast") == "model_polling"


def test_reference_images_and_runtime_parameters_filter_capabilities() -> None:
    candidates = [
        _candidate("sensenova", "sensenova-u1-fast", supports_edit=False),
        _candidate("xai", "grok-imagine-image"),
        _candidate("stepfun", "step-1x-medium"),
    ]

    edit_candidates = select_candidates(candidates, has_reference_images=True)
    negative_candidates = select_candidates(
        candidates,
        required_parameters={"negative_prompt"},
    )

    assert [candidate.api_type for candidate in edit_candidates] == ["xai", "stepfun"]
    assert [candidate.api_type for candidate in negative_candidates] == ["stepfun"]
    assert IMAGE_TO_IMAGE not in candidate_capability(candidates[0])["generation_modes"]


def test_request_overrides_use_provider_specific_fields_and_native_limit() -> None:
    candidate = _candidate(
        "minimax",
        "image-01",
        n=2,
        aigc_watermark=True,
    )
    config = ApiRequestConfig(
        model="",
        prompt="test",
        image_count=20,
        watermark=False,
    )

    settings, effective_count = apply_request_overrides(
        config,
        candidate,
        candidate.settings,
    )

    assert effective_count == 9
    assert settings["n"] == 9
    assert settings["aigc_watermark"] is False
    assert candidate.settings["n"] == 2


def test_config_loader_parses_alias_and_batch_limits() -> None:
    config = ConfigLoader(
        {
            "provider_settings": {
                "provider_overrides": [
                    {
                        "__template_key": "xai",
                        "api_keys": ["key"],
                        "model": "grok-imagine-image",
                        "model_alias": "fast",
                    }
                ]
            },
            "image_generation_settings": {
                "batch_max_images_per_task": 12,
                "batch_max_tasks": 30,
                "batch_concurrency": 4,
                "background_task_retention_hours": 48,
            },
        }
    ).load()

    assert config.provider_candidates[0].model_alias == "fast"
    assert config.batch_max_images_per_task == 12
    assert config.batch_max_tasks == 30
    assert config.batch_concurrency == 4
    assert config.background_task_retention_hours == 48


@pytest.mark.parametrize(
    ("candidate", "expected_limit"),
    [
        (_candidate("xai", "grok-imagine-image"), 10),
        (_candidate("minimax", "image-01"), 9),
        (
            _candidate(
                "sensenova",
                "sensenova-u1-fast",
                supports_edit=False,
            ),
            4,
        ),
        (
            _candidate(
                "doubao",
                "doubao-seedream",
                sequential_image_generation="auto",
                sequential_max_images=12,
            ),
            12,
        ),
        (_candidate("dashscope", "wan2.7-image"), 4),
        (
            _candidate(
                "dashscope",
                "wan2.7-image-pro",
                enable_sequential=True,
            ),
            12,
        ),
        (_candidate("dashscope", "qwen-image-2.0"), 6),
        (_candidate("dashscope", "other-image-model"), 1),
        (_candidate("google", "gemini-image"), 1),
    ],
)
def test_native_batch_limits(candidate, expected_limit: int) -> None:
    assert candidate_capability(candidate)["native_batch_limit"] == expected_limit


def test_provider_specific_optional_parameter_capabilities() -> None:
    minimax = candidate_capability(_candidate("minimax", "image-01"))
    dashscope_wan = candidate_capability(_candidate("dashscope", "wan2.7-image-pro"))
    dashscope_qwen = candidate_capability(_candidate("dashscope", "qwen-image-2.0"))
    dall_e_3 = candidate_capability(_candidate("openai_images", "dall-e-3"))

    assert "watermark" in minimax["parameters"]
    assert "negative_prompt" not in dashscope_wan["parameters"]
    assert "negative_prompt" in dashscope_qwen["parameters"]
    assert dall_e_3["parameters"]["quality"]["enum"] == ["hd", "standard"]
