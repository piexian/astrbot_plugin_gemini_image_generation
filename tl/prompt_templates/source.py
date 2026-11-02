"""在线模板源协议。本模块只定义接口，不发起网络请求。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .models import PromptTemplate


@dataclass
class SearchPage:
    templates: list[PromptTemplate]
    next_cursor: str | None = None


@dataclass
class SourceStatus:
    """服务状态。``rate_limits`` 是各接口的剩余额度或限额，不包含密钥。"""

    version: str
    template_count: int
    rate_limits: dict[str, int]


@dataclass
class CatalogVersion:
    hash: str
    count: int
    updated_at: str
    size: int


class TemplateSourceError(Exception):
    """模板源调用失败。"""


class SourceUnavailable(TemplateSourceError):
    """服务不可达或暂时没有响应。"""


class SourceAuthError(TemplateSourceError):
    """缺少密钥，或密钥无效、权限不足。"""


class SourceRateLimited(TemplateSourceError):
    """触发限流。``retry_after`` 为建议等待的秒数。"""

    def __init__(self, retry_after: float, message: str = "请求过于频繁") -> None:
        self.retry_after = retry_after
        super().__init__(message)


class SourceRejected(TemplateSourceError):
    """服务拒绝了这条内容，``message`` 是可展示的原因。"""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@runtime_checkable
class TemplateSource(Protocol):
    async def search(
        self,
        query: str,
        *,
        tag: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> SearchPage:
        """按关键词和可选标签检索，游标分页。不需要密钥。"""

    async def get_many(self, ids: list[str]) -> list[PromptTemplate]:
        """按 id 批量取模板详情。不需要密钥。"""

    async def status(self) -> SourceStatus:
        """服务版本、模板数量和当前额度。不需要密钥。"""

    async def catalog_version(self) -> CatalogVersion:
        """目录快照的版本。不需要密钥；下载快照时以响应自身的校验值为准。"""

    async def upload(self, template: PromptTemplate) -> PromptTemplate:
        """上传一条本地模板。需要具备写入权限的密钥。"""

    async def update(
        self, template_id: str, template: PromptTemplate, *, etag: str | None = None
    ) -> PromptTemplate:
        """修改自己上传的模板。需要写入密钥；``etag`` 用于避免覆盖他人的更新。"""

    async def delete(self, template_id: str) -> None:
        """删除自己上传的模板。需要写入密钥。"""
