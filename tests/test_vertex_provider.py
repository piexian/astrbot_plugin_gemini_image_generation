"""tests for tl/api/vertex.py — Vertex AI 供应商适配与安全错误映射"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tl.api.registry import get_api_provider
from tl.api.vertex import VertexProvider
from tl.api_types import APIError, ApiRequestConfig
from tl.provider_hooks import validate_vertex_settings


class _FakeClient:
    async def _process_reference_image(self, image_input, idx, mode):
        return "image/png", image_input, False

    def _validate_b64_with_fallback(self, data, context=""):
        return data, True

    def _ensure_mime_type(self, mime):
        return mime or "image/png"

    def _collect_fallback_texts(self, response_data):
        return []

    def _find_image_urls_in_text(self, text):
        return []

    async def _extract_from_content(self, text):
        return [], []

    async def _append_images_from_texts(self, *args, **kwargs):
        return False

    async def _get_session(self, proxy=None):
        raise AssertionError("build_request 不应发起网络请求")

    def _request_http_proxy(self, config):
        return None


def _make_config(**overrides) -> ApiRequestConfig:
    kwargs: dict[str, Any] = {
        "model": "gemini-3-pro-image",
        "prompt": "draw a cat",
        "api_type": "vertex",
        "api_key": "express-key",
        "provider_settings": {},
    }
    kwargs.update(overrides)
    return ApiRequestConfig(**kwargs)


def _make_provider() -> VertexProvider:
    return VertexProvider()


def test_registry_resolves_vertex() -> None:
    provider = get_api_provider("vertex")
    assert isinstance(provider, VertexProvider)
    assert provider.name == "vertex"


@pytest.mark.asyncio
async def test_express_api_key_uses_express_endpoint() -> None:
    provider = _make_provider()
    request = await provider.build_request(client=_FakeClient(), config=_make_config())

    assert request.url == (
        "https://aiplatform.googleapis.com/v1"
        "/publishers/google/models/gemini-3-pro-image:generateContent"
    )
    assert request.headers["x-goog-api-key"] == "express-key"
    assert "Authorization" not in request.headers
    assert request.payload["contents"][0]["parts"][0]["text"] == "draw a cat"


def _write_sa_file(tmp_path: Path, project: str = "my-project") -> Path:
    info = {
        "type": "service_account",
        "project_id": project,
        "client_email": "bot@my-project.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
    }
    path = tmp_path / "sa.json"
    path.write_text(json.dumps(info), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_service_account_uses_full_endpoint_and_bearer(
    tmp_path, monkeypatch
) -> None:
    sa_path = _write_sa_file(tmp_path)
    provider = _make_provider()

    async def fake_exchange_jwt(client, config, private_key, client_email):
        assert "FAKE" in private_key
        assert client_email == "bot@my-project.iam.gserviceaccount.com"
        return "access-token", 3600

    monkeypatch.setattr(provider, "_exchange_jwt", fake_exchange_jwt)
    config = _make_config(
        provider_settings={
            "service_account_files": [str(sa_path)],
            "location": "us-central1",
        }
    )
    request = await provider.build_request(client=_FakeClient(), config=config)

    assert request.url == (
        "https://us-central1-aiplatform.googleapis.com/v1"
        "/projects/my-project/locations/us-central1"
        "/publishers/google/models/gemini-3-pro-image:generateContent"
    )
    assert request.headers["Authorization"] == "Bearer access-token"
    assert "x-goog-api-key" not in request.headers


@pytest.mark.asyncio
async def test_service_account_global_location_and_project_fallback(
    tmp_path, monkeypatch
) -> None:
    sa_path = _write_sa_file(tmp_path, project="from-json")
    provider = _make_provider()

    async def fake_exchange_jwt(client, config, private_key, client_email):
        return "tok", 3600

    monkeypatch.setattr(provider, "_exchange_jwt", fake_exchange_jwt)
    config = _make_config(provider_settings={"service_account_files": [str(sa_path)]})
    request = await provider.build_request(client=_FakeClient(), config=config)

    assert request.url.startswith(
        "https://aiplatform.googleapis.com/v1/projects/from-json/locations/global/"
    )
    assert request.url.endswith(
        "/publishers/google/models/gemini-3-pro-image:generateContent"
    )


@pytest.mark.asyncio
async def test_api_base_override_appends_publishers_path() -> None:
    provider = _make_provider()
    config = _make_config(api_base="https://gateway.example.com/vertex")
    request = await provider.build_request(client=_FakeClient(), config=config)

    assert request.url == (
        "https://gateway.example.com/vertex/v1"
        "/publishers/google/models/gemini-3-pro-image:generateContent"
    )


@pytest.mark.asyncio
async def test_person_generation_injected_into_image_config() -> None:
    provider = _make_provider()
    config = _make_config(
        provider_settings={"person_generation": "allow_all"},
        aspect_ratio="16:9",
    )
    request = await provider.build_request(client=_FakeClient(), config=config)

    image_config = request.payload["generationConfig"]["imageConfig"]
    assert image_config["personGeneration"] == "allow_all"
    assert image_config["aspect_ratio"] == "16:9"


@pytest.mark.asyncio
async def test_prompt_block_message() -> None:
    provider = _make_provider()
    with pytest.raises(APIError) as exc_info:
        await provider.parse_response(
            client=_FakeClient(),
            response_data={"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}},
            session=None,
            http_status=200,
        )
    assert exc_info.value.error_type == "safety"
    assert exc_info.value.retryable is False
    assert "安全过滤" in exc_info.value.message


@pytest.mark.asyncio
async def test_image_safety_finish_reason_maps_rai_code() -> None:
    provider = _make_provider()
    response_data = {
        "candidates": [
            {
                "finishReason": "IMAGE_SAFETY",
                "raiFilteredReason": "ERROR_MESSAGE. Support codes: 56562880",
            }
        ]
    }
    with pytest.raises(APIError) as exc_info:
        await provider.parse_response(
            client=_FakeClient(),
            response_data=response_data,
            session=None,
            http_status=200,
        )
    assert "56562880" in exc_info.value.message
    assert "暴力" in exc_info.value.message
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_blocked_candidate_with_text_only_response_reports_safety() -> None:
    provider = _make_provider()
    response_data = {
        "candidates": [
            {
                "content": {"parts": [{"text": "无法生成该内容"}]},
                "finishReason": "STOP",
            },
            {"finishReason": "IMAGE_PROHIBITED_CONTENT"},
        ]
    }
    with pytest.raises(APIError) as exc_info:
        await provider.parse_response(
            client=_FakeClient(),
            response_data=response_data,
            session=None,
            http_status=200,
        )
    assert exc_info.value.error_type == "safety"


@pytest.mark.asyncio
async def test_error_payload_quota_and_auth_mapping() -> None:
    provider = _make_provider()

    quota_error = provider._error_from_http(
        {
            "error": {
                "code": 429,
                "message": "Quota exceeded",
                "status": "RESOURCE_EXHAUSTED",
            }
        },
        429,
    )
    assert quota_error.error_type == "quota"
    assert quota_error.retryable is True

    auth_error = provider._error_from_http(
        {
            "error": {
                "code": 403,
                "message": "Permission denied",
                "status": "PERMISSION_DENIED",
            }
        },
        403,
    )
    assert auth_error.error_type == "auth"
    assert auth_error.retryable is False


@pytest.mark.asyncio
async def test_error_payload_with_rai_code_reports_safety() -> None:
    provider = _make_provider()
    error = provider._error_from_http(
        {
            "error": {
                "code": 400,
                "message": "Image was filtered.",
                "details": [
                    {"raiFilteredReason": "Support codes: 90789179"},
                ],
            }
        },
        400,
    )
    assert error.error_type == "safety"
    assert "性相关内容" in error.message


def test_validate_vertex_settings_normalizes() -> None:
    settings = {
        "api_keys": [],
        "location": " US-Central1 ",
        "person_generation": "ALLOW_ALL",
        "service_account_files": [" files/a/sa.json ", "", 123],
        "project_id": " proj ",
    }
    validate_vertex_settings(settings)

    assert settings["location"] == "US-Central1"
    assert settings["person_generation"] == "allow_all"
    assert settings["service_account_files"] == ["files/a/sa.json"]
    assert settings["project_id"] == "proj"


def test_validate_vertex_settings_rejects_invalid_person_generation() -> None:
    settings = {"person_generation": "everything", "api_keys": ["k"]}
    validate_vertex_settings(settings)
    assert settings["person_generation"] == ""


def test_validate_vertex_settings_rejects_mixed_credentials() -> None:
    settings = {
        "api_keys": ["k1"],
        "service_account_files": ["files/vertex/sa.json"],
    }
    with pytest.raises(ValueError, match="互斥"):
        validate_vertex_settings(settings)


def test_validate_vertex_settings_rejects_multiple_credentials() -> None:
    with pytest.raises(ValueError, match="最多上传一个服务账号"):
        validate_vertex_settings(
            {"service_account_files": ["files/a.json", "files/b.json"]}
        )
    with pytest.raises(ValueError, match="最多填写一个 API Key"):
        validate_vertex_settings({"api_keys": ["k1", "k2"]})


def test_validate_vertex_settings_rejects_missing_credential() -> None:
    with pytest.raises(ValueError, match="服务账号 JSON 凭证或一个 API Key"):
        validate_vertex_settings({})


def test_vertex_entry_with_multiple_keys_is_rejected_at_load() -> None:
    from tl.plugin_config import ConfigLoader

    raw = {
        "provider_settings": {
            "provider_overrides": [
                {
                    "__template_key": "vertex",
                    "model": "gemini-3-pro-image",
                    "api_keys": ["k1", "k2"],
                }
            ]
        }
    }
    config = ConfigLoader(raw).load()

    assert config.provider_candidates == []
    assert any("最多填写一个 API Key" in e for e in config.provider_config_errors)
    assert any("配置无效" in e for e in config.provider_config_errors)


def test_keyless_candidate_loads_with_service_account_only() -> None:
    from tl.plugin_config import ConfigLoader
    from tl.provider_settings import candidate_is_keyless

    raw = {
        "provider_settings": {
            "provider_overrides": [
                {
                    "__template_key": "vertex",
                    "model": "gemini-3-pro-image",
                    "service_account_files": ["files/vertex/sa.json"],
                }
            ]
        }
    }
    config = ConfigLoader(raw).load()

    assert len(config.provider_candidates) == 1
    candidate = config.provider_candidates[0]
    assert candidate.api_type == "vertex"
    assert candidate.settings["service_account_files"] == ["files/vertex/sa.json"]
    assert candidate_is_keyless("vertex", candidate.settings) is True
    # 有 api_keys 的候选不是 keyless；非 keyless 供应商不因该字段豁免
    assert candidate_is_keyless("vertex", {"api_keys": ["k"]}) is False
    assert candidate_is_keyless("google", {"service_account_files": ["x"]}) is False


def test_vertex_candidate_without_any_credential_is_rejected() -> None:
    from tl.plugin_config import ConfigLoader

    raw = {
        "provider_settings": {
            "provider_overrides": [
                {"__template_key": "vertex", "model": "gemini-3-pro-image"}
            ]
        }
    }
    config = ConfigLoader(raw).load()

    assert config.provider_candidates == []
    assert any(
        "服务账号 JSON 凭证或一个 API Key" in e for e in config.provider_config_errors
    )
