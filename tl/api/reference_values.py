"""参考图 → API 图片值（公网 URL / data URI）的共享归一化。

提取自 DashScope edits/messages 场景；sensenova-u1.5-lite 编辑接口等
需要“URL 或 data:image/*;base64, 形态”输入的 provider 复用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from .data_uri import format_data_uri, looks_like_base64, strip_data_uri_prefix
from .reference_intake import announce_reference_intake


async def resolve_reference_api_values(
    client: Any,  # noqa: ANN401
    config: ApiRequestConfig,
    refs: list[str] | None,
    *,
    max_count: int,
    log_prefix: str = "",
    error_label: str = "dashscope",
) -> list[str]:
    """将参考图列表归一化为 API 可接受的 URL / data URI 值列表。

    超出 ``max_count`` 按顺序截取；URL 默认透传（image_input_mode=force_base64 时
    转 base64），本地路径经客户端归一化后转 data URI。
    """
    if not refs:
        return []

    announce_reference_intake(refs, max_count, log_prefix=log_prefix)
    force_b64 = getattr(config, "image_input_mode", "force_base64") == "force_base64"

    values: list[str] = []
    for image_str in refs[:max_count]:
        value = await _resolve_single_value(
            client=client,
            config=config,
            image_str=str(image_str or ""),
            force_b64=force_b64,
            error_label=error_label,
            log_prefix=log_prefix,
        )
        if value:
            values.append(value)
    return values


async def _resolve_single_value(
    *,
    client: Any,  # noqa: ANN401
    config: ApiRequestConfig,
    image_str: str,
    force_b64: bool,
    error_label: str,
    log_prefix: str,
) -> str | None:
    if not image_str:
        return None

    # URL 输入且不强制 base64 → 原样透传
    if image_str.startswith(("http://", "https://")) and not force_b64:
        return image_str

    # 已是标准 data URI → 原样
    if image_str.startswith("data:image/") and ";base64," in image_str:
        return image_str

    # 裸 base64 → 补 data URI 前缀
    if looks_like_base64(image_str) and not image_str.startswith("data:"):
        return format_data_uri(strip_data_uri_prefix(image_str))

    # 其余（本地路径 / 强制 base64 的 URL）走客户端归一化；
    # 共享归一化器不认裸路径，先转 file:// URI
    normalize_input = image_str
    if "://" not in image_str and Path(image_str).is_file():
        normalize_input = Path(image_str).resolve().as_uri()
    try:
        mime_type, b64_data = await client._normalize_reference_image_input(
            normalize_input,
            image_input_mode=getattr(config, "image_input_mode", "force_base64"),
        )
    except Exception as e:
        logger.debug("%s normalize_reference_image_input failed: %s", log_prefix, e)
        mime_type, b64_data = None, None

    if not b64_data:
        if force_b64:
            raise APIError(
                f"参考图转换失败（{error_label}），请检查图片来源后重试。",
                None,
                "invalid_reference_image",
                retryable=False,
            )
        if image_str.startswith(("http://", "https://")):
            return image_str
        return None

    return format_data_uri(strip_data_uri_prefix(b64_data), mime_type)
