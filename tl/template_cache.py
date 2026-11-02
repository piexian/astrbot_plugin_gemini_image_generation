"""在线模板的本地缓存与检索。

缓存只保存快照内容与版本元数据；检索在本地做，不联网。
``hub:<remote_id>`` 形式的模板引用在生成时也从缓存取正文。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .prompt_templates import (
    PromptTemplate,
    RemoteOrigin,
    TemplateSourceError,
    TemplateValidationError,
    validate_template_data,
)

_CACHE_VERSION = 1
_FILENAME = "template_hub_cache.json"
_WEIGHTS = {"name": 3.0, "tags": 2.0, "ai_description": 1.5, "content": 1.0}
_MODE_CACHE = "cache"
_MODE_ONLINE = "online"
_MODE_OFF = "off"
_MODES = (_MODE_CACHE, _MODE_ONLINE, _MODE_OFF)
_HUB_PREFIX = "hub:"
_INITIAL_DELAY_MAX = 120.0
_INTERVAL_DEFAULT = 6 * 3600


@dataclass
class CacheState:
    """缓存的同步状态。"""

    hash: str = ""
    count: int = 0
    updated_at: str = ""
    synced_at: float = 0.0
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "hash": self.hash,
            "count": self.count,
            "updated_at": self.updated_at,
            "synced_at": self.synced_at,
            "source": self.source,
        }


class TemplateHubCache:
    """目录快照的本地缓存：同步、检索与按 id 取正文。"""

    def __init__(
        self, data_dir: str | Path, *, source=None, mode: str = _MODE_CACHE
    ) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / _FILENAME
        self.source = source
        self.mode = mode if mode in _MODES else _MODE_CACHE
        self._lock = threading.RLock()
        self._sync_lock = asyncio.Lock()
        self._templates: dict[str, PromptTemplate] = {}
        self.state = CacheState()
        self._load()

    @property
    def enabled(self) -> bool:
        return self.mode != _MODE_OFF and self.source is not None

    @property
    def live_search(self) -> bool:
        return self.mode == _MODE_ONLINE and self.source is not None

    def reconfigure(self, *, source=None, mode: str | None = None) -> None:
        self.source = source
        if mode in _MODES:
            self.mode = mode

    # --- 持久化 ---

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[模板广场] 在线缓存读取失败，将重新同步: %s", exc)
            return
        if (
            not isinstance(payload, dict)
            or payload.get("cache_version") != _CACHE_VERSION
        ):
            return
        meta = payload.get("state")
        if isinstance(meta, dict):
            self.state = CacheState(
                hash=str(meta.get("hash") or ""),
                count=int(meta.get("count") or len(self._templates)),
                updated_at=str(meta.get("updated_at") or ""),
                synced_at=float(meta.get("synced_at") or 0.0),
                source=str(meta.get("source") or ""),
            )
        else:
            self.state.count = len(self._templates)

        records = payload.get("templates")
        if isinstance(records, list):
            for item in records:
                template = self._decode(item, source=self.state.source or "hub")
                if template is not None:
                    self._templates[template.remote.remote_id] = template

    @staticmethod
    def _decode(item: Any, *, source: str = "hub") -> PromptTemplate | None:
        if not isinstance(item, dict):
            return None
        remote_id = str(item.get("remote_id") or "").strip()
        if not remote_id:
            return None
        payload = dict(item.get("template") or {})
        if not payload:
            return None
        payload.setdefault("id", remote_id)
        payload["origin"] = "remote"
        try:
            template = validate_template_data(payload, origin="remote")
            template.remote = RemoteOrigin(source=source or "hub", remote_id=remote_id)
            return template
        except TemplateValidationError:
            return None

    def _write(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "cache_version": _CACHE_VERSION,
            "state": self.state.as_dict(),
            "templates": [
                {
                    "remote_id": template.remote.remote_id,
                    "template": _record(template),
                }
                for template in self._templates.values()
            ],
        }
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_bytes(encoded)
        os.replace(temporary, self.path)

    # --- 同步 ---

    async def sync(self, *, force: bool = False) -> CacheState:
        """按哈希同步目录快照；失败时保留旧缓存。"""

        if not self.enabled:
            return self.state
        async with self._sync_lock:
            return await asyncio.to_thread(self._sync_sync, force)

    def _sync_sync(self, force: bool) -> CacheState:
        with self._lock:
            known = self.state.hash
            if not force and known:
                try:
                    version = _run(self.source.catalog_version())
                except TemplateSourceError as exc:
                    logger.warning("[模板广场] 目录版本检查失败: %s", exc)
                    return self.state
                if version.hash == known:
                    return self.state
            try:
                downloaded = _run(self.source.download_catalog(known or None))
            except TemplateSourceError as exc:
                logger.warning("[模板广场] 目录快照下载失败: %s", exc)
                return self.state
            if downloaded is None:
                return self.state
            etag, payload = downloaded
            templates: dict[str, PromptTemplate] = {}
            for item in payload.get("templates") or []:
                template = self._decode(
                    {"remote_id": str(item.get("id") or ""), "template": item},
                    source=getattr(self.source, "source_name", "") or "hub",
                )
                if template is not None:
                    templates[template.remote.remote_id] = template
            self._templates = templates
            self.state = CacheState(
                hash=_hash_from(etag) or known or "",
                count=len(templates),
                updated_at=str(payload.get("updated_at") or ""),
                synced_at=time.time(),
                source=getattr(self.source, "source_name", "") or "",
            )
            self._write()
            return self.state

    def clear(self) -> None:
        with self._lock:
            self._templates = {}
            self.state = CacheState()
            self._write()

    # --- 检索与读取 ---

    def all(self) -> list[PromptTemplate]:
        with self._lock:
            return list(self._templates.values())

    def get(self, remote_id: str) -> PromptTemplate | None:
        with self._lock:
            return self._templates.get(str(remote_id or "").strip())

    def resolve(self, selector: str) -> PromptTemplate | None:
        """解析 ``hub:<remote_id>``；不是在线引用时返回 None。"""

        text = str(selector or "").strip()
        if not text.startswith(_HUB_PREFIX):
            return None
        return self.get(text[len(_HUB_PREFIX) :].strip())

    def search(
        self, query: str, *, tag: str | None = None, limit: int = 20
    ) -> list[tuple[float, PromptTemplate]]:
        """本地加权检索，中文按子串匹配。返回按分数倒序的结果。"""

        words = _terms(query)
        wanted = str(tag or "").strip()
        scored: list[tuple[float, PromptTemplate]] = []
        for template in self.all():
            if wanted and wanted not in (template.tags or []):
                continue
            if not words:
                scored.append((0.0, template))
                continue
            score = 0.0
            haystacks = {
                "name": template.name,
                "tags": " ".join(template.tags or []),
                "ai_description": template.ai_description,
                "content": template.content,
            }
            for field, weight in _WEIGHTS.items():
                text = str(haystacks.get(field) or "").lower()
                if not text:
                    continue
                hit = sum(1 for word in words if word in text)
                if hit:
                    score += weight * hit
            if score > 0:
                scored.append((score, template))
        scored.sort(key=lambda item: (-item[0], item[1].name))
        size = max(1, min(int(limit or 20), 200))
        return scored[:size]


def _terms(query: str) -> list[str]:
    text = str(query or "").strip().lower()
    if not text:
        return []
    return [part for part in text.split() if part]


def _hash_from(etag: str) -> str:
    tag = str(etag or "").strip()
    if tag.startswith("W/"):
        tag = tag[2:]
    return tag.strip('"')


def _record(template: PromptTemplate) -> dict[str, Any]:
    return {
        "id": template.remote.remote_id if template.remote else template.id,
        "name": template.name,
        "content": template.content,
        "ai_description": template.ai_description,
        "defaults": dict(template.defaults),
        "reference": template.reference,
        "tags": list(template.tags),
        "author": template.author,
        "source_url": template.source_url,
        "created_at": template.created_at,
        "updated_at": template.updated_at,
    }


def _run(coro):
    """在同步路径里驱动一次协程。"""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("sync path must not run inside an event loop")


def initial_delay() -> float:
    """启动后的随机延迟，避免大量实例同时请求。"""

    return random.uniform(0.0, _INITIAL_DELAY_MAX)


def interval_seconds(configured: Any) -> float:
    try:
        value = float(configured)
    except (TypeError, ValueError):
        return float(_INTERVAL_DEFAULT)
    return value if value > 0 else float(_INTERVAL_DEFAULT)
