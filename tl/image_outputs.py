"""Normalize provider image outputs into paths and usable URLs."""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from astrbot.api import logger

from .tl_utils import save_base64_image

_AUTH_QUERY_KEYS = {
    "key",
    "token",
    "sig",
    "signature",
    "expires",
    "x-amz-algorithm",
    "x-amz-credential",
    "x-amz-date",
    "x-amz-expires",
    "x-amz-security-token",
    "x-amz-signature",
    "x-amz-signedheaders",
}
_DATA_URI_RE = re.compile(
    r"^data:(image/[\w.+-]+);base64,(.+)$", re.IGNORECASE | re.DOTALL
)


def _auth_like_url(value: str) -> bool:
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    keys = {
        key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    }
    return bool(keys & _AUTH_QUERY_KEYS)


def _decode_inline(value: str) -> tuple[str, str] | None:
    match = _DATA_URI_RE.match(value.strip())
    if match:
        mime, encoded = match.groups()
        return mime.split("/", 1)[1], encoded
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except Exception:
        return None
    if raw.startswith(b"\x89PNG"):
        return "png", value
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpg", value
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "gif", value
    if raw.startswith(b"RIFF"):
        return "webp", value
    return None


async def normalize_image_outputs(
    image_urls: Iterable[str] | None,
    image_paths: Iterable[str] | None,
) -> tuple[list[str], list[str]]:
    """Save inline image outputs and return paths plus public URLs."""

    paths: list[str] = []
    urls: list[str] = []
    seen_paths: set[str] = set()
    seen_urls: set[str] = set()
    url_candidates = 0
    dropped_auth_urls = 0

    for raw in [*(image_paths or ()), *(image_urls or ())]:
        if not isinstance(raw, str):
            continue
        value = raw.strip()
        if not value:
            continue
        inline = _decode_inline(value)
        if inline is not None:
            image_format, encoded = inline
            saved = await save_base64_image(encoded, image_format)
            if saved and saved not in seen_paths:
                paths.append(saved)
                seen_paths.add(saved)
            elif not saved:
                logger.warning("[图片结果] 内联图片落盘失败，已丢弃")
            continue
        if os.path.isfile(value):
            path = str(Path(value))
            if path not in seen_paths:
                paths.append(path)
                seen_paths.add(path)
            continue
        if value.startswith(("http://", "https://")):
            url_candidates += 1
            if _auth_like_url(value):
                dropped_auth_urls += 1
                continue
            if value not in seen_urls:
                urls.append(value)
                seen_urls.add(value)

    if dropped_auth_urls:
        logger.debug(
            "[图片结果] 过滤非持久化源 URL: 候选 URL=%s 保留 URL=%s 本地路径=%s 丢弃=%s",
            url_candidates,
            len(urls),
            len(paths),
            dropped_auth_urls,
        )

    return paths, urls
