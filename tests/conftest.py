from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "tl" not in sys.modules:
    tl_module = types.ModuleType("tl")
    tl_module.__path__ = [str(ROOT / "tl")]
    sys.modules["tl"] = tl_module

if "tl.api" not in sys.modules:
    tl_api_module = types.ModuleType("tl.api")
    tl_api_module.__path__ = [str(ROOT / "tl" / "api")]
    from tl.provider_metadata import normalize_api_type, supports_image_edit

    def _get_api_provider(api_type: str | None):
        raise NotImplementedError("provider registry is not needed in unit tests")

    tl_api_module.normalize_api_type = normalize_api_type
    tl_api_module.get_api_provider = _get_api_provider
    tl_api_module.supports_image_edit = supports_image_edit
    sys.modules["tl.api"] = tl_api_module

if "tl.tl_utils" not in sys.modules:
    tl_utils_module = types.ModuleType("tl.tl_utils")

    def _save_base64_image(*args, **kwargs):
        return None

    def _encode_file_to_base64(*args, **kwargs):
        return ""

    def _format_error_message(message, *args, **kwargs):
        return str(message)

    tl_utils_module.save_base64_image = _save_base64_image
    tl_utils_module.encode_file_to_base64 = _encode_file_to_base64
    tl_utils_module.format_error_message = _format_error_message
    sys.modules["tl.tl_utils"] = tl_utils_module


class _BootstrapLogger:
    def warning(self, message: str) -> None:
        return None

    def debug(self, message: str) -> None:
        return None

    def info(self, message: str) -> None:
        return None

    def error(self, message: str) -> None:
        return None


if "astrbot.api" not in sys.modules:
    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = _BootstrapLogger()
    astrbot_module.api = api_module
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module
