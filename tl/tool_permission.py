"""AstrBot 函数工具权限兼容辅助。"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

TOOL_PERMISSION_SCOPE = "global"
TOOL_PERMISSION_SCOPE_ID = "global"
TOOL_PERMISSION_KEY = "tool_permissions"


def ensure_admin_default_tool_permission(
    tool_name: str,
    *,
    sp_obj: Any | None = None,
) -> bool:
    """在支持的 AstrBot 版本上，为非内置工具写入默认 admin 权限。

    只在该工具没有显式权限配置时写入，避免覆盖管理员在 WebUI 的手动选择。
    返回 True 表示本次写入了默认权限。
    """
    if not tool_name:
        return False

    if sp_obj is None:
        try:
            from astrbot.core import sp as sp_obj
        except Exception as exc:
            logger.warning(
                "当前 AstrBot 版本不支持函数工具权限自动配置；"
                f"无法将 {tool_name} 默认设为管理员可用: {exc}"
            )
            return False

    try:
        perms_store = sp_obj.get(
            TOOL_PERMISSION_KEY,
            {},
            scope=TOOL_PERMISSION_SCOPE,
            scope_id=TOOL_PERMISSION_SCOPE_ID,
        )
        if not isinstance(perms_store, dict):
            perms_store = {}

        defaults = perms_store.get("_default", {})
        if not isinstance(defaults, dict):
            defaults = {}

        if tool_name in defaults:
            logger.debug(
                f"函数工具 {tool_name} 已有显式权限配置：{defaults[tool_name]}"
            )
            return False

        defaults[tool_name] = "admin"
        perms_store["_default"] = defaults
        sp_obj.put(
            TOOL_PERMISSION_KEY,
            perms_store,
            scope=TOOL_PERMISSION_SCOPE,
            scope_id=TOOL_PERMISSION_SCOPE_ID,
        )
        logger.info(f"已将函数工具 {tool_name} 默认权限设置为管理员")
        return True
    except Exception as exc:
        logger.warning(f"设置函数工具 {tool_name} 默认权限失败: {exc}")
        return False
