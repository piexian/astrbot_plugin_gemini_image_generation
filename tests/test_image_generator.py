from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tl.core.results import GenerationResult
from tl.core.states import JobState
from tl.image_generator import ImageGenerator


def _keep_all(images: list[str] | None, source: str) -> list[str]:
    return images or []


class _FakeV2Service:
    """最小 v2 服务：记录提交的请求并立即返回成功结果。"""

    def __init__(self, result: GenerationResult | None = None) -> None:
        self.result = result or GenerationResult(artifacts=("/tmp/generated.png",))
        self.requests: list = []

    def result_wait_timeout(self, deadline_seconds):
        return deadline_seconds + 1

    async def prepare_references(self, references, *, requester):
        return tuple(references)

    async def submit(self, request, *, admission=None):
        self.requests.append(request)
        return SimpleNamespace(job_id="job-v2")

    async def wait(self, job_id, timeout=None):
        return SimpleNamespace(state=JobState.SUCCEEDED, result=self.result)

    async def cancel(self, job_id):
        pass


@pytest.mark.asyncio
async def test_generate_image_core_binds_session_save_dir(monkeypatch) -> None:
    from tl import image_generator as ig

    calls: list[tuple[str, object]] = []
    token = object()

    async def fake_bind(event, context=None):
        calls.append(("bind", event, context))
        return token

    monkeypatch.setattr(ig, "bind_session_image_dir", fake_bind)
    monkeypatch.setattr(
        ig, "reset_session_image_dir", lambda value: calls.append(("reset", value))
    )
    generator = ImageGenerator(
        context=None, api_client=object(), filter_valid_fn=_keep_all
    )
    event = SimpleNamespace(unified_msg_origin="platform:GroupMessage:123")

    success, _ = await generator.generate_image_core(
        event=event,
        prompt="draw",
        reference_images=[],
        avatar_reference=[],
        v2_service=_FakeV2Service(),
    )

    assert success is True
    assert calls == [("bind", event, None), ("reset", token)]


@pytest.mark.asyncio
async def test_tool_call_generation_uses_plugin_total_timeout() -> None:
    service = _FakeV2Service()
    generator = ImageGenerator(
        context=None,
        api_client=object(),
        total_timeout=777,
        filter_valid_fn=_keep_all,
    )

    success, _ = await generator.generate_image_core(
        event=None,
        prompt="draw",
        reference_images=[],
        avatar_reference=[],
        is_tool_call=True,
        v2_service=service,
    )

    assert success is True
    (request,) = service.requests
    assert request.metadata["total_timeout"] == 777
    remaining = (request.deadline_at - datetime.now(timezone.utc)).total_seconds()
    assert 770 <= remaining <= 777
    assert request.source == "llm_tool"
