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
