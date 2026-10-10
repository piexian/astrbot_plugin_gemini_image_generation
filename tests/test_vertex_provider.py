"""tests for tl/api/vertex.py — Vertex AI 供应商适配与安全错误映射"""

from __future__ import annotations

import pytest

from tl.api.registry import get_api_provider
from tl.api.vertex import VertexProvider
from tl.provider_hooks import validate_vertex_settings


def _make_provider() -> VertexProvider:
    return VertexProvider()


def test_registry_resolves_vertex() -> None:
    provider = get_api_provider("vertex")
    assert isinstance(provider, VertexProvider)
    assert provider.name == "vertex"


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


def test_validate_vertex_settings_rejects_mixed_credentials() -> None:
    settings = {
        "api_keys": ["k1"],
        "service_account_files": ["files/vertex/sa.json"],
    }
    with pytest.raises(ValueError, match="互斥"):
        validate_vertex_settings(settings)


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
