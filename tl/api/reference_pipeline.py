"""参考图输入 → API 图片值 的唯一共享管道。

所有供应商不得私写参考图解码/转换逻辑；新增输入形态（如网关相对路径）
只扩展本文件。复用 :mod:`tl.reference_image` 的归一化与
:mod:`tl.api.data_uri` 的编码辅助。
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Any, Final

from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from .compat_utils import is_temp_cache_url
from .data_uri import format_data_uri, strip_data_uri_prefix

# 本地文件优先判定的输入长度上限；超过视为 base64 数据而非路径
_LOCAL_PATH_MAX_CHARS: Final[int] = 1024

# 单条源 URL 的长度上限与任务记录上限
SOURCE_URL_MAX_CHARS: Final[int] = 2048
SOURCE_URL_MAX_ITEMS: Final[int] = 20

__all__ = [
    "load_reference_bytes",
    "reference_data_uri",
    "select_persistent_source_urls",
    "transcode_to_supported_mime",
]


async def load_reference_bytes(
    client: Any,  # noqa: ANN401
    config: ApiRequestConfig,
    image_input: Any,  # noqa: ANN401
    *,
    log_prefix: str,
) -> bytes | None:
    """把参考图输入解析为原始字节。

    base64/data URI 直接解码；本地路径与远程 URL 交给客户端共享归一化
    （本地路径转 file:// URI，URL 透传候选代理）。解析失败返回 None，
    由调用方决定错误语义。
    """
    if isinstance(image_input, (bytes, bytearray)):
        return bytes(image_input) or None
    text = str(image_input or "").strip()
    if not text:
        return None

    is_local_file = _looks_like_local_file(text)
    if not is_local_file:
        payload = _strip_data_uri_header(text)
        try:
            return base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            pass

    normalize = getattr(client, "_normalize_reference_image_input", None)
    if normalize is None:
        logger.debug("%s 参考图非 base64 且客户端缺少共享归一化器", log_prefix)
        return None
    normalize_input = Path(text).resolve().as_uri() if is_local_file else text
    try:
        _mime, b64_data = await _normalize(normalize, normalize_input, config)
    except Exception as exc:
        logger.debug("%s 参考图归一化失败: %s", log_prefix, exc)
        return None
    if not b64_data:
        return None
    try:
        return base64.b64decode(strip_data_uri_prefix(b64_data), validate=True)
    except (binascii.Error, ValueError) as exc:
        logger.debug("%s 参考图 base64 解码失败: %s", log_prefix, exc)
        return None


def _looks_like_local_file(text: str) -> bool:
    """短、无 scheme、非 data URI 且真实存在的路径按文件处理。"""
    return (
        "://" not in text
        and not text.startswith("data:")
        and len(text) <= _LOCAL_PATH_MAX_CHARS
        and Path(text).is_file()
    )


def _strip_data_uri_header(text: str) -> str:
    if text.startswith("data:"):
        parts = text.split(",", 1)
        if len(parts) == 2:
            return parts[1]
    return text


def _normalize(normalize: Any, image_input: str, config: ApiRequestConfig):
    return normalize(
        image_input,
        image_input_mode=getattr(config, "image_input_mode", "force_base64"),
        **({"request_proxy": config.proxy} if getattr(config, "proxy", None) else {}),
    )


async def reference_data_uri(
    client: Any,  # noqa: ANN401
    config: ApiRequestConfig,
    image_input: Any,  # noqa: ANN401
    *,
    log_prefix: str,
    error_label: str,
    force_b64: bool | None = None,
    validate_data_uri: bool = False,
) -> str | None:
    """把参考图归一化为 ``data:image/*;base64,`` 形态。

    已是 data URI 原样返回（``validate_data_uri`` 时校验，无效返回 None）；
    裸 base64 补前缀；本地路径与 URL 解码后重编码。
    解析失败时：force_base64 抛不可重试错误；URL 在非强制时原样透传
    （跳过临时缓存 URL）；其余返回 None。
    """
    if isinstance(image_input, (bytes, bytearray)):
        raw = bytes(image_input)
        return (
            format_data_uri(base64.b64encode(raw).decode(), "image/png")
            if raw
            else None
        )
    image_str = str(image_input or "").strip()
    if not image_str:
        return None

    force = (
        getattr(config, "image_input_mode", "force_base64") == "force_base64"
        if force_b64 is None
        else force_b64
    )
    if image_str.startswith("data:image/") and ";base64," in image_str:
        if not validate_data_uri:
            return image_str
        payload = image_str.split(",", 1)[1]
        try:
            base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            logger.debug("%s 无效 data URI 参考图: %s", log_prefix, exc)
            return None
        return image_str
    if not image_str.startswith("data:") and not _looks_like_local_file(image_str):
        try:
            raw = base64.b64decode(image_str, validate=True)
        except (binascii.Error, ValueError):
            raw = None
        if raw is not None:
            return format_data_uri(image_str)
    if image_str.startswith(("http://", "https://")) and not force:
        if not is_temp_cache_url(image_str):
            return image_str
        logger.debug("%s 跳过临时缓存 URL: %s", log_prefix, image_str[:120])
        return None

    # 本地路径与 force 模式的 URL 走客户端共享归一化；
    # normalize 返回的 base64 视为可信结果，不再二次校验。
    normalize = getattr(client, "_normalize_reference_image_input", None)
    if normalize is None:
        logger.debug("%s 参考图非 base64 且客户端缺少共享归一化器", log_prefix)
        if force:
            raise APIError(
                f"参考图转换失败（{error_label}），请检查图片来源后重试。",
                None,
                "invalid_reference_image",
                retryable=False,
            )
        return None
    normalize_input = (
        Path(image_str).resolve().as_uri()
        if _looks_like_local_file(image_str)
        else image_str
    )
    try:
        mime_type, b64_data = await _normalize(normalize, normalize_input, config)
    except Exception as exc:
        logger.debug("%s 参考图归一化失败: %s", log_prefix, exc)
        mime_type, b64_data = None, None
    if not b64_data:
        if force:
            raise APIError(
                f"参考图转换失败（{error_label}），请检查图片来源后重试。",
                None,
                "invalid_reference_image",
                retryable=False,
            )
        if image_str.startswith(("http://", "https://")):
            return image_str
        return None
    if not mime_type or not mime_type.startswith("image/"):
        try:
            sniffed = _sniff_mime(
                base64.b64decode(strip_data_uri_prefix(b64_data), validate=False),
                image_str,
            )
        except (binascii.Error, ValueError):
            sniffed = None
        if sniffed:
            mime_type = sniffed
    return format_data_uri(strip_data_uri_prefix(b64_data), mime_type)


def _sniff_mime(raw: bytes, image_str: str) -> str | None:
    if image_str.startswith("data:"):
        header = image_str.split(",", 1)[0]
        if header.startswith("data:image/"):
            return header[len("data:") :]
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if raw.startswith(b"BM"):
        return "image/bmp"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def select_persistent_source_urls(value: Any) -> list[str]:
    """从交付来源中筛选可长期访问的 http(s) URL，供任务记录与回溯。"""
    if isinstance(value, (str, bytes)) or value is None:
        return []
    try:
        items = list(value)
    except TypeError:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        url = item.strip()
        if (
            not url.startswith(("http://", "https://"))
            or len(url) > SOURCE_URL_MAX_CHARS
            or url in seen
            or is_temp_cache_url(url)
        ):
            continue
        seen.add(url)
        result.append(url)
        if len(result) >= SOURCE_URL_MAX_ITEMS:
            break
    return result


def transcode_to_supported_mime(
    raw: bytes,
    mime_type: str | None,
    *,
    error_label: str,
    supported: frozenset[str] = frozenset({"image/jpeg", "image/png"}),
    max_bytes: int = 10 * 1024 * 1024,
) -> tuple[str, str]:
    """把参考图字节归一化为受支持格式，返回 (b64, mime)。

    mime 在白名单内原样返回；其余解码后转码为 PNG（动图取首帧）；
    解码失败或超过大小限制抛不可重试错误。
    """
    import io

    mime = (mime_type or "").strip().lower()
    if len(raw) > max_bytes:
        raise APIError(
            f"参考图超过 {max_bytes // (1024 * 1024)}MB 大小限制（{error_label}）",
            None,
            "invalid_reference_image",
            retryable=False,
        )
    if mime in supported:
        return base64.b64encode(raw).decode("ascii"), mime

    from PIL import Image as PILImage

    try:
        with PILImage.open(io.BytesIO(raw)) as img:
            img.seek(0)  # 动图（GIF/WebP）取首帧
            if img.mode in ("RGBA", "LA", "P", "PA"):
                img = img.convert("RGBA")
            else:
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
    except Exception as exc:
        raise APIError(
            f"参考图格式 {mime or '未知'} 无法转换为受支持的 PNG/JPEG（{error_label}）：{exc}",
            None,
            "invalid_reference_image",
            retryable=False,
        ) from exc
    encoded = buf.getvalue()
    if len(encoded) > max_bytes:
        raise APIError(
            f"参考图转码后超过 {max_bytes // (1024 * 1024)}MB 大小限制（{error_label}）",
            None,
            "invalid_reference_image",
            retryable=False,
        )
    logger.info(
        "%s参考图 %s 不在白名单，已转码为 PNG",
        f"[{error_label}] ",
        mime or "未知",
    )
    return base64.b64encode(encoded).decode("ascii"), "image/png"
