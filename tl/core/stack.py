"""v2 栈装配：把 v2 组件组合成 GenerationService，组件生命周期由 close 统一收口。"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .job_store import SQLiteJobStore
from .leases import Lease, LeaseKind
from .legacy_adapter import LegacyClientAdapter
from .models import Artifact
from .path_policy import PathPolicy
from .reference_service import ReferenceService
from .remote_fetch import RemoteFetchService
from .router import ProviderRouter
from .scheduler import JobScheduler
from .service import GenerationService


class _ReferenceLeaseStore:
    """为 ReferenceService 补上 scheduler 的引用租约入口，resolve 原样转发。"""

    def __init__(self, service: ReferenceService) -> None:
        self._service = service

    async def reserve(self, references: tuple[str, ...]) -> Lease:
        # 落盘缓存由 cache 目录统一清理，租约无需额外释放动作；
        # job_id 在持久化 LeaseRecord 时由 scheduler 补齐，此处留空。
        return Lease(f"reference-{uuid.uuid4().hex}", LeaseKind.REFERENCE, "")

    async def resolve(
        self, references: tuple[str, ...], *, deadline_at: Any = None
    ) -> list[Artifact]:
        return [item async for item in self._service.resolve(references)]


def _build_adapters(
    client: Any, candidates: Sequence[Mapping[str, Any]]
) -> list[LegacyClientAdapter]:
    adapters: list[LegacyClientAdapter] = []
    for index, item in enumerate(candidates):
        if not isinstance(item, Mapping):
            raise ValueError(f"candidates[{index}] 必须是映射")
        provider = item.get("provider")
        model = item.get("model")
        candidate_id = item.get("candidate_id")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError(f"candidates[{index}] 缺少 provider")
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"candidates[{index}] 缺少 model")
        if candidate_id is not None and not isinstance(candidate_id, str):
            raise ValueError(f"candidates[{index}] candidate_id 必须是字符串")
        adapters.append(
            LegacyClientAdapter(
                client,
                provider=provider,
                model=model,
                candidate_id=candidate_id,
                proxy=item.get("proxy"),
                provider_settings=item.get("provider_settings"),
            )
        )
    return adapters


def build_generation_stack(
    client: Any = None,
    candidates: Sequence[Mapping[str, Any]] = (),
    data_dir: str | Path | None = None,
    allowed_local_roots: Sequence[Path] = (),
    *,
    max_body_bytes: int | None = None,
    connect_timeout: float | None = None,
    max_pixels: int | None = None,
    max_reference_bytes: int | None = None,
    import_legacy: bool = False,
    quota: Any = None,
    limiter: Any = None,
    artifact_store: Any = None,
    delivery_store: Any = None,
) -> GenerationService:
    """装配 PathPolicy→Fetch→Reference→JobStore→Router→Scheduler→Service。"""

    if not candidates:
        raise ValueError("candidates 至少需要一个候选")
    if client is None:
        raise ValueError("client 不能为空")
    if data_dir is None:
        raise ValueError("data_dir 不能为空")

    policy = PathPolicy(tuple(allowed_local_roots), allow_private_networks=False)
    fetch_options: dict[str, Any] = {}
    if max_body_bytes is not None:
        fetch_options["max_body_bytes"] = max_body_bytes
    if connect_timeout is not None:
        fetch_options["connect_timeout"] = connect_timeout
    fetcher = RemoteFetchService(policy, **fetch_options)

    reference_options: dict[str, Any] = {}
    if max_pixels is not None:
        reference_options["max_pixels"] = max_pixels
    if max_reference_bytes is not None:
        reference_options["max_reference_bytes"] = max_reference_bytes
    reference_service = ReferenceService(policy, fetcher, **reference_options)

    store = SQLiteJobStore(data_dir, import_legacy=import_legacy)
    router = ProviderRouter(_build_adapters(client, candidates))
    try:
        scheduler = JobScheduler(
            store,
            provider=router,
            reference_store=_ReferenceLeaseStore(reference_service),
            quota=quota,
            limiter=limiter,
            artifact_store=artifact_store,
            delivery_store=delivery_store,
        )
    except BaseException:
        store.close_sync()
        raise
    return GenerationService(scheduler)
