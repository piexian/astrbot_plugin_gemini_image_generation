from __future__ import annotations

import asyncio

import pytest

from test_model_catalog import FakeResponse, request  # noqa: F401 -- request 复用模块级 fixture
from test_model_catalog import http as http  # noqa: F811 -- 显式导入以注册 http fixture
from tl.model_catalog import ModelCatalogError, ModelCatalogService


@pytest.mark.asyncio
async def test_catalog_queue_is_bounded_and_rejection_does_not_open_another_session(
    http,  # noqa: F811 -- 显式导入以在本模块注册 fixture
):
    release = asyncio.Event()
    responses = [FakeResponse({"data": []}, block=release) for _ in range(2)]
    http.responses = responses.copy()
    service = ModelCatalogService()
    tasks = [asyncio.create_task(service.fetch(request())) for _ in range(4)]
    await asyncio.wait_for(
        asyncio.gather(*(response.started.wait() for response in responses)), 1
    )
    assert len(service._workers) == 4
    with pytest.raises(ModelCatalogError) as error:
        await service.fetch(request())
    assert error.value.status_code == 429 and error.value.reason == "busy"
    assert len(http.sessions) == 2
    await service.close()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert not service._workers
