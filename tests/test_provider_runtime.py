from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tl.provider_runtime import ProviderRuntime, ProviderRuntimeBusy, provider_operation


class Event:
    def plain_result(self, message: str) -> dict[str, str]:
        return {"text": message}


def test_operation_and_update_are_synchronous_mutually_exclusive_leases():
    runtime = ProviderRuntime()
    assert not runtime.busy
    with runtime.operation():
        assert runtime.active == 1
        with runtime.operation():
            assert runtime.active == 2
            with pytest.raises(ProviderRuntimeBusy, match="生成任务") as error:
                with runtime.update():
                    pytest.fail("update entered while operations are active")
            assert error.value.reason == "busy"
        assert runtime.active == 1
    assert runtime.active == 0
    with runtime.update():
        assert runtime.updating
        with pytest.raises(ProviderRuntimeBusy) as error:
            with runtime.operation():
                pytest.fail("operation entered during update")
        assert error.value.reason == "updating"
        with pytest.raises(ProviderRuntimeBusy):
            with runtime.update():
                pytest.fail("nested update entered")
    assert not runtime.updating
    assert not runtime.busy


@pytest.mark.asyncio
async def test_coroutine_cancellation_releases_lease():
    runtime = ProviderRuntime()
    entered = asyncio.Event()

    @provider_operation("api")
    async def wait(owner):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(wait(SimpleNamespace(provider_runtime=runtime)))
    await entered.wait()
    assert runtime.active == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not runtime.busy
