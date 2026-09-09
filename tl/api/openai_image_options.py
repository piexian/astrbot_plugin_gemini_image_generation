"""GPT Image output options shared by the two OpenAI image protocols."""

from __future__ import annotations

from typing import Any

from ..api_types import APIError


def gpt_image_quality_values(model: str) -> list[str]:
    values = ["auto", "high", "medium", "low"]
    name = model.strip().lower()
    if name.startswith("gpt-image-2.5") or not name.startswith(
        ("gpt-image-1", "gpt-image-2", "chatgpt-image")
    ):
        values.extend(["xhigh", "max"])
    return values


def gpt_image_output_options(model: str, settings: dict[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    allowed = {
        "quality": gpt_image_quality_values(model),
        "output_format": ["png", "jpeg", "webp"],
        "background": ["auto", "opaque", "transparent"],
        "moderation": ["auto", "low"],
    }
    for key, values in allowed.items():
        value = settings.get(key)
        if value in (None, ""):
            continue
        if value not in values:
            raise APIError(
                f"{model} 的 {key} 不支持 {value!r}，可选：{', '.join(values)}",
                error_type="invalid_parameter",
                retryable=False,
            )
        options[key] = value
    output_format = options.get("output_format", "png")
    if options.get("background") == "transparent" and output_format == "jpeg":
        raise APIError(
            "透明背景只支持 PNG 或 WebP。",
            error_type="invalid_parameter",
            retryable=False,
        )
    compression = settings.get("output_compression")
    if compression not in (None, ""):
        if (
            isinstance(compression, bool)
            or not isinstance(compression, int)
            or not 0 <= compression <= 100
        ):
            raise APIError(
                "output_compression 必须是 0–100 的整数。",
                error_type="invalid_parameter",
                retryable=False,
            )
        # PNG has no compression control; a stored JPEG/WebP value may remain
        # when the user switches formats in the configuration editor.
        if output_format in {"jpeg", "webp"}:
            options["output_compression"] = compression
    return options
