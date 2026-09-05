"""Studio 单次生成参数：复用供应商 schema，隔离连接配置。"""

from __future__ import annotations

import copy
import json
import math
from functools import cache
from pathlib import Path
from typing import Any

from .provider_capabilities import candidate_capability, candidate_reference_limit

# 只向 Studio 开放生成字段；新连接或管理字段不会随 schema 增加而自动暴露。
GENERATION_SETTING_KEYS = frozenset(
    {
        "resolution",
        "aspect_ratio",
        "max_reference_images",
        "force_resolution",
        "resolution_param_name",
        "aspect_ratio_param_name",
        "enable_text_response",
        "enable_grounding",
        "image_search",
        "thinking_level",
        "response_format",
        "reference_image_mode",
        "quality",
        "n",
        "prompt_optimizer",
        "aigc_watermark",
        "subject_reference_type",
        "style_type",
        "style_weight",
        "width",
        "height",
        "seed",
        "steps",
        "cfg_scale",
        "negative_prompt",
        "text_mode",
        "size_mode",
        "custom_size",
        "style",
        "background",
        "output_format",
        "output_compression",
        "moderation",
        "generations_only",
        "size",
        "watermark",
        "optimize_prompt_mode",
        "sequential_image_generation",
        "sequential_max_images",
        "default_size",
        "prompt_extend",
        "thinking_mode",
        "enable_sequential",
        "guidance",
        "loras",
        "num_inference_steps",
        "guidance_scale",
    }
)


@cache
def _templates() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    return schema["provider_settings"]["items"]["provider_overrides"]["templates"]


def generation_fields(candidate: Any) -> dict[str, dict[str, Any]]:
    template = _templates().get(candidate.api_type, {})
    capability = candidate_capability(candidate)
    runtime_fields = capability.get("parameters") or {}
    setting_map = capability.get("request_setting_map") or {}
    reverse_map = {setting: runtime for runtime, setting in setting_map.items()}
    fields: dict[str, dict[str, Any]] = {}
    for name, schema in template.get("items", {}).items():
        if name not in GENERATION_SETTING_KEYS:
            continue
        field = {
            "type": {
                "bool": "boolean",
                "int": "integer",
                "float": "number",
                "string": "string",
            }[schema["type"]],
            "label": schema.get("description") or name,
            "hint": schema.get("hint") or "",
            "value": copy.deepcopy(candidate.settings.get(name, schema.get("default"))),
        }
        if "options" in schema:
            field["enum"] = copy.deepcopy(schema["options"])
        for source, target in (
            ("min", "minimum"),
            ("max", "maximum"),
            ("step", "step"),
        ):
            if source in schema.get("slider", {}):
                field[target] = schema["slider"][source]
        if schema.get("condition"):
            field["condition"] = copy.deepcopy(schema["condition"])
        if field["type"] == "string":
            field["max_length"] = 16384 if name == "loras" else 2000
        field["runtime_parameter"] = reverse_map.get(name, name)
        field["multiline"] = name in {"negative_prompt", "loras"}
        runtime = runtime_fields.get(reverse_map.get(name, name), {})
        # 模型能力提供更准确的枚举（例如 DALL-E 与 GPT Image 的画质）。
        for key in ("enum", "minimum", "maximum"):
            if key in runtime and name != "n":
                field[key] = copy.deepcopy(runtime[key])
                if (
                    key == "enum"
                    and "" in schema.get("options", [])
                    and "" not in field[key]
                ):
                    field[key].insert(0, "")
        if name == "max_reference_images":
            field["minimum"] = 0
            field["maximum"] = native_reference_limit(candidate)
        if name == "generations_only":
            field["disables_references_when"] = True
        if name == "n":
            field["label"] = "单次请求张数上限（n）"
            field["hint"] = (
                "不覆盖时随任务张数自动安排；临时覆盖后，每次请求最多生成此数量，总张数仍以上方设置为准。"
            )
        fields[name] = field
    return fields


def validate_generation_settings(candidate: Any, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("临时生成参数必须是 JSON 对象")
    fields = generation_fields(candidate)
    result = {}
    for name, item in value.items():
        if name not in fields:
            raise ValueError(f"不允许临时覆盖参数：{name}")
        field = fields[name]
        kind = field["type"]
        valid = (
            (kind == "boolean" and type(item) is bool)
            or (kind == "integer" and type(item) is int and abs(item) <= 2**53 - 1)
            or (
                kind == "number"
                and (
                    (type(item) is int and abs(item) <= 2**53 - 1)
                    or (type(item) is float and math.isfinite(item))
                )
            )
            or (
                kind == "string"
                and isinstance(item, str)
                and len(item) <= field["max_length"]
            )
        )
        if not valid:
            raise ValueError(f"{field['label']} 的类型或长度不正确")
        if "enum" in field and item not in field["enum"]:
            raise ValueError(f"{field['label']} 不在可选范围内")
        if kind in {"integer", "number"}:
            if "minimum" in field and item < field["minimum"]:
                raise ValueError(f"{field['label']} 不能小于 {field['minimum']}")
            if "maximum" in field and item > field["maximum"]:
                raise ValueError(f"{field['label']} 不能大于 {field['maximum']}")
        result[name] = item
    return result


def clean_history_settings(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        name: item
        for name, item in value.items()
        if name in GENERATION_SETTING_KEYS
        and (
            type(item) in (bool, int)
            or (type(item) is float and math.isfinite(item))
            or (
                isinstance(item, str)
                and len(item) <= (16384 if name == "loras" else 2000)
            )
        )
    }


def native_reference_limit(candidate: Any) -> int:
    from .provider_settings import candidate_with_overrides

    overrides = {"max_reference_images": 999}
    if "generations_only" in _templates().get(candidate.api_type, {}).get("items", {}):
        overrides["generations_only"] = False
    return candidate_reference_limit(candidate_with_overrides(candidate, overrides))


def clean_browser_preferences(value: Any, candidates: list[Any]) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    saved = source.get("candidates")
    saved = saved if isinstance(saved, dict) else {}
    result: dict[str, Any] = {"selected": None, "candidates": {}}
    for candidate in candidates:
        identity = {
            "candidate_id": candidate.id,
            "provider": candidate.api_type,
            "model": candidate.model,
        }
        if source.get("selected") == identity:
            result["selected"] = identity
        item = saved.get(candidate.id)
        if not isinstance(item, dict) or any(
            item.get(key) != val for key, val in identity.items()
        ):
            continue
        settings = {}
        raw_settings = item.get("generation_settings")
        if isinstance(raw_settings, dict):
            for name, val in raw_settings.items():
                try:
                    settings.update(
                        validate_generation_settings(candidate, {name: val})
                    )
                except ValueError:
                    continue
        entry = {
            **identity,
            "generation_settings": settings,
            "expanded": item.get("expanded") is True,
        }
        descriptors = candidate_capability(candidate).get("parameters") or {}
        for name in ("resolution", "aspect_ratio"):
            val = item.get(name)
            if isinstance(val, str) and (
                val == "" or val in descriptors.get(name, {}).get("enum", [])
            ):
                entry[name] = val
        count = item.get("image_count")
        if type(count) is int and 1 <= count <= 10:
            entry["image_count"] = count
        result["candidates"][candidate.id] = entry
    return result
