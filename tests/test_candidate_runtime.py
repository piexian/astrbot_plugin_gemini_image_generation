from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from tl.api.base import ProviderRequest
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


class _FakeImageResponse:
    status = 200
    headers = {"Content-Type": "image/png"}
    content = object()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeImageDownloadSession:
    def __init__(self) -> None:
        self.proxy_seen: Any = None

    def get(self, *args, **kwargs):
        self.proxy_seen = kwargs.get("proxy")
        return _FakeImageResponse()


class _FakeJsonResponse:
    def __init__(self, status: int, payload: str) -> None:
        self.status = status
        self.headers = {"Content-Type": "application/json"}
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self) -> str:
        return self._payload


class _FakePostSession:
    def __init__(self, response: _FakeJsonResponse) -> None:
        self.response = response

    def post(self, *args, **kwargs):
        return self.response


@pytest.mark.asyncio
async def test_qq_reference_image_avoids_reusing_proxied_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GeminiAPIClient(["fallback"])
    client.proxy = "socks5://proxy.local:1080"
    captured: dict[str, Any] = {}

    async def fail_get_session(proxy: str | None = None):
        raise AssertionError("QQ reference downloads should use a direct session")

    async def fake_normalize_reference_image_input(*args, **kwargs):
        captured.update(kwargs)
        return "image/png", "encoded"

    monkeypatch.setattr(client, "_get_session", fail_get_session)
    monkeypatch.setattr(
        "tl.tl_api.normalize_reference_image_input",
        fake_normalize_reference_image_input,
    )

    mime_type, encoded = await client._normalize_reference_image_input(
        "https://gchat.qpic.cn/gchatpic_new/0/0-0.png"
    )

    assert mime_type == "image/png"
    assert encoded == "encoded"
    assert captured["session"] is None
    assert captured["proxy"] is None


def test_gitignore_does_not_hide_tests_directory() -> None:
    gitignore = Path(__file__).resolve().parents[1] / ".gitignore"
    ignored_entries = {
        line.strip()
        for line in gitignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "tests/" not in ignored_entries
    assert "tests" not in ignored_entries


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
async def test_candidate_proxy_is_used_for_response_image_downloads() -> None:
    client = GeminiAPIClient(["fallback"])
    captured: dict[str, str | None] = {}
    config = ApiRequestConfig(
        model="gpt-image",
        prompt="test",
        api_type="openai",
        proxy="http://candidate-proxy.local:8080",
    )
    response_data = {"data": [{"url": "https://cdn.example/image.png"}]}

    async def fake_download_image(image_url, session, use_cache=False, proxy=None):
        captured["url"] = image_url
        captured["proxy"] = proxy
        return "/tmp/generated.png", "/tmp/generated.png"

    client._download_image = fake_download_image  # type: ignore[method-assign]

    image_urls, image_paths, _, _ = await client._parse_openai_response(
        response_data,
        session=None,  # type: ignore[arg-type]
        request_config=config,
    )

    assert captured == {
        "url": "https://cdn.example/image.png",
        "proxy": "http://candidate-proxy.local:8080",
    }
    assert image_urls == ["/tmp/generated.png"]
    assert image_paths == ["/tmp/generated.png"]


@pytest.mark.asyncio
async def test_download_image_respects_explicit_none_proxy_for_socks_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GeminiAPIClient(["fallback"])
    client.proxy = "http://global-proxy.local:8080"
    session = _FakeImageDownloadSession()

    async def fake_save_image_stream(*args, **kwargs):
        return "/tmp/generated.png"

    monkeypatch.setattr("tl.tl_api.save_image_stream", fake_save_image_stream)

    _, image_path = await client._download_image(
        "https://cdn.example/image.png",
        session,  # type: ignore[arg-type]
        use_cache=False,
        proxy=None,
    )

    assert image_path == "/tmp/generated.png"
    assert session.proxy_seen is None


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


@pytest.mark.asyncio
async def test_process_reference_image_prefers_normalize_without_temp_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GeminiAPIClient(["fallback"])

    async def fake_normalize(*args, **kwargs):
        return "image/png", "BASE64DATA"

    async def fail_resolve(*args, **kwargs):
        raise AssertionError("base64 fast path should not resolve a temp file")

    monkeypatch.setattr(client, "_normalize_reference_image_input", fake_normalize)
    monkeypatch.setattr(
        "tl.tl_api.resolve_image_source_to_path",
        fail_resolve,
        raising=False,
    )

    assert await client._process_reference_image("BASE64DATA", 0) == (
        "image/png",
        "BASE64DATA",
        False,
    )


@pytest.mark.asyncio
async def test_candidate_polling_skips_config_build_errors() -> None:
    client = GeminiAPIClient(["fallback"])
    bad = _Candidate(
        id="openai_images#bad",
        api_type="openai_images",
        model="gpt-image-1",
        settings={
            "api_keys": ["bad-key"],
            "size_mode": "custom",
            "custom_size": "2048x1080",
        },
    )
    good = _Candidate(
        id="google#1",
        api_type="google",
        model="gemini-3-pro-image-preview",
        settings={"api_keys": ["good-key"]},
    )
    client.set_provider_candidates([bad, good])
    original_config = ApiRequestConfig(model="", prompt="test", api_type="")
    attempted: list[str] = []

    async def fake_generate_image_single(**kwargs):
        candidate_config = kwargs["config"]
        attempted.append(candidate_config.candidate_id)
        return ["url"], ["path"], "text", None

    client._generate_image_single = fake_generate_image_single  # type: ignore[method-assign]

    result = await client._generate_image_with_candidates(original_config)

    assert result == (["url"], ["path"], "text", None)
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


def test_candidate_config_preserves_suppressed_reference_image_size() -> None:
    client = GeminiAPIClient(["fallback"])
    candidate = _Candidate(
        id="google#1",
        api_type="google",
        model="gemini-3-pro-image-preview",
        settings={
            "api_keys": ["candidate-key"],
            "resolution": "2K",
            "aspect_ratio": "16:9",
        },
    )
    config = ApiRequestConfig(
        model="",
        prompt="test",
        api_type="",
        resolution=None,
        aspect_ratio=None,
        reference_images=["ref"],
        suppress_resolution=True,
    )

    candidate_config = client._build_candidate_config(config, candidate)

    assert candidate_config.resolution is None
    assert candidate_config.aspect_ratio is None
    assert candidate_config.suppress_resolution is True


@pytest.mark.asyncio
async def test_retry_does_not_rebuild_request_for_plain_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GeminiAPIClient(["fallback"])
    config = ApiRequestConfig(model="model", prompt="test", api_type="google")
    attempts = 0

    async def fake_get_session(*args, **kwargs):
        return object()

    async def fake_perform_request(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise APIError("temporary", 500, "server_error")
        return ["url"], [], None, None

    async def fake_sleep(*args, **kwargs):
        return None

    def fail_get_api_provider(*args, **kwargs):
        raise AssertionError("plain provider retry should not rebuild request")

    monkeypatch.setattr(client, "_get_session", fake_get_session)
    monkeypatch.setattr(client, "_perform_request", fake_perform_request)
    monkeypatch.setattr("tl.tl_api.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("tl.tl_api.get_api_provider", fail_get_api_provider)

    result = await client._make_request(
        url="https://example.test",
        payload={},
        headers={},
        api_type="google",
        model="model",
        max_retries=2,
        config=config,
    )

    assert result == (["url"], [], None, None)
    assert attempts == 2


@pytest.mark.asyncio
async def test_minimax_retry_rebuild_receives_retry_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GeminiAPIClient(["fallback"])
    config = ApiRequestConfig(model="model", prompt="test", api_type="minimax")
    attempts = 0
    captured_retry_error: APIError | None = None

    class _Provider:
        async def build_request(self, **kwargs):
            nonlocal captured_retry_error
            captured_retry_error = kwargs.get("retry_error")
            return ProviderRequest(
                url="https://example.test/retry",
                headers={},
                payload={"rebuilt": True},
            )

    async def fake_get_session(*args, **kwargs):
        return object()

    async def fake_perform_request(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise APIError("minimax unknown", 500, "server_error", "1000")
        return ["url"], [], None, None

    async def fake_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(client, "_get_session", fake_get_session)
    monkeypatch.setattr(client, "_perform_request", fake_perform_request)
    monkeypatch.setattr("tl.tl_api.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("tl.tl_api.get_api_provider", lambda _api_type: _Provider())

    result = await client._make_request(
        url="https://example.test",
        payload={},
        headers={},
        api_type="minimax",
        model="model",
        max_retries=2,
        config=config,
    )

    assert result == (["url"], [], None, None)
    assert isinstance(captured_retry_error, APIError)
    assert captured_retry_error.error_code == "1000"


@pytest.mark.asyncio
async def test_doubao_non_200_response_uses_provider_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GeminiAPIClient(["fallback"])
    config = ApiRequestConfig(model="model", prompt="test", api_type="doubao")
    captured: dict[str, Any] = {}

    class _Provider:
        async def parse_response(self, **kwargs):
            captured.update(kwargs)
            return ["parsed"], [], None, None

    monkeypatch.setattr("tl.tl_api.get_api_provider", lambda _api_type: _Provider())

    result = await client._perform_request(
        _FakePostSession(_FakeJsonResponse(500, '{"error": {"message": "boom"}}')),
        "https://example.test",
        {"_is_retry": True},
        {},
        "doubao",
        "model",
        request_config=config,
    )

    assert result == (["parsed"], [], None, None)
    assert captured["http_status"] == 500
    assert captured["is_retry"] is True


@pytest.mark.asyncio
async def test_success_response_uses_provider_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GeminiAPIClient(["fallback"])
    config = ApiRequestConfig(model="model", prompt="test", api_type="openai")
    captured: dict[str, Any] = {}

    class _Provider:
        async def parse_response(self, **kwargs):
            captured.update(kwargs)
            return ["parsed"], [], "text", None

    monkeypatch.setattr("tl.tl_api.get_api_provider", lambda _api_type: _Provider())

    result = await client._perform_request(
        _FakePostSession(_FakeJsonResponse(200, '{"data": []}')),
        "https://example.test",
        {},
        {},
        "openai",
        "model",
        request_config=config,
    )

    assert result == (["parsed"], [], "text", None)
    assert captured["http_status"] == 200
    assert captured["request_config"] is config
    assert "is_retry" not in captured


def test_legacy_openai_url_extractor_no_longer_extracts_relative_paths() -> None:
    client = GeminiAPIClient(["fallback"])

    assert client._find_image_urls_in_text("![img](/images/generated.png)") == []


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
