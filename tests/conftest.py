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

if "tl.tl_utils" not in sys.modules:
    tl_utils_module = types.ModuleType("tl.tl_utils")

    def _save_base64_image(*args, **kwargs):
        return None

    def _encode_file_to_base64(*args, **kwargs):
        return ""

    def _format_error_message(message, *args, **kwargs):
        return str(message)

    def _get_temp_dir():
        return ROOT

    tl_utils_module.save_base64_image = _save_base64_image
    tl_utils_module.encode_file_to_base64 = _encode_file_to_base64
    tl_utils_module.format_error_message = _format_error_message
    tl_utils_module.get_temp_dir = _get_temp_dir
    sys.modules["tl.tl_utils"] = tl_utils_module


class _BootstrapLogger:
    def warning(self, message: str, *args) -> None:
        return None

    def debug(self, message: str, *args) -> None:
        return None

    def info(self, message: str, *args) -> None:
        return None

    def error(self, message: str, *args) -> None:
        return None


if "astrbot.api" not in sys.modules:
    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = _BootstrapLogger()
    astrbot_module.api = api_module
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module
