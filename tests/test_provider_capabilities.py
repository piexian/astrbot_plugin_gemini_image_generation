from __future__ import annotations

from types import SimpleNamespace

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
        _candidate("stepfun", "step-image-edit-2"),
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
                "background_failure_notify_llm": False,
            },
        }
    ).load()

    assert config.provider_candidates[0].model_alias == "fast"
    assert config.batch_max_images_per_task == 12
    assert config.batch_max_tasks == 30
    assert config.batch_concurrency == 4
    assert config.background_task_retention_hours == 48
    assert config.background_failure_notify_llm is False


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
        (
            _candidate(
                "doubao",
                "doubao-seedream-5.0-pro",
                sequential_image_generation="auto",
                sequential_max_images=12,
            ),
            1,
        ),
        (
            _candidate(
                "doubao",
                "ep-20260628-seedream-pro",
                model_capability="seedream_5_pro",
                sequential_image_generation="auto",
                sequential_max_images=12,
            ),
            1,
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
    doubao_pro = candidate_capability(
        _candidate(
            "doubao",
            "doubao-seedream-5.0-pro",
            sequential_image_generation="auto",
        )
    )

    assert doubao_pro["parameters"]["image_count"]["native_request_maximum"] == 1
    assert doubao_pro["native_batch_limit"] == 1
    assert "watermark" in minimax["parameters"]
    assert "negative_prompt" not in dashscope_wan["parameters"]
    assert "negative_prompt" in dashscope_qwen["parameters"]
    assert dall_e_3["parameters"]["quality"]["enum"] == ["hd", "standard"]


def test_stepfun_negative_prompt_only_for_edit_models() -> None:
    """negative_prompt 仅 step-image-edit 系列声明，step-2x-large 等不参与该参数路由。"""
    edit_cap = candidate_capability(_candidate("stepfun", "step-image-edit-2"))
    t2i_cap = candidate_capability(_candidate("stepfun", "step-2x-large"))
    assert "negative_prompt" in edit_cap["parameters"]
    assert edit_cap["request_setting_map"].get("negative_prompt") == "negative_prompt"
    assert "negative_prompt" not in t2i_cap["parameters"]
    assert "negative_prompt" not in t2i_cap["request_setting_map"]

    candidates = [_candidate("stepfun", "step-2x-large")]
    assert select_candidates(candidates, required_parameters={"negative_prompt"}) == []


def test_dashscope_zimage_omits_watermark_capability() -> None:
    """z-image 纯文生图最小参数集：不声明 watermark，显式 watermark 请求不可路由。"""
    zimage = candidate_capability(_candidate("dashscope", "z-image-turbo"))
    assert "watermark" not in zimage["parameters"]
    assert "watermark" not in zimage["request_setting_map"]
    assert "negative_prompt" not in zimage["parameters"]

    wan = candidate_capability(_candidate("dashscope", "wan2.7-image-pro"))
    assert "watermark" in wan["parameters"]

    candidates = [_candidate("dashscope", "z-image-turbo")]
    assert select_candidates(candidates, required_parameters={"watermark"}) == []


def test_agnes_ai_capability_declares_3k_resolution() -> None:
    cap = candidate_capability(_candidate("agnes_ai", "agnes-image-2.5-flash"))
    assert cap["parameters"]["resolution"]["enum"] == ["1K", "2K", "3K", "4K"]


def test_gemini_image_capability_family_layering() -> None:
    """Google 系图像模型按家族声明分辨率/比例，不支持的参数进 unsupported_settings。"""
    from tl.provider_capabilities import gemini_image_capability

    def _cap(model):
        candidate = SimpleNamespace(
            id="google#1",
            api_type="google",
            model=model,
            supports_image_edit=True,
            settings={"model": model, "api_keys": ["k"]},
        )
        return gemini_image_capability(candidate)

    lite = _cap("gemini-3.1-flash-lite-image")
    assert lite["parameters"]["resolution"]["enum"] == ["1K"]
    assert "1:4" in lite["parameters"]["aspect_ratio"]["enum"]
    assert lite["unsupported_settings"] == {"enable_grounding", "image_search"}

    flash31 = _cap("gemini-3.1-flash-image")
    assert flash31["parameters"]["resolution"]["enum"] == ["1K", "2K", "4K"]
    assert "1:8" in flash31["parameters"]["aspect_ratio"]["enum"]
    assert "unsupported_settings" not in flash31

    pro = _cap("gemini-3-pro-image")
    assert pro["parameters"]["resolution"]["enum"] == ["1K", "2K", "4K"]
    assert "1:4" not in pro["parameters"]["aspect_ratio"]["enum"]
    assert "21:9" in pro["parameters"]["aspect_ratio"]["enum"]

    legacy = _cap("gemini-2.5-flash-image")
    # base 合并会保留 resolution 键：隐藏由 generation_fields 按 unsupported_settings 实现
    assert "resolution" in legacy["unsupported_settings"]
    assert "1:4" not in legacy["parameters"]["aspect_ratio"]["enum"]


def test_generation_fields_hides_unsupported_settings(tmp_path) -> None:
    """lite 候选的工作台字段不含 enable_grounding/image_search，分辨率仅 1K。"""
    from tl.plugin_config import ProviderCandidate
    from tl.studio_parameters import (
        generation_fields,
        validate_generation_settings,
    )

    candidate = ProviderCandidate(
        id="vertex#1",
        api_type="vertex",
        settings={
            "api_keys": ["k"],
            "model": "gemini-3.1-flash-lite-image",
            "resolution": "1K",
            "enable_grounding": False,
        },
    )
    fields = generation_fields(candidate)

    assert "enable_grounding" not in fields
    assert "image_search" not in fields
    assert fields["resolution"]["enum"] == ["1K"]

    with pytest.raises(ValueError, match="不允许临时覆盖参数"):
        validate_generation_settings(candidate, {"enable_grounding": True})


def test_minimax_capability_model_aware() -> None:
    from tl.provider_capabilities import minimax_capability

    def _cap(model):
        candidate = SimpleNamespace(
            id="minimax#1",
            api_type="minimax",
            model=model,
            supports_image_edit=False,
            settings={"model": model, "api_keys": ["k"]},
        )
        return minimax_capability(candidate)

    assert "21:9" in _cap("image-01")["parameters"]["aspect_ratio"]["enum"]
    assert "21:9" not in _cap("image-01-live")["parameters"]["aspect_ratio"]["enum"]
    assert _cap("image-01-live")["parameters"]["resolution"]["enum"] == ["1K"]
