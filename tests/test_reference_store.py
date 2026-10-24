"""TmpReferenceStore 单元测试：句柄登记、伪造拒绝、越权、TTL 与容量淘汰。"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tl.core.errors import ReferenceAccessDeniedError, ReferenceResolveError
from tl.core.reference_store import TmpReferenceStore


def _write_cache(root: Path):
    def write(raw: bytes, mime_type: str) -> Path:
        path = root / f"{len(raw)}.{mime_type.split('/')[-1]}"
        path.write_bytes(raw)
        return path

    return write


class _FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.mark.asyncio
async def test_put_resolve_roundtrip(tmp_path):
    store = TmpReferenceStore(_write_cache(tmp_path))
    ref_id = await store.put(b"png-bytes", "image/png", requester={"user_id": "u1"})

    assert len(ref_id) == 32
    assert ref_id == ref_id.lower()

    artifact = await store.resolve(
        ref_id, requester={"user_id": "u1"}, deadline_at=None
    )
    assert artifact.kind == "reference"
    assert artifact.mime_type == "image/png"
    assert artifact.size_bytes == len(b"png-bytes")
    assert artifact.location == str(tmp_path / "9.png")
    assert artifact.metadata["source"] == "inline"
    assert artifact.metadata["handle"] == f"ref:tmp:{ref_id}"
    assert artifact.artifact_id.startswith("ref-")


@pytest.mark.asyncio
async def test_forged_ids_rejected_without_fs(tmp_path, monkeypatch):
    store = TmpReferenceStore(_write_cache(tmp_path))

    def _boom(*args, **kwargs):
        raise AssertionError("rejected path must not touch the filesystem")

    monkeypatch.setattr(os, "stat", _boom)
    forged = [
        "../../etc/passwd",
        "A" * 32,  # 大写十六进制
        "0" * 31,  # 长度不足
        "0" * 33,  # 长度超
        "g" * 32,  # 非十六进制
    ]
    for ref_id in forged:
        with pytest.raises(ReferenceResolveError, match="32 位小写十六进制"):
            await store.resolve(ref_id, requester={"user_id": "u1"})
    # 合法形态但未登记：同样在触碰文件系统之前拒绝。
    with pytest.raises(ReferenceResolveError, match="已失效"):
        await store.resolve("a" * 32, requester={"user_id": "u1"})
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_requester_mismatch_denied_before_fs(tmp_path, monkeypatch):
    store = TmpReferenceStore(_write_cache(tmp_path))
    ref_id = await store.put(b"data", "image/png", requester={"user_id": "u1"})

    def _boom(*args, **kwargs):
        raise AssertionError("access denied must not touch the filesystem")

    monkeypatch.setattr(os, "stat", _boom)
    with pytest.raises(ReferenceAccessDeniedError):
        await store.resolve(ref_id, requester={"user_id": "u2"})
    with pytest.raises(ReferenceAccessDeniedError):
        await store.resolve(ref_id, requester={})
    monkeypatch.undo()

    # 登记方本人仍可解析。
    artifact = await store.resolve(ref_id, requester={"user_id": "u1"})
    assert artifact.size_bytes == 4


@pytest.mark.asyncio
async def test_empty_requester_registered_resolves_only_for_empty(tmp_path):
    store = TmpReferenceStore(_write_cache(tmp_path))
    ref_id = await store.put(b"data", "image/png", requester={})

    artifact = await store.resolve(ref_id, requester={})
    assert artifact.kind == "reference"
    with pytest.raises(ReferenceAccessDeniedError):
        await store.resolve(ref_id, requester={"user_id": "u1"})


@pytest.mark.asyncio
async def test_expired_entry_fails_and_is_dropped(tmp_path):
    clock = _FakeClock()
    store = TmpReferenceStore(_write_cache(tmp_path), ttl_seconds=600, clock=clock)
    ref_id = await store.put(b"data", "image/png", requester={})

    clock.advance(601)
    with pytest.raises(ReferenceResolveError, match="已失效"):
        await store.resolve(ref_id, requester={})
    assert store._entries == {}


@pytest.mark.asyncio
async def test_capacity_evicts_oldest(tmp_path):
    store = TmpReferenceStore(_write_cache(tmp_path), max_entries=1)
    first = await store.put(b"one", "image/png", requester={})
    second = await store.put(b"two", "image/png", requester={})

    with pytest.raises(ReferenceResolveError, match="已失效"):
        await store.resolve(first, requester={})
    artifact = await store.resolve(second, requester={})
    assert artifact.size_bytes == 3


@pytest.mark.asyncio
async def test_missing_cache_file_reports_stale(tmp_path):
    store = TmpReferenceStore(_write_cache(tmp_path))
    ref_id = await store.put(b"data", "image/png", requester={})
    store._entries[ref_id].path.unlink()

    with pytest.raises(ReferenceResolveError, match="文件已失效"):
        await store.resolve(ref_id, requester={})


@pytest.mark.asyncio
async def test_requester_normalized_on_both_sides(tmp_path):
    """登记与解析共用 GenerationRequest 的 requester 归一化口径。"""

    from tl.core.requests import GenerationRequest

    store = TmpReferenceStore(_write_cache(tmp_path))
    raw = {
        "user_id": "u1",
        "group_id": None,  # None 值丢弃
        "extra_field": "x",  # 白名单外键丢弃
        "session_id": "S" * 300,  # 超 256 字符截断
    }
    ref_id = await store.put(b"data", "image/png", requester=raw)
    normalized = GenerationRequest(prompt="p", source="test", requester=raw).requester
    assert normalized == {"user_id": "u1", "session_id": "S" * 256}

    # 解析侧传归一化结果或同一份原始输入都能命中。
    assert (await store.resolve(ref_id, requester=normalized)).kind == "reference"
    assert (await store.resolve(ref_id, requester=raw)).kind == "reference"

    # user_id 不同仍判越权。
    with pytest.raises(ReferenceAccessDeniedError):
        await store.resolve(ref_id, requester={"user_id": "u2"})
