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

DOUBAO_ENDPOINT_MODES = frozenset({"official", "agent_plan"})
_DOUBAO_ENDPOINT_MODE_ALIASES = {"plan": "agent_plan"}
DOUBAO_OUTPUT_FORMATS = frozenset({"jpeg", "png"})
_DOUBAO_OUTPUT_FORMAT_ALIASES = {"jpg": "jpeg"}
DOUBAO_MODEL_CAPABILITIES = frozenset({"auto", "seedream_5_pro"})
_DOUBAO_MODEL_CAPABILITY_ALIASES = {
    "pro": "seedream_5_pro",
    "seedream_5_pro": "seedream_5_pro",
    "seedream_5_0_pro": "seedream_5_pro",
    "seedream_5.0_pro": "seedream_5_pro",
}


def _normalize_doubao_model_capability(value: Any) -> str:
    raw = str(value or "auto").strip().lower()
    normalized = raw.replace("-", "_").replace(" ", "_")
    return _DOUBAO_MODEL_CAPABILITY_ALIASES.get(normalized, normalized)


def is_doubao_seedream_5_pro(
    model: Any, settings: dict[str, Any] | None = None
) -> bool:
    """Return whether a model ID or declared endpoint capability is Seedream 5 Pro."""
    if isinstance(settings, dict):
        capability = _normalize_doubao_model_capability(
            settings.get("model_capability")
        )
        if capability == "seedream_5_pro":
            return True
    normalized = "-".join(str(model or "").strip().lower().replace("_", "-").split())
    return any(
        marker in normalized
        for marker in (
            "seedream-5.0-pro",
            "seedream-5-0-pro",
            "seedream-5-pro",
            "seedream-5pro",
        )
    )


def _logger():
    from astrbot.api import logger

    return logger


def normalize_doubao_endpoint_mode(value: Any) -> str:
    """Normalize a Doubao endpoint mode to a supported canonical value."""
    raw_mode = str(value or "official")
    mode = raw_mode.strip().lower().replace("-", "_")
    mode = _DOUBAO_ENDPOINT_MODE_ALIASES.get(mode, mode)
    if mode not in DOUBAO_ENDPOINT_MODES:
        _logger().warning(
            "[配置加载] doubao.endpoint_mode=%r 无效（仅支持 official / agent_plan），"
            "已回退为 official",
            value,
        )
        return "official"
    return mode


def normalize_doubao_output_format(value: Any) -> str:
    """Normalize a Doubao output format to a supported canonical value."""
    raw_format = str(value or "jpeg")
    output_format = raw_format.strip().lower()
    output_format = _DOUBAO_OUTPUT_FORMAT_ALIASES.get(output_format, output_format)
    if output_format not in DOUBAO_OUTPUT_FORMATS:
        _logger().warning(
            "[配置加载] doubao.output_format=%r 无效（仅支持 jpeg / png），"
            "已回退为 jpeg",
            value,
        )
        return "jpeg"
    return output_format


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
    settings["endpoint_mode"] = normalize_doubao_endpoint_mode(
        settings.get("endpoint_mode")
    )
    settings["output_format"] = normalize_doubao_output_format(
        settings.get("output_format")
    )

    model_capability = _normalize_doubao_model_capability(
        settings.get("model_capability")
    )
    if model_capability not in DOUBAO_MODEL_CAPABILITIES:
        _logger().warning(
            "[配置加载] doubao.model_capability=%r 无效（仅支持 auto / seedream_5_pro），"
            "已回退为 auto",
            settings.get("model_capability"),
        )
        model_capability = "auto"
    settings["model_capability"] = model_capability

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


_DASHSCOPE_SHORTHAND_SIZES = frozenset({"1K", "2K", "4K"})
_DASHSCOPE_ENDPOINT_MODES = frozenset({"dashscope", "token_plan"})


def normalize_dashscope_settings(settings: dict[str, Any]) -> None:
    """Normalize dashscope-specific override settings."""
    endpoint_mode = str(settings.get("endpoint_mode") or "dashscope").strip().lower()
    if endpoint_mode not in _DASHSCOPE_ENDPOINT_MODES:
        _logger().warning(
            "[配置加载] dashscope.endpoint_mode=%r 无效（仅支持 dashscope / token_plan），已回退为 dashscope",
            settings.get("endpoint_mode"),
        )
        endpoint_mode = "dashscope"
    settings["endpoint_mode"] = endpoint_mode

    try:
        size_mode = normalize_size_mode(
            settings.get("size_mode"), field_name="dashscope.size_mode"
        )
    except ValueError as exc:
        _logger().warning(
            f"[配置加载] {exc}；已回退为 preset，以允许插件继续加载并在 WebUI 中修复配置"
        )
        size_mode = "preset"
    settings["size_mode"] = size_mode

    custom_size = settings.get("custom_size")
    if isinstance(custom_size, str) and custom_size.strip():
        raw = custom_size.strip()
        if raw.upper() in _DASHSCOPE_SHORTHAND_SIZES:
            settings["custom_size"] = raw.upper()
        else:
            normalized = normalize_custom_size_input(raw.replace("*", "x"))
            match = re.fullmatch(r"(\d+)x(\d+)", normalized)
            if match:
                width, height = int(match.group(1)), int(match.group(2))
                if 16 <= width <= 4096 and 16 <= height <= 4096:
                    settings["custom_size"] = f"{width}*{height}"
                else:
                    _logger().warning(
                        "[配置加载] dashscope.custom_size=%s 超出 16-4096 像素范围，已保留原值",
                        raw,
                    )
            else:
                _logger().warning(
                    "[配置加载] dashscope.custom_size=%s 格式无法识别（应为 WxH 或 1K/2K/4K），已保留原值",
                    raw,
                )

    n_raw = settings.get("n")
    if n_raw is not None:
        try:
            settings["n"] = max(1, min(int(n_raw), 12))
        except (TypeError, ValueError):
            _logger().warning("[配置加载] dashscope.n=%r 无效，已回退为 1", n_raw)
            settings["n"] = 1


def _make_model_prefix_edit_gate(*capable_prefixes: str):
    """构造按模型前缀判定编辑能力的 hook（供 edit_capability_path 使用）。"""

    def gate(settings: dict[str, Any]) -> bool:
        model = str(settings.get("model") or "").strip().lower()
        return model.startswith(capable_prefixes)

    return gate


_stepfun_edit_gate = _make_model_prefix_edit_gate("step-image-edit")
_dashscope_edit_gate = _make_model_prefix_edit_gate(
    "wan2.7",
    "qwen-image-2.0",
    "qwen-image-3.0",
    "qwen-image-edit",
)


def stepfun_edit_capability(settings: dict[str, Any]) -> bool:
    """仅 step-image-edit 系列支持 /v1/images/edits。"""
    return _stepfun_edit_gate(settings)


def dashscope_edit_capability(settings: dict[str, Any]) -> bool:
    """wan2.7 / qwen-image-2.0 / qwen-image-3.0 / qwen-image-edit 系列支持图像输入。"""
    return _dashscope_edit_gate(settings)
