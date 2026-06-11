from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace


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


def test_custom_size_mode_is_valid() -> None:
    logger = _DummyLogger()
    plugin_config = _import_plugin_config_module(logger)
    settings = {"size_mode": "custom", "custom_size": "1024×1024"}

    plugin_config._validate_openai_images_settings(settings)

    assert settings["size_mode"] == "custom"
    assert settings["custom_size"] == "1024x1024"


def test_schema_hides_openai_images_resolution_fields_in_size_mode() -> None:
    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    items = schema["provider_settings"]["items"]["provider_overrides"]["templates"][
        "openai_images"
    ]["items"]

    assert items["size_mode"]["options"] == ["preset", "custom"]
    assert items["resolution"]["condition"] == {"size_mode": "preset"}
    assert items["aspect_ratio"]["condition"] == {"size_mode": "preset"}
    assert items["custom_size"]["condition"] == {"size_mode": "custom"}


def test_provider_settings_default_polling_order_from_overrides_only() -> None:
    logger = _DummyLogger()
    plugin_config = _import_plugin_config_module(logger)
    raw_config = {
        "provider_settings": {
            "provider_overrides": [
                {
                    "__template_key": "google",
                    "api_keys": [" g1 "],
                    "model": "gemini-3-pro-image-preview",
                },
                {
                    "__template_key": "openai",
                    "api_keys": [" o1 "],
                    "model": "gpt-image",
                },
                {
                    "__template_key": "openai_images",
                    "api_keys": [" i1 "],
                    "model": "gpt-image-1",
                },
            ]
        }
    }

    cfg = plugin_config.ConfigLoader(raw_config).load()

    assert cfg.provider_polling == ["google", "openai", "openai_images"]
    assert [candidate.api_type for candidate in cfg.provider_candidates] == [
        "google",
        "openai",
        "openai_images",
    ]


def test_provider_settings_polling_deduplicates_and_reports_unknown() -> None:
    logger = _DummyLogger()
    plugin_config = _import_plugin_config_module(logger)
    raw_config = {
        "provider_settings": {
            "provider_polling": ["google", "bad-provider", "google", "openai"],
            "provider_overrides": [
                {
                    "__template_key": "google",
                    "api_keys": [" g1 "],
                    "model": "gemini-3-pro-image-preview",
                },
                {
                    "__template_key": "openai",
                    "api_keys": [" o1 "],
                    "model": "gpt-image",
                },
            ],
        },
        "quick_mode_settings": [
            {
                "__template_key": "avatar",
                "resolution": "2K",
                "aspect_ratio": "1:1",
            }
        ],
    }

    cfg = plugin_config.ConfigLoader(raw_config).load()

    assert cfg.provider_polling == ["google", "openai"]
    assert [candidate.api_type for candidate in cfg.provider_candidates] == [
        "google",
        "openai",
    ]
    assert cfg.provider_candidates[0].api_keys == ["g1"]
    assert cfg.provider_candidates[1].api_keys == ["o1"]
    assert cfg.quick_mode_overrides["avatar"] == ("2K", "1:1")
    assert any("bad_provider" in message for message in cfg.provider_config_errors)
    assert any("bad_provider" in message for message in logger.errors)


def test_provider_settings_reports_valid_provider_missing_from_polling() -> None:
    logger = _DummyLogger()
    plugin_config = _import_plugin_config_module(logger)
    cfg = plugin_config.ConfigLoader(
        {
            "provider_settings": {
                "provider_polling": ["google"],
                "provider_overrides": [
                    {
                        "__template_key": "google",
                        "api_keys": ["g1"],
                        "model": "gemini-3-pro-image-preview",
                    },
                    {
                        "__template_key": "openai",
                        "api_keys": ["o1"],
                        "model": "gpt-image",
                    },
                ],
            }
        }
    ).load()

    assert [candidate.api_type for candidate in cfg.provider_candidates] == ["google"]
    assert any("供应商配置未加入轮询表" in msg for msg in cfg.provider_config_errors)
    assert any("openai" in msg for msg in cfg.provider_config_errors)


def test_same_provider_candidates_sort_by_priority() -> None:
    logger = _DummyLogger()
    plugin_config = _import_plugin_config_module(logger)
    raw_config = {
        "provider_settings": {
            "provider_polling": ["google"],
            "provider_overrides": [
                {
                    "__template_key": "google",
                    "api_keys": ["low"],
                    "model": "low-model",
                    "priority": 1,
                },
                {
                    "__template_key": "google",
                    "api_keys": ["high"],
                    "model": "high-model",
                    "priority": 9,
                },
            ],
        }
    }

    cfg = plugin_config.ConfigLoader(raw_config).load()

    assert [candidate.id for candidate in cfg.provider_candidates] == [
        "google#2",
        "google#1",
    ]
    assert [candidate.model for candidate in cfg.provider_candidates] == [
        "high-model",
        "low-model",
    ]
    assert list(cfg.provider_overrides) == ["google#2", "google#1"]
    assert cfg.provider_settings_by_type["google"] == [
        cfg.provider_candidates[0].settings,
        cfg.provider_candidates[1].settings,
    ]


def test_settings_attr_legacy_fields_project_first_provider_candidate() -> None:
    logger = _DummyLogger()
    plugin_config = _import_plugin_config_module(logger)
    from tl.provider_metadata import iter_provider_specs

    overrides = []
    for spec in iter_provider_specs():
        if not spec.settings_attr:
            continue
        entry = {
            "__template_key": spec.api_type,
            "api_keys": [f"{spec.api_type}-key"],
        }
        entry[spec.model_field] = f"{spec.api_type}-model"
        overrides.append(entry)

    cfg = plugin_config.ConfigLoader(
        {"provider_settings": {"provider_overrides": overrides}}
    ).load()

    assert cfg.provider_config_errors == []
    for spec in iter_provider_specs():
        if not spec.settings_attr:
            continue
        assert (
            getattr(cfg, spec.settings_attr)
            is cfg.provider_settings_by_type[spec.api_type][0]
        )


def test_provider_settings_allows_zero_max_reference_images() -> None:
    logger = _DummyLogger()
    plugin_config = _import_plugin_config_module(logger)
    cfg = plugin_config.ConfigLoader(
        {
            "provider_settings": {
                "provider_overrides": [
                    {
                        "__template_key": "google",
                        "api_keys": ["g1"],
                        "model": "gemini-3-pro-image-preview",
                        "max_reference_images": 0,
                    }
                ]
            }
        }
    ).load()

    assert cfg.provider_candidates[0].settings["max_reference_images"] == 0


def test_max_configured_reference_images_preserves_zero_when_all_disabled() -> None:
    logger = _DummyLogger()
    plugin_config = _import_plugin_config_module(logger)

    assert (
        plugin_config.max_configured_reference_images(
            [
                SimpleNamespace(settings={"max_reference_images": 0}),
                SimpleNamespace(settings={"max_reference_images": "bad"}),
            ]
        )
        == 0
    )


def test_max_configured_reference_images_uses_largest_candidate_limit() -> None:
    logger = _DummyLogger()
    plugin_config = _import_plugin_config_module(logger)

    assert (
        plugin_config.max_configured_reference_images(
            [
                SimpleNamespace(settings={"max_reference_images": 0}),
                SimpleNamespace(settings={"max_reference_images": "3"}),
                SimpleNamespace(settings={"max_reference_images": 2}),
            ]
        )
        == 3
    )


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


def test_google_candidate_edit_capable_by_default() -> None:
    logger = _DummyLogger()
    plugin_config = _import_plugin_config_module(logger)
    cfg = plugin_config.ConfigLoader(
        {
            "provider_settings": {
                "provider_overrides": [
                    {
                        "__template_key": "google",
                        "api_keys": ["img-key"],
                        "model": "test-image-model",
                    }
                ]
            }
        }
    ).load()

    assert len(cfg.provider_candidates) == 1
    assert cfg.provider_candidates[0].api_type == "google"
    assert cfg.provider_candidates[0].supports_image_edit is True


def test_sensenova_candidate_not_edit_capable_by_default() -> None:
    logger = _DummyLogger()
    plugin_config = _import_plugin_config_module(logger)
    cfg = plugin_config.ConfigLoader(
        {
            "provider_settings": {
                "provider_overrides": [
                    {
                        "__template_key": "sensenova",
                        "api_keys": ["sense-key"],
                        "model": "sensenova-u1-fast",
                    }
                ]
            }
        }
    ).load()

    assert len(cfg.provider_candidates) == 1
    assert cfg.provider_candidates[0].api_type == "sensenova"
    assert cfg.provider_candidates[0].supports_image_edit is False


def test_doubao_uses_spec_model_field_and_settings_normalizer() -> None:
    logger = _DummyLogger()
    plugin_config = _import_plugin_config_module(logger)
    cfg = plugin_config.ConfigLoader(
        {
            "provider_settings": {
                "provider_overrides": [
                    {
                        "__template_key": "doubao",
                        "api_keys": ["doubao-key"],
                        "endpoint_id": "doubao-model",
                    }
                ]
            }
        }
    ).load()

    assert cfg.provider_candidates[0].model == "doubao-model"
    assert cfg.doubao_settings["optimize_prompt_mode"] == "standard"


def test_provider_settings_accepts_agnes_ai_template() -> None:
    logger = _DummyLogger()
    plugin_config = _import_plugin_config_module(logger)
    cfg = plugin_config.ConfigLoader(
        {
            "provider_settings": {
                "provider_overrides": [
                    {
                        "__template_key": "agnes_ai",
                        "api_keys": [" agnes-key "],
                        "model": "agnes-image-2.1-flash",
                        "response_format": " b64_json ",
                        "reference_image_mode": " base64 ",
                    }
                ]
            }
        }
    ).load()

    assert cfg.provider_polling == ["agnes_ai"]
    assert len(cfg.provider_candidates) == 1
    assert cfg.provider_candidates[0].api_type == "agnes_ai"
    assert cfg.provider_candidates[0].supports_image_edit is True
    assert cfg.agnes_ai_settings["api_keys"] == ["agnes-key"]
    assert cfg.agnes_ai_settings["response_format"] == "b64_json"
    assert cfg.agnes_ai_settings["reference_image_mode"] == "base64"


def test_schema_contains_agnes_ai_template() -> None:
    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    template = schema["provider_settings"]["items"]["provider_overrides"]["templates"][
        "agnes_ai"
    ]

    assert template["items"]["model"]["default"] == "agnes-image-2.1-flash"
    assert template["items"]["api_base"]["default"] == "https://apihub.agnes-ai.com"
    assert template["items"]["response_format"]["options"] == ["url", "b64_json"]
    assert template["items"]["reference_image_mode"]["default"] == "base64"
