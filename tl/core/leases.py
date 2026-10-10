"""Job 生命周期资源 lease。

lease 的释放是幂等的，并且 ``LeaseSet`` 会在异常和取消路径中尝试释放
全部资源。这只是领域生命周期组件；具体 quota、限流器和文件存储由后续
阶段通过协议注入。
"""

from __future__ import annotations

import asyncio
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


class LeaseStatus(str, Enum):
    ACTIVE = "active"
    RELEASING = "releasing"
    RELEASED = "released"
    RELEASE_FAILED = "release_failed"


ReleaseCallback = Callable[[], Any | Awaitable[Any]]


@dataclass(slots=True)
class Lease:
    token: str
    kind: LeaseKind | str
    job_id: str
    release_callback: ReleaseCallback | None = None
    expires_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    status: LeaseStatus | str = LeaseStatus.ACTIVE
    release_attempts: int = 0
    release_error: str | None = None

    @property
    def released(self) -> bool:
        """Compatibility view for callers that only need terminal success."""

        return self.status == LeaseStatus.RELEASED

    async def release(self) -> None:
        """Release once, retaining a failed lease for a later retry."""

        if self.status == LeaseStatus.RELEASED:
            return
        self.status = LeaseStatus.RELEASING
        self.release_attempts += 1
        self.release_error = None
        callback = self.release_callback
        if callback is None:
            self.status = LeaseStatus.RELEASED
            return
        try:
            result = callback()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            self.status = LeaseStatus.RELEASE_FAILED
            self.release_error = "CancelledError"
            raise
        except BaseException as exc:
            self.status = LeaseStatus.RELEASE_FAILED
            self.release_error = f"{type(exc).__name__}: {exc}"[:500]
            raise LeaseError(
                f"释放 {self.kind} lease 失败",
                details={
                    "job_id": self.job_id,
                    "kind": str(self.kind),
                    "status": self.status.value,
                    "attempts": self.release_attempts,
                },
            ) from exc
        else:
            self.status = LeaseStatus.RELEASED


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
        if self._released and all(lease.released for lease in self._leases):
            return
        errors: list[BaseException] = []
        cancellations: list[asyncio.CancelledError] = []
        # 反向释放，保持文件/artifact 依赖其输入 lease 的惯例。
        for lease in reversed(self._leases):
            try:
                await lease.release()
            except asyncio.CancelledError as exc:
                cancellations.append(exc)
            except BaseException as exc:  # cleanup must continue for all leases
                errors.append(exc)
        if cancellations:
            self._released = False
            raise cancellations[0]
        if errors:
            self._released = False
            raise LeaseError(
                f"{len(errors)} 个 lease 释放失败",
                details={
                    "errors": [type(error).__name__ for error in errors],
                    "failed_leases": [
                        lease.token
                        for lease in self._leases
                        if lease.status == LeaseStatus.RELEASE_FAILED
                    ],
                },
            ) from errors[0]
        self._released = True

    async def __aenter__(self) -> LeaseSet:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        await self.release_all()
        return False
