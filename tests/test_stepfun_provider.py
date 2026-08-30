"""tests for tl/api/stepfun.py — step-2x-large 适配与参数门控"""

from __future__ import annotations

import pytest

from tl.api.stepfun import StepfunProvider, _gen_size_presets_for, _resolve_step_size
from tl.api_types import ApiRequestConfig
from tl.provider_hooks import stepfun_edit_capability


def test_step2x_large_uses_own_size_presets() -> None:
    presets = _gen_size_presets_for("step-2x-large")
    assert {"256x256", "1280x800", "800x1280"}.issubset(
        {f"{w}x{h}" for w, h in presets}
    )
    assert _resolve_step_size("1K", "16:9", model="step-2x-large") == "1280x800"
    assert _resolve_step_size("1K", "9:16", model="step-2x-large") == "800x1280"
    assert _resolve_step_size(None, None, model="step-2x-large") == "1024x1024"


def test_edit2_presets_unchanged() -> None:
    assert _resolve_step_size("1K", "16:9", model="step-image-edit-2") == "1360x768"
    assert _resolve_step_size("1K", "9:16", model="step-image-edit-2") == "768x1360"
    # legacy 模型沿用 2x-large 尺寸表
    assert _gen_size_presets_for("step-1x-medium") == _gen_size_presets_for(
        "step-2x-large"
    )


def test_stepfun_edit_capability_gates_by_model() -> None:
    assert stepfun_edit_capability({"model": "step-image-edit-2"})
    assert stepfun_edit_capability({"model": "step-image-edit-3"})
    assert not stepfun_edit_capability({"model": "step-2x-large"})
    assert not stepfun_edit_capability({})


def _make_config(**overrides) -> ApiRequestConfig:
    kwargs: dict = {
        "model": "",
        "prompt": "draw a cat",
        "api_type": "stepfun",
        "api_key": "test-key",
        "resolution": "1K",
        "aspect_ratio": "1:1",
        "provider_settings": {"model": "step-2x-large"},
    }
    kwargs.update(overrides)
    return ApiRequestConfig(**kwargs)


def test_generations_payload_gates_negative_prompt_for_2x_large() -> None:
    provider = StepfunProvider()
    payload = provider._prepare_generations_payload(
        config=_make_config(),
        settings={"negative_prompt": "模糊", "text_mode": True},
        model="step-2x-large",
    )
    assert "negative_prompt" not in payload
    assert "text_mode" not in payload


def test_generations_payload_keeps_params_and_clamps_for_edit2() -> None:
    provider = StepfunProvider()
    payload = provider._prepare_generations_payload(
        config=_make_config(),
        settings={"negative_prompt": "模糊", "text_mode": True, "steps": 60},
        model="step-image-edit-2",
    )
    assert payload["negative_prompt"] == "模糊"
    assert payload["text_mode"] is True
    assert payload["steps"] == 50


def test_generations_payload_model_name_case_insensitive() -> None:
    """模型名大小写混合时按 step-image-edit 规则处理，与 capability gate 口径一致。"""
    provider = StepfunProvider()
    payload = provider._prepare_generations_payload(
        config=_make_config(),
        settings={"negative_prompt": "模糊"},
        model="Step-Image-Edit-2",
    )
    assert payload["negative_prompt"] == "模糊"


@pytest.mark.asyncio
async def test_prompt_over_limit_raises_non_retryable() -> None:
    provider = StepfunProvider()
    config = _make_config(prompt="x" * 513)
    with pytest.raises(Exception) as exc_info:
        await provider.build_request(client=object(), config=config)
    assert getattr(exc_info.value, "retryable", True) is False
