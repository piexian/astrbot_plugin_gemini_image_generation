"""聊天会话生成图保存目录：绑定到会话工作区（框架本地文件发送白名单内）。"""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .api_types import APIError

try:  # AstrBot 新版提供会话工作区；旧版本无此模块，维持插件数据目录
    from astrbot.core.workspace import (
        default_workspace_root,
        resolve_workspace_root_for_umo,
    )
except ImportError:  # pragma: no cover - 旧版框架
    default_workspace_root = resolve_workspace_root_for_umo = None

SESSION_IMAGE_SUBDIR = "astrbot_plugin_gemini_image_generation"
_WORKSPACE_FAILED = object()

_session_image_dir: ContextVar[Path | object | None] = ContextVar(
    "gemini_image_session_image_dir", default=None
)


async def bind_session_image_dir(event: Any, context: Any = None):
    """绑定当前异步上下文的生成图保存目录并返回 token；无会话或旧框架不绑定。"""
    umo = getattr(event, "unified_msg_origin", None) if event is not None else None
    if not umo or resolve_workspace_root_for_umo is None:
        return None
    try:
        root = await _send_allowed_workspace_root(umo, context)
    except Exception as e:
        logger.warning(f"[生成图保存] 解析会话工作区失败: {e}")
        return _session_image_dir.set(_WORKSPACE_FAILED)
    return _session_image_dir.set(Path(root) / SESSION_IMAGE_SUBDIR)


async def _send_allowed_workspace_root(umo: str, context: Any = None) -> Path:
    """对齐框架发送白名单根：local 运行时用项目感知解析，其余用会话默认根。"""
    runtime = ""
    if context is not None:
        try:
            cfg = context.get_config(umo=umo) or {}
            runtime = str(
                (cfg.get("provider_settings") or {}).get("computer_use_runtime", "")
            )
        except Exception:
            runtime = ""
    if runtime == "local":
        return Path(await resolve_workspace_root_for_umo(umo))
    return Path(default_workspace_root(umo))


def reset_session_image_dir(token: Any) -> None:
    """按 token 还原绑定。"""
    if token is not None:
        _session_image_dir.reset(token)


def current_session_image_dir() -> Path | None:
    """返回已绑定的工作区保存目录；绑定失败时抛错（不落盘），未绑定返回 None。"""
    bound = _session_image_dir.get()
    if bound is _WORKSPACE_FAILED:
        raise APIError(
            "会话工作区不可用，无法保存生成图",
            None,
            "workspace_unavailable",
            retryable=False,
        )
    return bound if isinstance(bound, Path) else None


def cleanup_session_images_by_size(max_size_mb: float) -> None:
    """按容量清理全部会话工作区生成图：总量超限时按最旧优先删除约 30%，防跨会话累积。"""
    if max_size_mb <= 0:
        return
    bound = _session_image_dir.get()
    if not isinstance(bound, Path) or bound.name != SESSION_IMAGE_SUBDIR:
        return
    try:
        files = [
            p
            for p in bound.parent.parent.glob(f"*/{SESSION_IMAGE_SUBDIR}/*")
            if p.is_file()
        ]
        total = sum(p.stat().st_size for p in files)
        if total <= int(max_size_mb * 1024**2):
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        target_release = max(int(total * 0.3), 1)
        released = removed = 0
        for path in files:
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError:
                continue
            released += size
            removed += 1
            if released >= target_release:
                break
        logger.debug(f"会话工作区生成图超出容量上限，已清理 {removed} 个最旧文件")
    except Exception as e:
        logger.warning(f"会话工作区生成图清理失败: {e}")
