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
from .sensenova import SenseNovaProvider
from .stepfun import StepfunProvider
from .xai import XAIProvider
from .zai import ZaiProvider

_DOUBAO: Final[DoubaoProvider] = DoubaoProvider()
_GOOGLE: Final[GoogleProvider] = GoogleProvider()
_GROK2API: Final[Grok2ApiProvider] = Grok2ApiProvider()
_MINIMAX: Final[MiniMaxProvider] = MiniMaxProvider()
_OPENAI: Final[OpenAICompatProvider] = OpenAICompatProvider()
_OPENAI_IMAGES: Final[OpenAIImagesProvider] = OpenAIImagesProvider()
_SENSENOVA: Final[SenseNovaProvider] = SenseNovaProvider()
_STEPFUN: Final[StepfunProvider] = StepfunProvider()
_XAI: Final[XAIProvider] = XAIProvider()
_ZAI: Final[ZaiProvider] = ZaiProvider()

# canonical api_type -> provider 映射表（与 _conf_schema.json 中 provider 模板名一致）
_PROVIDERS: Final[dict[str, ApiProvider]] = {
    "google": _GOOGLE,
    "openai": _OPENAI,
    "openai_images": _OPENAI_IMAGES,
    "xai": _XAI,
    "minimax": _MINIMAX,
    "stepfun": _STEPFUN,
    "sensenova": _SENSENOVA,
    "zai": _ZAI,
    "grok2api": _GROK2API,
    "doubao": _DOUBAO,
}

_IMAGE_EDIT_CAPABLE: Final[frozenset[str]] = frozenset(
    {
        "google",
        "openai",
        "openai_images",
        "xai",
        "minimax",
        "stepfun",
        "zai",
        "grok2api",
        "doubao",
    }
)


def normalize_api_type(api_type: str | None) -> str:
    """规范化 API 类型字符串（小写 + 去空格 + 连字符转下划线）。"""
    return (api_type or "").strip().lower().replace("-", "_")


def get_api_provider(api_type: str | None) -> ApiProvider:
    """根据 canonical ``api_type`` 返回对应的供应商实现。

    未知值回退到 ``OpenAICompatProvider``。
    """
    return _PROVIDERS.get(normalize_api_type(api_type), _OPENAI)


def is_known_api_type(api_type: str | None) -> bool:
    """检查是否为已注册的 canonical API 类型。"""
    return normalize_api_type(api_type) in _PROVIDERS


def iter_api_types() -> tuple[str, ...]:
    """返回已注册 API 类型，顺序与注册表一致。"""
    return tuple(_PROVIDERS.keys())


def supports_image_edit(api_type: str | None) -> bool:
    """检查供应商是否支持带参考图的改图请求。"""
    return normalize_api_type(api_type) in _IMAGE_EDIT_CAPABLE
