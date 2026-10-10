"""tests for tl/api/modelscope.py — 异步任务制 provider 的构建/轮询/门控"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tl.api.modelscope import ModelScopeProvider
from tl.api_types import APIError, ApiRequestConfig
from tl.provider_hooks import modelscope_edit_capability

_DATA_URI = "data:image/png;base64,QUJD"


def _make_config(**overrides) -> ApiRequestConfig:
    kwargs: dict = {
        "model": "",
        "prompt": "画一只猫",
        "api_type": "modelscope",
        "api_key": "test-key",
        "resolution": "1K",
        "aspect_ratio": "1:1",
        "provider_settings": {"model": "Qwen/Qwen-Image"},
    }
    kwargs.update(overrides)
    return ApiRequestConfig(**kwargs)


class _FakeClient:
    def __init__(self, *, proxy: bool = False, download_path: str | None = None):
        self._proxy = proxy
        self._download_path = download_path
        self.download_calls: list[str] = []

    def _request_has_proxy(self, request_config) -> bool:  # noqa: ANN001
        return self._proxy

    def _request_http_proxy(self, request_config) -> str | None:  # noqa: ANN001
        return "http://127.0.0.1:7890" if self._proxy else None

    async def _download_image(self, image_url, session, **kwargs):  # noqa: ANN001, ANN003
        self.download_calls.append(image_url)
        if self._download_path is None:
            raise RuntimeError("download boom")
        return None, self._download_path


class _FakePollResponse:
    def __init__(self, body: str, status: int = 200):
        self._body = body
        self.status = status

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> _FakePollResponse:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """按顺序返回响应体；最后一个响应体会被重复返回。"""

    def __init__(self, bodies: list):
        self._bodies = bodies
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _FakePollResponse:
        self.calls.append({"url": url, **kwargs})
        item = self._bodies.pop(0) if len(self._bodies) > 1 else self._bodies[0]
        # 元素可为 (http_status, body) 元组，用于模拟非 200 轮询响应
        status, body = item if isinstance(item, tuple) else (200, item)
        return _FakePollResponse(body, status=status)


def _task_body(status: str, **extra: Any) -> str:
    return json.dumps({"task_status": status, **extra})


@pytest.mark.asyncio
async def test_prompt_over_2000_fails_fast() -> None:
    provider = ModelScopeProvider()
    with pytest.raises(APIError) as exc_info:
        await provider.build_request(
            client=object(), config=_make_config(prompt="a" * 2001)
        )
    assert getattr(exc_info.value, "retryable", True) is False


@pytest.mark.asyncio
async def test_non_edit_model_with_reference_raises_non_retryable() -> None:
    provider = ModelScopeProvider()
    config = _make_config(
        provider_settings={"model": "Qwen/Qwen-Image"},
        reference_images=[_DATA_URI],
    )
    with pytest.raises(APIError) as exc_info:
        await provider.build_request(client=_FakeClient(), config=config)
    assert getattr(exc_info.value, "retryable", True) is False


def test_modelscope_edit_capability_gates_by_model() -> None:
    assert modelscope_edit_capability({"model": "Qwen/Qwen-Image-Edit"})
    assert not modelscope_edit_capability({"model": "Qwen/Qwen-Image"})
    assert not modelscope_edit_capability({})


@pytest.mark.asyncio
async def test_poll_timeout_does_not_resubmit_accepted_task() -> None:
    provider = ModelScopeProvider()
    session = _FakeSession([_task_body("Running")])
    config = _make_config(
        provider_settings={
            "model": "Qwen/Qwen-Image",
            "poll_interval": 0.001,
            "poll_timeout": 0.05,
        }
    )
    with pytest.raises(APIError) as exc_info:
        await provider.parse_response(
            client=_FakeClient(),
            response_data={"task_id": "task-1"},
            session=session,
            http_status=200,
            request_config=config,
        )
    assert exc_info.value.error_type == "timeout"
    assert exc_info.value.retryable is False
