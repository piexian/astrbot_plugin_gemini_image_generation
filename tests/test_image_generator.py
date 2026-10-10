from __future__ import annotations

import pytest

from tl.image_generator import ImageGenerator


class _FakeAPIClient:
    def __init__(self) -> None:
        self.config = None

    async def generate_image(self, *, config, **kwargs):
        self.config = config
        return [], ["/tmp/generated.png"], None, None


def _keep_all(images: list[str] | None, source: str) -> list[str]:
    return images or []


class _RecordingAPIClient:
    def __init__(self) -> None:
        self.kwargs: dict | None = None

    async def generate_image(self, *, config, **kwargs):
        self.kwargs = kwargs
        return [], ["/tmp/generated.png"], None, None


@pytest.mark.asyncio
async def test_generate_image_core_binds_session_save_dir(monkeypatch) -> None:
    from types import SimpleNamespace

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
        context=None, api_client=_FakeAPIClient(), filter_valid_fn=_keep_all
    )
    event = SimpleNamespace(unified_msg_origin="platform:GroupMessage:123")

    success, _ = await generator.generate_image_core(
        event=event, prompt="draw", reference_images=[], avatar_reference=[]
    )

    assert success is True
    assert calls == [("bind", event, None), ("reset", token)]


@pytest.mark.asyncio
async def test_tool_call_generation_uses_plugin_total_timeout(monkeypatch) -> None:
    api_client = _RecordingAPIClient()
    generator = ImageGenerator(
        context=None,
        api_client=api_client,
        total_timeout=777,
    )
    monkeypatch.setattr("tl.image_generator.Path.exists", lambda self: True)

    success, _ = await generator.generate_image_core(
        event=None,
        prompt="draw",
        reference_images=[],
        avatar_reference=[],
        is_tool_call=True,
    )

    assert success is True
    assert api_client.kwargs["per_retry_timeout"] == 777
    assert api_client.kwargs["max_total_time"] == 777
