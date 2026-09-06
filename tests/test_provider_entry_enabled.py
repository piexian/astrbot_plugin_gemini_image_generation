from __future__ import annotations

from typing import Any

from tl.plugin_config import ConfigLoader


def _make_raw_config(overrides: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "provider_settings": {
            "provider_overrides": overrides,
        }
    }


def _google_entry(**kwargs: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "__template_key": "google",
        "priority": 0,
        "api_keys": ["test-key"],
        "model": "gemini-3-pro-image-preview",
    }
    entry.update(kwargs)
    return entry


def test_disabled_entry_is_skipped() -> None:
    raw = _make_raw_config(
        [
            _google_entry(enabled=False, model="disabled-model"),
            _google_entry(enabled=True, model="enabled-model"),
        ]
    )
    config = ConfigLoader(raw).load()

    candidates = config.provider_candidates
    assert len(candidates) == 1
    assert candidates[0].model == "enabled-model"


def test_enabled_defaults_to_true_when_missing() -> None:
    raw = _make_raw_config([_google_entry()])
    config = ConfigLoader(raw).load()

    assert len(config.provider_candidates) == 1
    assert config.provider_candidates[0].model == "gemini-3-pro-image-preview"


def test_disabled_entry_skips_validation() -> None:
    """禁用条目缺少模型/密钥时不报错，直接跳过。"""
    raw = _make_raw_config(
        [
            _google_entry(enabled=False, api_keys=[], model=""),
            _google_entry(model="valid-model"),
        ]
    )
    config = ConfigLoader(raw).load()

    assert len(config.provider_candidates) == 1
    assert config.provider_candidates[0].model == "valid-model"
    # 禁用条目不产生配置错误
    disabled_errors = [e for e in config.provider_config_errors if "缺少" in e]
    assert disabled_errors == []


def test_disabled_entry_does_not_leak_into_settings() -> None:
    raw = _make_raw_config([_google_entry(enabled=True)])
    config = ConfigLoader(raw).load()

    candidate = config.provider_candidates[0]
    assert "enabled" not in candidate.settings


def test_all_entries_disabled_produces_no_candidates() -> None:
    raw = _make_raw_config(
        [
            _google_entry(enabled=False),
            _google_entry(enabled=False, model="another"),
        ]
    )
    config = ConfigLoader(raw).load()

    assert config.provider_candidates == []
    assert any("未找到任何有效供应商配置" in e for e in config.provider_config_errors)


def test_disabled_entry_same_provider_type_keeps_channel_active() -> None:
    """同渠道下禁用一条不影响另一条。"""
    raw = _make_raw_config(
        [
            _google_entry(priority=10, enabled=False, model="high-prio-disabled"),
            _google_entry(priority=0, enabled=True, model="low-prio-enabled"),
        ]
    )
    config = ConfigLoader(raw).load()

    assert len(config.provider_candidates) == 1
    assert config.provider_candidates[0].model == "low-prio-enabled"
    # google 渠道仍在轮询中
    assert "google" in config.provider_polling


def test_disabled_string_value_is_recognized() -> None:
    """字符串 "false" 也应被识别为禁用（容错手动编辑）。"""
    raw = _make_raw_config([_google_entry(enabled="false")])
    config = ConfigLoader(raw).load()

    assert config.provider_candidates == []


def _make_raw_config_with_polling(
    overrides: list[dict[str, Any]], polling: list[str]
) -> dict[str, Any]:
    raw = _make_raw_config(overrides)
    raw["provider_settings"]["provider_polling"] = polling
    return raw


def _openai_entry(**kwargs: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "__template_key": "openai",
        "priority": 0,
        "api_keys": ["openai-key"],
        "model": "gpt-image-1",
    }
    entry.update(kwargs)
    return entry


def test_enabled_entry_missing_from_polling_stays_selectable_in_studio() -> None:
    """未入轮询的启用条目不参与聊天轮询，但保留给工作台选择。"""
    raw = _make_raw_config_with_polling(
        [_google_entry(model="polling-model"), _openai_entry(model="studio-model")],
        polling=["google"],
    )
    config = ConfigLoader(raw).load()

    assert [c.api_type for c in config.provider_candidates] == ["google"]
    assert [c.api_type for c in config.provider_candidates_all] == [
        "google",
        "openai",
    ]
    assert config.provider_candidates_all[1].model == "studio-model"
    # KeyManager 依赖 provider_overrides，必须覆盖全部启用候选
    assert "openai#1" in config.provider_overrides
    # 未入轮询不再是配置错误
    assert not any("轮询" in e for e in config.provider_config_errors)


def test_polling_candidates_precede_extras_in_all_candidates() -> None:
    """全量候选按「轮询顺序在前，其余按配置表顺序」排列。"""
    raw = _make_raw_config_with_polling(
        [
            _openai_entry(model="studio-model"),
            _google_entry(model="polling-model"),
        ],
        polling=["google"],
    )
    config = ConfigLoader(raw).load()

    assert [c.api_type for c in config.provider_candidates] == ["google"]
    assert [c.api_type for c in config.provider_candidates_all] == [
        "google",
        "openai",
    ]


def test_empty_polling_keeps_all_candidates_identical() -> None:
    """轮询表留空时自动跟随全部启用类型，两层候选一致。"""
    raw = _make_raw_config(
        [
            _google_entry(model="model-a"),
            _openai_entry(model="model-b"),
        ]
    )
    config = ConfigLoader(raw).load()

    assert config.provider_candidates_all == config.provider_candidates
    assert len(config.provider_candidates) == 2
