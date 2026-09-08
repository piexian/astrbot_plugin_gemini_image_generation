from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tl.api.openai_responses import OpenAIResponsesProvider, read_responses_response
from tl.api.registry import get_api_provider
from tl.api_types import APIError, ApiRequestConfig
from tl.tl_api import GeminiAPIClient


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


def config(**kwargs):
    return ApiRequestConfig(
        model=kwargs.pop("model", ""),
        prompt="画一只猫",
        api_type="openai_responses",
        api_key="test",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_defaults_and_free_model_names():
    provider = get_api_provider("openai_responses")
    assert isinstance(provider, OpenAIResponsesProvider)
    req = await provider.build_request(client=object(), config=config())
    assert req.payload["model"] == "gpt-5.6-luna"
    assert req.payload["tools"][0]["model"] == "gpt-image-2.5-flare"
    req = await provider.build_request(
        client=object(),
        config=config(
            model="my-custom-image",
            api_base="https://gateway.test/v1/responses",
            provider_settings={
                "base_model": "my-custom-router",
                "size_mode": "custom",
                "custom_size": "1024x1024",
            },
        ),
    )
    assert req.url == "https://gateway.test/v1/responses"
    assert req.payload["model"] == "my-custom-router"
    assert req.payload["tools"][0]["model"] == "my-custom-image"
    assert req.payload["tools"][0]["size"] == "1024x1024"
    assert req.payload["tool_choice"] == {"type": "image_generation"}
    assert req.payload["store"] is False
    assert "max_output_tokens" not in req.payload


@pytest.mark.asyncio
async def test_reference_images_are_input_blocks(monkeypatch):
    resolve = AsyncMock(return_value=["data:image/png;base64,aW1hZ2U="])
    monkeypatch.setattr("tl.api.openai_responses.resolve_reference_api_values", resolve)
    req = await OpenAIResponsesProvider().build_request(
        client=object(), config=config(reference_images=["/test.png"])
    )
    assert req.payload["input"][0]["content"][1] == {
        "type": "input_image",
        "image_url": "data:image/png;base64,aW1hZ2U=",
    }


@pytest.mark.asyncio
async def test_completed_stream_routes_through_client_and_saves_once(monkeypatch):
    item = {"id": "img1", "type": "image_generation_call", "result": "aW1hZ2U="}
    response = Response(
        [
            {
                "type": "response.image_generation_call.partial_image",
                "partial_image_b64": "ignored",
            },
            {"type": "response.output_item.done", "item": item},
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [item],
                    "usage": {"input_tokens": 7, "output_tokens": 9},
                },
            },
        ]
    )
    save = AsyncMock(return_value="/saved/image.png")
    monkeypatch.setattr("tl.api.openai_responses.save_base64_image", save)
    session = SimpleNamespace(post=lambda *a, **k: response)
    client = GeminiAPIClient(["test"])
    request_config = config()
    result = await client._perform_request(
        session,
        "https://gateway.test/v1/responses",
        {},
        {},
        "openai_responses",
        "custom",
        timeout=30,
        request_config=request_config,
    )
    assert result[:2] == (["/saved/image.png"], ["/saved/image.png"])
    save.assert_awaited_once_with("aW1hZ2U=", "png")
    assert request_config.token_usage["input_tokens"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "events",
    [
        [],
        [
            {
                "type": "response.output_item.done",
                "item": {"type": "image_generation_call", "id": "img", "result": "abc"},
            }
        ],
        [{"type": "response.incomplete"}],
    ],
)
async def test_unfinished_stream_stops_retry(events):
    with pytest.raises(APIError) as exc:
        await read_responses_response(response=Response(events))
    assert exc.value.error_type == "outcome_unknown"
    assert exc.value.retryable is False


@pytest.mark.asyncio
async def test_failed_stream_is_not_success():
    data = await read_responses_response(
        response=Response(
            [
                {
                    "type": "response.failed",
                    "response": {"error": {"message": "generation failed"}},
                }
            ]
        )
    )
    with pytest.raises(APIError, match="generation failed"):
        await OpenAIResponsesProvider().parse_response(
            client=object(), session=None, response_data=data
        )


def test_schema_models_are_free_text():
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text()
    )
    fields = schema["provider_settings"]["items"]["provider_overrides"]["templates"][
        "openai_responses"
    ]["items"]
    for name, default in [
        ("base_model", "gpt-5.6-luna"),
        ("model", "gpt-image-2.5-flare"),
    ]:
        assert fields[name]["type"] == "string"
        assert fields[name]["default"] == default
        assert "options" not in fields[name]


@pytest.mark.asyncio
async def test_transport_timeout_does_not_resubmit(monkeypatch):
    client = GeminiAPIClient(["test"])
    monkeypatch.setattr(client, "_get_session", AsyncMock(return_value=object()))
    perform = AsyncMock(side_effect=asyncio.TimeoutError())
    monkeypatch.setattr(client, "_perform_request", perform)
    with pytest.raises(APIError) as exc:
        await client._make_request(
            "https://gateway.test/v1/responses",
            {},
            {},
            "openai_responses",
            "custom",
            max_retries=3,
            config=config(),
        )
    assert exc.value.error_type == "outcome_unknown"
    assert perform.await_count == 1


@pytest.mark.asyncio
async def test_unknown_result_does_not_switch_candidate(monkeypatch):
    client = GeminiAPIClient(["test"])
    candidates = [
        SimpleNamespace(id=name, api_type="openai_responses", api_keys=["test"])
        for name in ["a", "b"]
    ]
    monkeypatch.setattr("tl.tl_api.select_candidates", lambda *a, **k: candidates)
    monkeypatch.setattr(
        client, "_build_candidate_config", lambda request, candidate: request
    )
    generate = AsyncMock(
        side_effect=APIError("unknown", error_type="outcome_unknown", retryable=False)
    )
    monkeypatch.setattr(client, "_generate_image_single", generate)
    with pytest.raises(APIError) as exc:
        await client._generate_image_with_candidates(config())
    assert exc.value.error_type == "outcome_unknown"
    assert generate.await_count == 1
