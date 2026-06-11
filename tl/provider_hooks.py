"""Provider-specific config and runtime hooks declared by ProviderSpec."""

from __future__ import annotations

import re
from typing import Any

from .openai_image_size import (
    CUSTOM_SIZE_DEFAULT,
    normalize_custom_size_input,
    normalize_size_mode,
    resolve_openai_custom_size,
    validate_custom_size,
)

DOUBAO_SEQUENTIAL_IMAGES_MIN = 1
DOUBAO_SEQUENTIAL_IMAGES_MAX = 15
DOUBAO_CUSTOM_SIZE_DEFAULT = "2048x2048"


def _logger():
    from astrbot.api import logger

    return logger


def validate_openai_images_settings(settings: dict[str, Any]) -> None:
    """Validate and normalize openai_images override settings."""
    try:
        size_mode = normalize_size_mode(settings.get("size_mode"))
    except ValueError as exc:
        _logger().warning(
            f"[配置加载] {exc}；已回退为 preset，以允许插件继续加载并在 WebUI 中修复配置"
        )
        size_mode = "preset"
    settings["size_mode"] = size_mode

    custom_size = settings.get("custom_size")
    if size_mode == "custom":
        settings["custom_size"] = normalize_custom_size_input(custom_size)
        try:
            settings["custom_size"] = validate_custom_size(custom_size)
        except ValueError as exc:
            _logger().warning(
                f"[配置加载] {exc}；已保留当前值，以便在 WebUI 中继续修改"
            )
    elif isinstance(custom_size, str):
        settings["custom_size"] = normalize_custom_size_input(custom_size)


def normalize_doubao_settings(settings: dict[str, Any]) -> None:
    """Normalize doubao-specific override settings."""
    legacy_size = settings.pop("default_size", None)
    if not settings.get("size") and legacy_size:
        settings["size"] = legacy_size

    try:
        size_mode = normalize_size_mode(
            settings.get("size_mode"),
            field_name="doubao.size_mode",
        )
    except ValueError as exc:
        _logger().warning(
            f"[配置加载] {exc}；已回退为 preset，以允许插件继续加载并在 WebUI 中修复配置"
        )
        size_mode = "preset"
    settings["size_mode"] = size_mode

    custom_size = settings.get("custom_size")
    if size_mode == "custom":
        settings["custom_size"] = normalize_custom_size_input(custom_size)
        try:
            settings["custom_size"] = validate_doubao_custom_size(custom_size)
        except ValueError as exc:
            _logger().warning(
                f"[配置加载] {exc}；已保留当前值，以便在 WebUI 中继续修改"
            )
    elif isinstance(custom_size, str):
        settings["custom_size"] = normalize_custom_size_input(custom_size)

    if not settings.get("optimize_prompt_mode"):
        settings["optimize_prompt_mode"] = "standard"

    max_images = settings.get("sequential_max_images")
    if max_images is None:
        return
    try:
        max_images_int = int(max_images)
        if (
            max_images_int < DOUBAO_SEQUENTIAL_IMAGES_MIN
            or max_images_int > DOUBAO_SEQUENTIAL_IMAGES_MAX
        ):
            raise ValueError(
                f"sequential_max_images 必须在 {DOUBAO_SEQUENTIAL_IMAGES_MIN}-"
                f"{DOUBAO_SEQUENTIAL_IMAGES_MAX} 之间，当前值: {max_images_int}"
            )
        settings["sequential_max_images"] = max_images_int
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and "必须在" in str(exc):
            raise
        raise ValueError(f"sequential_max_images 配置无效: {max_images}") from exc


def validate_doubao_custom_size(value: Any) -> str:
    """Validate Doubao custom size format without applying model-specific limits."""
    normalized = normalize_custom_size_input(value)
    if not normalized:
        raise ValueError(
            "doubao.custom_size 不能为空；切换到 custom 模式后必须填写合法尺寸，"
            f"如 {DOUBAO_CUSTOM_SIZE_DEFAULT}"
        )

    match = re.fullmatch(r"(\d+)x(\d+)", normalized)
    if not match:
        raise ValueError(
            "doubao.custom_size 格式无效，必须为 WxH（支持 x 或 ×），"
            f"例如 {DOUBAO_CUSTOM_SIZE_DEFAULT}"
        )

    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        raise ValueError("doubao.custom_size 宽高必须大于 0")
    return f"{width}x{height}"


def openai_images_edit_capability(settings: dict[str, Any]) -> bool:
    """Return whether an openai_images candidate can process reference images."""
    return not bool(settings.get("generations_only"))


def openai_images_candidate_config(
    base_config: Any, candidate: Any, settings: dict[str, Any]
) -> dict[str, Any]:
    """Resolve openai_images custom-size candidate request config overrides."""
    if getattr(base_config, "suppress_resolution", False):
        return {"resolution": None, "aspect_ratio": None}

    size_mode = normalize_size_mode(settings.get("size_mode"))
    if size_mode != "custom":
        return {}

    has_request_size_override = (
        getattr(base_config, "resolution", None) is not None
        or getattr(base_config, "aspect_ratio", None) is not None
    )
    resolution_candidate = None
    aspect_ratio_candidate = None
    if has_request_size_override:
        resolution_candidate = (
            getattr(base_config, "resolution", None)
            or settings.get("resolution")
            or "1K"
        )
        aspect_ratio_candidate = (
            getattr(base_config, "aspect_ratio", None)
            or settings.get("aspect_ratio")
            or "1:1"
        )

    try:
        return {
            "resolution": resolve_openai_custom_size(
                getattr(base_config, "resolution", None),
                resolution_candidate,
                aspect_ratio_candidate,
                settings,
                size_field_name="size",
                resolution_field_name="provider.resolution",
                aspect_ratio_field_name="provider.aspect_ratio",
            ),
            "aspect_ratio": "",
        }
    except ValueError as exc:
        if not has_request_size_override:
            raise
        _logger().warning(
            f"[openai_images] 根据请求参数解析 custom size 失败，回退配置 custom_size: {exc}"
        )
        try:
            return {
                "resolution": resolve_openai_custom_size(
                    None,
                    None,
                    None,
                    settings,
                    custom_size_field_name="openai_images.custom_size",
                ),
                "aspect_ratio": "",
            }
        except ValueError as config_exc:
            _logger().warning(
                "[openai_images] 配置 custom_size 也非法，"
                f"回退默认尺寸 {CUSTOM_SIZE_DEFAULT}: {config_exc}"
            )
            return {"resolution": CUSTOM_SIZE_DEFAULT, "aspect_ratio": ""}


def openai_images_tool_profile(
    plugin_or_config: Any, settings: dict[str, Any]
) -> dict[str, Any]:
    """Return LLM-tool behavior flags for openai_images."""
    try:
        size_mode = normalize_size_mode(settings.get("size_mode"))
    except ValueError as exc:
        _logger().warning(
            f"[工具定义] openai_images size_mode 非法，回退为预设模式: {exc}"
        )
        size_mode = "preset"
    return {
        "custom_size_mode": size_mode == "custom",
        "settings": settings,
    }
