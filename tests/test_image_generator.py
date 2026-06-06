from __future__ import annotations

import pytest

from tl.image_generator import DEFAULT_MAX_REFERENCE_IMAGES, ImageGenerator


class _FakeAPIClient:
    def __init__(self) -> None:
        self.config = None

    async def generate_image(self, *, config, **kwargs):
        self.config = config
        return [], ["/tmp/generated.png"], None, None


def _keep_all(images: list[str] | None, source: str) -> list[str]:
    return images or []


@pytest.mark.asyncio
async def test_avatar_note_uses_truncated_reference_images(monkeypatch) -> None:
    api_client = _FakeAPIClient()
    generator = ImageGenerator(
        context=None,
        api_client=api_client,
        max_reference_images=6,
        filter_valid_fn=_keep_all,
    )
    monkeypatch.setattr("tl.image_generator.Path.exists", lambda self: True)

    success, _ = await generator.generate_image_core(
        event=None,
        prompt="draw",
        reference_images=[f"msg-{idx}" for idx in range(10)],
        avatar_reference=[f"avatar-{idx}" for idx in range(3)],
    )

    assert success is True
    assert api_client.config.reference_images == [f"msg-{idx}" for idx in range(6)]
    assert "User Avatars" not in api_client.config.prompt


@pytest.mark.asyncio
async def test_avatar_note_counts_only_retained_avatars(monkeypatch) -> None:
    api_client = _FakeAPIClient()
    generator = ImageGenerator(
        context=None,
        api_client=api_client,
        max_reference_images=4,
        filter_valid_fn=_keep_all,
    )
    monkeypatch.setattr("tl.image_generator.Path.exists", lambda self: True)

    success, _ = await generator.generate_image_core(
        event=None,
        prompt="draw",
        reference_images=["msg-1", "msg-2"],
        avatar_reference=["avatar-1", "avatar-2", "avatar-3"],
    )

    assert success is True
    assert api_client.config.reference_images == [
        "msg-1",
        "msg-2",
        "avatar-1",
        "avatar-2",
    ]
    assert "The last 2 image(s) provided are User Avatars" in api_client.config.prompt


def test_image_generator_invalid_max_reference_images_falls_back_to_default() -> None:
    generator = ImageGenerator(
        context=None,
        api_client=None,
        max_reference_images="bad",
    )

    assert generator.max_reference_images == DEFAULT_MAX_REFERENCE_IMAGES
