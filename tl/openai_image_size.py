from __future__ import annotations

import math
import re
from typing import Any

CUSTOM_SIZE_MAX_EDGE = 3840
CUSTOM_SIZE_MIN_PIXELS = 655_360
CUSTOM_SIZE_MAX_PIXELS = 8_294_400
CUSTOM_SIZE_DEFAULT = "1024x1024"
CUSTOM_SIZE_SEPARATOR_PATTERN = r"[xX×✕✖]"
PRESET_RESOLUTIONS = ("1K", "2K", "4K")
PRESET_ASPECT_RATIOS = (
    "1:1",
    "16:9",
    "4:3",
    "3:2",
    "9:16",
    "4:5",
    "5:4",
    "21:9",
    "3:4",
    "2:3",
)
VALID_SIZE_MODES = {"preset", "custom"}
PRESET_LONG_EDGE_TARGETS = {"1K": 1024, "2K": 2048, "4K": 3840}


def normalize_size_mode(
    value: Any, *, field_name: str = "openai_images.size_mode"
) -> str:
    """Normalize and validate the OpenAI Images size mode."""
    size_mode = str(value or "preset").strip().lower()
    if size_mode not in VALID_SIZE_MODES:
        raise ValueError(f"{field_name} 仅支持 preset 或 custom，当前值: {value!r}")
    return size_mode


def normalize_custom_size_input(value: Any) -> str:
    """Normalize user-entered custom size text into canonical WxH form."""
    raw_size = str(value or "").strip()
    normalized = re.sub(r"\s+", "", raw_size)
    return re.sub(CUSTOM_SIZE_SEPARATOR_PATTERN, "x", normalized)


def validate_custom_size(
    value: Any, *, field_name: str = "openai_images.custom_size"
) -> str:
    """Validate a custom OpenAI Images size against official constraints."""
    normalized = normalize_custom_size_input(value)
    if not normalized:
        raise ValueError(
            f"{field_name} 不能为空；切换到 custom 模式后必须填写合法尺寸，如 {CUSTOM_SIZE_DEFAULT}"
        )

    match = re.fullmatch(r"(\d+)x(\d+)", normalized)
    if not match:
        raise ValueError(
            f"{field_name} 格式无效，必须为 WxH（支持 x 或 ×），例如 {CUSTOM_SIZE_DEFAULT}"
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


def _normalize_preset_resolution(value: Any, *, field_name: str = "resolution") -> str:
    resolution = str(value or "").strip().upper()
    if resolution not in PRESET_RESOLUTIONS:
        raise ValueError(
            f"{field_name} 仅支持 {'/'.join(PRESET_RESOLUTIONS)}，当前值: {value!r}"
        )
    return resolution


def _normalize_preset_aspect_ratio(
    value: Any, *, field_name: str = "aspect_ratio"
) -> str:
    aspect_ratio = str(value or "").strip()
    if aspect_ratio not in PRESET_ASPECT_RATIOS:
        raise ValueError(
            f"{field_name} 仅支持 {'/'.join(PRESET_ASPECT_RATIOS)}，当前值: {value!r}"
        )
    return aspect_ratio


def _parse_ratio_components(ratio_text: str) -> tuple[int, int]:
    width_text, height_text = ratio_text.split(":", 1)
    width = int(width_text)
    height = int(height_text)
    divisor = math.gcd(width, height)
    return width // divisor, height // divisor


def derive_custom_size_from_preset_params(
    resolution: Any,
    aspect_ratio: Any,
    *,
    resolution_field_name: str = "resolution",
    aspect_ratio_field_name: str = "aspect_ratio",
) -> str:
    """Derive a valid OpenAI custom size from legacy preset resolution/aspect_ratio."""
    normalized_resolution = _normalize_preset_resolution(
        resolution,
        field_name=resolution_field_name,
    )
    normalized_aspect_ratio = _normalize_preset_aspect_ratio(
        aspect_ratio,
        field_name=aspect_ratio_field_name,
    )

    ratio_width, ratio_height = _parse_ratio_components(normalized_aspect_ratio)
    width_factor = 16 // math.gcd(ratio_width, 16)
    height_factor = 16 // math.gcd(ratio_height, 16)
    base_scale = math.lcm(width_factor, height_factor)

    base_width = ratio_width * base_scale
    base_height = ratio_height * base_scale
    base_pixels = base_width * base_height
    base_max_edge = max(base_width, base_height)

    min_scale = math.ceil(math.sqrt(CUSTOM_SIZE_MIN_PIXELS / base_pixels))
    max_scale_by_pixels = math.floor(math.sqrt(CUSTOM_SIZE_MAX_PIXELS / base_pixels))
    max_scale_by_edge = CUSTOM_SIZE_MAX_EDGE // base_max_edge
    max_scale = min(max_scale_by_pixels, max_scale_by_edge)
    if min_scale > max_scale:
        raise ValueError(
            f"无法根据 {normalized_resolution} + {normalized_aspect_ratio} 推导合法尺寸"
        )

    target_edge = PRESET_LONG_EDGE_TARGETS[normalized_resolution]
    target_scale = round(target_edge / base_max_edge)
    scale = min(max(target_scale, min_scale), max_scale)

    width = base_width * scale
    height = base_height * scale
    return validate_custom_size(f"{width}x{height}", field_name="derived_custom_size")


def resolve_openai_custom_size(
    size_candidate: Any,
    resolution_candidate: Any,
    aspect_ratio_candidate: Any,
    settings: dict[str, Any],
    *,
    size_field_name: str = "size",
    resolution_field_name: str = "resolution",
    aspect_ratio_field_name: str = "aspect_ratio",
    custom_size_field_name: str = "openai_images.custom_size",
) -> str | None:
    """Resolve the actual custom size from explicit size or legacy preset params."""
    size_mode = normalize_size_mode(settings.get("size_mode"))
    if size_mode != "custom":
        return None

    normalized_candidate = normalize_custom_size_input(size_candidate)
    if normalized_candidate and re.fullmatch(r"\d+x\d+", normalized_candidate):
        return validate_custom_size(normalized_candidate, field_name=size_field_name)

    normalized_resolution = str(resolution_candidate or "").strip().upper()
    normalized_aspect_ratio = str(aspect_ratio_candidate or "").strip()
    if normalized_resolution or normalized_aspect_ratio:
        return derive_custom_size_from_preset_params(
            resolution_candidate,
            aspect_ratio_candidate,
            resolution_field_name=resolution_field_name,
            aspect_ratio_field_name=aspect_ratio_field_name,
        )

    return validate_custom_size(
        settings.get("custom_size"),
        field_name=custom_size_field_name,
    )
