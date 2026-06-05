from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from tl.api_types import ApiRequestConfig
from tl.api_types import APIError
from tl.key_manager import KeyManager
from tl.tl_api import GeminiAPIClient


@dataclass
class _Candidate:
    id: str
    api_type: str
    model: str
    settings: dict
    api_base: str = ""

    @property
    def api_keys(self) -> list[str]:
        return self.settings.get("api_keys") or []

    @property
    def proxy(self) -> str | None:
        return self.settings.get("proxy")


@dataclass
class _Config:
    provider_overrides: dict[str, dict]


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_key_manager_shares_daily_usage_for_same_key_across_candidates() -> None:
    manager = KeyManager(
        _Config(
            provider_overrides={
                "google#1": {"api_keys": ["same-key"], "daily_limit_per_key": 1},
                "google#2": {"api_keys": ["same-key"], "daily_limit_per_key": 1},
            }
        )
    )

    first_key = await manager.get_available_key("google#1")
    second_key = await manager.get_available_key("google#2")

    assert first_key == "same-key"
    assert second_key is None


@pytest.mark.asyncio
async def test_key_manager_keeps_same_key_separate_across_provider_types() -> None:
    manager = KeyManager(
        _Config(
            provider_overrides={
                "google#1": {"api_keys": ["same-key"], "daily_limit_per_key": 1},
                "openai#1": {"api_keys": ["same-key"], "daily_limit_per_key": 1},
            }
        )
    )

    first_key = await manager.get_available_key("google#1")
    second_key = await manager.get_available_key("openai#1")

    assert first_key == "same-key"
    assert second_key == "same-key"


@pytest.mark.asyncio
async def test_key_manager_ignores_malformed_persisted_usage_count() -> None:
    async def get_kv(key, default):
        return {
            "google#1": {
                "keys": {
                    "bad-key": {
                        "usage_count": "not-a-number",
                        "last_reset_date": "2026-06-06",
                    },
                    "good-key": {
                        "usage_count": "3",
                        "last_reset_date": "2026-06-06",
                    },
                }
            }
        }

    manager = KeyManager(
        _Config(
            provider_overrides={
                "google#1": {
                    "api_keys": ["bad-key", "good-key"],
                    "daily_limit_per_key": 10,
                },
            }
        ),
        get_kv=get_kv,
    )

    await manager._load_from_kv()
    status = manager.get_key_status("google#1")

    assert status["keys"][0]["usage_today"] == 0
    assert status["keys"][1]["usage_today"] == 3


@pytest.mark.asyncio
async def test_candidate_polling_copies_stats_back_to_original_config() -> None:
    client = GeminiAPIClient(["fallback"])
    candidate = _Candidate(
        id="google#1",
        api_type="google",
        model="gemini-3-pro-image-preview",
        settings={"api_keys": ["candidate-key"]},
    )
    client.set_provider_candidates([candidate])
    original_config = ApiRequestConfig(model="", prompt="test", api_type="")

    async def fake_generate_image_single(**kwargs):
        candidate_config = kwargs["config"]
        candidate_config.retry_count = 2
        candidate_config.token_usage = {"total_tokens": 11}
        candidate_config.retry_note = "重试 2 次后成功"
        return ["url"], ["path"], "text", None

    client._generate_image_single = fake_generate_image_single  # type: ignore[method-assign]

    result = await client._generate_image_with_candidates(original_config)

    assert result == (["url"], ["path"], "text", None)
    assert original_config.retry_count == 2
    assert original_config.token_usage == {"total_tokens": 11}
    assert original_config.retry_note == "重试 2 次后成功"
    assert original_config.api_type == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", ["cancelled", "timeout"])
async def test_candidate_polling_stops_on_framework_timeout_errors(
    error_type: str,
) -> None:
    client = GeminiAPIClient(["fallback"])
    first = _Candidate(
        id="google#1",
        api_type="google",
        model="gemini-3-pro-image-preview",
        settings={"api_keys": ["candidate-key"]},
    )
    second = _Candidate(
        id="openai#1",
        api_type="openai",
        model="gpt-image",
        settings={"api_keys": ["candidate-key"]},
    )
    client.set_provider_candidates([first, second])
    original_config = ApiRequestConfig(model="", prompt="test", api_type="")
    attempted: list[str] = []

    async def fake_generate_image_single(**kwargs):
        attempted.append(kwargs["config"].candidate_id)
        raise APIError("stop", None, error_type)

    client._generate_image_single = fake_generate_image_single  # type: ignore[method-assign]

    with pytest.raises(APIError, match="stop") as exc_info:
        await client._generate_image_with_candidates(original_config)

    assert exc_info.value.error_type == error_type
    assert attempted == ["google#1"]


def test_candidate_config_uses_request_level_settings_and_proxy() -> None:
    client = GeminiAPIClient(["fallback"])
    candidate = _Candidate(
        id="openai_images#1",
        api_type="openai_images",
        model="gpt-image-1",
        settings={
            "api_keys": ["candidate-key"],
            "size_mode": "custom",
            "custom_size": "1536x1024",
            "proxy": "http://proxy.local:8080",
        },
    )
    config = ApiRequestConfig(model="", prompt="test", api_type="")

    candidate_config = client._build_candidate_config(config, candidate)

    assert candidate_config.provider_settings is candidate.settings
    assert candidate_config.proxy == "http://proxy.local:8080"


@pytest.mark.asyncio
async def test_invalidate_session_closes_proxy_sessions() -> None:
    client = GeminiAPIClient(["fallback"])
    default_session = _FakeSession()
    proxy_session = _FakeSession()
    client._session = default_session  # type: ignore[assignment]
    client._proxy_sessions["socks5://127.0.0.1:1080"] = proxy_session  # type: ignore[assignment]

    client.invalidate_session()
    await asyncio.sleep(0)

    assert client._session is None
    assert client._proxy_sessions == {}
    assert default_session.closed is True
    assert proxy_session.closed is True
