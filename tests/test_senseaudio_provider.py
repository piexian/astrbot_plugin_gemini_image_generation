from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from tl.api.senseaudio import (
    DEFAULT_MODEL,
    MODEL_SIZES,
    SenseAudioProvider,
    _resolve_size,
)
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


class _Response:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return json.dumps(self.data)


class _Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_request_protocol(mode):
    request = await SenseAudioProvider().build_request(
        client=_client(),
        config=_config(
            api_base="https://relay.example/v1/",
            resolution="2K",
            aspect_ratio="16:9",
            provider_settings={"request_mode": mode, "seed": "0"},
        ),
    )
    assert request.url == f"https://relay.example/v1/image/{mode}"
    assert request.headers == {
        "Authorization": "Bearer test-key",
        "Content-Type": "application/json",
    }
    assert request.payload == {
        "model": DEFAULT_MODEL,
        "prompt": "画一只猫",
        "size": "2048x1152",
        "seed": 0,
    }


@pytest.mark.asyncio
async def test_default_and_request_seed_override():
    provider = SenseAudioProvider()
    request = await provider.build_request(client=_client(), config=_config())
    assert request.url == "https://api.senseaudio.cn/v1/image/sync"
    assert request.payload["size"] == "1024x1024"
    assert "seed" not in request.payload
    request = await provider.build_request(
        client=_client(), config=_config(seed=0, provider_settings={"seed": 42})
    )
    assert request.payload["seed"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference,mode",
    [
        ("https://cdn.example/ref.png", "url"),
        ("data:image/png;base64,QUJD", "force_base64"),
    ],
)
async def test_single_reference_and_preserve_size(reference, mode):
    request = await SenseAudioProvider().build_request(
        client=_client(),
        config=_config(
            reference_images=[reference, "https://cdn.example/ignored.png"],
            image_input_mode=mode,
            suppress_resolution=True,
            provider_settings={"size": "1536x864"},
        ),
    )
    assert request.payload["reference"] == reference
    assert "size" not in request.payload


@pytest.mark.asyncio
async def test_local_reference_uses_shared_conversion_and_proxy():
    client = _client()
    client._normalize_reference_image_input = AsyncMock(
        return_value=("image/png", "QUJD")
    )
    request = await SenseAudioProvider().build_request(
        client=client,
        config=_config(
            reference_images=["/tmp/reference.png"],
            proxy="http://proxy:8080",
        ),
    )
    assert request.payload["reference"] == "data:image/png;base64,QUJD"
    assert (
        client._normalize_reference_image_input.call_args.kwargs["request_proxy"]
        == "http://proxy:8080"
    )


@pytest.mark.asyncio
async def test_text_generation_still_requires_size_when_suppressed():
    request = await SenseAudioProvider().build_request(
        client=_client(), config=_config(suppress_resolution=True)
    )
    assert request.payload["size"] == "1024x1024"


@pytest.mark.parametrize(
    "model,expected",
    [
        (DEFAULT_MODEL, "1024x1024"),
        ("senseaudio-image-1.0-260319", "1328x1328"),
        ("doubao-seedream-5-0-260128", "3072x3072"),
        ("sensenova-u1-fast", "2048x2048"),
    ],
)
def test_model_specific_square_sizes(model, expected):
    assert _resolve_size(model, "4K", "1:1") == expected


@pytest.mark.parametrize("model", list(MODEL_SIZES))
def test_auto_sizes_always_use_documented_presets(model):
    for resolution in ("1K", "2K", "4K"):
        for ratio in ("1:1", "16:9", "9:16", "21:9", "1:8", "8:1", "4:5"):
            assert _resolve_size(model, resolution, ratio) in MODEL_SIZES[model]


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
async def test_fixed_size_overrides_mapping():
    request = await SenseAudioProvider().build_request(
        client=_client(), config=_config(provider_settings={"size": "3840x1648"})
    )
    assert request.payload["size"] == "3840x1648"


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy", [None, "http://proxy:8080"])
async def test_sync_url_response(proxy):
    client = _client(proxy)
    result = await SenseAudioProvider().parse_response(
        client=client,
        session=object(),
        response_data={"url": "https://cdn.example/output.png"},
        http_status=200,
        request_config=_config(),
    )
    assert result == (
        (["/tmp/result.png"], ["/tmp/result.png"], None, None)
        if proxy
        else (["https://cdn.example/output.png"], [], None, None)
    )
    if proxy:
        assert client._download_image.call_args.kwargs["proxy"] == proxy


@pytest.mark.asyncio
async def test_async_pending_then_completed_keeps_task_key_and_proxy(monkeypatch):
    monkeypatch.setattr("tl.api.senseaudio.asyncio.sleep", AsyncMock())
    session = _Session(
        _Response({"status": "pending"}),
        _Response({}, 500),
        aiohttp.ClientConnectionError(),
        _Response({}, 429),
        _Response({"status": "completed", "url": "https://cdn.example/a.png"}),
    )
    config = _config(
        api_key="submission-key",
        api_base="https://relay.example/v1",
        provider_settings={"request_mode": "async"},
    )
    result = await SenseAudioProvider().parse_response(
        client=_client("http://proxy:8080"),
        session=session,
        response_data={"task_id": "task&1"},
        request_config=config,
        http_status=200,
    )
    assert result[1] == ["/tmp/result.png"]
    assert len(session.calls) == 5
    for url, kwargs in session.calls:
        assert url == "https://relay.example/v1/image/pending"
        assert kwargs["params"] == {"task_id": "task&1"}
        assert kwargs["headers"]["Authorization"] == "Bearer submission-key"
        assert kwargs["proxy"] == "http://proxy:8080"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,error_type",
    [
        (_Response({"status": "failed", "error_message": "生成失败"}), "api_error"),
        (_Response({"message": "不存在", "ref_code": "404000"}, 404), "api_error"),
        (_Response({"message": "未认证"}, 401), "api_error"),
        (_Response({"status": "unknown"}), "invalid_response"),
        (_Response([]), "invalid_response"),
        (_Response({"status": "completed"}), "no_image"),
    ],
)
async def test_poll_failure_does_not_resubmit(response, error_type):
    with pytest.raises(APIError) as exc:
        await SenseAudioProvider().parse_response(
            client=_client(),
            session=_Session(response),
            response_data={"task_id": "task"},
            request_config=_config(provider_settings={"request_mode": "async"}),
        )
    assert exc.value.error_type == error_type
    assert exc.value.retryable is False


@pytest.mark.asyncio
async def test_poll_obeys_total_deadline():
    session = _Session(_Response({"status": "pending"}))
    config = _config(
        provider_settings={"request_mode": "async"},
        request_deadline=asyncio.get_running_loop().time() + 0.02,
    )
    with pytest.raises(APIError) as exc:
        await SenseAudioProvider().parse_response(
            client=_client(),
            session=session,
            response_data={"task_id": "task"},
            request_config=config,
        )
    assert exc.value.error_type == "timeout"
    assert exc.value.retryable is False
    assert len(session.calls) == 1
    assert session.calls[0][1]["timeout"].total <= 0.02


@pytest.mark.asyncio
async def test_download_failure_does_not_repeat_generation():
    client = _client("http://proxy:8080")
    client._download_image.return_value = (None, None)
    with pytest.raises(APIError) as exc:
        await SenseAudioProvider().parse_response(
            client=client,
            session=object(),
            response_data={"url": "https://cdn.example/a.png"},
            request_config=_config(),
        )
    assert exc.value.error_type == "download_error"
    assert exc.value.retryable is False


@pytest.mark.asyncio
async def test_poll_cancellation_propagates():
    with pytest.raises(asyncio.CancelledError):
        await SenseAudioProvider().parse_response(
            client=_client(),
            session=_Session(asyncio.CancelledError()),
            response_data={"task_id": "task"},
            request_config=_config(provider_settings={"request_mode": "async"}),
        )


@pytest.mark.asyncio
async def test_submit_error_retains_code():
    with pytest.raises(APIError) as exc:
        await SenseAudioProvider().parse_response(
            client=_client(),
            session=object(),
            response_data={"message": "繁忙", "ref_code": "500000"},
            http_status=500,
        )
    assert exc.value.error_code == "500000"
    assert exc.value.status_code == 500
    assert exc.value.retryable is None


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True])
async def test_missing_submit_result_does_not_resubmit(async_mode):
    with pytest.raises(APIError) as exc:
        await SenseAudioProvider().parse_response(
            client=_client(),
            session=object(),
            response_data={},
            request_config=_config(
                provider_settings={"request_mode": "async" if async_mode else "sync"}
            ),
        )
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
