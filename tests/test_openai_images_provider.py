from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tl.api.openai_images import OpenAIImagesProvider, read_images_response
from tl.api_types import APIError, ApiRequestConfig
from tl.provider_capabilities import candidate_reference_limit, openai_images_capability


class Response:
    status = 200
    headers = {"Content-Type": "text/event-stream"}

    def __init__(self, events, *, chunk_size=7):
        self.raw = "".join(
            f"event: {event['type']}\r\nid: 1\r\ndata: {json.dumps(event, ensure_ascii=False)}\r\n\r\n"
            for event in events
        ).encode()
        self.chunk_size = chunk_size
        self.content = self

    async def iter_any(self):
        for pos in range(0, len(self.raw), self.chunk_size):
            yield self.raw[pos : pos + self.chunk_size]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def config(**overrides):
    return ApiRequestConfig(
        model=overrides.pop("model", "gpt-image-2.5-flare"),
        prompt="draw",
        api_type="openai_images",
        api_key="test",
        **overrides,
    )


@pytest.mark.asyncio
async def test_native_stream_without_completion_is_not_retried():
    with pytest.raises(APIError) as error:
        await read_images_response(
            response=Response(
                [{"type": "image_edit.partial_image", "b64_json": "preview"}]
            )
        )
    assert error.value.error_type == "outcome_unknown"


def test_native_batch_and_reference_limits():
    candidate = SimpleNamespace(
        api_type="openai_images",
        model="gpt-image-2.5-flare",
        supports_image_edit=True,
        settings={"n": 10, "max_reference_images": 99},
    )
    assert openai_images_capability(candidate)["native_batch_limit"] == 10
    assert candidate_reference_limit(candidate) == 16
    candidate.settings["stream"] = True
    assert openai_images_capability(candidate)["native_batch_limit"] == 1


@pytest.mark.asyncio
async def test_invalid_stream_batch_is_rejected():
    with pytest.raises(APIError):
        await OpenAIImagesProvider().build_request(
            client=object(), config=config(provider_settings={"n": 2, "stream": True})
        )
