"""API 供应商。

该目录用于存放不同 API 供应商的请求/响应差异实现。
对外统一入口仍然是 `tl/tl_api.py`。
"""

from .registry import DOUBAO_API_TYPES as DOUBAO_API_TYPES
from .registry import get_api_provider as get_api_provider
from .registry import is_doubao_api_type as is_doubao_api_type
from .registry import normalize_api_type as normalize_api_type
