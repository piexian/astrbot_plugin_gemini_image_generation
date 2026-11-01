"""image-prompt-hub 在线模板源客户端。

只实现 ``TemplateSource`` 协议与目录快照下载；不做缓存、不做调度。
请求经出站策略检查，只允许 https；密钥只进 Authorization 头，日志脱敏。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import aiohttp
from astrbot.api import logger

from .core.errors import PathPolicyError
from .core.path_policy import check_remote_url
from .prompt_templates import (
    CatalogVersion,
    PromptTemplate,
    RemoteOrigin,
    SearchPage,
    SourceAuthError,
    SourceRateLimited,
    SourceRejected,
    SourceStatus,
    SourceUnavailable,
    TemplateSourceError,
    TemplateValidationError,
    validate_template_data,
)

DEFAULT_BASE_URL = "https://image-hub.alxy.app"
_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)
_MAX_BYTES = 32 * 1024 * 1024
_BATCH_IDS_MAX = 50
_SEARCH_LIMIT_MAX = 50


def _join(base_url: str, path: str, query: dict[str, Any] | None = None) -> str:
    base = str(base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise SourceUnavailable("在线模板服务地址必须是 https 链接")
    merged = urlunsplit(
        parts._replace(path=f"{parts.path.rstrip('/')}{path}", query="", fragment="")
    )
    if query:
        merged = f"{merged}?{urlencode({k: v for k, v in query.items() if v not in (None, '')})}"
    return merged


def _header(headers: Any, name: str) -> str:
    """按 HTTP 语义取响应头：头名大小写不敏感。

    aiohttp 返回的是大小写不敏感容器，转成 dict 后就丢了这一特性，
    直接用 headers.get("ETag") 会漏掉服务端发来的 "Etag"。
    """

    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    if getter is not None:
        value = getter(name)
        if value is not None:
            return str(value)
    wanted = name.lower()
    try:
        items = headers.items()
    except AttributeError:
        return ""
    for key, value in items:
        if str(key).lower() == wanted:
            return str(value)
    return ""


def _record(data: Any, *, source: str = "hub") -> PromptTemplate:
    """把在线响应转成模板对象，带服务端 id 作为来源标记。"""

    if not isinstance(data, dict):
        raise SourceUnavailable("在线模板返回格式异常")
    payload = dict(data)
    remote_id = str(payload.get("id") or "").strip()
    if not remote_id:
        raise SourceUnavailable("在线模板缺少 id")
    payload["origin"] = "remote"
    try:
        template = validate_template_data(payload, origin="remote")
    except TemplateValidationError as exc:
        raise SourceUnavailable("在线模板字段不受支持") from exc
    template.remote = RemoteOrigin(source=source or "hub", remote_id=remote_id)
    return template


def _source_name(base_url: str) -> str:
    host = urlsplit(str(base_url or DEFAULT_BASE_URL)).hostname or "hub"
    return host.lower()


class HubTemplateSource:
    """image-prompt-hub 的 HTTP 客户端。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        *,
        session_factory: Callable[[], aiohttp.ClientSession] | None = None,
    ) -> None:
        self.base_url = str(base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        self.api_key = str(api_key or "").strip() or None
        self.source_name = _source_name(self.base_url)
        self._session_factory = session_factory or (
            lambda: aiohttp.ClientSession(timeout=_TIMEOUT, trust_env=False)
        )

    def _headers(self, *, auth: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "astrbot-gemini-image/1.0",
        }
        if auth and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def close(self) -> None:
        """保留协议要求的关闭入口；连接已按请求创建，无需关闭状态。"""

    async def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        auth: bool = False,
        json_body: Any = None,
        etag: str | None = None,
    ) -> tuple[int, dict[str, str], Any]:
        url = _join(self.base_url, path, query)
        try:
            check_remote_url(url)
        except PathPolicyError as exc:
            raise SourceUnavailable(f"在线模板服务地址不可访问：{exc}") from exc
        headers = self._headers(auth=auth)
        if etag:
            headers["If-None-Match"] = etag
        # 每次请求新建连接：同步每 6 小时一次、只发两个请求，复用连接没有收益，
        # 却会把 session 绑在已关闭的 loop 上（缓存同步走 asyncio.run）。
        async with self._session_factory() as session:
            try:
                async with session.request(
                    method, url, headers=headers, json=json_body, allow_redirects=False
                ) as response:
                    if response.status == 304:
                        return 304, dict(response.headers), None
                    if response.status == 429:
                        retry = response.headers.get("Retry-After", "")
                        raise SourceRateLimited(_retry_after(retry))
                    if response.status in (401, 403):
                        raise SourceAuthError("在线模板密钥无效或权限不足")
                    if response.status >= 500:
                        raise SourceUnavailable("在线模板服务暂时不可用")
                    raw = await response.read()
                    if len(raw) > _MAX_BYTES:
                        raise SourceUnavailable("在线模板响应过大")
                    payload = _decode(raw)
                    if response.status >= 400:
                        raise SourceRejected(
                            _message(payload) or "在线模板服务拒绝了本次请求"
                        )
                    return response.status, dict(response.headers), payload
            except (
                SourceAuthError,
                SourceRateLimited,
                SourceRejected,
                SourceUnavailable,
            ):
                raise
            except asyncio.TimeoutError as exc:
                raise SourceUnavailable("在线模板服务响应超时") from exc
            except aiohttp.ClientError as exc:
                raise SourceUnavailable(
                    f"在线模板服务连接失败：{type(exc).__name__}"
                ) from exc

    async def search(
        self,
        query: str,
        *,
        tag: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> SearchPage:
        size = max(1, min(int(limit or 20), _SEARCH_LIMIT_MAX))
        _status, _headers, payload = await self._request(
            "GET",
            "/v1/templates",
            query={
                "q": str(query or "").strip() or None,
                "tag": str(tag or "").strip() or None,
                "cursor": str(cursor or "").strip() or None,
                "limit": size,
            },
        )
        items = payload.get("templates") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise SourceUnavailable("在线检索返回格式异常")
        next_cursor = payload.get("next_cursor") or payload.get("cursor")
        return SearchPage(
            templates=[_record(item, source=self.source_name) for item in items],
            next_cursor=str(next_cursor) if next_cursor else None,
        )

    async def get_many(self, ids: list[str]) -> list[PromptTemplate]:
        wanted = [str(item).strip() for item in (ids or []) if str(item).strip()]
        if not wanted:
            return []
        if len(wanted) > _BATCH_IDS_MAX:
            raise SourceRejected(f"一次最多取 {_BATCH_IDS_MAX} 条在线模板")
        _status, _headers, payload = await self._request(
            "GET", "/v1/templates/batch", query={"ids": ",".join(wanted)}
        )
        items = payload.get("templates") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise SourceUnavailable("在线模板详情返回格式异常")
        return [_record(item, source=self.source_name) for item in items]

    async def get(self, template_id: str) -> PromptTemplate:
        found = await self.get_many([template_id])
        if not found:
            raise SourceRejected("在线模板不存在")
        return found[0]

    async def status(self) -> SourceStatus:
        _status, _headers, payload = await self._request("GET", "/v1/status")
        if not isinstance(payload, dict):
            raise SourceUnavailable("在线服务状态返回格式异常")
        limits = payload.get("rate_limits")
        return SourceStatus(
            version=str(payload.get("version") or ""),
            template_count=int(payload.get("template_count") or 0),
            rate_limits=dict(limits) if isinstance(limits, dict) else {},
        )

    async def catalog_version(self) -> CatalogVersion:
        _status, _headers, payload = await self._request("GET", "/v1/catalog/version")
        if not isinstance(payload, dict) or not payload.get("hash"):
            raise SourceUnavailable("目录版本返回格式异常")
        return CatalogVersion(
            hash=str(payload.get("hash")),
            count=int(payload.get("count") or 0),
            updated_at=str(payload.get("updated_at") or ""),
            size=int(payload.get("size") or 0),
        )

    async def download_catalog(self, etag: str | None = None) -> tuple[str, Any] | None:
        """下载目录快照。返回 (etag, payload)；未变化时返回 None。"""

        status, headers, payload = await self._request("GET", "/v1/catalog", etag=etag)
        if status == 304:
            return None
        if not isinstance(payload, dict) or not isinstance(
            payload.get("templates"), list
        ):
            raise SourceUnavailable("目录快照返回格式异常")
        tag = str(_header(headers, "ETag")).strip()
        return tag, payload

    async def request_key(self, email: str) -> None:
        address = str(email or "").strip()
        if not address:
            raise SourceRejected("请先填写邮箱")
        try:
            await self._request(
                "POST", "/v1/keys/request", json_body={"email": address}
            )
        except SourceRejected:
            raise
        except TemplateSourceError:
            raise
        except Exception as exc:
            raise SourceUnavailable(f"申请密钥失败：{type(exc).__name__}") from exc

    async def key_status(self) -> dict[str, Any]:
        _status, _headers, payload = await self._request("GET", "/v1/key", auth=True)
        return payload if isinstance(payload, dict) else {}

    async def revoke_key(self) -> None:
        await self._request("DELETE", "/v1/key", auth=True)

    async def upload(self, template: PromptTemplate) -> PromptTemplate:
        _status, _headers, payload = await self._request(
            "POST", "/v1/templates", auth=True, json_body=_upload_body(template)
        )
        return _record(payload, source=self.source_name)

    async def update(
        self, template_id: str, template: PromptTemplate, *, etag: str | None = None
    ) -> PromptTemplate:
        identifier = str(template_id or "").strip()
        if not identifier:
            raise SourceRejected("缺少在线模板 id")
        _status, _headers, payload = await self._request(
            "PUT",
            f"/v1/templates/{identifier}",
            auth=True,
            json_body=_upload_body(template),
            etag=etag,
        )
        return _record(payload, source=self.source_name)

    async def delete(self, template_id: str) -> None:
        identifier = str(template_id or "").strip()
        if not identifier:
            raise SourceRejected("缺少在线模板 id")
        await self._request("DELETE", f"/v1/templates/{identifier}", auth=True)

    async def report(self, template_id: str, reason: str) -> None:
        identifier = str(template_id or "").strip()
        if not identifier or not str(reason or "").strip():
            raise SourceRejected("举报需要模板 id 和原因")
        await self._request(
            "POST",
            f"/v1/templates/{identifier}/report",
            json_body={"reason": str(reason).strip()[:500]},
        )


def _upload_body(template: PromptTemplate) -> dict[str, Any]:
    return {
        "name": template.name,
        "content": template.content,
        "ai_description": template.ai_description,
        "defaults": dict(template.defaults),
        "reference": template.reference,
        "tags": list(template.tags),
        "author": template.author,
        "source_url": template.source_url,
    }


def _retry_after(raw: str) -> float:
    try:
        return max(float(str(raw).strip()), 0.0)
    except (TypeError, ValueError):
        return 60.0


def _message(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("message", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _decode(raw: bytes) -> Any:
    import json

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SourceUnavailable("在线模板响应不是合法 JSON") from exc


def source_for(base_url: str, api_key: str | None = None) -> HubTemplateSource:
    """按插件配置构造在线源；地址为空时用官方默认地址。"""

    return HubTemplateSource(
        base_url=str(base_url or "").strip() or DEFAULT_BASE_URL,
        api_key=api_key,
    )


async def close_quietly(source: HubTemplateSource | None) -> None:
    if source is None:
        return
    with contextlib.suppress(Exception):
        await source.close()
    logger.debug("[模板广场] 在线源已关闭")
