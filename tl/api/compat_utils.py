"""已下线供应商（zai / grok2api）沉淀的通用兼容辅助。

供未来适配同类网关（相对路径图片、临时缓存 URL、generation_config 参数约定）复用。
grok2api 原有的"相对路径转换 + 临时缓存强制下载"编排在删除时一并移除，
其规则内核由本模块的纯函数保留。
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

# Markdown 图片中仅匹配相对路径（含 / 且无 http/data 前缀），兼容 ![img](images/xxx) 写法
_MARKDOWN_RELATIVE_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\((/[^)]+|[^/:)]+/[^)]+)\)", flags=re.IGNORECASE
)

# grok2api 网关的临时缓存路径特征，超时即失效，需立即下载落盘
_TEMP_CACHE_MARKERS: tuple[str, ...] = ("/images/users-", "/temp/image/")


def origin_from_api_base(api_base: str | None) -> str | None:
    """从 api_base 推导 origin（scheme://netloc），无 scheme/host 时返回 None。"""
    if not api_base:
        return None
    parsed = urllib.parse.urlparse(api_base)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def is_temp_cache_url(url: str) -> bool:
    """判断是否为网关临时缓存 URL（需要立即下载避免过期）。"""
    return any(marker in url for marker in _TEMP_CACHE_MARKERS)


def find_markdown_relative_image_urls(text: str) -> list[str]:
    """从文本的 Markdown 图片语法中提取相对路径图片 URL，去重并归一化前导斜杠。"""
    urls: list[str] = []
    seen: set[str] = set()
    for match in _MARKDOWN_RELATIVE_IMAGE_RE.findall(text or ""):
        candidate = str(match).strip().replace("&amp;", "&").rstrip(").,;").strip("'\"")
        if not candidate:
            continue
        if candidate.startswith(("http://", "https://", "data:")):
            continue
        if not candidate.startswith("/"):
            candidate = f"/{candidate}"
        if candidate not in seen:
            seen.add(candidate)
            urls.append(candidate)
    return urls


def build_generation_config(
    *,
    resolution: str | None = None,
    aspect_ratio: str | None = None,
    resolution_key: str = "image_size",
    aspect_ratio_key: str = "aspect_ratio",
) -> dict[str, Any]:
    """按 zai 约定构建 generation_config 载荷（顶层分辨率/比例键 + generation_config 嵌套）。"""
    config: dict[str, Any] = {}
    if resolution:
        config[resolution_key] = resolution
    if aspect_ratio:
        config[aspect_ratio_key] = aspect_ratio
    return config


def resolve_relative_url(origin: str | None, relative_url: str) -> str | None:
    """将相对路径图片 URL 基于 origin 补全为绝对 URL；无 origin 时返回 None。"""
    if not origin:
        return None
    return urllib.parse.urljoin(origin, relative_url)
