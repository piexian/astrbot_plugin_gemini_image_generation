"""WebUI 组合准入与父任务并发名额。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace

import pytest

from tests.test_scheduler_batch import BatchProvider, child, eventually, parent
from tl.core.errors import StudioBusyError
from tl.core.job_store import SQLiteJobStore
from tl.core.requests import GenerationRequest
from tl.core.scheduler import JobScheduler
from tl.v2_admission import (
    CompositeAdmissionLimiter,
    LegacyFifoLimiter,
    StudioAdmissionLimiter,
)


def request(metadata=None):
    return GenerationRequest(prompt="draw", source="webui", metadata=metadata or {})


@pytest.mark.asyncio
async def test_studio_limiter_scopes_capacity_and_releases_idempotently():
    limiter = StudioAdmissionLimiter(lambda: 1)
    assert limiter.reserve(request()) is None
    reservation = limiter.reserve(request({"studio_admission": True}))
    with pytest.raises(StudioBusyError, match="并发上限"):
        limiter.reserve(request({"studio_admission": True}))

    reservation.release()
    reservation.release()
    assert limiter._active == 0
    assert limiter.reserve(request({"studio_admission": True})) is not None


@pytest.mark.asyncio
async def test_composite_limiter_rolls_back_and_scopes_in_order():
    events = []

    class Reservation:
        def __init__(self, name):
            self.name = name

        @contextmanager
        def runtime_scope(self):
            events.append(f"enter-{self.name}")
            yield
            events.append(f"exit-{self.name}")

        async def release(self):
            events.append(f"release-{self.name}")

    class Component:
        def __init__(self, name, error=None):
            self.name = name
            self.error = error

        async def reserve(self, request, admission=None):
            events.append(f"reserve-{self.name}")
            if self.error:
                raise self.error
            return Reservation(self.name)

    composite = CompositeAdmissionLimiter(Component("a"), Component("b"))
    reservation = await composite.reserve(request())
    with reservation.runtime_scope():
        pass
    await reservation.release()
    await reservation.release()
    assert events == [
        "reserve-a",
        "reserve-b",
        "enter-a",
        "enter-b",
        "exit-b",
        "exit-a",
        "release-a",
        "release-b",
    ]

    events.clear()
    failing = CompositeAdmissionLimiter(
        Component("a"), Component("b", RuntimeError("full"))
    )
    with pytest.raises(RuntimeError, match="full"):
        await failing.reserve(request())
    assert events == ["reserve-a", "reserve-b", "release-a"]


@pytest.mark.asyncio
async def test_batch_parent_holds_one_studio_slot_until_completion(tmp_path):
    gate = asyncio.Event()
    studio = StudioAdmissionLimiter(lambda: 1)
    limiter = CompositeAdmissionLimiter(studio, LegacyFifoLimiter(lambda: None))
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    scheduler = JobScheduler(store, provider=BatchProvider(gate=gate), limiter=limiter)
    children = [child("a"), child("b")]

    def studio_parent():
        base = parent(children, cost=2)
        return replace(base, metadata={**base.metadata, "studio_admission": True})

    try:
        first = await scheduler.submit_batch(studio_parent(), children, concurrency=2)
        await eventually(lambda: studio._active == 1)
        with pytest.raises(StudioBusyError):
            await scheduler.submit_batch(studio_parent(), children, concurrency=2)
        assert studio._active == 1

        gate.set()
        await scheduler.wait(first.job_id)
        assert studio._active == 0
        second = await scheduler.submit_batch(studio_parent(), children, concurrency=2)
        await scheduler.wait(second.job_id)
        assert studio._active == 0
    finally:
        gate.set()
        await scheduler.close()
