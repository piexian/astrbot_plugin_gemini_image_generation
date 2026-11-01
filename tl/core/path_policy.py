"""参考图来源校验：本地根目录约束与远端 URL 基本形态。

远端 URL 不按地址过滤：抓取后一律按图片格式与像素上限校验，
拿不到、也传不到供应商的内容不构成暴露面。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from .errors import PathPolicyError, redact_url

_REMOTE_URL_SCHEMES = frozenset({"http", "https"})


def check_remote_url(url: str) -> str:
    """校验 URL 可作为远端抓取目标，原样返回。"""

    text = str(url or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in _REMOTE_URL_SCHEMES or not parsed.hostname:
        raise PathPolicyError(f"仅允许 http/https URL: {redact_url(text)}")
    if parsed.username is not None or parsed.password is not None:
        raise PathPolicyError(f"出站 URL 不允许携带用户信息: {redact_url(text)}")
    return text


class PathPolicy:
    """校验本地路径落在允许根内。"""

    def __init__(self, allowed_local_roots: tuple[Path, ...]) -> None:
        self.allowed_local_roots = tuple(
            Path(root).expanduser().resolve() for root in allowed_local_roots
        )

    def validate_local_path(self, path: str | Path) -> Path:
        """校验 file:// URI 或裸绝对路径，返回 resolve 后的路径。"""

        text = str(path or "").strip()
        if not text:
            raise PathPolicyError("本地路径为空")
        if text.lower().startswith("file:"):
            candidate = self._path_from_file_uri(text)
        else:
            candidate = Path(text)
        if not candidate.is_absolute():
            raise PathPolicyError(f"本地路径必须是绝对路径: {text}")
        # resolve 展开符号链接与 ..，之后再次校验以阻止逃逸。
        resolved = candidate.resolve()
        self._ensure_within_roots(resolved)
        return resolved

    def _path_from_file_uri(self, text: str) -> Path:
        parsed = urlsplit(text)
        if parsed.scheme.lower() != "file":
            raise PathPolicyError(f"仅支持 file:// URI: {text}")
        if parsed.query or parsed.fragment:
            raise PathPolicyError(f"file:// URI 不允许携带 query/fragment: {text}")
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        if netloc not in ("", "localhost"):
            raise PathPolicyError(f"file:// URI 不允许远端 host: {text}")
        return Path(url2pathname(parsed.path))

    def _ensure_within_roots(self, resolved: Path) -> None:
        for root in self.allowed_local_roots:
            if resolved.is_relative_to(root):
                return
        raise PathPolicyError(f"本地路径越出允许目录: {resolved}")
