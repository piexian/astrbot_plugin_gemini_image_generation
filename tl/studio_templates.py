"""Studio 提示词广场的模板管理与预览图资源。"""

from __future__ import annotations

import asyncio
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .prompt_templates import (
    PromptTemplateStore,
    TemplateValidationError,
    export_package,
    is_builtin_id,
)
from .web_studio_service import StudioServiceError

_SAFE_FILE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_ASSET_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
_ASSET_MAX_BYTES = 8 * 1024 * 1024
_ASSET_DIR_MAX_BYTES = 256 * 1024 * 1024
_ASSET_COUNT_MAX = 2000


class StudioTemplateService:
    """模板库与预览图资源的工作台服务，只做校验、编排和落盘。"""

    def __init__(
        self,
        store: PromptTemplateStore,
        data_dir: str | Path,
        *,
        gallery_dir: str | Path | None = None,
    ) -> None:
        self.store = store
        self.data_dir = Path(data_dir)
        self.asset_dir = self.data_dir / "template_assets"
        self.gallery_dir = Path(gallery_dir) if gallery_dir else None
        self._asset_lock = asyncio.Lock()

    def list_templates(self) -> list[dict[str, Any]]:
        return [_public_record(item) for item in self.store.list_templates()]

    def get(self, template_id: Any):
        """删除前取回模板，用于清理与之绑定的预览图。"""

        found = self.store.get(str(template_id or "").strip())
        return _public_record(found) if found is not None else None

    async def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action") or "").strip()
        if action == "create":
            template = await self.store.create(payload.get("template") or {})
        elif action == "update":
            template_id = _require_id(payload.get("id"))
            template = await self.store.update(
                template_id, payload.get("changes") or {}
            )
        elif action == "delete":
            template_id = _require_id(payload.get("id"))
            await self.store.delete(template_id)
            return {"id": template_id}
        elif action == "restore":
            template_id = _require_id(payload.get("id"))
            template = await self.store.restore_builtin(template_id)
        elif action == "set_ai_enabled":
            template_id = _require_id(payload.get("id"))
            enabled = payload.get("ai_enabled")
            if not isinstance(enabled, bool):
                raise TemplateValidationError("ai_enabled", "ai_enabled 必须是布尔值")
            template = await self.store.set_ai_enabled(template_id, enabled)
        else:
            raise StudioServiceError("不支持的模板操作", status_code=400)
        return _public_record(template)

    def export_package(self) -> dict[str, Any]:
        return export_package(self.store.list_templates())

    async def import_package(self, payload: Any) -> dict[str, Any]:
        data = payload if isinstance(payload, dict) else None
        if data is None:
            raise StudioServiceError("请求体必须是有效的 JSON 对象", status_code=400)
        keep_ids = (
            payload.get("keep_ids") is True if isinstance(payload, dict) else False
        )
        results = await self.store.import_package(data, keep_ids=keep_ids)
        imported = sum(1 for item in results if item["ok"])
        return {
            "imported": imported,
            "failed": len(results) - imported,
            "results": results,
        }

    async def save_asset_from_gallery(self, image_name: str) -> str:
        """把画廊图片复制为模板预览图，使其不受画廊容量清理影响。"""

        if self.gallery_dir is None:
            raise StudioServiceError("画廊目录不可用", status_code=503)
        if not isinstance(image_name, str) or not _SAFE_FILE_NAME.fullmatch(image_name):
            raise StudioServiceError("非法图片文件名", status_code=400)
        source = (self.gallery_dir / image_name).resolve()
        if source.parent != self.gallery_dir.resolve() or not source.is_file():
            raise StudioServiceError("图片不存在或已清理", status_code=404)
        suffix = source.suffix.lower()
        if suffix not in _ASSET_SUFFIXES:
            raise StudioServiceError("预览图只支持常见图片格式", status_code=400)
        async with self._asset_lock:
            name = await asyncio.to_thread(
                self._copy_asset_sync, source, suffix, _SAFE_FILE_NAME
            )
        return name

    def _copy_asset_sync(
        self, source: Path, suffix: str, pattern: re.Pattern[str]
    ) -> str:
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        if source.stat().st_size > _ASSET_MAX_BYTES:
            raise StudioServiceError("预览图不能超过 8 MB", status_code=413)
        if not self._reserve_asset_space(source.stat().st_size):
            raise StudioServiceError(
                "预览图空间已满，请先删除部分模板预览图", status_code=507
            )
        name = f"{uuid.uuid4().hex}{suffix}"
        target = self.asset_dir / name
        shutil.copyfile(source, target)
        if not pattern.fullmatch(name):
            target.unlink(missing_ok=True)
            raise StudioServiceError("生成预览图文件名失败", status_code=500)
        return name

    def _reserve_asset_space(self, incoming: int) -> bool:
        if not self.asset_dir.is_dir():
            return True
        total = 0
        count = 0
        for path in self.asset_dir.iterdir():
            if not path.is_file():
                continue
            count += 1
            total += path.stat().st_size
        if count >= _ASSET_COUNT_MAX:
            return False
        return total + incoming <= _ASSET_DIR_MAX_BYTES

    def asset_file(self, name: str) -> Path:
        if not isinstance(name, str) or not _SAFE_FILE_NAME.fullmatch(name):
            raise StudioServiceError("非法图片文件名", status_code=400)
        root = self.asset_dir.resolve()
        path = (self.asset_dir / name).resolve()
        if path.parent != root or not path.is_file():
            raise StudioServiceError("预览图不存在", status_code=404)
        return path

    async def drop_asset(self, name: str | None) -> None:
        """删除模板时清理预览图；内置模板恢复默认不清理，避免误删共用资源。"""

        if not name or not isinstance(name, str) or not _SAFE_FILE_NAME.fullmatch(name):
            return
        async with self._asset_lock:
            await asyncio.to_thread(self._drop_asset_sync, name)

    def _drop_asset_sync(self, name: str) -> None:
        path = self.asset_dir / name
        try:
            if path.parent.resolve() == self.asset_dir.resolve():
                path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("[模板广场] 清理预览图失败: %s", exc)


def _require_id(value: Any) -> str:
    template_id = str(value or "").strip()
    if not template_id:
        raise StudioServiceError("缺少模板 id", status_code=400)
    return template_id


def _public_record(template: Any) -> dict[str, Any]:
    return {
        "id": template.id,
        "name": template.name,
        "ai_description": template.ai_description,
        "content": template.content,
        "defaults": dict(template.defaults),
        "reference": template.reference,
        "tags": list(template.tags),
        "author": template.author,
        "source_url": template.source_url,
        "preview": template.preview,
        "ai_enabled": template.ai_enabled,
        "origin": template.origin,
        "builtin": is_builtin_id(template.id),
        "created_at": template.created_at,
        "updated_at": template.updated_at,
    }
