"""供应商接口定义。

用于约束各供应商实现的输入/输出形态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp

from ..api_types import ApiRequestConfig


@dataclass(frozen=True)
class ProviderRequest:
    url: str
    headers: dict[str, str]
    payload: dict[str, Any]


class ApiProvider(Protocol):
    """供应商策略接口。

    每个供应商负责：请求 URL/headers/payload 的构建，以及响应的解析。
    通用能力（图片规范化、下载、落盘等）仍由 `GeminiAPIClient` 提供并被供应商复用。
    """

    name: str

    async def build_request(
        self, *, client: Any, config: ApiRequestConfig
    ) -> ProviderRequest:  # noqa: ANN401
        ...

    async def parse_response(
        self,
        *,
        client: Any,
        response_data: dict[str, Any],
        session: aiohttp.ClientSession,
        api_base: str | None = None,
        http_status: int | None = None,
    ) -> tuple[list[str], list[str], str | None, str | None]:  # noqa: ANN401
        ...
