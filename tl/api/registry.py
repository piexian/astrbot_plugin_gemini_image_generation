"""供应商注册表。

集中管理 `api_type` -> 供应商实现 的映射关系。
"""

from __future__ import annotations

from typing import Final

from .base import ApiProvider
from .doubao import DoubaoProvider
from .google import GoogleProvider
from .grok2api import Grok2ApiProvider
from .minimax import MiniMaxProvider
from .openai_compat import OpenAICompatProvider
from .openai_images import OpenAIImagesProvider
from .stepfun import StepfunProvider
from .xai import XAIProvider
from .zai import ZaiProvider

_DOUBAO: Final[DoubaoProvider] = DoubaoProvider()
_GOOGLE: Final[GoogleProvider] = GoogleProvider()
_GROK2API: Final[Grok2ApiProvider] = Grok2ApiProvider()
_MINIMAX: Final[MiniMaxProvider] = MiniMaxProvider()
_OPENAI: Final[OpenAICompatProvider] = OpenAICompatProvider()
_OPENAI_IMAGES: Final[OpenAIImagesProvider] = OpenAIImagesProvider()
_STEPFUN: Final[StepfunProvider] = StepfunProvider()
_XAI: Final[XAIProvider] = XAIProvider()
_ZAI: Final[ZaiProvider] = ZaiProvider()

# canonical api_type -> provider 映射表（与 _conf_schema.json 中 api_type.options 严格一致）
_PROVIDERS: Final[dict[str, ApiProvider]] = {
    "google": _GOOGLE,
    "openai": _OPENAI,
    "openai_images": _OPENAI_IMAGES,
    "xai": _XAI,
    "minimax": _MINIMAX,
    "stepfun": _STEPFUN,
    "zai": _ZAI,
    "grok2api": _GROK2API,
    "doubao": _DOUBAO,
}


def normalize_api_type(api_type: str | None) -> str:
    """规范化 API 类型字符串（小写 + 去空格 + 连字符转下划线）。"""
    return (api_type or "").strip().lower().replace("-", "_")


def get_api_provider(api_type: str | None) -> ApiProvider:
    """根据 canonical ``api_type`` 返回对应的供应商实现。

    未知值回退到 ``OpenAICompatProvider``。
    """
    return _PROVIDERS.get(normalize_api_type(api_type), _OPENAI)
