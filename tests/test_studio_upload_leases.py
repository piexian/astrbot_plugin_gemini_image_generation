from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path

import pytest
import pytest_asyncio

import tl.web_studio_service as studio_module
from tests.test_web_studio_service import _config, _png, _SequenceClient
from tl.generation_tracker import GenerationTracker, ReferenceImageUnavailableError
from tl.web_studio_service import StudioServiceError, WebStudioService


@pytest_asyncio.fixture
async def service(tmp_path):
    tracker = GenerationTracker(tmp_path, 20)
    studio = WebStudioService(_SequenceClient([]), tracker, _config(), tmp_path)
    studio.upload_dir.mkdir()
    try:
        yield studio
    finally:
        await studio.close()
        await tracker.close()


def _upload(service, name="ref.png"):
    path = _png(service.upload_dir / name)
    expired = time.time() - 25 * 3600
    os.utime(path, (expired, expired))
    return path


async def _protected(service, path):
    await asyncio.to_thread(service._cleanup_uploads_sync)
    assert path.is_file()
    with pytest.raises(StudioServiceError) as caught:
        await asyncio.to_thread(service._enforce_upload_quota_sync, set())
    assert caught.value.status_code == 507
    assert path.is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_admission_protects_upload_before_tracker_begin(
    service, monkeypatch, batch
):
    path = _upload(service)
    unused = _upload(service, "unused.png")
    entered, release = asyncio.Event(), asyncio.Event()
    begin = service.tracker.begin
    calls = 0
    payload = (
        {"batch": [{"name": name, "prompt": name} for name in ("one", "two")]}
        if batch
        else {"prompt": "draw"}
    )

    async def blocked_begin(**kwargs):
        nonlocal calls
        calls += 1
        if calls == (3 if batch else 1):
            entered.set()
            await release.wait()
        return await begin(**kwargs)

    class Upload:
        def __init__(self):
            self.data = path.read_bytes()
            self.closed = False

        async def read(self, size):
            chunk, self.data = self.data[:size], self.data[size:]
            return chunk

        async def close(self):
            self.closed = True

    monkeypatch.setattr(service.tracker, "begin", blocked_begin)
    monkeypatch.setattr(studio_module, "_UPLOAD_QUOTA_BYTES", path.stat().st_size)
    pending = asyncio.create_task(
        service.generate({**payload, "upload_names": [path.name]})
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert service._upload_ref_counts == {path.name: 1}
        upload = Upload()
        with pytest.raises(StudioServiceError) as caught:
            await service.save_uploads([upload])
        assert caught.value.status_code == 507
        assert upload.closed
        assert path.is_file()
        assert not unused.exists()
        assert list(service.upload_dir.iterdir()) == [path]
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    assert service._admitted_jobs == 0
    assert service._upload_ref_counts == {}
    await asyncio.to_thread(service._cleanup_uploads_sync)
    assert not path.exists()


@pytest.mark.asyncio
async def test_queued_single_jobs_share_upload_until_last_completion(
    service, monkeypatch
):
    path = _upload(service)
    entered = {name: asyncio.Event() for name in ("first", "second")}
    release = {name: asyncio.Event() for name in entered}

    class Client:
        async def generate_image(self, config, **kwargs):
            assert config.reference_images == [str(path)] * (
                2 if config.prompt == "first" else 1
            )
            entered[config.prompt].set()
            await release[config.prompt].wait()
            assert path.is_file()
            return [], [], None, None

    service.api_client = Client()
    service._api_semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(studio_module, "_UPLOAD_QUOTA_BYTES", 0)
    first = await service.generate(
        {"prompt": "first", "upload_names": [path.name, path.name]}
    )
    first_task = service._runtime_tasks[first["job_id"]]
    second = await service.generate({"prompt": "second", "upload_names": [path.name]})
    second_task = service._runtime_tasks[second["job_id"]]
    await asyncio.wait_for(entered["first"].wait(), 5)
    assert not entered["second"].is_set()
    assert service._upload_ref_counts == {path.name: 2}
    await _protected(service, path)
    release["first"].set()
    await first_task
    await asyncio.wait_for(entered["second"].wait(), 5)
    assert service._upload_ref_counts == {path.name: 1}
    await _protected(service, path)
    release["second"].set()
    await second_task
    assert service._upload_ref_counts == {}
    await asyncio.to_thread(service._enforce_upload_quota_sync, set())
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_batch_exception_waits_for_siblings_and_shared_single(
    service, monkeypatch, error_type
):
    path = _upload(service)
    service.config.batch_concurrency = 1
    entered = {name: asyncio.Event() for name in ("first", "second", "single")}
    release = {name: asyncio.Event() for name in entered}

    async def execute(job_id, payload):
        name = payload["prompt"]
        entered[name].set()
        await release[name].wait()
        if name == "first":
            raise error_type("child failed")
        await service.tracker.fail(job_id, error="no image")

    monkeypatch.setattr(service, "_execute_generation", execute)
    monkeypatch.setattr(studio_module, "_UPLOAD_QUOTA_BYTES", 0)
    batch = await service.generate(
        {
            "batch": [
                {"name": "first", "prompt": "first"},
                {"name": "second", "prompt": "second"},
            ],
            "upload_names": [path.name, path.name],
        }
    )
    batch_task = service._runtime_tasks[batch["job_id"]]
    single = await service.generate({"prompt": "single", "upload_names": [path.name]})
    single_task = service._runtime_tasks[single["job_id"]]
    await asyncio.wait_for(entered["single"].wait(), 5)
    await asyncio.wait_for(entered["first"].wait(), 5)
    assert not entered["second"].is_set()
    assert service._upload_ref_counts == {path.name: 4}
    await _protected(service, path)
    release["first"].set()
    await asyncio.wait_for(entered["second"].wait(), 5)
    assert not batch_task.done()
    assert service._upload_ref_counts == {path.name: 3}
    await _protected(service, path)
    release["second"].set()
    if error_type is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await batch_task
        status = "interrupted"
    else:
        await batch_task
        status = "failed"
    assert service.tracker.get(batch["job_id"])["status"] == status
    assert service._upload_ref_counts == {path.name: 1}
    await _protected(service, path)
    release["single"].set()
    await single_task
    assert service._upload_ref_counts == {}
    await asyncio.to_thread(service._cleanup_uploads_sync)
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["succeeded", "partial_success", "failed", "exception"]
)
@pytest.mark.parametrize("batch", [False, True])
async def test_terminal_paths_release_upload(service, monkeypatch, status, batch):
    path = _upload(service)
    output = _png(service.data_dir / "output.png", 1)
    service.api_client = _SequenceClient(
        [([], [str(output)], None, None), ([], [], None, None)]
        if status != "failed"
        else [([], [], None, None)]
    )
    if status == "exception":

        async def archive(*args, **kwargs):
            raise RuntimeError("archive failed")

        monkeypatch.setattr(service, "archive_sources", archive)
    payload = (
        {"batch": [{"name": "one", "prompt": "draw"}]} if batch else {"prompt": "draw"}
    )
    accepted = await service.generate(
        {
            **payload,
            "upload_names": [path.name],
            "image_count": 2 if status == "partial_success" else 1,
        }
    )
    task = service._runtime_tasks[accepted["job_id"]]
    if status == "exception" and not batch:
        with pytest.raises(RuntimeError, match="archive failed"):
            await task
    else:
        await task
        expected = "failed" if status == "exception" else status
        assert service.tracker.get(accepted["job_id"])["status"] == expected
    assert service._upload_ref_counts == {}
    assert service._admitted_jobs == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_cancellation_before_runtime_starts_releases_upload(service, batch):
    path = _upload(service)
    payload = (
        {"batch": [{"name": "one", "prompt": "draw"}]} if batch else {"prompt": "draw"}
    )
    accepted = await service.generate({**payload, "upload_names": [path.name]})
    task = service._runtime_tasks[accepted["job_id"]]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert service._upload_ref_counts == {}
    assert service._admitted_jobs == 0


@pytest.mark.asyncio
async def test_close_waits_for_batch_children_before_releasing(service, monkeypatch):
    path = _upload(service)
    entered, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    service.config.batch_concurrency = 1

    async def execute(job_id, payload):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelling.set()
            await release.wait()
            assert path.is_file()

    monkeypatch.setattr(service, "_execute_generation", execute)
    monkeypatch.setattr(studio_module, "_UPLOAD_QUOTA_BYTES", 0)
    await service.generate(
        {
            "batch": [
                {"name": "one", "prompt": "draw one"},
                {"name": "two", "prompt": "draw two"},
            ],
            "upload_names": [path.name],
        }
    )
    await asyncio.wait_for(entered.wait(), 5)
    closing = asyncio.create_task(service.close())
    try:
        await asyncio.wait_for(cancelling.wait(), 5)
        assert not closing.done()
        assert service._upload_ref_counts == {path.name: 2}
        await _protected(service, path)
    finally:
        release.set()
        await closing
    assert service._upload_ref_counts == {}
    assert service._runtime_tasks == {}
    assert service._admitted_jobs == 0
    await service.close()
    await asyncio.to_thread(service._cleanup_uploads_sync)
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["begin", "child_begin", "reference", "attach"])
async def test_start_failure_rolls_back_only_its_lease(service, monkeypatch, failure):
    path = _upload(service)
    existing = service._acquire_uploads([path.name])
    begin = service.tracker.begin
    calls = 0

    async def failing_begin(**kwargs):
        nonlocal calls
        calls += 1
        if failure == "begin" or (failure == "child_begin" and calls == 3):
            raise RuntimeError("start failed")
        if failure == "reference":
            raise ReferenceImageUnavailableError("reference missing")
        return await begin(**kwargs)

    attach = service._attach
    coroutines = []

    def failing_attach(job_id, coroutine, upload_names):
        def fail_create(coro):
            raise RuntimeError("start failed")

        coroutines.append(coroutine)
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "create_task", fail_create)
            attach(job_id, coroutine, upload_names)

    monkeypatch.setattr(service.tracker, "begin", failing_begin)
    if failure == "attach":
        monkeypatch.setattr(service, "_attach", failing_attach)
    payload = (
        {
            "batch": [
                {"name": "one", "prompt": "draw one"},
                {"name": "two", "prompt": "draw two"},
            ]
        }
        if failure == "child_begin"
        else {"prompt": "draw"}
    )
    error_type = StudioServiceError if failure == "reference" else RuntimeError
    with pytest.raises(error_type) as caught:
        await service.generate({**payload, "upload_names": [path.name]})
    if failure == "reference":
        assert caught.value.status_code == 404
    assert all(coroutine.cr_frame is None for coroutine in coroutines)
    assert service._upload_ref_counts == {path.name: 1}
    assert service._admitted_jobs == 0
    assert service._runtime_tasks == {}
    service._release_uploads(existing)
    assert service._upload_ref_counts == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_close_during_admission_cannot_attach_late_job(
    service, monkeypatch, batch
):
    path = _upload(service)
    entered, release = asyncio.Event(), asyncio.Event()
    begin = service.tracker.begin
    calls = 0
    payload = (
        {"batch": [{"name": name, "prompt": name} for name in ("one", "two")]}
        if batch
        else {"prompt": "draw"}
    )

    async def blocked_begin(**kwargs):
        nonlocal calls
        calls += 1
        if calls == (3 if batch else 1):
            entered.set()
            await release.wait()
        return await begin(**kwargs)

    monkeypatch.setattr(service.tracker, "begin", blocked_begin)
    monkeypatch.setattr(studio_module, "_UPLOAD_QUOTA_BYTES", 0)
    pending = asyncio.create_task(
        service.generate({**payload, "upload_names": [path.name]})
    )
    await asyncio.wait_for(entered.wait(), 5)
    closing = asyncio.create_task(service.close())
    try:
        await asyncio.sleep(0)
        assert service.closed
        assert not closing.done()
        await _protected(service, path)
    finally:
        release.set()
        result = await asyncio.gather(pending, return_exceptions=True)
        await closing
    assert isinstance(result[0], StudioServiceError)
    assert result[0].status_code == 503
    assert service._upload_ref_counts == {}
    assert service._runtime_tasks == {}
    assert service._admitted_jobs == 0
    with pytest.raises(StudioServiceError) as caught:
        await service.generate({"prompt": "late", "upload_names": [path.name]})
    assert caught.value.status_code == 503


@pytest.mark.asyncio
async def test_admission_rechecks_all_uploads_without_partial_lease(
    service, monkeypatch
):
    path = _upload(service)
    missing = _upload(service, "removed.png")
    validate = service.validate_payload

    def validate_then_remove(payload):
        result = validate(payload)
        missing.unlink()
        return result

    monkeypatch.setattr(service, "validate_payload", validate_then_remove)
    with pytest.raises(StudioServiceError) as caught:
        await service.generate(
            {"prompt": "draw", "upload_names": [path.name, missing.name]}
        )
    assert caught.value.status_code == 404
    assert path.is_file()
    assert service._upload_ref_counts == {}
    assert service._admitted_jobs == 0
    assert service._runtime_tasks == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["expiry", "quota"])
async def test_cleanup_and_acquisition_are_atomic(service, monkeypatch, cleanup):
    path = _upload(service)
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
        return service._acquire_uploads([path.name])

    monkeypatch.setattr(Path, "unlink", blocked_unlink)
    monkeypatch.setattr(studio_module, "_UPLOAD_QUOTA_BYTES", 0)
    clean = (
        asyncio.to_thread(service._cleanup_uploads_sync)
        if cleanup == "expiry"
        else asyncio.to_thread(service._enforce_upload_quota_sync, set())
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
    assert service._upload_ref_counts == {}
