"""Grok2API 兼容模式供应商实现。

该供应商主要解决 grok2api 网关的特殊返回格式：
- 图片 URL 可能是相对路径（如 `/images/xxx`）
- 部分图片 URL 为临时缓存路径（如 `/images/users-` 或 `/temp/image/`），需要立即下载落盘避免过期

实现策略：
- 仅对 grok2api 启用上述规则，保持 OpenAICompatProvider 通用性
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

import aiohttp
from astrbot.api import logger

from .openai_compat import OpenAICompatProvider


class Grok2ApiProvider(OpenAICompatProvider):
    name = "grok2api"

    @staticmethod
    def _is_temp_cache_url(url: str) -> bool:
        return "/images/users-" in url or "/temp/image/" in url

    @staticmethod
    def _origin_from_api_base(api_base: str | None) -> str | None:
        if not api_base:
            return None
        parsed = urllib.parse.urlparse(api_base)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        return None

    def _find_additional_image_urls_in_text(self, text: str) -> list[str]:
        # grok2api：支持 Markdown 中的相对路径图片，例如 ![img](/images/xxx) 或 ![img](images/xxx)
        markdown_relative_pattern = r"!\[[^\]]*\]\((/[^)]+|[^/:)]+/[^)]+)\)"
        matches = re.findall(markdown_relative_pattern, text or "", flags=re.IGNORECASE)

        urls: list[str] = []
        seen: set[str] = set()
        for match in matches:
            candidate = (
                str(match).strip().replace("&amp;", "&").rstrip(").,;").strip("'\"")
            )
            if not candidate:
                continue
            if candidate.startswith(("http://", "https://", "data:")):
                continue
            # 兼容 ![img](images/xxx) 这类不带前导斜杠的相对路径
            if not candidate.startswith("/"):
                candidate = f"/{candidate}"
            if candidate not in seen:
                seen.add(candidate)
                urls.append(candidate)
        return urls

    async def _handle_special_candidate_url(
        self,
        *,
        client: Any,
        session: aiohttp.ClientSession,
        candidate_url: str,
        image_urls: list[str],
        image_paths: list[str],
        api_base: str | None,
        state: dict[str, Any],
    ) -> bool:  # noqa: ANN401
        seen: set[str] = state.setdefault("seen_special_urls", set())
        if candidate_url in seen:
            return True

        is_relative = candidate_url.startswith("/") and not candidate_url.startswith(
            "//"
        )
        origin = self._origin_from_api_base(api_base)

        if is_relative:
            seen.add(candidate_url)
            if not origin:
                logger.warning(
                    "[grok2api] 发现相对路径图片，但未提供 api_base，已跳过: %s",
                    candidate_url,
                )
                return True

            full_url = urllib.parse.urljoin(origin, candidate_url)
            seen.add(full_url)
            logger.debug(
                "[grok2api] 相对路径转换并下载: %s -> %s",
                candidate_url,
                full_url,
            )
            _, image_path = await client._download_image(
                full_url, session, use_cache=False
            )
            if image_path and image_path not in image_paths:
                image_paths.append(image_path)
            return True

        if candidate_url.startswith(
            ("http://", "https://")
        ) and self._is_temp_cache_url(candidate_url):
            seen.add(candidate_url)
            logger.debug("[grok2api] 检测到临时缓存 URL，强制下载: %s", candidate_url)
            _, image_path = await client._download_image(
                candidate_url, session, use_cache=False
            )
            if image_path and image_path not in image_paths:
                image_paths.append(image_path)
            return True

        return False
