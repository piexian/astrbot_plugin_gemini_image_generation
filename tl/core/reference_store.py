"""内联参考图的临时句柄存储（进程内、TTL + 容量淘汰）。"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrbot.api import logger

from .errors import ReferenceAccessDeniedError, ReferenceResolveError
from .models import Artifact
from .requests import normalize_requester

TMP_REFERENCE_KIND = "tmp"
# 句柄有效期与登记容量；重启后句柄失效（任务恢复不重放句柄）。
TMP_REFERENCE_TTL_SECONDS = 3600.0
TMP_REFERENCE_MAX_ENTRIES = 256
TMP_REFERENCE_MAX_ENTRIES_PER_REQUESTER = 64
# id 必须是纯 32 位小写十六进制，杜绝路径片段混入。
_HANDLE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class _TmpReferenceEntry:
    path: Path
    mime_type: str
    requester: dict[str, str]
    expires_at: datetime
    leases: int = 0


class TmpReferenceStore:
    """``kind="tmp"``：校验后的内联图字节经外部落盘回调登记为短句柄。"""

    kind = TMP_REFERENCE_KIND

    def __init__(
        self,
        write_cache: Callable[[bytes, str], Path],
        *,
        max_entries: int = TMP_REFERENCE_MAX_ENTRIES,
        max_entries_per_requester: int = TMP_REFERENCE_MAX_ENTRIES_PER_REQUESTER,
        ttl_seconds: float = TMP_REFERENCE_TTL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        # write_cache 复用 ReferenceService 的内容寻址落盘，本模块不另写盘。
        self._write_cache = write_cache
        self._max_entries = max(int(max_entries), 1)
        self._max_entries_per_requester = max(int(max_entries_per_requester), 1)
        self._ttl_seconds = float(ttl_seconds)
        self._now = clock or (lambda: datetime.now(timezone.utc))
        self._entries: OrderedDict[str, _TmpReferenceEntry] = OrderedDict()

    async def put(
        self, raw: bytes, mime_type: str, *, requester: Mapping[str, str]
    ) -> str:
        """落盘并登记句柄，返回 32 位十六进制 id（与内容无关，避免跨请求者串号）。"""

        normalized_requester = normalize_requester(requester)
        path = await asyncio.to_thread(self._write_cache, raw, mime_type)
        ref_id = uuid.uuid4().hex
        now = self._now()
        self._evict(now)
        self._make_room(normalized_requester)
        self._entries[ref_id] = _TmpReferenceEntry(
            path=path,
            mime_type=mime_type,
            requester=normalized_requester,
            expires_at=now + timedelta(seconds=self._ttl_seconds),
        )
        return ref_id

    async def resolve(
        self,
        ref_id: str,
        *,
        requester: Mapping[str, str],
        deadline_at: datetime | None = None,
    ) -> Artifact:
        if not _HANDLE_ID_RE.match(ref_id):
            raise ReferenceResolveError("参考图句柄 id 必须是 32 位小写十六进制")
        entry = self._entries.get(ref_id)
        now = self._now()
        if entry is None or (entry.expires_at <= now and entry.leases <= 0):
            self._entries.pop(ref_id, None)
            raise ReferenceResolveError("参考图引用已失效（不存在、过期或被淘汰）")
        if normalize_requester(requester or {}) != entry.requester:
            raise ReferenceAccessDeniedError("参考图句柄不属于当前请求者")
        try:
            stat = await asyncio.to_thread(entry.path.stat)
        except OSError as exc:
            logger.warning("参考图句柄文件缺失: %s", entry.path.name)
            raise ReferenceResolveError("参考图引用文件已失效") from exc
        return Artifact(
            artifact_id=f"ref-{uuid.uuid4().hex}",
            kind="reference",
            location=str(entry.path),
            mime_type=entry.mime_type,
            size_bytes=stat.st_size,
            metadata={"source": "inline", "handle": f"ref:{self.kind}:{ref_id}"},
        )

    def retain(self, ref_id: str) -> None:
        """在活动 reference lease 期间保护句柄不被 TTL 或容量淘汰。"""

        if not _HANDLE_ID_RE.match(ref_id):
            raise ReferenceResolveError("参考图句柄 id 必须是 32 位小写十六进制")
        entry = self._entries.get(ref_id)
        if entry is None or entry.expires_at <= self._now():
            raise ReferenceResolveError("参考图引用已失效（不存在、过期或被淘汰）")
        entry.leases += 1

    def release(self, ref_id: str) -> None:
        """释放一个活动 lease；已过期句柄在最后一个 lease 释放后回收。"""

        entry = self._entries.get(ref_id)
        if entry is None or entry.leases <= 0:
            return
        entry.leases -= 1
        if entry.leases == 0 and entry.expires_at <= self._now():
            self._entries.pop(ref_id, None)

    def _evict(self, now: datetime) -> None:
        # 文件由 AstrBot TempDirCleaner 统一清理；这里只回收空闲的过期映射。
        for ref_id in [
            ref_id
            for ref_id, entry in self._entries.items()
            if entry.expires_at <= now and entry.leases <= 0
        ]:
            self._entries.pop(ref_id, None)

    def _pop_oldest_idle(self, requester: dict[str, str]) -> bool:
        """只淘汰同 requester 的空闲句柄，避免跨请求者互相驱逐。"""

        for ref_id, entry in self._entries.items():
            if entry.requester == requester and entry.leases <= 0:
                self._entries.pop(ref_id, None)
                return True
        return False

    def _make_room(self, requester: dict[str, str]) -> None:
        """预留一个槽位；活动句柄占满时拒绝新登记而不牺牲已有任务。"""

        while (
            sum(entry.requester == requester for entry in self._entries.values())
            >= self._max_entries_per_requester
        ):
            if not self._pop_oldest_idle(requester):
                raise ReferenceResolveError("当前请求者的临时参考图句柄容量已满")
        if len(self._entries) >= self._max_entries:
            if not self._pop_oldest_idle(requester):
                raise ReferenceResolveError("临时参考图句柄总容量已满")
