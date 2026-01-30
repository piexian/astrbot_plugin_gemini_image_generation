"""供应商注册表。

集中管理 `api_type` -> 供应商实现 的映射关系。
"""

from __future__ import annotations

from typing import Final

from .base import ApiProvider
from .google import GoogleProvider
from .grok2api import Grok2ApiProvider
from .openai_compat import OpenAICompatProvider
from .zai import ZaiProvider

_GOOGLE: Final[GoogleProvider] = GoogleProvider()
_GROK2API: Final[Grok2ApiProvider] = Grok2ApiProvider()
_OPENAI: Final[OpenAICompatProvider] = OpenAICompatProvider()
_ZAI: Final[ZaiProvider] = ZaiProvider()


def get_api_provider(api_type: str | None) -> ApiProvider:
    """根据 api_type 返回对应的供应商实现。

    当前映射：
    - `google/gemini/...` -> GoogleProvider
    - `grok2api` -> Grok2ApiProvider
    - `zai` -> ZaiProvider
    - 其他 -> OpenAICompatProvider（用于各类 OpenAI 兼容服务）
    """
    normalized_raw = (api_type or "").strip().lower()
    normalized = normalized_raw.replace("-", "_")

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
