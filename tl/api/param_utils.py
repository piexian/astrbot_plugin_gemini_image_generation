"""provider 共享参数处理辅助：钳制与 prompt 校验。"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from ..api_types import APIError


def coerce_int(
    value: Any,  # noqa: ANN401
    *,
    lo: int,
    hi: int,
    default: int = 1,
    warn_prefix: str = "",
) -> int:
    """解析 int 并钳位到 [lo, hi]；解析失败回退 default，越界记录警告。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        if warn_prefix:
            logger.warning("%s %r 无效，已回退为 %s", warn_prefix, value, default)
        return default
    if number < lo or number > hi:
        if warn_prefix:
            logger.warning("%s %s 越界，已钳位到 [%d, %d]", warn_prefix, number, lo, hi)
        return max(lo, min(number, hi))
    return number


def coerce_float(
    value: Any,  # noqa: ANN401
    *,
    lo: float,
    hi: float,
    default: float = 0.0,
    warn_prefix: str = "",
) -> float:
    """解析 float 并钳位到 [lo, hi]；解析失败回退 default。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        if warn_prefix:
            logger.warning("%s %r 无效，已回退为 %s", warn_prefix, value, default)
        return default
    if number < lo or number > hi:
        if warn_prefix:
            logger.warning("%s %s 越界，已钳位到 [%s, %s]", warn_prefix, number, lo, hi)
        return max(lo, min(number, hi))
    return number


def ensure_prompt_length(prompt: str, *, max_chars: int, provider: str) -> None:
    """prompt 超过硬上限时抛不可重试错误，避免注定失败的服务端调用消耗配额。"""
    if len(prompt) > max_chars:
        raise APIError(
            f"{provider} prompt 长度 {len(prompt)} 超过上限 {max_chars} 字符，请精简后重试",
            None,
            "prompt_too_long",
            retryable=False,
        )
