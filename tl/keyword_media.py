"""关键词绑定素材：上传/URL 摄入参考图库并注册共享 gen 记录。

保存与加载时幂等补注册：上传从配置 files/ 目录复制进图库，URL 预下载进
图库并预热 URL 缓存；每份素材一条 shared gen 记录，job_id 由来源哈希决定，
记录被淘汰后可按原句柄重建。本模块不参与运行时匹配，注入值仍由
keyword_trigger 解析。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

from astrbot.api import logger

from .core.errors import redact_text
from .core.remote_fetch import RemoteFetchService
from .keyword_trigger import normalize_bindings
from .reference_image import REFERENCE_IMAGE_CACHE_DIR, save_reference_image_cache

_SYSTEM_USER_ID = "keyword"
_SYSTEM_UMO = "plugin:keyword_media"
_STATE_FILE = "keyword_media.json"
_FETCH_TIMEOUT_SECONDS = 30.0


def media_job_id(source: str) -> str:
    """绑定素材的 gen 记录句柄：来源哈希决定，跨重启与重建保持一致。"""
    return f"kw-{hashlib.sha256(source.encode('utf-8')).hexdigest()[:12]}"


def _address_kind(address: str) -> tuple[str, str]:
    """地址分类：返回 (kind, 读取目标)；file:// 转为本地路径。"""
    lower = address.lower()
    if lower.startswith(("http://", "https://")):
        return "url", address
    if lower.startswith("file:"):
        return "path", url2pathname(urlsplit(address).path)
    return "path", address


class KeywordMediaService:
    """摄入绑定素材并注册 gen 记录；状态文件记录来源到图库文件与句柄的映射。"""

    def __init__(
        self,
        *,
        library: Any,
        tracker: Any,
        data_dir: str | Path,
        files_root: str | Path,
        validate_fn: Any,
        proxy_fn: Any = None,
    ) -> None:
        self.library = library
        self.tracker = tracker
        self.data_dir = Path(data_dir)
        self.files_root = Path(files_root)
        self._validate_fn = validate_fn
        self._proxy_fn = proxy_fn or (lambda: None)
        self._fetcher = RemoteFetchService()
        self._lock = asyncio.Lock()
        self._state: dict[str, dict[str, str]] = self._load_state()

    def protected_names(self) -> set[str]:
        """绑定素材的图库文件名，供跟踪器淘汰时跳过。"""
        return {
            str(entry.get("library_file") or "")
            for entry in self._state.values()
            if entry.get("library_file")
        }

    def media_state(self) -> dict[str, dict[str, str]]:
        """来源到素材状态的映射副本，供 Studio 展示 gen 句柄。"""
        return {source: dict(entry) for source, entry in self._state.items()}

    def resolve_address(self, address: str) -> dict[str, Any] | None:
        """地址注入值：URL 原样透传（缓存已预热）；本地地址取图库副本。"""
        lower = address.lower()
        if lower.startswith(("http://", "https://")):
            name = urlsplit(address).path.rsplit("/", 1)[-1] or address
            return {"label": name, "path": address}
        entry = self._state.get(address) or {}
        library_file = str(entry.get("library_file") or "")
        if library_file and self._library_file_exists(library_file):
            return {
                "label": Path(library_file).name,
                "path": str(self.library.file_path(library_file)),
            }
        return None

    async def ensure_registered(self, raw_bindings: Any) -> None:
        """为绑定里的每份素材补注册；单份失败只告警，不影响注入。"""
        if self.library is None or self.tracker is None:
            return
        if not getattr(self.tracker, "enabled", True):
            return
        sources: list[tuple[str, str]] = []
        for binding in normalize_bindings(raw_bindings):
            sources.extend(("file", rel, rel) for rel in binding.ref_files)
            for address in binding.ref_addresses:
                kind, target = _address_kind(address)
                sources.append((kind, address, target))
        if not sources:
            return
        async with self._lock:
            for kind, source, target in sources:
                try:
                    await self._ensure_one(kind, source, target)
                except Exception as exc:
                    logger.warning(
                        "[关键词素材] 注册失败，不影响注入: "
                        f"source={redact_text(source)[:120]}, "
                        f"原因={redact_text(str(exc))[:200]}"
                    )
            self._save_state()

    async def _ensure_one(self, kind: str, source: str, target: str) -> None:
        job_id = media_job_id(source)
        entry = self._state.get(source) or {}
        library_file = str(entry.get("library_file") or "")
        record = self.tracker.get(job_id)
        if kind == "url" and library_file and self._library_file_exists(library_file):
            if (
                record is None
                or record.get("images") != [library_file]
                or not record.get("shared")
            ):
                await self._register_record(
                    job_id, kind, source, library_file, Path(library_file).name
                )
            return
        if kind == "file":
            data, label = self._read_upload(target)
        elif kind == "url":
            data, label = await self._fetch_url(target)
        else:
            data, label = self._read_local(target)
        extension = self._validate_fn(data)
        if kind == "url":
            mime = "image/jpeg" if extension == "jpg" else f"image/{extension}"
            save_reference_image_cache(source, data, mime, REFERENCE_IMAGE_CACHE_DIR)
        lib_entry = await self.library.add_bytes(data, label=label, source="keyword")
        library_file = str(lib_entry["file"])
        await self._register_record(job_id, kind, source, library_file, label)
        self._state[source] = {
            "kind": kind,
            "library_file": library_file,
            "job_id": job_id,
        }

    async def _register_record(
        self, job_id: str, kind: str, source: str, library_file: str, label: str
    ) -> None:
        record = self.tracker.get(job_id)
        if record is None:
            await self.tracker.begin(
                source="keyword",
                prompt=f"关键词绑定素材：{label}",
                params={},
                requester={
                    "user_id": _SYSTEM_USER_ID,
                    "user_name": "关键词绑定",
                    "umo": _SYSTEM_UMO,
                },
                job_id=job_id,
            )
            await self.tracker.complete(
                job_id,
                image_files=[library_file],
                text_content=None,
                stats={},
                status="succeeded",
                source_urls=[source] if kind == "url" else [],
            )
        else:
            await self.tracker.update(job_id, images=[library_file], status="succeeded")
        record = self.tracker.get(job_id)
        if record is not None and not record.get("shared"):
            await self.tracker.set_shared(
                job_id, True, user_id=_SYSTEM_USER_ID, umo=_SYSTEM_UMO
            )
        logger.info(
            f"[关键词素材] gen 记录已注册: job_id={job_id}, 类型={kind}, 标签={label}"
        )

    def _library_file_exists(self, library_file: str) -> bool:
        try:
            return self.library.file_path(library_file).exists()
        except Exception:
            return False

    def _read_upload(self, rel: str) -> tuple[bytes, str]:
        path = (self.data_dir / rel).resolve()
        if not path.is_relative_to(self.files_root.resolve()):
            raise ValueError("上传路径越出配置 files 目录")
        return path.read_bytes(), path.name

    async def _fetch_url(self, url: str) -> tuple[bytes, str]:
        deadline = datetime.now(timezone.utc) + timedelta(
            seconds=_FETCH_TIMEOUT_SECONDS
        )
        data = await self._fetcher.fetch(
            url, deadline_at=deadline, proxy=self._proxy_fn()
        )
        name = urlsplit(url).path.rsplit("/", 1)[-1] or "关键词素材"
        return data, name

    def _read_local(self, path_text: str) -> tuple[bytes, str]:
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            raise ValueError("本地地址必须是绝对路径")
        return path.read_bytes(), path.name

    def _load_state(self) -> dict[str, dict[str, str]]:
        path = self.data_dir / _STATE_FILE
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.warning(
                f"[关键词素材] 状态文件不可读，按空状态重建: {redact_text(str(exc))[:160]}"
            )
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(source): {
                "kind": str(entry.get("kind") or ""),
                "library_file": str(entry.get("library_file") or ""),
                "job_id": str(entry.get("job_id") or ""),
            }
            for source, entry in raw.items()
            if isinstance(entry, dict)
        }

    def _save_state(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        destination = self.data_dir / _STATE_FILE
        fd, name = tempfile.mkstemp(prefix=".keyword-media-", dir=self.data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, destination)
        finally:
            if os.path.exists(name):
                os.unlink(name)
