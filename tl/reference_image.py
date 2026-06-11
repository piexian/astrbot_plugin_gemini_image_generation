"""Reference image normalization helpers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import re
import urllib.parse
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger

try:
    from PIL import Image as PILImage
except ImportError:  # pragma: no cover - depends on runtime optional dependency
    PILImage = None


SUPPORTED_REFERENCE_IMAGE_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}
)

QQ_REFERENCE_IMAGE_HOSTS = frozenset(
    {"qpic.cn", "qq.com", "nt.qq.com", "gchat.qpic.cn"}
)


def get_reference_image_cache_dir() -> Path:
    """Return the plugin cache directory used for downloaded reference images."""
    try:
        from astrbot.api.star import StarTools

        return (
            StarTools.get_data_dir("astrbot_plugin_gemini_image_generation")
            / "images"
            / "download_cache"
        )
    except Exception:
        return Path(".") / "images" / "download_cache"


REFERENCE_IMAGE_CACHE_DIR = get_reference_image_cache_dir()


def extract_reference_image_source(image_input: Any) -> str:
    """Normalize light text wrappers around a reference image source."""
    image_str = str(image_input or "").strip()
    md_match = re.search(r"!\[[^\]]*\]\(\s*([^)]+)\s*\)", image_str)
    if md_match:
        image_str = md_match.group(1).strip()
    if "&amp;" in image_str:
        image_str = image_str.replace("&amp;", "&")
    return image_str


def is_qq_image_host(host: str) -> bool:
    """Return whether host belongs to QQ image delivery."""
    if not host:
        return False
    host_lower = host.lower()
    return any(qq_host in host_lower for qq_host in QQ_REFERENCE_IMAGE_HOSTS)


def detect_reference_image_mime(raw: bytes) -> str | None:
    """Detect MIME for image formats providers can receive directly."""
    if not raw or len(raw) < 4:
        return None
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if raw.startswith(b"RIFF") and len(raw) >= 12 and raw[8:12] == b"WEBP":
        return "image/webp"
    if len(raw) >= 12:
        brand = raw[4:12]
        if brand in {b"ftypheic", b"ftyphevc"}:
            return "image/heic"
        if brand in {b"ftypheif", b"ftypmif1", b"ftypmsf1"}:
            return "image/heif"
    return None


def build_reference_image_headers(
    host: str = "", for_qq: bool = False
) -> dict[str, str]:
    """Build HTTP headers suitable for fetching reference images."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Accept-Encoding": "gzip, deflate, br",
    }
    if host:
        scheme = "https" if not for_qq else "http"
        headers["Referer"] = f"{scheme}://{host}"

    if for_qq or is_qq_image_host(host):
        headers["Referer"] = "https://qun.qq.com"
        headers["Origin"] = "https://qun.qq.com"
        if ",image/png" not in headers["Accept"]:
            headers["Accept"] += ",image/png"

    return headers


def check_reference_image_cache(url: str, cache_dir: Path) -> Path | None:
    """Return a cached reference image path for url when present."""
    try:
        cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached = next(
            (
                p
                for p in cache_dir.glob(f"{cache_key}.*")
                if p.exists() and p.stat().st_size > 0
            ),
            None,
        )
        return cached
    except Exception as e:
        logger.debug(f"检查参考图缓存失败: {e}")
        return None


def save_reference_image_cache(
    url: str, data: bytes, mime_type: str, cache_dir: Path
) -> Path | None:
    """Save downloaded reference image bytes to cache."""
    try:
        cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        suffix = mime_type.split("/")[-1] if "/" in mime_type else "png"
        cache_file = cache_dir / f"{cache_key}.{suffix}"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(data)
        return cache_file
    except Exception as e:
        logger.debug(f"写入参考图缓存失败: {e}")
        return None


def _encode_file_to_base64(file_path: Path, chunk_size: int = 65536) -> str:
    chunk_size = (chunk_size // 3) * 3
    if chunk_size == 0:
        chunk_size = 3

    encoded_parts: list[str] = []
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            encoded_parts.append(base64.b64encode(chunk).decode("utf-8"))
    return "".join(encoded_parts)


def coerce_reference_image_bytes(
    mime_type: str | None, raw_bytes: bytes
) -> tuple[str | None, str | None]:
    """
    Convert image bytes to a provider-supported MIME/base64 pair.

    PNG/JPEG/WEBP/HEIC/HEIF bytes pass through unchanged. Unsupported formats are
    re-encoded with Pillow when it is available.
    """
    normalized_mime = (mime_type or "").lower()
    detected_mime = detect_reference_image_mime(raw_bytes)
    if detected_mime:
        if normalized_mime and normalized_mime != detected_mime:
            logger.debug(
                "参考图 MIME 与文件头不一致，使用文件头识别结果: "
                f"{normalized_mime} -> {detected_mime}"
            )
        return detected_mime, base64.b64encode(raw_bytes).decode("utf-8")

    target_mime = (
        normalized_mime
        if normalized_mime in SUPPORTED_REFERENCE_IMAGE_MIME_TYPES
        else "image/png"
    )
    if PILImage is None:
        logger.warning(f"参考图格式不受支持且 Pillow 不可用: mime={mime_type}")
        return None, None

    try:
        with PILImage.open(io.BytesIO(raw_bytes)) as img:
            if target_mime == "image/png":
                save_format = "PNG"
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")
            elif target_mime == "image/jpeg":
                save_format = "JPEG"
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
            elif target_mime == "image/webp":
                save_format = "WEBP"
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")
            else:
                save_format = "PNG"
                target_mime = "image/png"
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")

            buffer = io.BytesIO()
            img.save(buffer, format=save_format)
            buffer.seek(0)
            encoded = base64.b64encode(buffer.read()).decode("utf-8")
            return target_mime, encoded
    except Exception as e:
        logger.warning(f"参考图格式不受支持且转换失败: mime={mime_type}, err={e}")
        return None, None


def coerce_reference_image(
    mime_type: str | None, base64_data: str
) -> tuple[str | None, str | None]:
    """Decode base64 image data and coerce it to a supported reference image."""
    try:
        raw = base64.b64decode(base64_data, validate=False)
    except Exception as e:
        logger.warning(f"base64 解码失败，无法转换为参考图: {e}")
        return None, None
    return coerce_reference_image_bytes(mime_type, raw)


async def normalize_reference_image_input(
    image_input: Any,
    *,
    image_cache_dir: Path | None = None,
    image_input_mode: str = "force_base64",
    session: aiohttp.ClientSession | None = None,
    proxy: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Normalize a reference image input to (mime_type, base64_data).

    Supports data URI, raw/relaxed base64, local paths, file:// and http(s) URLs.
    """
    try:
        if image_input is None:
            return None, None

        image_str = extract_reference_image_source(image_input)
        if not image_str:
            return None, None

        cache_dir = image_cache_dir or REFERENCE_IMAGE_CACHE_DIR
        logger.debug(
            f"规范化参考图输入: len={len(image_str)} "
            f"type={type(image_input)} mode={image_input_mode}"
        )

        if image_str.startswith("data:image/") and ";base64," in image_str:
            header, data = image_str.split(";base64,", 1)
            mime_type = header.replace("data:", "")
            logger.debug(f"检测到参考图 data URI，mime={mime_type}")
            try:
                raw = base64.b64decode(data, validate=False)
            except Exception:
                logger.warning("参考图 data URL base64 解码失败")
                return None, None
            return coerce_reference_image_bytes(mime_type, raw)

        if image_str.startswith("file://"):
            parsed = urllib.parse.urlparse(image_str)
            image_path = Path(parsed.path)
            if image_path.exists() and image_path.is_file():
                suffix = image_path.suffix.lower().lstrip(".") or "png"
                mime_type = f"image/{suffix}"
                logger.debug(f"使用参考图 file:// 路径: {image_path}")
                try:
                    data_bytes = image_path.read_bytes()
                    return coerce_reference_image_bytes(mime_type, data_bytes)
                except Exception as e:
                    logger.warning(f"读取参考图 file:// 路径失败: {e}")
            else:
                logger.warning(f"参考图 file:// 路径不存在: {image_str}")

        if image_str.startswith(("http://", "https://")):
            cleaned_url = image_str.replace("&amp;", "&")
            parsed_url = urllib.parse.urlparse(cleaned_url)
            parsed_host = parsed_url.netloc or ""
            logger.debug(f"下载 http(s) 参考图: {cleaned_url}")

            cached = check_reference_image_cache(cleaned_url, cache_dir)
            if cached:
                mime_guess = f"image/{cached.suffix.lstrip('.') or 'png'}"
                data = _encode_file_to_base64(cached)
                logger.debug(f"参考图命中缓存: {cleaned_url}")
                return mime_guess, data

            is_qq = is_qq_image_host(parsed_host)
            headers = build_reference_image_headers(parsed_host, for_qq=is_qq)

            timeout = aiohttp.ClientTimeout(total=20, connect=10)
            trust_env = not is_qq
            request_proxy = None if is_qq else proxy

            async def fetch_with_session(
                active_session: aiohttp.ClientSession,
            ) -> tuple[str | None, str | None]:
                fallback_reason = None

                try:
                    async with active_session.get(
                        cleaned_url,
                        headers=headers,
                        timeout=timeout,
                        proxy=request_proxy,
                    ) as resp:
                        if resp.status == 200:
                            content_type = resp.headers.get("Content-Type", "image/png")
                            mime_type = (
                                content_type.split(";")[0]
                                if content_type
                                else "image/png"
                            )
                            try:
                                data_bytes = await resp.read()
                            except Exception as e:
                                fallback_reason = f"读取响应体失败: {e}"
                            else:
                                save_reference_image_cache(
                                    cleaned_url, data_bytes, mime_type, cache_dir
                                )
                                return coerce_reference_image_bytes(
                                    mime_type, data_bytes
                                )
                        else:
                            fallback_reason = f"HTTP {resp.status} {resp.reason}"
                except Exception as e:
                    fallback_reason = str(e)
                    logger.warning(
                        f"下载参考图失败: {cleaned_url}，原因: {fallback_reason}"
                    )
                return None, None

            if session is not None and not session.closed:
                return await fetch_with_session(session)

            async with aiohttp.ClientSession(
                timeout=timeout, trust_env=trust_env
            ) as new_session:
                return await fetch_with_session(new_session)

        logger.debug("尝试将参考图输入视为纯 base64")
        try:
            base64.b64decode(image_str, validate=False)
            return coerce_reference_image(None, image_str)
        except binascii.Error:
            cleaned = re.sub(r"[^A-Za-z0-9+/=_-]", "", image_str)
            pad_len = (-len(cleaned)) % 4
            if pad_len:
                cleaned += "=" * pad_len
            try:
                base64.b64decode(cleaned, validate=False)
                return coerce_reference_image(None, cleaned)
            except Exception:
                return None, None

    except Exception as e:
        logger.error(f"规范化参考图输入失败: {e}")
        return None, None
