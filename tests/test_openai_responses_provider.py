from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tl.api.openai_responses import OpenAIResponsesProvider
from tl.api_types import APIError, ApiRequestConfig
from tl.tl_api import GeminiAPIClient


def config(**kwargs):
    return ApiRequestConfig(
        model=kwargs.pop("model", ""),
        prompt="画一只猫",
        api_type="openai_responses",
        api_key="test",
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model, settings",
    [
        ("gpt-image-2", {"quality": "xhigh"}),
        ("gpt-image-2.5-flare", {"background": "transparent", "output_format": "jpeg"}),
        ("gpt-image-2.5-flare", {"output_compression": 101}),
        ("gpt-image-2.5-flare", {"partial_images": 4}),
        ("gpt-image-2.5-flare", {"action": "edit"}),
    ],
)
async def test_invalid_output_combinations_fail_before_request(model, settings):
    with pytest.raises(APIError) as error:
        await OpenAIResponsesProvider().build_request(
            client=object(), config=config(model=model, provider_settings=settings)
        )
    assert error.value.retryable is False


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
