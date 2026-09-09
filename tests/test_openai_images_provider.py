from __future__ import annotations

import base64
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from test_openai_responses_provider import Response

from tl.api.openai_images import OpenAIImagesProvider, read_images_response
from tl.api_types import APIError, ApiRequestConfig
from tl.provider_capabilities import candidate_reference_limit, openai_images_capability


def config(**overrides):
    return ApiRequestConfig(
        model=overrides.pop("model", "gpt-image-2.5-flare"),
        prompt="draw",
        api_type="openai_images",
        api_key="test",
        **overrides,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model", ["gpt-image-2.5-flare", "gpt-image-2.5-sunburst-2026-09-08"]
)
async def test_new_model_generation_options(model):
    request = await OpenAIImagesProvider().build_request(
        client=object(),
        config=config(
            model=model,
            resolution="2K",
            aspect_ratio="16:9",
            seed=123,
            provider_settings={
                "n": 2,
                "quality": "max",
                "background": "transparent",
                "output_format": "webp",
                "output_compression": 0,
                "response_format": "url",
            },
        ),
    )
    assert request.payload["model"] == model
    assert request.payload["size"] == "2048x1152"
    assert request.payload["quality"] == "max"
    assert request.payload["n"] == 2
    assert request.payload["output_compression"] == 0
    assert request.payload["background"] == "transparent"
    assert "response_format" not in request.payload
    assert "seed" not in request.payload


@pytest.mark.asyncio
async def test_edit_forwards_output_options_and_uses_image_array():
    output = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(output, format="JPEG")
    ref = base64.b64encode(output.getvalue()).decode()
    request = await OpenAIImagesProvider().build_request(
        client=object(),
        config=config(
            reference_images=[ref, ref],
            provider_settings={
                "n": 1,
                "quality": "xhigh",
                "background": "transparent",
                "output_format": "webp",
                "output_compression": 0,
                "moderation": "low",
                "stream": True,
                "partial_images": 2,
                "size_mode": "auto",
            },
        ),
    )
    fields = request.payload["_form_data"]._fields
    images = [field for field in fields if field[0]["name"] == "image[]"]
    assert len(images) == 2
    assert all(field[1]["Content-Type"] == "image/jpeg" for field in images)
    options = {
        field[0]["name"]: field[2] for field in fields if field[0]["name"] != "image[]"
    }
    for key, value in {
        "quality": "xhigh",
        "background": "transparent",
        "output_format": "webp",
        "output_compression": "0",
        "moderation": "low",
        "stream": "true",
        "partial_images": "2",
        "size": "auto",
    }.items():
        assert options[key] == value
    assert "response_format" not in options


@pytest.mark.asyncio
async def test_dalle_keeps_legacy_parameters():
    request = await OpenAIImagesProvider().build_request(
        client=object(),
        config=config(
            model="dall-e-3",
            resolution="2K",
            provider_settings={
                "quality": "hd",
                "style": "vivid",
                "response_format": "url",
            },
        ),
    )
    assert request.payload["size"] == "1792x1024"
    assert request.payload["response_format"] == "url"
    assert request.payload["quality"] == "hd"


@pytest.mark.asyncio
async def test_native_stream_parses_completed_image_and_format(monkeypatch):
    response = Response(
        [
            {"type": "image_generation.partial_image", "b64_json": "preview"},
            {
                "type": "image_generation.completed",
                "b64_json": "final",
                "output_format": "webp",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        ]
    )
    data = await read_images_response(response=response)
    save = AsyncMock(return_value="/tmp/final.webp")
    monkeypatch.setattr("tl.api.openai_images.save_base64_image", save)
    await OpenAIImagesProvider().parse_response(
        client=object(), session=None, response_data=data
    )
    save.assert_awaited_once_with("final", "webp")
    assert data["usage"]["output_tokens"] == 2


@pytest.mark.asyncio
async def test_native_stream_without_completion_is_not_retried():
    with pytest.raises(APIError) as error:
        await read_images_response(
            response=Response(
                [{"type": "image_edit.partial_image", "b64_json": "preview"}]
            )
        )
    assert error.value.error_type == "outcome_unknown"


@pytest.mark.asyncio
async def test_json_base64_with_null_url_uses_requested_format(monkeypatch):
    save = AsyncMock(return_value="/tmp/final.jpeg")
    monkeypatch.setattr("tl.api.openai_images.save_base64_image", save)
    result = await OpenAIImagesProvider().parse_response(
        client=object(),
        session=None,
        response_data={"data": [{"url": None, "b64_json": "final"}]},
        request_config=config(provider_settings={"output_format": "jpeg"}),
    )
    save.assert_awaited_once_with("final", "jpeg")
    assert result[1] == ["/tmp/final.jpeg"]


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
