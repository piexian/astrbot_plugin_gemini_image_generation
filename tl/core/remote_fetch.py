"""RemoteFetchService：受 PathPolicy 约束的远端抓取（大小/时间/重定向边界）。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.resolver import ThreadedResolver
from astrbot.api import logger

from .errors import (
    PathPolicyError,
    RemoteFetchBodyTooLargeError,
    RemoteFetchError,
    RemoteFetchRejectedError,
    RemoteFetchTimeoutError,
    RemoteFetchTooManyRedirectsError,
    redact_url,
)
from .path_policy import PathPolicy

# 手动跟随的最大重定向跳数，每跳都用 PathPolicy 重新校验。
MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_READ_CHUNK_BYTES = 64 * 1024
_SENSITIVE_REDIRECT_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "cookie2"}
)


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port)


def _remaining(
    deadline_at: datetime | None, deadline_mono: float | None
) -> float | None:
    """与 tl/core/retry.py 相同的 deadline 剩余时间口径。"""

    if deadline_mono is not None:
        return max(deadline_mono - time.monotonic(), 0.0)
    if deadline_at is None:
        return None
    if deadline_at.tzinfo is None or deadline_at.utcoffset() is None:
        raise ValueError("deadline 必须是带时区的 datetime")
    return max(
        (
            deadline_at.astimezone(timezone.utc) - datetime.now(timezone.utc)
        ).total_seconds(),
        0.0,
    )


class _PolicyResolver(ThreadedResolver):
    """把实际连接使用的 DNS 结果交给 PathPolicy 再次校验。"""

    def __init__(self, policy: PathPolicy) -> None:
        super().__init__()
        self._policy = policy

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ):
        hosts = await super().resolve(host, port, family)
        self._policy.validate_addresses(host, (item["host"] for item in hosts))
        return hosts


class RemoteFetchService:
    """按 deadline、响应体上限与逐跳地址校验抓取远端字节。"""

    def __init__(
        self,
        policy: PathPolicy,
        *,
        max_body_bytes: int = 16 * 1024 * 1024,
        connect_timeout: float = 10.0,
        max_redirects: int = MAX_REDIRECTS,
    ) -> None:
        self.policy = policy
        self.max_body_bytes = int(max_body_bytes)
        self.connect_timeout = float(connect_timeout)
        self.max_redirects = int(max_redirects)

    async def check_url(
        self,
        url: str,
        deadline_at: datetime | None = None,
        *,
        deadline_mono: float | None = None,
    ) -> str:
        """校验 URL；域名解析在线程池执行并受调用时剩余 deadline 约束。"""

        remaining = _remaining(deadline_at, deadline_mono)
        if remaining is not None and remaining <= 0:
            raise RemoteFetchTimeoutError(f"远端抓取 DNS 校验超时: {redact_url(url)}")
        hostname = urlsplit(url).hostname or ""
        try:
            ipaddress.ip_address(hostname.rstrip("."))
        except ValueError:
            validation = asyncio.to_thread(self.policy.validate_url, url)
            try:
                if remaining is None:
                    await validation
                else:
                    await asyncio.wait_for(validation, timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise RemoteFetchTimeoutError(
                    f"远端抓取 DNS 校验超时: {redact_url(url)}"
                ) from exc
        else:
            self.policy.validate_url(url)
        return url

    async def fetch(
        self,
        url: str,
        *,
        deadline_at: datetime | None = None,
        proxy: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        """抓取 URL 字节；deadline_at 截断总时长，proxy 由调用方显式传入。"""

        current = str(url or "").strip()
        request_headers = dict(headers or {})
        remaining = _remaining(deadline_at, None)
        deadline_mono = time.monotonic() + remaining if remaining is not None else None
        connector = aiohttp.TCPConnector(resolver=_PolicyResolver(self.policy))
        async with aiohttp.ClientSession(
            connector=connector, trust_env=False
        ) as session:
            for hop in range(self.max_redirects + 1):
                remaining = _remaining(deadline_at, deadline_mono)
                if remaining is not None and remaining <= 0:
                    logger.error("远端抓取超过 deadline: %s", redact_url(current))
                    raise RemoteFetchTimeoutError(
                        f"远端抓取超过 deadline: {redact_url(current)}"
                    )
                try:
                    await self.check_url(
                        current, deadline_at, deadline_mono=deadline_mono
                    )
                except PathPolicyError as exc:
                    logger.warning(
                        "远端抓取目标被安全策略拒绝: %s", redact_url(current)
                    )
                    raise RemoteFetchRejectedError(
                        f"远端抓取目标被安全策略拒绝: {redact_url(current)}"
                    ) from exc
                remaining = _remaining(deadline_at, deadline_mono)
                if remaining is not None and remaining <= 0:
                    logger.error("远端抓取超过 deadline: %s", redact_url(current))
                    raise RemoteFetchTimeoutError(
                        f"远端抓取超过 deadline: {redact_url(current)}"
                    )
                timeout = aiohttp.ClientTimeout(
                    total=remaining if remaining is not None else None,
                    connect=self.connect_timeout,
                    sock_connect=self.connect_timeout,
                )
                try:
                    async with session.get(
                        current,
                        timeout=timeout,
                        proxy=proxy,
                        headers=request_headers,
                        allow_redirects=False,
                    ) as response:
                        if response.status in _REDIRECT_STATUSES:
                            if hop >= self.max_redirects:
                                logger.error(
                                    "远端抓取重定向超过 %d 跳: %s",
                                    self.max_redirects,
                                    redact_url(current),
                                )
                                raise RemoteFetchTooManyRedirectsError(
                                    f"远端抓取重定向超过 {self.max_redirects} 跳: "
                                    f"{redact_url(current)}"
                                )
                            location = response.headers.get("Location", "")
                            if not location:
                                raise RemoteFetchError(
                                    f"重定向缺少 Location: {redact_url(current)}"
                                )
                            next_url = urljoin(current, location)
                            if _origin(next_url) != _origin(current):
                                request_headers = {
                                    key: value
                                    for key, value in request_headers.items()
                                    if key.lower() not in _SENSITIVE_REDIRECT_HEADERS
                                }
                            current = next_url
                            continue
                        if response.status != 200:
                            logger.error(
                                "远端抓取 HTTP %s: %s",
                                response.status,
                                redact_url(current),
                            )
                            raise RemoteFetchError(
                                f"远端抓取失败: HTTP {response.status} "
                                f"{redact_url(current)}"
                            )
                        return await self._read_body(response, current)
                except PathPolicyError as exc:
                    logger.warning(
                        "远端抓取连接地址被安全策略拒绝: %s", redact_url(current)
                    )
                    raise RemoteFetchRejectedError(
                        f"远端抓取连接地址被安全策略拒绝: {redact_url(current)}"
                    ) from exc
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError as exc:
                    logger.error("远端抓取超时: %s", redact_url(current))
                    raise RemoteFetchTimeoutError(
                        f"远端抓取超时: {redact_url(current)}"
                    ) from exc
                except (aiohttp.ClientError, OSError) as exc:
                    logger.error("远端抓取网络错误: %s (%s)", redact_url(current), exc)
                    raise RemoteFetchError(
                        f"远端抓取网络错误: {redact_url(current)}: {exc}"
                    ) from exc
        # 循环耗尽只可能是末跳 redirect 分支已抛错，这里仅作兜底。
        raise RemoteFetchTooManyRedirectsError(
            f"远端抓取重定向超过 {self.max_redirects} 跳"
        )

    async def _read_body(self, response: aiohttp.ClientResponse, url: str) -> bytes:
        content_length = response.headers.get("Content-Length", "")
        if content_length.isdigit() and int(content_length) > self.max_body_bytes:
            logger.error(
                "远端响应体超过 %d 字节上限: %s",
                self.max_body_bytes,
                redact_url(url),
            )
            raise RemoteFetchBodyTooLargeError(
                f"远端响应体超过 {self.max_body_bytes} 字节上限: {redact_url(url)}"
            )
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.content.iter_chunked(_READ_CHUNK_BYTES):
            size += len(chunk)
            if size > self.max_body_bytes:
                logger.error(
                    "远端响应体超过 %d 字节上限: %s",
                    self.max_body_bytes,
                    redact_url(url),
                )
                raise RemoteFetchBodyTooLargeError(
                    f"远端响应体超过 {self.max_body_bytes} 字节上限: {redact_url(url)}"
                )
            chunks.append(chunk)
        return b"".join(chunks)
