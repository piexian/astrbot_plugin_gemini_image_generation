"""tests for tl/api/stepfun.py — step-2x-large 适配与参数门控"""

from __future__ import annotations

import pytest

from tl.api.stepfun import StepfunProvider, _gen_size_presets_for, _resolve_step_size
from tl.api_types import ApiRequestConfig
from tl.provider_hooks import stepfun_edit_capability


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


@pytest.mark.asyncio
async def test_prompt_over_limit_raises_non_retryable() -> None:
    provider = StepfunProvider()
    config = _make_config(prompt="x" * 513)
    with pytest.raises(Exception) as exc_info:
        await provider.build_request(client=object(), config=config)
    assert getattr(exc_info.value, "retryable", True) is False
