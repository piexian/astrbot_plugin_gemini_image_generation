"""供应商注册表。

集中管理 `api_type` -> 供应商实现 的懒加载映射。
"""

from __future__ import annotations

from typing import Final

from ..provider_loader import load_callable
from ..provider_metadata import get_provider_spec, normalize_api_type
from .base import ApiProvider

_PROVIDER_CACHE: Final[dict[str, ApiProvider]] = {}


def _load_provider(api_type: str) -> ApiProvider:
    spec = get_provider_spec(api_type)
    if spec is None:
        raise ValueError(f"Unknown provider api_type: {api_type}")
    provider = _PROVIDER_CACHE.get(spec.api_type)
    if provider is None:
        provider_class = load_callable(spec.provider_path)
        provider = provider_class()
        _PROVIDER_CACHE[spec.api_type] = provider
    return provider


def get_api_provider(api_type: str | None) -> ApiProvider:
    """根据 canonical ``api_type`` 返回对应的供应商实现。

    未知值回退到 ``OpenAICompatProvider``。
    """
    normalized = normalize_api_type(api_type)
    if get_provider_spec(normalized) is None:
        normalized = "openai"
    return _load_provider(normalized)
