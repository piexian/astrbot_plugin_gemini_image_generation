"""限流配置校验与 UMO 归一化。"""

from __future__ import annotations

from typing import Any

MAX_PERIOD = 604800
MAX_REQUESTS = 10000
MAX_RULES = 100
MAX_UMOS = 500
MAX_UMO_LENGTH = 1024
LIMIT_KEYS = ("global_rate_limit", "default_rate_limit", "rate_limit_rules")
GROUP_ACCESS_KEYS = ("group_limit_mode", "group_limit_list")
MAX_GROUP_IDS = 1000


def validate_umo(value: Any) -> str:
    """验证完整 UMO，保留平台大小写和会话内的分隔符。"""
    if not isinstance(value, str):
        raise ValueError("UMO 必须是字符串")
    value = value.strip()
    parts = value.split(":", 2)
    if (
        len(value) > MAX_UMO_LENGTH
        or len(parts) != 3
        or not all(part.strip() for part in parts)
        or parts[1] not in {"GroupMessage", "FriendMessage", "OtherMessage"}
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("请输入完整 UMO：平台实例ID:消息类型:会话ID")
    return value


def _boolean(value: Any, *, strict: bool) -> bool:
    if type(value) is bool:
        return value
    if not strict and isinstance(value, str):
        if value.lower().strip() in {"true", "1", "yes", "on"}:
            return True
        if value.lower().strip() in {"false", "0", "no", "off"}:
            return False
    raise ValueError("限流开关必须是布尔值")


def _integer(value: Any, name: str, maximum: int, *, strict: bool) -> int:
    if not strict and isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            pass
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name}必须是 1 到 {maximum} 之间的整数")
    return value


def _policy(value: Any, *, strict: bool, enabled: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("限流设置必须是对象")
    return {
        "enabled": _boolean(value.get("enabled", enabled), strict=strict),
        "period_seconds": _integer(
            value.get("period_seconds", 60), "限流周期", MAX_PERIOD, strict=strict
        ),
        "max_requests": _integer(
            value.get("max_requests", 5), "最大请求数", MAX_REQUESTS, strict=strict
        ),
    }


def _group_access(value: dict[str, Any], *, strict: bool) -> dict[str, Any]:
    mode = value.get("group_limit_mode", "none")
    groups = value.get("group_limit_list", [])
    if not strict:
        mode = str(mode or "none").strip().lower()
        if mode not in {"none", "whitelist", "blacklist"}:
            mode = "none"
        groups = groups or []
    if mode not in ("none", "whitelist", "blacklist"):
        raise ValueError("群限制模式必须是不限制、白名单或黑名单")
    if not isinstance(groups, list) or len(groups) > MAX_GROUP_IDS:
        raise ValueError(f"群号列表必须是最多 {MAX_GROUP_IDS} 项的数组")
    cleaned = []
    for group in groups:
        if not strict and type(group) is int:
            group = str(group)
        if not isinstance(group, str):
            raise ValueError("群号必须是字符串")
        group = group.strip()
        if not group:
            continue
        if len(group) > 1024 or any(
            ord(char) < 32 or ord(char) == 127 for char in group
        ):
            raise ValueError("群号不能包含控制字符或超过 1024 字符")
        cleaned.append(group)
    return {"group_limit_mode": mode, "group_limit_list": list(dict.fromkeys(cleaned))}


def normalize_limits(value: Any, *, strict: bool = True) -> dict[str, Any]:
    """统一校验限流和可选群访问字段；旧客户端省略名单时不重置。"""
    if not isinstance(value, dict):
        raise ValueError("限流配置必须是对象")
    if strict and set(value) - set(LIMIT_KEYS + GROUP_ACCESS_KEYS):
        raise ValueError("包含不支持的限流配置字段")
    access_keys = set(value).intersection(GROUP_ACCESS_KEYS)
    if strict and access_keys and access_keys != set(GROUP_ACCESS_KEYS):
        raise ValueError("群限制模式与群号列表必须同时提交")
    rules = value.get("rate_limit_rules", [])
    if not isinstance(rules, list) or len(rules) > MAX_RULES:
        raise ValueError(f"限流规则必须是最多 {MAX_RULES} 项的数组")
    result = {
        key: _policy(value.get(key, {}), strict=strict)
        for key in ("global_rate_limit", "default_rate_limit")
    }
    normalized = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("每条限流规则必须是对象")
        if strict and set(rule) - {
            "rule_name",
            "enabled",
            "period_seconds",
            "max_requests",
            "umos",
            "group_ids",
        }:
            raise ValueError("限流规则包含不支持的字段")
        name = rule.get("rule_name", "默认规则")
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("规则名称必须是 1 到 100 个字符")
        umos = rule.get("umos", [])
        groups = rule.get("group_ids", [])
        if not isinstance(umos, list) or len(umos) > MAX_UMOS:
            raise ValueError(f"每条规则最多允许 {MAX_UMOS} 个 UMO")
        if not isinstance(groups, list) or len(groups) > MAX_UMOS:
            raise ValueError("旧群号列表格式无效")
        if any(not isinstance(g, (str, int)) or isinstance(g, bool) for g in groups):
            raise ValueError("旧群号格式无效")
        normalized.append(
            {
                **_policy(rule, strict=strict, enabled=True),
                "rule_name": name.strip(),
                "umos": list(dict.fromkeys(validate_umo(umo) for umo in umos)),
                "group_ids": list(
                    dict.fromkeys(str(g).strip() for g in groups if str(g).strip())
                ),
            }
        )
    result["rate_limit_rules"] = normalized
    if not strict or access_keys:
        result.update(_group_access(value, strict=strict))
    return result


def pending_migration(limits: dict[str, Any]) -> bool:
    return any(
        rule["enabled"] and rule["group_ids"] for rule in limits["rate_limit_rules"]
    )


def apply_limits(config: Any, limits: dict[str, Any]) -> None:
    for key in LIMIT_KEYS:
        setattr(config, key, limits[key])
    if "group_limit_mode" in limits:
        config.group_limit_mode = limits["group_limit_mode"]
        config.group_limit_list = set(limits["group_limit_list"])
    config.limit_config_error = ""
