"""Studio 上传暂存租约：只在参考图入库窗口持有，入库后立即释放。

v2 唯一执行链路下，`generate()` 在 `prepare_references` 读盘入库期间持有
`_upload_ref_counts`；提交返回后立刻释放，在途 Job 引用的是临时句柄，
不再依赖上传原图。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path

import pytest
import pytest_asyncio

import tl.web_studio_service as studio_module
from tests.test_web_studio_service import _close_v2, _config, _png, _start_v2
from tl.core.errors import ReferenceResolveError
from tl.generation_tracker import GenerationTracker
from tl.web_studio_service import StudioServiceError, WebStudioService


class _Client:
    """记录请求并可阻塞的 legacy 客户端；默认返回一张本地图。"""

    def __init__(
        self, *, gate: asyncio.Event | None = None, output: Path | None = None
    ):
        self.gate = gate
        self.output = output
        self.started = asyncio.Event()
        self.configs = []

    async def generate_image(self, config, **kwargs):
        self.configs.append(config)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.output is None:
            return [], [], None, None
        return [], [str(self.output)], None, None


@pytest_asyncio.fixture
async def studio_env(tmp_path):
    tracker = GenerationTracker(tmp_path, 20)
    studio = WebStudioService(_Client(), tracker, _config(), tmp_path)
    studio.upload_dir.mkdir()
    env = await _start_v2(studio, tracker)
    env.studio = studio
    try:
        yield env
    finally:
        await _close_v2(env)
        await studio.close()


def _upload(studio, name="ref.png"):
    path = _png(studio.upload_dir / name)
    expired = time.time() - 25 * 3600
    os.utime(path, (expired, expired))
    return path


async def _protected(studio, path, monkeypatch):
    """过期文件不被回收，且容量清理仍判定超限（说明剩余文件被租约占住）。"""

    monkeypatch.setattr(studio_module, "_UPLOAD_QUOTA_BYTES", path.stat().st_size - 1)
    await asyncio.to_thread(studio._cleanup_uploads_sync)
    assert path.is_file()
    with pytest.raises(StudioServiceError) as caught:
        await asyncio.to_thread(studio._enforce_upload_quota_sync, set())
    assert caught.value.status_code == 507
    assert path.is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_lease_held_during_reference_ingest_only(studio_env, monkeypatch, batch):
    studio = studio_env.studio
    path = _upload(studio)
    entered, release = asyncio.Event(), asyncio.Event()
    prepare = studio_env.v2.prepare_references

    async def blocked_prepare(*args, **kwargs):
        entered.set()
        await release.wait()
        return await prepare(*args, **kwargs)

    monkeypatch.setattr(studio_env.v2, "prepare_references", blocked_prepare)
    monkeypatch.setattr(studio_module, "_UPLOAD_QUOTA_BYTES", path.stat().st_size)
    payload = (
        {"batch": [{"name": name, "prompt": name} for name in ("one", "two")]}
        if batch
        else {"prompt": "draw"}
    )
    pending = asyncio.create_task(
        studio.generate({**payload, "upload_names": [path.name]})
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        # 入库窗口内文件被租约保护：容量清理不能删掉它。
        assert studio._upload_ref_counts == {path.name: 1}
        await _protected(studio, path, monkeypatch)
    finally:
        release.set()
        accepted = await pending

    await studio_env.v2.wait(accepted["job_id"])
    assert studio._upload_ref_counts == {}
    await asyncio.to_thread(studio._cleanup_uploads_sync)
    assert not path.exists()


@pytest.mark.asyncio
async def test_ingest_failure_releases_only_its_lease(studio_env, monkeypatch):
    studio = studio_env.studio
    path = _upload(studio)
    existing = studio._acquire_uploads([path.name])

    async def fail_ingest(*args, **kwargs):
        raise ReferenceResolveError("读取本地参考图失败: ref.png")

    monkeypatch.setattr(studio_env.v2, "prepare_references", fail_ingest)
    try:
        with pytest.raises(StudioServiceError) as caught:
            await studio.generate({"prompt": "draw", "upload_names": [path.name]})
        assert caught.value.status_code == 404
        assert studio._upload_ref_counts == {path.name: 1}
        assert studio_env.tracker.active_and_recent() == []
    finally:
        studio._release_uploads(existing)
    assert studio._upload_ref_counts == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_submit_rejection_releases_lease(studio_env, batch):
    studio = studio_env.studio
    studio_env.quota.allowed = False
    path = _upload(studio)
    payload = (
        {"batch": [{"name": name, "prompt": name} for name in ("one", "two")]}
        if batch
        else {"prompt": "draw"}
    )

    with pytest.raises(StudioServiceError) as caught:
        await studio.generate({**payload, "upload_names": [path.name]})

    assert caught.value.status_code == 429
    assert caught.value.data == {"scope": "global", "retry_after": 17}
    assert studio._upload_ref_counts == {}
    assert studio_env.tracker.active_and_recent() == []


@pytest.mark.asyncio
async def test_upload_can_be_cleaned_while_job_runs(studio_env, monkeypatch):
    """提交返回后上传原图即可删除：在途 Job 引用的是入库句柄。"""

    studio = studio_env.studio
    path = _upload(studio)
    gate = asyncio.Event()
    output = _png(studio.data_dir / "output.png", 1)
    studio.api_client = _Client(gate=gate, output=output)

    accepted = await studio.generate({"prompt": "draw", "upload_names": [path.name]})

    assert studio._upload_ref_counts == {}
    await asyncio.wait_for(studio.api_client.started.wait(), 5)
    reference = studio.api_client.configs[0].reference_images[0]
    assert reference != str(path)
    await asyncio.to_thread(studio._cleanup_uploads_sync)
    assert not path.exists()
    gate.set()
    await studio_env.v2.wait(accepted["job_id"])
    assert studio_env.tracker.get(accepted["job_id"])["status"] == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_cancelled_generate_releases_lease(studio_env, monkeypatch, batch):
    studio = studio_env.studio
    path = _upload(studio)
    entered = asyncio.Event()
    prepare = studio_env.v2.prepare_references

    async def blocked_prepare(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
        return await prepare(*args, **kwargs)

    monkeypatch.setattr(studio_env.v2, "prepare_references", blocked_prepare)
    payload = (
        {"batch": [{"name": "one", "prompt": "draw"}]} if batch else {"prompt": "draw"}
    )
    pending = asyncio.create_task(
        studio.generate({**payload, "upload_names": [path.name]})
    )
    await asyncio.wait_for(entered.wait(), 5)
    assert studio._upload_ref_counts == {path.name: 1}

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert studio._upload_ref_counts == {}


@pytest.mark.asyncio
async def test_close_releases_leases_and_rejects_late_requests(studio_env, monkeypatch):
    studio = studio_env.studio
    path = _upload(studio)
    entered, release = asyncio.Event(), asyncio.Event()
    prepare = studio_env.v2.prepare_references

    async def blocked_prepare(*args, **kwargs):
        entered.set()
        await release.wait()
        return await prepare(*args, **kwargs)

    monkeypatch.setattr(studio_env.v2, "prepare_references", blocked_prepare)
    pending = asyncio.create_task(
        studio.generate({"prompt": "draw", "upload_names": [path.name]})
    )
    await asyncio.wait_for(entered.wait(), 5)
    closing = asyncio.create_task(studio.close())
    try:
        await asyncio.sleep(0)
        assert studio.closed
        assert not closing.done()
        await _protected(studio, path, monkeypatch)
    finally:
        release.set()
        accepted = await pending
        await closing

    assert studio._upload_ref_counts == {}
    await studio_env.v2.wait(accepted["job_id"])
    with pytest.raises(StudioServiceError) as caught:
        await studio.generate({"prompt": "late", "upload_names": [path.name]})
    assert caught.value.status_code == 503


@pytest.mark.asyncio
async def test_admission_rechecks_all_uploads_without_partial_lease(
    studio_env, monkeypatch
):
    studio = studio_env.studio
    path = _upload(studio)
    missing = _upload(studio, "removed.png")
    validate = studio.validate_payload

    def validate_then_remove(payload):
        result = validate(payload)
        missing.unlink()
        return result

    monkeypatch.setattr(studio, "validate_payload", validate_then_remove)
    with pytest.raises(StudioServiceError) as caught:
        await studio.generate(
            {"prompt": "draw", "upload_names": [path.name, missing.name]}
        )
    assert caught.value.status_code == 404
    assert path.is_file()
    assert studio._upload_ref_counts == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["expiry", "quota"])
async def test_cleanup_and_acquisition_are_atomic(studio_env, monkeypatch, cleanup):
    studio = studio_env.studio
    path = _upload(studio)
    entered, release, acquiring = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    unlink = Path.unlink

    def blocked_unlink(target, *args, **kwargs):
        if target == path:
            entered.set()
            assert release.wait(5)
        return unlink(target, *args, **kwargs)

    def acquire():
        acquiring.set()
        return studio._acquire_uploads([path.name])

    monkeypatch.setattr(Path, "unlink", blocked_unlink)
    monkeypatch.setattr(studio_module, "_UPLOAD_QUOTA_BYTES", 0)
    clean = (
        asyncio.to_thread(studio._cleanup_uploads_sync)
        if cleanup == "expiry"
        else asyncio.to_thread(studio._enforce_upload_quota_sync, set())
    )
    cleaning = asyncio.create_task(clean)
    pending = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        pending = asyncio.create_task(asyncio.to_thread(acquire))
        assert await asyncio.to_thread(acquiring.wait, 5)
        assert not pending.done()
    finally:
        release.set()
        await cleaning
        if pending is not None:
            with pytest.raises(StudioServiceError) as caught:
                await pending
            assert caught.value.status_code == 404
    assert not path.exists()
    assert studio._upload_ref_counts == {}
