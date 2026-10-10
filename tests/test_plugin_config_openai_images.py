from __future__ import annotations

import importlib
import sys
import types


class _DummyLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def debug(self, message: str) -> None:
        return None

    def error(self, message: str) -> None:
        self.errors.append(message)


def _import_plugin_config_module(logger: _DummyLogger):
    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = logger
    astrbot_module.api = api_module

    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module
    sys.modules.pop("tl.plugin_config", None)

    return importlib.import_module("tl.plugin_config")


def test_invalid_custom_size_does_not_block_plugin_load() -> None:
    logger = _DummyLogger()
    plugin_config = _import_plugin_config_module(logger)
    settings = {"size_mode": "custom", "custom_size": "2048×1080"}

    plugin_config._validate_openai_images_settings(settings)

    assert settings["size_mode"] == "custom"
    assert settings["custom_size"] == "2048x1080"
    assert any("16 的倍数" in message for message in logger.warnings)


def test_no_valid_provider_records_error() -> None:
    logger = _DummyLogger()
    plugin_config = _import_plugin_config_module(logger)
    cfg = plugin_config.ConfigLoader(
        {
            "provider_settings": {
                "provider_polling": ["unknown"],
                "provider_overrides": [
                    {"__template_key": "unknown", "api_keys": ["x"]},
                ],
            }
        }
    ).load()

    assert cfg.provider_candidates == []
    assert any(
        "未找到任何有效供应商配置" in message for message in cfg.provider_config_errors
    )


def test_provider_entries_require_name_model_and_keys() -> None:
    logger = _DummyLogger()
    plugin_config = _import_plugin_config_module(logger)
    cfg = plugin_config.ConfigLoader(
        {
            "provider_settings": {
                "provider_overrides": [
                    {"api_keys": ["x"], "model": "missing-name"},
                    {"__template_key": "google", "api_keys": ["x"]},
                    {
                        "__template_key": "openai",
                        "model": "missing-keys",
                        "api_keys": [],
                    },
                ]
            }
        }
    ).load()

    joined_errors = "\n".join(cfg.provider_config_errors)
    assert "缺少供应商名称" in joined_errors
    assert "google 第 1 条配置缺少模型" in joined_errors
    assert "openai 第 1 条配置缺少 api_keys" in joined_errors
    assert "未找到任何有效供应商配置" in joined_errors


def test_openai_images_generations_only_marks_candidate_not_edit_capable() -> None:
    logger = _DummyLogger()
    plugin_config = _import_plugin_config_module(logger)
    cfg = plugin_config.ConfigLoader(
        {
            "provider_settings": {
                "provider_overrides": [
                    {
                        "__template_key": "openai_images",
                        "api_keys": ["img-key"],
                        "model": "gpt-image-1",
                        "generations_only": True,
                    }
                ]
            }
        }
    ).load()

    assert len(cfg.provider_candidates) == 1
    assert cfg.provider_candidates[0].api_type == "openai_images"
    assert cfg.provider_candidates[0].supports_image_edit is False
