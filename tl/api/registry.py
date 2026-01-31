"""供应商注册表。

集中管理 `api_type` -> 供应商实现 的映射关系。
"""

from __future__ import annotations

from typing import Final

from .base import ApiProvider
from .doubao import DoubaoProvider
from .google import GoogleProvider
from .grok2api import Grok2ApiProvider
from .openai_compat import OpenAICompatProvider
from .zai import ZaiProvider

_DOUBAO: Final[DoubaoProvider] = DoubaoProvider()
_GOOGLE: Final[GoogleProvider] = GoogleProvider()
_GROK2API: Final[Grok2ApiProvider] = Grok2ApiProvider()
_OPENAI: Final[OpenAICompatProvider] = OpenAICompatProvider()
_ZAI: Final[ZaiProvider] = ZaiProvider()

# Doubao/Volcengine Ark 相关的 API 类型别名
DOUBAO_API_TYPES: Final[frozenset[str]] = frozenset(
    {"doubao", "volcengine", "ark", "seedream"}
)


def normalize_api_type(api_type: str | None) -> str:
    """规范化 API 类型字符串。

    将 api_type 转换为小写、去除空格、替换连字符为下划线。

    Args:
        api_type: 原始 API 类型字符串

    Returns:
        规范化后的字符串
    """
    return (api_type or "").strip().lower().replace("-", "_")


def is_doubao_api_type(api_type: str | None) -> bool:
    """判断是否为豆包/火山引擎 API 类型。

    Args:
        api_type: API 类型字符串

    Returns:
        是否为豆包相关类型
    """
    return normalize_api_type(api_type) in DOUBAO_API_TYPES


def get_api_provider(api_type: str | None) -> ApiProvider:
    """根据 api_type 返回对应的供应商实现。

    当前映射：
    - `google/gemini/...` -> GoogleProvider
    - `grok2api` -> Grok2ApiProvider
    - `zai` -> ZaiProvider
    - `doubao/volcengine/ark/seedream` -> DoubaoProvider
    - 其他 -> OpenAICompatProvider（用于各类 OpenAI 兼容服务）
    """
    normalized = normalize_api_type(api_type)

    # Doubao/Volcengine Ark
    if normalized in DOUBAO_API_TYPES:
        return _DOUBAO

    # Zai 独立供应商
    if normalized == "zai" or normalized.startswith("zai_"):
        return _ZAI

    # grok2api 独立供应商
    if normalized in {"grok2api", "grok2_api"} or normalized.startswith("grok2api_"):
        return _GROK2API

    # Google/Gemini 官方
    if normalized in {"google", "gemini", "googlegenai", "google_genai"}:
        return _GOOGLE

    return _OPENAI
