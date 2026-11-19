"""tests for tl/api/vertex.py — Vertex AI 供应商适配与安全错误映射"""

from __future__ import annotations

import pytest

from tl.api.registry import get_api_provider
from tl.api.vertex import VertexProvider
from tl.api_types import APIError
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


# ===== Vertex 共享 Google 解析器的 S1 兼容 =====


class _VertexParseClient:
    """vertex 解析所需的最小 client 桩。"""

    def _collect_fallback_texts(self, payload: dict) -> list[str]:  # noqa: ANN001
        return []


@pytest.mark.asyncio
async def test_vertex_parse_response_extracts_image_via_shared_parser(monkeypatch):
    """Vertex 正常响应经共享 google 解析器仍成功出图。"""

    async def fake_save(data: str, fmt: str) -> str:
        return f"/tmp/vertex_parse_ok.{fmt}"

    # registry 隔离测试会重建 tl.api.google 模块对象，补丁必须落在实际被调用的
    # _parse_gresponse 函数全局上，才能与导入顺序无关地命中旧绑定。
    monkeypatch.setitem(
        VertexProvider._parse_gresponse.__globals__, "save_base64_image", fake_save
    )
    provider = _make_provider()
    response = {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "parts": [
                        {"inlineData": {"mimeType": "image/png", "data": "aGVsbG8="}}
                    ]
                },
            }
        ]
    }
    image_urls, image_paths, text_content, sig = await provider.parse_response(
        client=_VertexParseClient(),
        response_data=response,
        session=None,
        http_status=200,
    )
    assert image_paths == ["/tmp/vertex_parse_ok.png"]
    assert image_urls == ["/tmp/vertex_parse_ok.png"]
    assert text_content is None
    assert sig is None


@pytest.mark.asyncio
async def test_vertex_non_dict_response_is_non_retryable_invalid_response():
    """非对象响应：invalid_response 且不可重试。"""
    provider = _make_provider()
    with pytest.raises(APIError) as excinfo:
        await provider.parse_response(
            client=_VertexParseClient(),
            response_data=[1],
            session=None,
            http_status=200,
        )
    assert excinfo.value.error_type == "invalid_response"
    assert excinfo.value.retryable is False
