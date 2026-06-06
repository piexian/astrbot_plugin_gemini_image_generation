from __future__ import annotations

import base64

import pytest

from tl.napcat_stream import upload_file_stream


class _FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_action(self, action: str, **params):
        self.calls.append((action, params))
        if params.get("is_complete"):
            return {
                "status": "ok",
                "data": {
                    "status": "file_complete",
                    "file_path": "/app/napcat/temp/result.png",
                },
            }
        return {"status": "ok", "data": {"received_chunks": params["chunk_index"] + 1}}


class _FakeEvent:
    def __init__(self) -> None:
        self.bot = _FakeBot()


@pytest.mark.asyncio
async def test_upload_file_stream_uses_existing_bot_connection(tmp_path):
    target = tmp_path / "image.png"
    target.write_bytes(b"a" * 10)
    event = _FakeEvent()

    result = await upload_file_stream(event, target, chunk_size=4)

    assert result == "/app/napcat/temp/result.png"
    assert len(event.bot.calls) == 4
    assert all(action == "upload_file_stream" for action, _ in event.bot.calls)
    assert event.bot.calls[-1][1] == {
        "stream_id": event.bot.calls[0][1]["stream_id"],
        "is_complete": True,
    }
    first_chunk = event.bot.calls[0][1]
    assert base64.b64decode(first_chunk["chunk_data"]) == b"aaaa"
    assert first_chunk["total_chunks"] == 3
    assert first_chunk["file_size"] == 10
    assert first_chunk["filename"] == "image.png"


@pytest.mark.asyncio
async def test_upload_file_stream_returns_none_without_bot(tmp_path):
    target = tmp_path / "image.png"
    target.write_bytes(b"image")

    result = await upload_file_stream(object(), target)

    assert result is None
