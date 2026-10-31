"""Sanitize image locations before returning them through plugin LLM tools."""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from .file_uri import file_uri_to_path

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
_DATA_SCHEME_RE = re.compile(r"data:\S+", re.IGNORECASE)
_BARE_BASE64_RE = re.compile(
    r"(?<![A-Za-z0-9+/=_-])([A-Za-z0-9+/=_-]{64,})(?![A-Za-z0-9+/=_-])"
)


def _is_inline_image_data(value: str) -> bool:
    """Detect bare Base64 image data without importing provider utilities."""

    try:
        raw = base64.b64decode(value, validate=True)
    except Exception:
        return False
    return raw.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"RIFF"))


def _public_url(value: str) -> str | None:
    """Return a URL without authentication-like query parameters, if public."""

    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    query_keys = {
        key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    }
    if query_keys & _AUTH_QUERY_KEYS:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def image_locations(
    image_urls: list[str] | None,
    image_paths: list[str] | None,
) -> tuple[list[str], list[str]]:
    """Return local paths first and usable public HTTP(S) URLs second."""

    paths: list[str] = []
    urls: list[str] = []
    seen: set[str] = set()
    for raw in [*(image_paths or []), *(image_urls or [])]:
        if not isinstance(raw, str):
            continue
        value = raw.strip()
        if not value or value.lower().startswith("data:"):
            continue
        if _is_inline_image_data(value):
            continue
        local_path = file_uri_to_path(value) or value
        if os.path.isfile(local_path):
            if local_path not in seen:
                paths.append(local_path)
                seen.add(local_path)
            continue
        public_url = _public_url(value)
        if public_url and public_url not in seen:
            urls.append(public_url)
            seen.add(public_url)
    return paths, urls


def image_location_list(
    image_urls: list[str] | None,
    image_paths: list[str] | None,
) -> list[str]:
    """Return safe image locations in path-first order."""

    paths, urls = image_locations(image_urls, image_paths)
    return [*paths, *urls]


def sanitize_text(value: str | None) -> str | None:
    """Remove inline image data before text enters an LLM tool result."""

    if not isinstance(value, str):
        return value
    cleaned = _DATA_SCHEME_RE.sub("[inline image data omitted]", value)

    def replace_bare_base64(match: re.Match[str]) -> str:
        token = match.group(1)
        return "[inline image data omitted]" if _is_inline_image_data(token) else token

    return _BARE_BASE64_RE.sub(replace_bare_base64, cleaned)


def sanitize_task_result(value: Any) -> Any:
    """Sanitize nested background-task payloads sent back to the LLM."""

    if isinstance(value, Mapping):
        result = {key: sanitize_task_result(item) for key, item in value.items()}
        if "image_urls" in value or "image_paths" in value:
            paths, urls = image_locations(
                value.get("image_urls"), value.get("image_paths")
            )
            result["image_paths"] = paths
            result["image_urls"] = urls
        for key in ("text_content", "result", "message", "error"):
            if isinstance(value.get(key), str):
                result[key] = sanitize_text(value[key])
        return result
    if isinstance(value, list):
        return [sanitize_task_result(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_task_result(item) for item in value)
    return value
