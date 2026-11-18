"""永久参考图库：用户显式保存的参考图与元数据，不参与任何自动清理。

目录结构（data_dir 下）：
- ``references/``  图片文件，按内容 SHA256 命名，同内容多入口共享一份文件
- ``references.json``  元数据，单文件原子写

设计边界：
- 无过期、无容量淘汰；条目文件去向（晋升移入 / 降级回画廊 / 无引用删除）由 WebStudioService 编排
- 晋升事务用条目上的 origin 字段做未完成标记，启动对账补完收尾
- 关键词仅用于检索与展示，永不作为任何自动触发条件（#80 教训）
- 图片字节校验由调用方注入 ``validate_fn``（复用 Studio 的校验实现）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .core.errors import redact_text

_SAFE_FILE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_SHA256_FILE = re.compile(r"^[0-9a-f]{64}\.[A-Za-z0-9]+$")
_MAX_LABEL = 64
_MAX_DESCRIPTION = 500
_MAX_KEYWORDS = 16
_MAX_KEYWORD_LEN = 64
_VALID_SOURCES = frozenset({"upload", "gallery"})


class ReferenceLibraryError(Exception):
    """参考图库操作失败；status_code 供 Web 层映射。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_label(value: Any) -> str:
    label = str(value or "").strip()
    if not label:
        raise ReferenceLibraryError("参考图名称不能为空")
    if len(label) > _MAX_LABEL:
        raise ReferenceLibraryError(f"参考图名称不能超过 {_MAX_LABEL} 字")
    return label


def _clean_description(value: Any) -> str:
    description = str(value or "").strip()
    if len(description) > _MAX_DESCRIPTION:
        raise ReferenceLibraryError(f"参考图描述不能超过 {_MAX_DESCRIPTION} 字")
    return description


def _clean_keywords(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReferenceLibraryError("关键词必须是数组")
    keywords: list[str] = []
    for item in value:
        keyword = str(item or "").strip()
        if not keyword:
            continue
        if len(keyword) > _MAX_KEYWORD_LEN:
            raise ReferenceLibraryError(f"单个关键词不能超过 {_MAX_KEYWORD_LEN} 字")
        if keyword not in keywords:
            keywords.append(keyword)
        if len(keywords) > _MAX_KEYWORDS:
            raise ReferenceLibraryError(f"关键词数量不能超过 {_MAX_KEYWORDS} 个")
    return keywords


class ReferenceLibrary:
    """永久参考图库。线程模型：单 asyncio 锁串行化元数据写，IO 走线程池。"""

    def __init__(
        self,
        data_dir: Path,
        *,
        validate_fn: Callable[[bytes], str] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.directory = self.data_dir / "references"
        self._path = self.data_dir / "references.json"
        self._validate_fn = validate_fn
        self._lock = asyncio.Lock()
        self._closed = False
        self._entries: dict[str, dict[str, Any]] = {}
        self._load()

    # -- 元数据持久化 ---------------------------------------------------

    def _load(self) -> None:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            logger.warning(f"[参考图库] 元数据读取失败，按空库启动: {redact_text(exc)}")
            return
        if not isinstance(payload, dict):
            logger.warning("[参考图库] 元数据格式异常，按空库启动")
            return
        for entry_id, entry in payload.items():
            if isinstance(entry_id, str) and isinstance(entry, dict):
                self._entries[entry_id] = entry

    def _save_sync(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(self._entries, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, self._path)

    def _check_open(self) -> None:
        if self._closed:
            raise ReferenceLibraryError("参考图库已关闭", status_code=503)

    # -- 查询 ------------------------------------------------------------

    def list_references(self) -> list[dict[str, Any]]:
        """全部条目，新存入的在前。"""
        entries = list(self._entries.values())
        entries.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return [dict(entry) for entry in entries]

    def get(self, entry_id: str) -> dict[str, Any] | None:
        entry = self._entries.get(str(entry_id or ""))
        return dict(entry) if entry is not None else None

    def file_path(self, file_name: str) -> Path:
        """返回库内文件的绝对路径；越界或缺失时报错。"""
        if not isinstance(file_name, str) or not _SHA256_FILE.fullmatch(file_name):
            raise ReferenceLibraryError("非法参考图文件名")
        root = self.directory.resolve()
        path = (self.directory / file_name).resolve()
        if path.parent != root or not path.is_file():
            raise ReferenceLibraryError("参考图片不存在", status_code=404)
        return path

    def resolve_path(self, name: str) -> str:
        """按条目 id 或文件名解析为本地路径，供生成管线作参考图输入。"""
        entry = self._entries.get(str(name or ""))
        if entry is not None:
            return str(self.file_path(entry["file"]))
        return str(self.file_path(name))

    # -- 写操作 ----------------------------------------------------------

    async def add_bytes(
        self,
        data: bytes,
        *,
        label: Any,
        description: Any = "",
        keywords: Any = None,
        source: str = "upload",
        origin: str | None = None,
    ) -> dict[str, Any]:
        """校验并永久保存一张参考图；同内容文件按哈希去重复用。"""
        clean_label = _clean_label(label)
        clean_description = _clean_description(description)
        clean_keywords = _clean_keywords(keywords)
        if source not in _VALID_SOURCES:
            source = "upload"
        if not data:
            raise ReferenceLibraryError("图片内容为空")
        if self._validate_fn is not None:
            extension = await asyncio.to_thread(self._validate_fn, data)
        else:  # pragma: no cover - 生产环境始终注入校验
            extension = "png"
        digest = hashlib.sha256(data).hexdigest()
        file_name = f"{digest}.{extension}"
        async with self._lock:
            self._check_open()
            target = self.directory / file_name
            if not await asyncio.to_thread(
                lambda: target.is_file() and target.stat().st_size == len(data)
            ):
                await asyncio.to_thread(self._write_file_sync, target, data)
            entry_id = f"ref-{uuid.uuid4().hex[:12]}"
            entry = {
                "id": entry_id,
                "label": clean_label,
                "description": clean_description,
                "keywords": clean_keywords,
                "file": file_name,
                "source": source,
                "created_at": _now_iso(),
            }
            if origin:
                entry["origin"] = str(origin)
            self._entries[entry_id] = entry
            try:
                await asyncio.to_thread(self._save_sync)
            except Exception:
                # 落盘失败回滚内存；已写入库目录的文件按内容寻址，重试可复用
                del self._entries[entry_id]
                raise
            logger.info(
                f"[参考图库] 新增参考图: id={entry_id}, 标签={clean_label}, "
                f"来源={source}, 大小={len(data)} 字节"
            )
            return dict(entry)

    async def add_file(self, path: Path, **metadata: Any) -> dict[str, Any]:
        """把已有图片文件（画廊/上传暂存）复制入库。"""
        try:
            data = await asyncio.to_thread(Path(path).read_bytes)
        except OSError as exc:
            raise ReferenceLibraryError("源图片读取失败", status_code=404) from exc
        return await self.add_bytes(data, **metadata)

    async def update(
        self,
        entry_id: str,
        *,
        label: Any = None,
        description: Any = None,
        keywords: Any = None,
    ) -> dict[str, Any]:
        async with self._lock:
            self._check_open()
            entry_key = str(entry_id or "")
            entry = self._entries.get(entry_key)
            if entry is None:
                raise ReferenceLibraryError("参考图不存在", status_code=404)
            backup = dict(entry)
            if label is not None:
                entry["label"] = _clean_label(label)
            if description is not None:
                entry["description"] = _clean_description(description)
            if keywords is not None:
                entry["keywords"] = _clean_keywords(keywords)
            try:
                await asyncio.to_thread(self._save_sync)
            except Exception:
                # 落盘失败回滚内存，重启后仍是旧值
                self._entries[entry_key] = backup
                raise
            return dict(entry)

    async def remove(self, entry_id: str) -> dict[str, Any]:
        """删除条目；先落盘元数据，失败回滚内存，文件去向交由调用方编排。"""
        async with self._lock:
            self._check_open()
            key = str(entry_id or "")
            entry = self._entries.pop(key, None)
            if entry is None:
                raise ReferenceLibraryError("参考图不存在", status_code=404)
            file_name = entry["file"]
            # 内容寻址文件可能被多个条目共享；是否仍共享决定调用方能否动文件
            shared = any(
                item.get("file") == file_name for item in self._entries.values()
            )
            try:
                await asyncio.to_thread(self._save_sync)
            except Exception:
                # 落盘失败回滚内存，避免磁盘元数据指向已删除的文件
                self._entries[key] = entry
                raise
            logger.info(f"[参考图库] 删除参考图: id={key}")
            return {"file": file_name, "shared": shared}

    async def clear_origin(self, entry_id: str) -> None:
        """晋升事务收尾标记：移除条目上的 origin 并落盘。"""
        async with self._lock:
            self._check_open()
            entry = self._entries.get(str(entry_id or ""))
            if entry is None or not entry.pop("origin", None):
                return
            await asyncio.to_thread(self._save_sync)

    def clear_origin_sync(self, entry_id: str) -> None:
        """仅初始化期对账使用的同步变体；此时没有并发任务。"""
        entry = self._entries.get(str(entry_id or ""))
        if entry is None or not entry.pop("origin", None):
            return
        self._save_sync()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True

    @staticmethod
    def _write_file_sync(target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, target)
