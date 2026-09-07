from __future__ import annotations

import json
from pathlib import Path

import pytest

from tl.limit_config import normalize_limits, pending_migration, validate_umo
from tl.plugin_config import ConfigLoader, PluginConfig


@pytest.mark.parametrize(
    "umo",
    ["a:FriendMessage:user:thread", "QQBot:GroupMessage:123_456", "x:OtherMessage:sys"],
)
def test_valid_umo_keeps_identity(umo):
    assert validate_umo(f" {umo} ") == umo


@pytest.mark.parametrize(
    "umo",
    [
        "123",
        "a:GroupMessage:",
        ":GroupMessage:1",
        "a:wrong:1",
        "a:FriendMessage:x\nq",
        "a:FriendMessage:" + "x" * 1024,
        123,
    ],
)
def test_partial_and_invalid_umos_are_rejected(umo):
    with pytest.raises(ValueError):
        validate_umo(umo)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "60", 604801])
def test_web_period_requires_bounded_integer(value):
    with pytest.raises(ValueError):
        normalize_limits({"global_rate_limit": {"period_seconds": value}})


@pytest.mark.parametrize("value", ["false", 1, None])
def test_web_switch_is_strict(value):
    with pytest.raises(ValueError):
        normalize_limits({"default_rate_limit": {"enabled": value}})


def test_loader_retains_legacy_groups_and_parses_old_string_values():
    cfg = PluginConfig()
    ConfigLoader(
        {
            "limit_settings": {
                "rate_limit_rules": [
                    {
                        "__template_key": "rule",
                        "group_ids": [123],
                        "enabled": "false",
                        "period_seconds": "60",
                    }
                ]
            }
        }
    )._load_limit_settings(cfg)
    rule = cfg.rate_limit_rules[0]
    assert rule["group_ids"] == ["123"] and rule["umos"] == []
    assert rule["enabled"] is False
    assert not cfg.limit_config_error
    rule["enabled"] = True
    assert pending_migration({"rate_limit_rules": [rule]})


def test_loader_invalid_rule_fails_closed_instead_of_turning_into_wildcard():
    cfg = PluginConfig()
    ConfigLoader(
        {"limit_settings": {"rate_limit_rules": [{"umos": "not-an-array"}]}}
    )._load_limit_settings(cfg)
    assert cfg.limit_config_error


def test_schema_and_runtime_defaults_match_and_legacy_field_is_retained():
    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text())
    fields = schema["limit_settings"]["items"]
    limits = normalize_limits({})
    cfg = PluginConfig()
    for name in ("global_rate_limit", "default_rate_limit"):
        values = {key: field["default"] for key, field in fields[name]["items"].items()}
        assert limits[name] == values == getattr(cfg, name)
    rule = fields["rate_limit_rules"]["templates"]["rule"]["items"]
    assert rule["umos"]["default"] == rule["group_ids"]["default"] == []


def test_unknown_config_fields_and_rule_limits_are_rejected():
    for value in (
        {"provider_settings": {}},
        {"rate_limit_rules": [{"api_keys": []}]},
        {"rate_limit_rules": [{}] * 101},
    ):
        with pytest.raises(ValueError):
            normalize_limits(value)
