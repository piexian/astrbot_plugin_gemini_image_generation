from __future__ import annotations

import re
from typing import Any

CUSTOM_SIZE_MAX_EDGE = 3840
CUSTOM_SIZE_MIN_PIXELS = 655_360
CUSTOM_SIZE_MAX_PIXELS = 8_294_400
CUSTOM_SIZE_DEFAULT = "1024x1024"
VALID_SIZE_MODES = {"preset", "custom"}


def normalize_size_mode(
    value: Any, *, field_name: str = "openai_images.size_mode"
) -> str:
    """Normalize and validate the OpenAI Images size mode."""
    size_mode = str(value or "preset").strip().lower()
    if size_mode not in VALID_SIZE_MODES:
        raise ValueError(f"{field_name} 仅支持 preset 或 custom，当前值: {value!r}")
    return size_mode


def validate_custom_size(
    value: Any, *, field_name: str = "openai_images.custom_size"
) -> str:
    """Validate a custom OpenAI Images size against official constraints."""
    raw_size = str(value or "").strip()
    normalized = raw_size.replace(" ", "")
    if not normalized:
        raise ValueError(
            f"{field_name} 不能为空；切换到 custom 模式后必须填写合法尺寸，如 {CUSTOM_SIZE_DEFAULT}"
        )

    match = re.fullmatch(r"(\d+)[xX](\d+)", normalized)
    if not match:
        raise ValueError(
            f"{field_name} 格式无效，必须为 WxH，例如 {CUSTOM_SIZE_DEFAULT}"
        )

    width = int(match.group(1))
    height = int(match.group(2))
    max_edge = max(width, height)
    min_edge = min(width, height)
    total_pixels = width * height

    if min_edge <= 0:
        raise ValueError(f"{field_name} 宽高必须大于 0")
    if width % 16 != 0 or height % 16 != 0:
        raise ValueError(f"{field_name} 宽高都必须是 16 的倍数")
    if max_edge > CUSTOM_SIZE_MAX_EDGE:
        raise ValueError(f"{field_name} 最大边长不能超过 {CUSTOM_SIZE_MAX_EDGE}px")
    if max_edge / min_edge > 3:
        raise ValueError(f"{field_name} 长边与短边之比不能超过 3:1")
    if not (CUSTOM_SIZE_MIN_PIXELS <= total_pixels <= CUSTOM_SIZE_MAX_PIXELS):
        raise ValueError(
            f"{field_name} 总像素必须在 {CUSTOM_SIZE_MIN_PIXELS} 到 {CUSTOM_SIZE_MAX_PIXELS} 之间"
        )

    return f"{width}x{height}"
