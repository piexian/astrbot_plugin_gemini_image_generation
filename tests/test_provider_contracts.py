"""注册供应商与 v2 LegacyClientAdapter 的契约测试。"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest

from tl.api.base import ProviderRequest
from tl.api.registry import get_api_provider
from tl.api_types import APIError, ApiRequestConfig
from tl.core.legacy_adapter import LegacyClientAdapter
from tl.core.requests import GenerationRequest, ProviderAttempt
from tl.provider_metadata import get_provider_spec, iter_provider_specs

_PROVIDER_TYPES = tuple(spec.api_type for spec in iter_provider_specs())
_NETWORK_BUILD_SKIP = {
    "vertex": "构造请求需要服务账号凭证校验与联网令牌交换",
}


@pytest.fixture(autouse=True)
def clean_provider_registry_cache():
    from tl.api import registry
    from tl.provider_loader import load_callable

    load_callable.cache_clear()

    registry._PROVIDER_CACHE.clear()
    yield
    load_callable.cache_clear()
    registry._PROVIDER_CACHE.clear()


def _schema_templates() -> dict:
    path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    return schema["provider_settings"]["items"]["provider_overrides"]["templates"]


def _minimal_config(api_type: str) -> ApiRequestConfig:
    spec = get_provider_spec(api_type)
    template = _schema_templates()[api_type]
    fields = template["items"]
    model_field = spec.model_field if spec else "model"
    model = fields.get(model_field, {}).get("default") or "test-model"
    api_base = fields.get("api_base", {}).get("default") or None
    return ApiRequestConfig(
        model=str(model),
        prompt="draw a cat",
        api_type=api_type,
        api_base=api_base,
        api_key="test-key",
        provider_settings={},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("api_type", _PROVIDER_TYPES)
async def test_registered_provider_contract(api_type: str, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("契约测试不得创建网络会话")

    monkeypatch.setattr(aiohttp, "ClientSession", no_network)
    provider = get_api_provider(api_type)
    assert isinstance(provider.name, str) and provider.name
    assert inspect.iscoroutinefunction(provider.build_request)
    assert inspect.iscoroutinefunction(provider.parse_response)
    assert {"client", "config"} <= set(
        inspect.signature(provider.build_request).parameters
    )
    assert {
        "client",
        "response_data",
        "session",
        "api_base",
        "http_status",
        "request_config",
    } <= set(inspect.signature(provider.parse_response).parameters)
    if api_type in _NETWORK_BUILD_SKIP:
        pytest.skip(_NETWORK_BUILD_SKIP[api_type])

    request = await provider.build_request(
        client=SimpleNamespace(), config=_minimal_config(api_type)
    )
    assert isinstance(request, ProviderRequest)
    assert isinstance(request.url, str) and request.url
    assert isinstance(request.headers, Mapping)
    assert isinstance(request.payload, Mapping)
    json.dumps(request.payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_legacy_adapter_matches_v2_provider_shape_and_rebuilds_body():
    adapter = LegacyClientAdapter(SimpleNamespace(), provider="legacy", model="auto")
    for name in ("supports", "normalize", "build_request", "send", "parse_response"):
        assert inspect.iscoroutinefunction(getattr(adapter, name))

    request = GenerationRequest(prompt="draw", source="contract")
    _url, _headers, factory = await adapter.build_request(
        request, ProviderAttempt(number=1)
    )
    first = factory()
    second = factory()
    assert first is not second
    assert first.prompt == second.prompt == "draw"


# ===== Google/Gemini 响应解析防护 =====


class _GoogleParseClient:
    """google 解析所需的最小 client 桩。"""

    def _collect_fallback_texts(self, payload: dict) -> list[str]:  # noqa: ANN001
        return []

    def _find_image_urls_in_text(self, text: str) -> list[str]:
        return []

    async def _extract_from_content(self, text: str) -> tuple[list[str], list[str]]:
        return [], []

    async def _append_images_from_texts(
        self,
        texts: list[str],
        image_urls: list[str],
        image_paths: list[str],
        *,
        session: object = None,
        request_config: object = None,
    ) -> bool:
        return False


@pytest.mark.asyncio
async def test_google_bad_candidate_skipped_and_valid_candidate_parses(monkeypatch):
    """坏 candidate 跳过后，后续有效 candidate 仍正常出图并保留签名。"""

    async def fake_save(data: str, fmt: str) -> str:
        return f"/tmp/google_parse_ok.{fmt}"

    monkeypatch.setattr("tl.api.google.save_base64_image", fake_save)
    provider = get_api_provider("google")
    response = {
        "candidates": [
            "not-a-dict",
            {
                "content": {
                    "parts": [
                        {"thoughtSignature": "sig-abc"},
                        {"inlineData": {"mimeType": "image/png", "data": "aGVsbG8="}},
                    ]
                }
            },
        ]
    }
    image_urls, image_paths, text_content, sig = await provider.parse_response(
        client=_GoogleParseClient(), response_data=response, session=None
    )
    assert image_paths == ["/tmp/google_parse_ok.png"]
    assert image_urls == ["/tmp/google_parse_ok.png"]
    assert text_content is None
    assert sig == "sig-abc"


@pytest.mark.asyncio
async def test_google_malformed_content_parts_keep_text_retry_semantics():
    """content/parts 畸形按空容器处理并跳过坏 part，纯文本仍走 no_image_retry。"""
    provider = get_api_provider("google")
    response = {
        "candidates": [
            {"content": "not-a-dict"},
            {"content": {"parts": "not-a-list"}},
            {"content": {"parts": [42, {"text": "hello"}]}},
        ]
    }
    with pytest.raises(APIError) as excinfo:
        await provider.parse_response(
            client=_GoogleParseClient(), response_data=response, session=None
        )
    assert excinfo.value.error_type == "no_image_retry"
    assert excinfo.value.retryable is None


@pytest.mark.asyncio
async def test_google_all_bad_structure_raises_non_retryable_invalid_response():
    """全部候选结构异常且无有效图像：invalid_response 且不可重试。"""
    provider = get_api_provider("google")
    response = {"candidates": ["junk", 42, {"content": {"parts": [None, "x"]}}]}
    with pytest.raises(APIError) as excinfo:
        await provider.parse_response(
            client=_GoogleParseClient(), response_data=response, session=None
        )
    assert excinfo.value.error_type == "invalid_response"
    assert excinfo.value.retryable is False


@pytest.mark.asyncio
async def test_google_non_dict_response_raises_non_retryable_invalid_response():
    """非对象响应直接判结构异常，不再落入未分类异常自动重试。"""
    provider = get_api_provider("google")
    with pytest.raises(APIError) as excinfo:
        await provider.parse_response(
            client=_GoogleParseClient(), response_data=[1, 2], session=None
        )
    assert excinfo.value.error_type == "invalid_response"
    assert excinfo.value.retryable is False


@pytest.mark.asyncio
async def test_google_candidates_not_list_treated_as_no_candidates():
    """candidates 非 list：记录有限结构警告并按无有效候选处理。"""
    provider = get_api_provider("google")
    result = await provider.parse_response(
        client=_GoogleParseClient(),
        response_data={"candidates": "oops"},
        session=None,
    )
    assert result == ([], [], None, None)
