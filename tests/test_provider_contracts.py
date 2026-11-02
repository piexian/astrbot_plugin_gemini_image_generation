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
from tl.api_types import ApiRequestConfig
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
