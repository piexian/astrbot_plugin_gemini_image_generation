"""API 供应商。

该目录用于存放不同 API 供应商的请求/响应差异实现。
对外统一入口仍然是 `tl/tl_api.py`。
"""

from ..provider_metadata import is_known_api_type as is_known_api_type
from ..provider_metadata import iter_api_types as iter_api_types
from ..provider_metadata import normalize_api_type as normalize_api_type
from ..provider_metadata import supports_image_edit as supports_image_edit
from .registry import get_api_provider as get_api_provider
