from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tl.api.senseaudio import DEFAULT_MODEL, SenseAudioProvider
from tl.api_types import APIError, ApiRequestConfig
from tl.provider_capabilities import candidate_capability, candidate_reference_limit


def _config(**kwargs):
    return ApiRequestConfig(
        **{
            "model": DEFAULT_MODEL,
            "prompt": "画一只猫",
            "api_type": "senseaudio",
            "api_key": "test-key",
            "resolution": "1K",
            "aspect_ratio": "1:1",
            **kwargs,
        }
    )


def _client(proxy=None):
    return SimpleNamespace(
        _request_has_proxy=lambda config: bool(proxy),
        _request_http_proxy=lambda config: proxy,
        _download_image=AsyncMock(return_value=(None, "/tmp/result.png")),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides,error",
    [
        ({"model": "unsupported"}, "invalid_model"),
        ({"api_key": None}, "missing_api_key"),
        ({"prompt": " "}, "empty_prompt"),
        ({"prompt": "猫" * 6001}, "prompt_too_long"),
        ({"model": "sensenova-u1-fast", "prompt": "猫" * 2001}, "prompt_too_long"),
        ({"provider_settings": {"size": "2048x2048"}}, "invalid_size"),
        ({"provider_settings": {"request_mode": "other"}}, "invalid_request"),
        ({"provider_settings": {"seed": "1.5"}}, "invalid_request"),
        ({"reference_images": [""]}, "invalid_reference_image"),
    ],
)
async def test_invalid_input_is_not_retryable(overrides, error):
    with pytest.raises(APIError) as exc:
        await SenseAudioProvider().build_request(
            client=_client(), config=_config(**overrides)
        )
    assert exc.value.error_type == error
    assert exc.value.retryable is False


@pytest.mark.asyncio
async def test_schema_defaults_load_as_candidate_and_build_request():
    from tl.api.registry import get_api_provider
    from tl.plugin_config import ConfigLoader
    from tl.studio_parameters import generation_fields

    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text()
    )
    settings_schema = schema["provider_settings"]["items"]
    fields = settings_schema["provider_overrides"]["templates"]["senseaudio"]["items"]
    settings = {key: value["default"] for key, value in fields.items()}
    settings["api_keys"] = ["test-key"]
    settings["__template_key"] = "senseaudio"
    cfg = ConfigLoader(
        {
            "provider_settings": {
                "provider_polling": ["senseaudio"],
                "provider_overrides": [settings],
            }
        }
    ).load()
    assert not cfg.provider_config_errors
    candidate = cfg.provider_candidates[0]
    assert candidate.api_type == "senseaudio"
    assert candidate.supports_image_edit
    assert candidate_reference_limit(candidate) == 1
    candidate.settings["max_reference_images"] = 99
    assert candidate_reference_limit(candidate) == 1
    assert "senseaudio" in settings_schema["provider_polling"]["options"]
    request = await get_api_provider("senseaudio").build_request(
        client=_client(),
        config=_config(model=candidate.model, provider_settings=candidate.settings),
    )
    assert request.payload["size"] == "1024x1024"
    assert "seed" not in request.payload
    cap = candidate_capability(candidate)
    assert cap["native_batch_limit"] == 1
    assert cap["parameters"]["resolution"]["enum"] == ["1K", "2K", "4K"]
    assert generation_fields(candidate)["max_reference_images"]["maximum"] == 1
    candidate.settings["model"] = "senseaudio-image-1.0-260319"
    assert generation_fields(candidate)["resolution"]["enum"] == ["1K"]
    candidate.settings["size"] = "1328x1328"
    assert "resolution" not in generation_fields(candidate)
