"""LLM 工具本地路径参考图的守卫模块。

`gemini_image_generation` 工具的 `reference_image_paths` 参数允许 LLM 提交本地
文件路径作为参考图。为避免 LLM 读取任意文件（如 `/etc/passwd`、API key），
默认白名单模式下，路径经规范化后必须落在允许目录内。

安全模型：
- `raw_has_traversal` 快速预检，拒绝原始串中显式的 `..` 段。
- `normalize_candidate` 做 expanduser + resolve(strict=False)，跟随符号链接。
- `is_path_allowed` 用 resolve 后的 `path.parents` 校验是否位于允许目录之下。
- 图片文件必须通过 Pillow 完整性校验，避免把损坏文件或非图片内容发给供应商。
- global 模式跳过白名单检查，但仍拒 traversal、不存在文件与无效图片，适用于管理员
  （权限管控交给 AstrBot webui「插件-管理行为-函数工具」的权限范围）。
"""

from __future__ import annotations

import os
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from astrbot.api import logger

# 各系统 AstrBot 数据目录的默认白名单。
# - Linux/macOS: ~/.astrbot/data
# - Docker 常见: /opt/astrbot/data、/AstrBot/data、/app/data
DEFAULT_ALLOWED_DIR_PATTERNS: tuple[str, ...] = (
    "~/.astrbot/data",
    "/opt/astrbot/data",
    "/AstrBot/data",
    "/app/data",
)


def _log_default(msg: str) -> None:
    logger.debug(msg)


def _resolve_dir(raw: str) -> Path | None:
    """expanduser + resolve(strict=False)；失败返 None。"""
    try:
        return Path(raw).expanduser().resolve(strict=False)
    except Exception:
        return None


def expand_allowed_dirs(configured: Iterable[str] | None) -> list[Path]:
    """合并默认目录、ASTRBOT_DATA_PATH 环境变量与用户配置，返回 resolve 后的去重列表。

    默认目录（DEFAULT_ALLOWED_DIR_PATTERNS）即使不存在也保留，以便用户后续创建立即生效；
    环境变量与用户配置的目录需存在才纳入。
    """
    resolved: list[Path] = []
    seen: set[str] = set()

    def _push(raw: str, *, require_exists: bool) -> None:
        p = _resolve_dir(raw)
        if p is None:
            return
        if require_exists and not p.exists():
            return
        key = str(p)
        if key not in seen:
            seen.add(key)
            resolved.append(p)

    # 1. 默认目录：不要求存在
    for raw in DEFAULT_ALLOWED_DIR_PATTERNS:
        _push(raw, require_exists=False)

    # 2. ASTRBOT_DATA_PATH 环境变量（Docker 自定义数据目录）：要求存在
    env_data_path = os.environ.get("ASTRBOT_DATA_PATH")
    if env_data_path and env_data_path.strip():
        _push(env_data_path.strip(), require_exists=True)

    # 3. 用户配置目录：要求存在
    if configured:
        for item in configured:
            if isinstance(item, str) and item.strip():
                _push(item.strip(), require_exists=True)

    return resolved


def raw_has_traversal(raw: str) -> bool:
    """快速预检：按 sep/os.altsep 分段，过滤空段后任一段为 '..' 即 True。

    os.altsep 为 None 时仅用 os.sep。最终安全由 resolve + is_path_allowed 保证。
    """
    if not raw:
        return False
    # 统一处理 POSIX 与 Windows 分隔符：backslash 转 / 后按 / 拆分；os.altsep 在
    # Windows 上是 /，已覆盖。最终安全由 resolve + is_path_allowed 保证。
    raw = urllib.parse.unquote(raw)
    segments: list[str] = []
    for seg in raw.replace("\\", "/").split("/"):
        if seg:
            segments.append(seg)
    return any(seg == ".." for seg in segments)


def normalize_candidate(raw: str) -> Path | None:
    """strip 引号/空白 → expanduser → resolve(strict=False)。失败返 None。"""
    cleaned = raw.strip().strip("\"'")
    if not cleaned:
        return None
    if cleaned.startswith("file://"):
        parsed = urllib.parse.urlparse(cleaned)
        cleaned = urllib.request.url2pathname(parsed.path)
    try:
        return Path(cleaned).expanduser().resolve(strict=False)
    except Exception:
        return None


def is_path_allowed(path: Path, allowed_resolved: list[Path]) -> bool:
    """path 必须等于或位于某个 allowed 之下（用 path.parents 检查）。"""
    if not allowed_resolved:
        return False
    for allowed in allowed_resolved:
        if path == allowed or allowed in path.parents:
            return True
    return False


def is_supported_image_file(path: Path) -> bool:
    """只放行可作为参考图处理的完整图片文件。"""
    try:
        from PIL import Image as PILImage

        try:
            from pillow_heif import register_heif_opener

            register_heif_opener()
        except ImportError:
            pass
        except Exception as exc:
            logger.debug(f"[path_guard] 注册 HEIF/HEIC opener 失败: {exc}")
    except ImportError:
        return False

    try:
        with PILImage.open(path) as image:
            image.verify()
        return True
    except Exception:
        return False


def _path_to_reference_uri(path: Path) -> str:
    """返回规范化 file:// URI，让下游走参考图校验/转换分支。"""
    try:
        return path.as_uri()
    except ValueError:
        return str(path)


def filter_reference_paths(
    raw_paths: Any,
    *,
    allowed_dirs: list[str],
    global_mode: bool,
    log_fn: Callable[[str], None] | None = None,
) -> tuple[list[str], list[str]]:
    """过滤 LLM 提交的本地路径参考图。

    Args:
        raw_paths: LLM 提交的原始路径字符串列表；异常形态会统一拒绝并记录。
        allowed_dirs: 用户配置 + 默认的允许目录（未 resolve 的原始串）。
        global_mode: True 时跳过白名单检查（仍拒 traversal、不存在与无效图片）。
        log_fn: 日志回调，默认 logger.debug。

    Returns:
        (accepted_paths, rejected_raw)：接受的规范化 file:// 路径列表与被拒原始串列表。
        接受项交给下游 _process_reference_image 继续做参考图转换。
    """
    log = log_fn or _log_default
    allowed_resolved = [] if global_mode else expand_allowed_dirs(allowed_dirs)

    accepted: list[str] = []
    rejected: list[str] = []

    if raw_paths is None:
        iterable_paths: Iterable[Any] = []
    elif isinstance(raw_paths, str):
        iterable_paths = [raw_paths]
    elif isinstance(raw_paths, (list, tuple)):
        iterable_paths = raw_paths
    else:
        log(f"[path_guard] 拒绝非列表路径参数: {type(raw_paths).__name__}")
        return [], [str(raw_paths)]

    for raw in iterable_paths:
        if not isinstance(raw, str):
            log(f"[path_guard] 拒绝非字符串路径: {type(raw).__name__}")
            rejected.append(str(raw))
            continue
        if not raw.strip():
            log("[path_guard] 拒绝空路径")
            rejected.append("")
            continue

        if raw_has_traversal(raw):
            log(f"[path_guard] 拒绝路径含 '..' 穿越: {raw[:80]}")
            rejected.append(raw)
            continue

        path = normalize_candidate(raw)
        if path is None:
            log(f"[path_guard] 拒绝路径规范化失败: {raw[:80]}")
            rejected.append(raw)
            continue

        if not path.exists() or not path.is_file():
            log(f"[path_guard] 拒绝路径不存在或非文件: {raw[:80]}")
            rejected.append(raw)
            continue

        if not global_mode and not is_path_allowed(path, allowed_resolved):
            log(f"[path_guard] 拒绝路径越出白名单: {raw[:80]}")
            rejected.append(raw)
            continue

        if not is_supported_image_file(path):
            log(f"[path_guard] 拒绝非图片文件: {raw[:80]}")
            rejected.append(raw)
            continue

        accepted.append(_path_to_reference_uri(path))

    return accepted, rejected
