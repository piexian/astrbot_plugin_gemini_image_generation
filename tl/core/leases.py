"""Job 生命周期资源 lease。

lease 的释放是幂等的，并且 ``LeaseSet`` 会在异常和取消路径中尝试释放
全部资源。这只是领域生命周期组件；具体 quota、限流器和文件存储由后续
阶段通过协议注入。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from .errors import LeaseError


class LeaseKind(str, Enum):
    QUOTA = "quota"
    LIMITER = "limiter"
    REFERENCE = "reference"
    ARTIFACT = "artifact"
    DELIVERY = "delivery"


ReleaseCallback = Callable[[], Any | Awaitable[Any]]


@dataclass(slots=True)
class Lease:
    token: str
    kind: LeaseKind | str
    job_id: str
    release_callback: ReleaseCallback | None = None
    expires_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    released: bool = False

    async def release(self) -> None:
        """释放一次资源；重复调用不会重复归还 quota 或删除文件。"""

        if self.released:
            return
        self.released = True
        callback = self.release_callback
        if callback is None:
            return
        try:
            result = callback()
            if inspect.isawaitable(result):
                await result
        except BaseException as exc:
            # 标记已释放后再抛出，调用方可以记录失败但不会二次归还。
            raise LeaseError(
                f"释放 {self.kind} lease 失败",
                details={"job_id": self.job_id, "kind": str(self.kind)},
            ) from exc


class LeaseSet:
    """Job 内所有 lease 的幂等聚合器。"""

    def __init__(self, leases: list[Lease] | None = None) -> None:
        self._leases: list[Lease] = list(leases or [])
        self._released = False

    def add(self, lease: Lease) -> Lease:
        if self._released:
            raise LeaseError("lease set 已释放")
        self._leases.append(lease)
        return lease

    @property
    def leases(self) -> tuple[Lease, ...]:
        return tuple(self._leases)

    async def release_all(self) -> None:
        if self._released:
            return
        self._released = True
        errors: list[BaseException] = []
        # 反向释放，保持文件/artifact 依赖其输入 lease 的惯例。
        for lease in reversed(self._leases):
            try:
                await lease.release()
            except BaseException as exc:  # cleanup must continue for all leases
                errors.append(exc)
        if errors:
            raise LeaseError(
                f"{len(errors)} 个 lease 释放失败",
                details={"errors": [type(error).__name__ for error in errors]},
            ) from errors[0]

    async def __aenter__(self) -> LeaseSet:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        await self.release_all()
        return False
