"""Bounded, independent model discovery; never uses generation clients or quotas."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import aiohttp

from .provider_metadata import get_provider_spec, iter_provider_specs

_MAX_BYTES = 2 * 1024 * 1024
_MAX_MODELS = 2000
_MAX_PAGES = 5
_TIMEOUT = 20
_UNFILTERED_WARNING = "目录可能包含非生图模型，请按供应商能力选择；仍可手动填写模型。"
_CREDENTIAL_PATTERN = re.compile(
    r"(?:\bBearer\s+|\bBasic\s+|\bsk-[\w-]{8,}|\bAIza[\w-]{12,}"
    r"|(?:api[_-]?key|(?:access[_-]?)?token|secret|authorization|password)[\"']?\s*[:=]"
    r"|https?://[^\s/]+@)",
    re.IGNORECASE,
)
_SECRET_QUERY = re.compile(r"key|token|secret|password|auth|signature", re.IGNORECASE)


class ModelCatalogError(Exception):
    """Only safe, local messages cross the Studio boundary."""

    def __init__(self, message: str, *, reason: str, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.reason = reason
        self.status_code = status_code


@dataclass(frozen=True)
class CatalogRequest:
    url: str = field(repr=False)
    proxy: str | None = field(repr=False)
    protocol: str
    headers: dict[str, str] = field(repr=False)
    params: dict[str, str] = field(repr=False)


def _invalid(message: str) -> ModelCatalogError:
    return ModelCatalogError(message, reason="invalid_request", status_code=400)


def _parse_url(value: str, *, proxy: bool = False):
    try:
        if (
            not isinstance(value, str)
            or not value
            or any(char.isspace() or ord(char) < 32 for char in value)
        ):
            raise ValueError
        parsed = urlsplit(value)
        allowed = {"http", "https"}
        if proxy:
            allowed |= {"socks4", "socks4a", "socks5", "socks5h"}
        if parsed.scheme not in allowed or not parsed.hostname:
            raise ValueError
        _ = parsed.port
    except (ValueError, TypeError):
        raise _invalid(
            "代理地址格式无效。" if proxy else "API 地址须为有效 HTTP(S) 地址。"
        ) from None
    if parsed.fragment:
        raise _invalid("目录查询不支持地址片段（#），请明确移除后重试。")
    if not proxy and (parsed.username is not None or parsed.password is not None):
        raise _invalid("目录查询不支持 API 地址中的用户名认证，请使用 API Key。")
    if proxy and (parsed.query or parsed.path not in {"", "/"}):
        raise _invalid("代理地址不能包含路径或查询参数。")
    return parsed


def make_catalog_request(
    api_type: str, api_base: str, api_key: str, proxy: str | None = None
) -> CatalogRequest:
    """Keep gateway prefixes/query parameters; explicitly reject unsupported auth."""
    spec = get_provider_spec(api_type)
    protocol = spec.model_catalog_kind if spec else None
    if protocol is None:
        raise ModelCatalogError(
            "此供应商暂未接入模型目录，请手动填写模型。",
            reason="unsupported",
            status_code=400,
        )
    if (
        not isinstance(api_key, str)
        or not api_key.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in api_key)
    ):
        raise _invalid("请先填写有效 API Key。")
    parsed = _parse_url(api_base.strip() if isinstance(api_base, str) else api_base)
    if proxy:
        _parse_url(proxy, proxy=True)
    if protocol == "ark":
        # 方舟 OpenAI 兼容层挂在 /api/v3 下而非 /v1；目录只认基址，避免猜路径。
        if parsed.path.strip("/"):
            raise _invalid(
                "火山方舟模型目录只支持填写基址，请移除 API 地址中的路径后重试。"
            )
        path = "/api/v3/models"
    else:
        path = parsed.path.rstrip("/")
        # Accept documented complete generation endpoints without losing gateway prefixes.
        path = re.sub(
            r"/models/[^/]+:(?:generateContent|streamGenerateContent)$", "", path
        )
        for suffix in (
            "/chat/completions",
            "/images/generations",
            "/images/edits",
            "/image_generation",
            "/interactions",
            "/image-generation-models",
            "/models",
        ):
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break
        if not path.endswith(("/v1", "/v1beta")):
            path += "/v1beta" if protocol == "google" else "/v1"
        path += "/image-generation-models" if protocol == "xai" else "/models"
    params: dict[str, str] = {}
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    reserved = (
        {"pageSize", "pageToken"}
        if protocol == "google"
        else {"type"}
        if protocol == "siliconflow"
        else set()
    )
    for key in reserved:
        if sum(name == key for name, _ in query_items) > 1:
            raise _invalid("目录地址包含重复的分页或类型参数，请明确指定唯一值。")
    query = dict(query_items)
    if protocol == "google":
        if "pageSize" in query:
            try:
                if not 1 <= int(query["pageSize"]) <= 1000:
                    raise ValueError
            except ValueError:
                raise _invalid("Google 目录 pageSize 必须为 1 至 1000。") from None
        else:
            params["pageSize"] = "1000"
        headers = {"x-goog-api-key": api_key.strip()}
    else:
        headers = {"Authorization": f"Bearer {api_key.strip()}"}
        if protocol == "siliconflow":
            if "type" in query and query["type"] != "image":
                raise _invalid("SiliconFlow 生图目录的 type 查询参数须为 image。")
            if "type" not in query:
                params["type"] = "image"
    return CatalogRequest(
        url=urlunsplit(parsed._replace(path=path)),
        proxy=proxy or None,
        protocol=protocol,
        headers=headers,
        params=params,
    )


def safe_target(request: CatalogRequest) -> str:
    """Only show the destination origin, never paths, userinfo or query values."""
    try:
        parsed = urlsplit(request.url)
        host = parsed.hostname or ""
        if (
            not host
            or "%" in host
            or _CREDENTIAL_PATTERN.search(host)
            or any(secret.lower() in host.lower() for secret in _secrets(request))
        ):
            return "自定义目标"
        if ":" in host:
            host = f"[{host}]"
        return f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")
    except ValueError:
        return "自定义目标"


def catalog_capabilities() -> dict[str, dict[str, Any]]:
    return {
        spec.api_type: {
            "supported": spec.model_catalog_kind is not None,
            "message": (
                "按当前连接草稿查询模型，不保存配置，也不消耗生成额度。"
                if spec.model_catalog_kind is not None
                else "此供应商暂未接入模型目录，可手动填写；不代表上游没有目录接口。"
            ),
        }
        for spec in iter_provider_specs()
    }


def _secrets(request: CatalogRequest) -> tuple[str, ...]:
    values = []
    for name, value in request.headers.items():
        if name.lower() == "authorization":
            values.append(value.split(" ", 1)[-1])
        elif _SECRET_QUERY.search(name):
            values.append(value)
    values.extend(
        value for name, value in request.params.items() if _SECRET_QUERY.search(name)
    )
    for url in (request.url, request.proxy):
        if not url:
            continue
        parsed = urlsplit(url)
        values.extend(unquote(v) for v in (parsed.username, parsed.password) if v)
        values.extend(
            value
            for name, value in parse_qsl(parsed.query)
            if _SECRET_QUERY.search(name) and value
        )
    return tuple(value for value in values if value)


def _format_error() -> ModelCatalogError:
    return ModelCatalogError(
        "供应商返回的模型目录格式无效。", reason="invalid_response"
    )


def _safe_text(value: Any, limit: int, secrets: tuple[str, ...]) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise _format_error()
    value = value.strip()
    if any(
        ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
        for char in value
    ):
        raise _format_error()
    decoded = unquote(value)
    if _CREDENTIAL_PATTERN.search(decoded) or any(
        secret in value or secret in decoded for secret in secrets
    ):
        return None
    return value


class _ModelResults:
    def __init__(self, secrets: tuple[str, ...] = ()):
        self.secrets = secrets
        self.models: dict[str, dict[str, str]] = {}
        self.truncated = False
        self.filtered = False

    def add(self, model_id: Any, label: Any = None) -> None:
        model_id = _safe_text(model_id, 512, self.secrets)
        label = _safe_text(label, 512, self.secrets) if label is not None else model_id
        if model_id is None or label is None:
            self.filtered = True
            return
        if model_id in self.models:
            return
        if len(self.models) >= _MAX_MODELS:
            self.truncated = True
            return
        self.models[model_id] = {"id": model_id, "label": label}

    def result(self, warning: str = "") -> dict[str, Any]:
        warnings = [warning] if warning else []
        if self.filtered:
            warnings.append("已过滤疑似包含凭据的目录项。")
        if self.truncated:
            warnings.append("目录达到查询上限，结果已截断；仍可手动填写模型。")
        return {
            "models": list(self.models.values()),
            "warning": " ".join(warnings),
            "truncated": self.truncated,
        }


class ModelCatalogService:
    """Own workers/sessions only; queue time is part of the total deadline."""

    def __init__(self):
        self._semaphore = asyncio.Semaphore(2)
        self._workers: set[asyncio.Task] = set()
        self._closed = False

    async def _run(self, operation: Callable[[], Awaitable[dict]]) -> dict[str, Any]:
        if self._closed:
            raise ModelCatalogError(
                "模型目录服务已关闭。", reason="closed", status_code=503
            )
        if len(self._workers) >= 4:
            raise ModelCatalogError(
                "模型目录请求繁忙，请稍后重试。", reason="busy", status_code=429
            )
        task = asyncio.create_task(self._worker(operation))
        self._workers.add(task)
        try:
            return await task
        finally:
            self._workers.discard(task)

    async def _worker(self, operation: Callable[[], Awaitable[dict]]) -> dict[str, Any]:
        try:
            async with asyncio.timeout(_TIMEOUT), self._semaphore:
                return await operation()
        except TimeoutError:
            raise ModelCatalogError(
                "模型目录查询超时，请稍后重试。", reason="timeout", status_code=504
            ) from None
        except ModelCatalogError:
            raise
        except Exception:
            # Upstream exceptions often embed full URLs, auth headers or response text.
            raise ModelCatalogError(
                "模型目录查询失败，请检查连接配置或供应商状态。",
                reason="connection_error",
            ) from None

    async def fetch(self, request: CatalogRequest) -> dict[str, Any]:
        return await self._run(lambda: self._fetch(request))

    async def fetch_vision(self, provider: Any) -> dict[str, Any]:
        return await self._run(lambda: self._fetch_vision(provider))

    async def close(self) -> None:
        self._closed = True
        tasks = tuple(self._workers)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_vision(self, provider: Any) -> dict[str, Any]:
        get_models = getattr(provider, "get_models", None)
        if not callable(get_models):
            raise ModelCatalogError(
                "此视觉提供商未提供模型目录，请手动填写。", reason="unsupported"
            )
        # The provider belongs to AstrBot; never terminate or otherwise reconfigure it.
        models = await get_models()
        if not isinstance(models, list):
            raise _format_error()
        results = _ModelResults()
        byte_count = 0
        for model in models:
            if not isinstance(model, str):
                raise _format_error()
            byte_count += len(model.encode("utf-8"))
            if byte_count > _MAX_BYTES:
                raise ModelCatalogError(
                    "模型目录响应超过 2 MiB 上限。", reason="response_too_large"
                )
            results.add(model)
            if results.truncated:
                break
        return results.result(
            "视觉模型目录不保证每个模型均支持图像理解，请按实际能力选择。"
        )

    async def _fetch(self, request: CatalogRequest) -> dict[str, Any]:
        if request.protocol not in {"google", "openai", "xai", "siliconflow", "ark"}:
            raise _invalid("不支持的模型目录协议。")
        _parse_url(request.url)
        connector = None
        proxy = request.proxy
        if proxy:
            parsed_proxy = _parse_url(proxy, proxy=True)
            if parsed_proxy.scheme.startswith("socks"):
                try:
                    from aiohttp_socks import ProxyConnector
                except ImportError:
                    raise ModelCatalogError(
                        "SOCKS 代理需要 aiohttp_socks，当前环境不可用；未尝试直连。",
                        reason="proxy_unavailable",
                    ) from None
                scheme = parsed_proxy.scheme.removesuffix("h")
                if scheme == "socks4a":
                    scheme = "socks4"
                connector = ProxyConnector.from_url(
                    urlunsplit(parsed_proxy._replace(scheme=scheme)), rdns=True
                )
                proxy = None
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
            trust_env=False,
        ) as session:
            return await self._fetch_pages(session, request, proxy)

    async def _fetch_pages(
        self, session: aiohttp.ClientSession, request: CatalogRequest, proxy: str | None
    ) -> dict[str, Any]:
        results = _ModelResults(_secrets(request))
        url = request.url
        params = dict(request.params)
        byte_count = 0
        page = 0
        seen_tokens = {
            value
            for name, value in parse_qsl(urlsplit(url).query)
            if name == "pageToken"
        }
        if params.get("pageToken"):
            seen_tokens.add(params["pageToken"])
        fallback = False
        while True:
            async with session.get(
                url,
                headers=request.headers,
                params=params,
                proxy=proxy,
                allow_redirects=False,
            ) as response:
                if (
                    response.status == 404
                    and request.protocol == "xai"
                    and not fallback
                ):
                    parsed = urlsplit(url)
                    url = urlunsplit(
                        parsed._replace(path=parsed.path.rsplit("/", 1)[0] + "/models")
                    )
                    fallback = True
                    continue
                self._check_status(response.status)
                if (
                    response.content_length is not None
                    and response.content_length > _MAX_BYTES - byte_count
                ):
                    raise ModelCatalogError(
                        "模型目录响应超过 2 MiB 上限。", reason="response_too_large"
                    )
                body = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    byte_count += len(chunk)
                    if byte_count > _MAX_BYTES:
                        raise ModelCatalogError(
                            "模型目录响应超过 2 MiB 上限。", reason="response_too_large"
                        )
                    body.extend(chunk)
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError, RecursionError):
                raise _format_error() from None
            page += 1
            field_name = (
                "models"
                if request.protocol in {"google", "xai"} and not fallback
                else "data"
            )
            if not isinstance(payload, dict) or not isinstance(
                payload.get(field_name), list
            ):
                raise _format_error()
            for item in payload[field_name]:
                if not isinstance(item, dict):
                    raise _format_error()
                if request.protocol == "google":
                    methods = item.get("supportedGenerationMethods")
                    if "supportedGenerationMethods" in item:
                        if not isinstance(methods, list) or any(
                            not isinstance(method, str) for method in methods
                        ):
                            raise _format_error()
                        if "generateContent" not in methods:
                            continue
                    name = item.get("name")
                    if not isinstance(name, str):
                        raise _format_error()
                    results.add(name.removeprefix("models/"), item.get("displayName"))
                else:
                    results.add(item.get("id"))
                if results.truncated:
                    break
            if request.protocol != "google":
                break
            token = payload.get("nextPageToken", "")
            if not isinstance(token, str) or len(token) > 8192:
                raise _format_error()
            if not token:
                break
            if token in seen_tokens:
                raise ModelCatalogError(
                    "供应商返回了重复分页标记，已停止目录查询。",
                    reason="repeated_page_token",
                )
            seen_tokens.add(token)
            if (
                page >= _MAX_PAGES
                or len(results.models) >= _MAX_MODELS
                or results.truncated
            ):
                results.truncated = True
                break
            # Only the documented page token is followed, never a response-provided URL.
            parsed = urlsplit(url)
            url = urlunsplit(
                parsed._replace(
                    query=urlencode(
                        [
                            (key, value)
                            for key, value in parse_qsl(
                                parsed.query, keep_blank_values=True
                            )
                            if key != "pageToken"
                        ]
                    )
                )
            )
            params["pageToken"] = token
        warning = (
            _UNFILTERED_WARNING
            if request.protocol in {"openai", "ark"} or fallback
            else ""
        )
        if fallback:
            warning = "专用生图目录不可用，已回退到通用模型目录。 " + warning
        return results.result(warning)

    @staticmethod
    def _check_status(status: int) -> None:
        if status == 200:
            return
        messages = {
            401: ("供应商认证失败，请检查 API Key。", "upstream_unauthorized"),
            403: ("供应商拒绝访问模型目录，请检查权限。", "upstream_forbidden"),
            404: ("供应商模型目录地址不存在，请检查 API 地址。", "upstream_not_found"),
            429: ("供应商模型目录请求过于频繁，请稍后重试。", "upstream_rate_limited"),
        }
        message, reason = messages.get(
            status, ("供应商模型目录返回异常状态。", "upstream_error")
        )
        if 300 <= status < 400:
            message, reason = (
                "模型目录禁止自动重定向，请检查 API 地址。",
                "redirect_blocked",
            )
        raise ModelCatalogError(message, reason=reason)
