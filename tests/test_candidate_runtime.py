from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from tl.api_types import APIError, ApiRequestConfig
from tl.key_manager import KeyManager
from tl.tl_api import GeminiAPIClient


@dataclass
class _Candidate:
    id: str
    api_type: str
    model: str
    settings: dict
    api_base: str = ""
    model_alias: str | None = None

    @property
    def api_keys(self) -> list[str]:
        return self.settings.get("api_keys") or []

    @property
    def proxy(self) -> str | None:
        return self.settings.get("proxy")


@dataclass
class _Config:
    provider_overrides: dict[str, dict]


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
    today = date.today().isoformat()

    async def get_kv(key, default):
        return {
            "google#1": {
                "keys": {
                    "bad-key": {
                        "usage_count": "not-a-number",
                        "last_reset_date": today,
                    },
                    "good-key": {
                        "usage_count": "3",
                        "last_reset_date": today,
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


@pytest.mark.asyncio
async def test_candidate_polling_shares_total_timeout_across_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GeminiAPIClient(["fallback"])
    first = _Candidate(
        id="google#1",
        api_type="google",
        model="gemini-3-pro-image-preview",
        settings={"api_keys": ["candidate-key"]},
    )
    second = _Candidate(
        id="google#2",
        api_type="google",
        model="gemini-3-pro-image-preview",
        settings={"api_keys": ["candidate-key"]},
    )
    client.set_provider_candidates([first, second])
    original_config = ApiRequestConfig(model="", prompt="test", api_type="")
    attempted: list[tuple[str, int | None]] = []

    class _FakeLoop:
        def __init__(self) -> None:
            self.times = iter([100.0, 100.0, 107.0])

        def time(self) -> float:
            return next(self.times, 107.0)

    async def fake_generate_image_single(**kwargs):
        attempted.append((kwargs["config"].candidate_id, kwargs.get("max_total_time")))
        raise APIError("fail", 500, "server_error")

    monkeypatch.setattr("tl.tl_api.asyncio.get_running_loop", lambda: _FakeLoop())
    client._generate_image_single = fake_generate_image_single  # type: ignore[method-assign]

    with pytest.raises(APIError, match="fail"):
        await client._generate_image_with_candidates(
            original_config,
            max_total_time=10,
        )

    assert attempted == [("google#1", 10), ("google#2", 3)]
