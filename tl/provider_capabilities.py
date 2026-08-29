"""Provider/model capabilities shared by routing and LLM tools."""

from __future__ import annotations

from typing import Any

from .api_normalize import normalize_api_type
from .provider_hooks import is_doubao_seedream_5_pro
from .provider_loader import load_callable
from .provider_metadata import get_provider_spec

TEXT_TO_IMAGE = "text_to_image"
IMAGE_TO_IMAGE = "image_to_image"

_RESOLUTIONS = ["1K", "2K", "4K"]
_ASPECT_RATIOS = [
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
]


def _model(candidate: Any) -> str:
    return str(getattr(candidate, "model", "") or "").strip()


def _settings(candidate: Any) -> dict[str, Any]:
    value = getattr(candidate, "settings", None)
    return value if isinstance(value, dict) else {}


def _base_parameters(*, supports_edit: bool, native_batch_limit: int) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "resolution": {
            "type": "string",
            "enum": _RESOLUTIONS,
            "default_source": "provider_config",
        },
        "aspect_ratio": {
            "type": "string",
            "enum": _ASPECT_RATIOS,
            "default_source": "provider_config",
        },
        "image_count": {
            "type": "integer",
            "minimum": 1,
            "native_request_maximum": max(int(native_batch_limit), 1),
        },
    }
    if supports_edit:
        parameters.update(
            {
                "use_reference_images": {"type": "boolean", "default": False},
                "include_user_avatar": {"type": "boolean", "default": False},
                "reference_image_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "preserve_reference_image_size": {
                    "type": "boolean",
                    "default_source": "plugin_config",
                },
            }
        )
    return parameters


def _profile(
    candidate: Any,
    *,
    native_batch_limit: int = 1,
    parameters: dict[str, Any] | None = None,
    request_setting_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    supports_edit = bool(getattr(candidate, "supports_image_edit", False))
    limit = max(int(native_batch_limit), 1)
    merged = _base_parameters(
        supports_edit=supports_edit,
        native_batch_limit=limit,
    )
    merged.update(parameters or {})
    return {
        "generation_modes": [
            TEXT_TO_IMAGE,
            *([IMAGE_TO_IMAGE] if supports_edit else []),
        ],
        "parameters": merged,
        "native_batch_limit": limit,
        "request_setting_map": dict(request_setting_map or {}),
    }


def xai_capability(candidate: Any) -> dict[str, Any]:
    return _profile(
        candidate,
        native_batch_limit=10,
        parameters={
            "quality": {
                "type": "string",
                "enum": ["low", "medium"],
                "default_source": "provider_config",
            }
        },
        request_setting_map={"quality": "quality", "image_count": "n"},
    )


def minimax_capability(candidate: Any) -> dict[str, Any]:
    return _profile(
        candidate,
        native_batch_limit=9,
        parameters={
            "watermark": {
                "type": "boolean",
                "default_source": "provider_config",
            }
        },
        request_setting_map={"watermark": "aigc_watermark", "image_count": "n"},
    )


def stepfun_capability(candidate: Any) -> dict[str, Any]:
    # negative_prompt/text_mode 仅 step-image-edit 系列支持，其余模型不声明
    if not _model(candidate).lower().startswith("step-image-edit"):
        return _profile(candidate)
    return _profile(
        candidate,
        parameters={
            "negative_prompt": {
                "type": "string",
                "default_source": "provider_config",
            }
        },
        request_setting_map={"negative_prompt": "negative_prompt"},
    )


def openai_images_capability(candidate: Any) -> dict[str, Any]:
    model = _model(candidate).lower()
    if model == "dall-e-3":
        quality_values = ["hd", "standard"]
    elif model == "dall-e-2":
        quality_values = ["standard"]
    elif model.startswith(("gpt-image", "chatgpt-image")):
        quality_values = ["auto", "high", "medium", "low"]
    else:
        quality_values = ["auto", "high", "medium", "low", "hd", "standard"]
    return _profile(
        candidate,
        parameters={
            "quality": {
                "type": "string",
                "enum": quality_values,
                "default_source": "provider_config",
            },
        },
        request_setting_map={"quality": "quality"},
    )


def doubao_capability(candidate: Any) -> dict[str, Any]:
    settings = _settings(candidate)
    model = _model(candidate) or str(settings.get("endpoint_id") or "").strip()
    is_seedream_5_pro = is_doubao_seedream_5_pro(model, settings)
    # Seedream 5.0 Pro 仅支持单图。
    sequential = (
        settings.get("sequential_image_generation") == "auto" and not is_seedream_5_pro
    )
    try:
        configured_limit = int(settings.get("sequential_max_images", 1))
    except (TypeError, ValueError):
        configured_limit = 1
    native_limit = min(max(configured_limit, 1), 15) if sequential else 1
    setting_map = {"watermark": "watermark"}
    if sequential:
        setting_map["image_count"] = "sequential_max_images"
    return _profile(
        candidate,
        native_batch_limit=native_limit,
        parameters={
            "watermark": {
                "type": "boolean",
                "default_source": "provider_config",
            }
        },
        request_setting_map=setting_map,
    )


def sensenova_capability(candidate: Any) -> dict[str, Any]:
    settings = _settings(candidate)
    model = _model(candidate).lower()
    if model.startswith("sensenova-u1.5"):
        return _profile(
            candidate,
            native_batch_limit=1,
            parameters={
                "watermark": {
                    "type": "boolean",
                    "default_source": "provider_config",
                }
            },
            request_setting_map={"watermark": "watermark", "image_count": "n"},
        )
    return _profile(
        candidate,
        native_batch_limit=4,
        request_setting_map={"image_count": "n"},
    )


def dashscope_capability(candidate: Any) -> dict[str, Any]:
    settings = _settings(candidate)
    model = _model(candidate).lower()
    is_wan27 = model.startswith("wan2.7")
    is_zimage = model.startswith("z-image")
    if is_wan27 and bool(settings.get("enable_sequential", False)):
        native_limit = 12
    elif is_wan27:
        native_limit = 4
    elif model.startswith(("qwen-image-2.0", "qwen-image-3.0")):
        native_limit = 6
    else:
        native_limit = 1
    parameters: dict[str, Any] = {}
    setting_map = {"image_count": "n"}
    if not is_zimage:
        # z-image 为纯文生图最小参数集，不支持 watermark/negative_prompt
        parameters["watermark"] = {
            "type": "boolean",
            "default_source": "provider_config",
        }
        setting_map["watermark"] = "watermark"
    if not is_wan27 and not is_zimage:
        parameters["negative_prompt"] = {
            "type": "string",
            "default_source": "provider_config",
        }
        setting_map["negative_prompt"] = "negative_prompt"
    return _profile(
        candidate,
        native_batch_limit=native_limit,
        parameters=parameters,
        request_setting_map=setting_map,
    )


def candidate_capability(candidate: Any) -> dict[str, Any]:
    """Return the effective tool capability for one configured candidate."""
    spec = get_provider_spec(getattr(candidate, "api_type", ""))
    if spec and spec.capability_profile_path:
        value = load_callable(spec.capability_profile_path)(candidate)
        if isinstance(value, dict):
            return value
    return _profile(candidate)


def routing_mode(provider: Any = None, model: Any = None) -> str:
    has_provider = bool(str(provider or "").strip())
    has_model = bool(str(model or "").strip())
    if has_provider and has_model:
        return "provider_model_retry"
    if has_provider:
        return "provider_retry"
    if has_model:
        return "model_polling"
    return "full_polling"


def routing_description(mode: str) -> str:
    return {
        "provider_model_retry": "正在指定供应商和模型内部重试生成",
        "provider_retry": "正在当前供应商内部按配置模型重试生成",
        "model_polling": "正在按指定模型跨供应商轮询生成",
        "full_polling": "正在按已配置供应商和模型轮询生成",
        "mixed": "正在按各批量条目指定的供应商和模型路由生成",
    }.get(mode, "正在生成图片")


def explicit_runtime_parameters(config: Any) -> set[str]:
    required: set[str] = set()
    if getattr(config, "negative_prompt", None) not in (None, ""):
        required.add("negative_prompt")
    if getattr(config, "watermark", None) is not None:
        required.add("watermark")
    if getattr(config, "quality", None) not in (None, ""):
        required.add("quality")
    return required


def select_candidates(
    candidates: list[Any],
    *,
    provider: Any = None,
    model: Any = None,
    has_reference_images: bool = False,
    required_parameters: set[str] | None = None,
    request_values: dict[str, Any] | None = None,
) -> list[Any]:
    """Filter candidates without changing their configured polling order."""
    provider_name = normalize_api_type(provider) if provider else ""
    model_name = str(model or "").strip()
    required = set(required_parameters or ())
    values = request_values or {}
    selected: list[Any] = []
    for candidate in candidates or []:
        if (
            provider_name
            and normalize_api_type(getattr(candidate, "api_type", "")) != provider_name
        ):
            continue
        alias = str(getattr(candidate, "model_alias", "") or "").strip()
        if model_name and model_name not in {_model(candidate), alias}:
            continue
        capability = candidate_capability(candidate)
        modes = set(capability.get("generation_modes") or ())
        if has_reference_images and IMAGE_TO_IMAGE not in modes:
            continue
        supported = set((capability.get("parameters") or {}).keys())
        if not required.issubset(supported):
            continue
        descriptors = capability.get("parameters") or {}
        invalid_value = False
        for name, value in values.items():
            descriptor = descriptors.get(name) or {}
            allowed = descriptor.get("enum")
            if allowed and value not in allowed:
                invalid_value = True
                break
        if invalid_value:
            continue
        selected.append(candidate)
    return selected


def apply_request_overrides(
    config: Any,
    candidate: Any,
    settings: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """Apply request-scoped tool values to a copied candidate settings mapping."""
    result = settings

    def set_request_value(name: str, value: Any) -> None:
        nonlocal result
        if result is settings:
            result = dict(settings)
        result[name] = value

    capability = candidate_capability(candidate)
    setting_map = capability.get("request_setting_map") or {}
    for request_name in ("negative_prompt", "watermark", "quality"):
        value = getattr(config, request_name, None)
        setting_name = setting_map.get(request_name)
        if value is not None and setting_name:
            set_request_value(setting_name, value)

    native_limit = max(int(capability.get("native_batch_limit") or 1), 1)
    try:
        requested_count = max(int(getattr(config, "image_count", 1) or 1), 1)
    except (TypeError, ValueError):
        requested_count = 1
    effective_count = min(requested_count, native_limit)
    count_setting = setting_map.get("image_count")
    if count_setting and result.get(count_setting) != effective_count:
        set_request_value(count_setting, effective_count)
    return result, effective_count
