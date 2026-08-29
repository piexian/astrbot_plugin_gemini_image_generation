"""tests for tl/api/param_utils.py"""

from __future__ import annotations

import pytest

from tl.api.param_utils import coerce_float, coerce_int, ensure_prompt_length


def test_coerce_int_clamps_and_defaults() -> None:
    assert coerce_int(5, lo=1, hi=4) == 4
    assert coerce_int(0, lo=1, hi=4) == 1
    assert coerce_int("3", lo=1, hi=4) == 3
    assert coerce_int("bad", lo=1, hi=4) == 1
    assert coerce_int(None, lo=1, hi=12, default=2) == 2


def test_coerce_float_clamps_and_defaults() -> None:
    assert coerce_float(0.5, lo=1.0, hi=10.0) == 1.0
    assert coerce_float("6", lo=1.0, hi=10.0) == 6.0
    assert coerce_float("bad", lo=1.0, hi=10.0) == 0.0
    assert coerce_float(99, lo=1.0, hi=10.0) == 10.0


def test_ensure_prompt_length_raises_non_retryable() -> None:
    ensure_prompt_length("ok", max_chars=512, provider="StepFun")
    with pytest.raises(Exception) as exc_info:
        ensure_prompt_length("x" * 513, max_chars=512, provider="StepFun")
    error = exc_info.value
    assert not getattr(error, "retryable", True)
