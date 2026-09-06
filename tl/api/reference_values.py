"""参考图 → API 图片值（公网 URL / data URI）的共享归一化。

提取自 DashScope edits/messages 场景；sensenova-u1.5-lite 编辑接口等
需要“URL 或 data:image/*;base64, 形态”输入的 provider 复用。
"""

from __future__ import annotations

from typing import Any

from ..api_types import APIError, ApiRequestConfig
from .data_uri import strip_data_uri_prefix
from .reference_intake import announce_reference_intake
from .reference_pipeline import reference_data_uri, transcode_to_supported_mime


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
    return await reference_data_uri(
        client,
        config,
        image_str,
        log_prefix=log_prefix,
        error_label=error_label,
        force_b64=force_b64,
    )


# MiniMax 官方 image_file 仅支持 JPG/JPEG/PNG 且小于 10MB
SUPPORTED_MIMES_JPEG_PNG: frozenset[str] = frozenset({"image/jpeg", "image/png"})
_MAX_REFERENCE_IMAGE_BYTES: int = 10 * 1024 * 1024


def normalize_image_mime(
    b64_data: str,
    mime_type: str | None,
    *,
    error_label: str = "minimax",
) -> tuple[str, str]:
    """将参考图 base64 归一化为受支持格式（委托共享管道）。"""
    import base64

    try:
        raw = base64.b64decode(strip_data_uri_prefix(b64_data), validate=True)
    except Exception as e:
        raise APIError(
            f"参考图 base64 解码失败（{error_label}）：{e}",
            None,
            "invalid_reference_image",
            retryable=False,
        ) from e
    return transcode_to_supported_mime(
        raw,
        mime_type,
        error_label=error_label,
        supported=SUPPORTED_MIMES_JPEG_PNG,
        max_bytes=_MAX_REFERENCE_IMAGE_BYTES,
    )
