import asyncio
from types import SimpleNamespace

import pytest

from tl.api_types import APIError
from tl.generation_scheduler import GenerationScheduler, scheduled_generation


@pytest.mark.asyncio
async def test_fifo_bound_cancel_and_exception_release():
    scheduler = GenerationScheduler(1, 2)
    entered = []
    gate = asyncio.Event()

    class Client:
        generation_scheduler = scheduler

        @scheduled_generation
        async def generate(self, name):
            entered.append(name)
            await gate.wait()
            if name == "first":
                raise ValueError("upstream failure")
            return name

    client = Client()
    first = asyncio.create_task(client.generate("first"))
    await asyncio.sleep(0)
    second = asyncio.create_task(client.generate("second"))
    third = asyncio.create_task(client.generate("third"))
    await asyncio.sleep(0)
    with pytest.raises(APIError, match="队列已满"):
        await client.generate("overflow")
    second.cancel()
    await asyncio.gather(second, return_exceptions=True)
    fourth = asyncio.create_task(client.generate("fourth"))
    gate.set()
    outcomes = await asyncio.gather(first, third, fourth, return_exceptions=True)
    assert isinstance(outcomes[0], ValueError)
    assert entered == ["first", "third", "fourth"]
    assert scheduler.active == 0 and not scheduler.waiters


@pytest.mark.asyncio
async def test_queue_wait_precedes_request_clock_and_close_interrupts():
    scheduler = GenerationScheduler(1, 5)
    reserved = scheduler.reserve()
    client = SimpleNamespace(generation_scheduler=scheduler)
    started = asyncio.Event()

    @scheduled_generation
    async def generate(owner):
        started.set()
        async with asyncio.timeout(0.01):
            await asyncio.sleep(0)
        return "ok"

    task = asyncio.create_task(generate(client))
    await asyncio.sleep(0.02)
    assert not started.is_set()
    reserved.release()
    assert await task == "ok"
    reserved = scheduler.reserve()
    waiting = asyncio.create_task(generate(client))
    await asyncio.sleep(0)
    await scheduler.close()
    assert waiting.cancelled()
    reserved.release()
    assert not scheduler.busy
    with pytest.raises(APIError):
        scheduler.reserve()
