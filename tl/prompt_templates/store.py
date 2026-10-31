"""本地模板库。内置模板只保存覆盖，文件损坏时仍可用内置定义。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .builtin import builtin_template, builtin_templates, is_builtin_id
from .models import (
    PromptTemplate,
    TemplateValidationError,
    template_record,
    utc_now,
    validate_template_data,
)
from .package import parse_package

SCHEMA_VERSION = 1
_MAX_LOCAL = 1000
_MAX_BYTES = 8 * 1024 * 1024
_ID_RE = re.compile(r"^[a-z0-9_-]{1,48}$")
_EDITABLE = (
    "name",
    "ai_description",
    "content",
    "defaults",
    "reference",
    "tags",
    "author",
    "source_url",
    "preview",
    "ai_enabled",
    "remote",
)


def _slug(name: str) -> str:
    chars: list[str] = []
    for char in name.strip().lower():
        if char.isascii() and (char.isalnum() or char in "_-"):
            chars.append(char)
        elif char.isspace():
            chars.append("-")
    slug = re.sub(r"-{2,}", "-", "".join(chars)).strip("-")[:48]
    if _ID_RE.fullmatch(slug or "") is None:
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
        slug = f"t-{digest}"
    return slug


def _allocate_id(name: str, taken: set[str]) -> str:
    base = _slug(name)
    if base not in taken:
        return base
    for index in range(2, 10000):
        suffix = f"-{index}"
        candidate = f"{base[: 48 - len(suffix)].strip('-')}{suffix}"
        if candidate not in taken and _ID_RE.fullmatch(candidate):
            return candidate
    raise TemplateValidationError("id", "无法分配模板 id")


class PromptTemplateStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "prompt_templates.json"
        self._lock = threading.RLock()
        self._templates: dict[str, PromptTemplate] = {}
        self._overrides: dict[str, dict[str, Any]] = {}
        self._readonly = False
        self._load()

    def list_templates(self) -> list[PromptTemplate]:
        with self._lock:
            builtins = [
                self._effective_builtin(item.id) for item in builtin_templates()
            ]
            local = sorted(
                self._templates.values(),
                key=lambda item: item.updated_at,
                reverse=True,
            )
            return [*builtins, *local]

    def get(self, template_id: str) -> PromptTemplate | None:
        with self._lock:
            if is_builtin_id(template_id):
                return self._effective_builtin(template_id)
            return self._templates.get(template_id)

    def builtin_override(self, template_id: str) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._overrides.get(template_id) or {})

    async def create(self, data: dict[str, Any]) -> PromptTemplate:
        return await asyncio.to_thread(self._create, data)

    async def update(self, template_id: str, changes: dict[str, Any]) -> PromptTemplate:
        return await asyncio.to_thread(self._update, template_id, changes)

    async def delete(self, template_id: str) -> None:
        await asyncio.to_thread(self._delete, template_id)

    async def restore_builtin(self, template_id: str) -> PromptTemplate:
        return await asyncio.to_thread(self._restore_builtin, template_id)

    async def set_ai_enabled(self, template_id: str, enabled: bool) -> PromptTemplate:
        return await asyncio.to_thread(self._set_ai_enabled, template_id, enabled)

    async def import_package(
        self, data: Any, *, keep_ids: bool = False
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._import_package, data, keep_ids)

    def export_package(self) -> dict[str, Any]:
        from .package import export_package

        return export_package(self.list_templates())

    def _create(self, data: dict[str, Any]) -> PromptTemplate:
        with self._lock:
            self._ensure_writable()
            if len(self._templates) >= _MAX_LOCAL:
                raise TemplateValidationError("templates", "本地模板不能超过 1000 条")
            payload = dict(data)
            now = utc_now()
            template_id = str(payload.get("id") or "").strip()
            if template_id:
                if is_builtin_id(template_id) or template_id in self._templates:
                    raise TemplateValidationError("id", "id 与已有模板冲突")
            else:
                name = payload.get("name")
                template_id = _allocate_id(
                    name if isinstance(name, str) else "",
                    set(self._templates) | {item.id for item in builtin_templates()},
                )
            payload.update(
                {
                    "id": template_id,
                    "origin": "local",
                    "created_at": payload.get("created_at") or now,
                    "updated_at": now,
                }
            )
            template = validate_template_data(payload, origin="local")
            templates = dict(self._templates)
            templates[template.id] = template
            self._commit(templates, self._overrides)
            return template

    def _update(self, template_id: str, changes: dict[str, Any]) -> PromptTemplate:
        with self._lock:
            self._ensure_writable()
            if is_builtin_id(template_id):
                return self._update_builtin(template_id, changes)
            current = self._templates.get(template_id)
            if current is None:
                raise TemplateValidationError("id", "模板不存在")
            template = self._apply_changes(current, changes, origin="local")
            templates = dict(self._templates)
            templates[template_id] = template
            self._commit(templates, self._overrides)
            return template

    def _delete(self, template_id: str) -> None:
        with self._lock:
            self._ensure_writable()
            if is_builtin_id(template_id):
                raise TemplateValidationError("id", "内置模板不能删除")
            if template_id not in self._templates:
                raise TemplateValidationError("id", "模板不存在")
            templates = dict(self._templates)
            templates.pop(template_id)
            self._commit(templates, self._overrides)

    def _restore_builtin(self, template_id: str) -> PromptTemplate:
        with self._lock:
            self._ensure_writable()
            if not is_builtin_id(template_id):
                raise TemplateValidationError("id", "只有内置模板可以恢复默认")
            overrides = dict(self._overrides)
            overrides.pop(template_id, None)
            self._commit(self._templates, overrides)
            return builtin_template(template_id)

    def _set_ai_enabled(self, template_id: str, enabled: bool) -> PromptTemplate:
        if not isinstance(enabled, bool):
            raise TemplateValidationError("ai_enabled", "ai_enabled 必须是布尔值")
        return self._update(template_id, {"ai_enabled": enabled})

    def _import_package(self, data: Any, keep_ids: bool) -> list[dict[str, Any]]:
        parse_package(data)
        items = data["templates"]
        with self._lock:
            self._ensure_writable()
            templates = dict(self._templates)
            results: list[dict[str, Any]] = []
            for index, item in enumerate(items):
                try:
                    parsed = self._imported_template(item, templates, keep_ids=keep_ids)
                except TemplateValidationError as exc:
                    results.append(
                        {"index": index, "ok": False, "id": None, "error": str(exc)}
                    )
                    continue
                if len(templates) >= _MAX_LOCAL:
                    results.append(
                        {
                            "index": index,
                            "ok": False,
                            "id": None,
                            "error": "本地模板不能超过 1000 条",
                        }
                    )
                    continue
                templates[parsed.id] = parsed
                results.append(
                    {"index": index, "ok": True, "id": parsed.id, "error": None}
                )
            if any(item["ok"] for item in results):
                self._commit(templates, self._overrides)
            return results

    def _imported_template(
        self, item: Any, templates: dict[str, PromptTemplate], *, keep_ids: bool
    ) -> PromptTemplate:
        from .package import _parse_item

        parsed = _parse_item(item)
        taken = set(templates) | {entry.id for entry in builtin_templates()}
        if keep_ids:
            raw_id = item.get("id") if isinstance(item, dict) else None
            if not str(raw_id or "").strip():
                raise TemplateValidationError("id", "id 不能为空")
            if parsed.id in taken:
                raise TemplateValidationError("id", "id 与已有模板冲突")
            template_id = parsed.id
        else:
            template_id = _allocate_id(parsed.name, taken)
        record = template_record(parsed)
        record["id"] = template_id
        record["origin"] = "local"
        record["ai_enabled"] = True
        record.pop("preview", None)
        return validate_template_data(record, origin="local")

    def _update_builtin(
        self, template_id: str, changes: dict[str, Any]
    ) -> PromptTemplate:
        current = self._effective_builtin(template_id)
        updated = self._apply_changes(current, changes, origin="builtin")
        base = builtin_template(template_id)
        delta = self._override_delta(base, updated)
        overrides = dict(self._overrides)
        if delta:
            overrides[template_id] = delta
        else:
            overrides.pop(template_id, None)
        self._commit(self._templates, overrides)
        return self._effective_builtin(template_id)

    def _apply_changes(
        self, current: PromptTemplate, changes: dict[str, Any], *, origin: str
    ) -> PromptTemplate:
        record = template_record(current)
        for key, value in changes.items():
            if key not in _EDITABLE:
                continue
            if key == "defaults":
                if not isinstance(value, dict):
                    raise TemplateValidationError("defaults", "defaults 必须是对象")
                record["defaults"] = {**current.defaults, **value}
            else:
                record[key] = value
        record["origin"] = origin
        record["updated_at"] = utc_now()
        if origin == "builtin":
            record["created_at"] = builtin_template(current.id).created_at
        return validate_template_data(record, origin=origin)

    @staticmethod
    def _override_delta(
        base: PromptTemplate, updated: PromptTemplate
    ) -> dict[str, Any]:
        delta: dict[str, Any] = {}
        for key in _EDITABLE:
            current = getattr(updated, key)
            original = getattr(base, key)
            if key == "defaults":
                changed = {
                    name: value
                    for name, value in current.items()
                    if original.get(name) != value
                }
                if changed:
                    delta["defaults"] = changed
                continue
            if key == "remote":
                if current is None and original is None:
                    continue
                if (
                    current is None
                    or original is None
                    or (
                        current.source != original.source
                        or current.remote_id != original.remote_id
                    )
                ):
                    delta["remote"] = (
                        None
                        if current is None
                        else {"source": current.source, "remote_id": current.remote_id}
                    )
                continue
            if current != original:
                delta[key] = current
        if delta:
            delta["updated_at"] = updated.updated_at
        return delta

    def _effective_builtin(self, template_id: str) -> PromptTemplate:
        base = builtin_template(template_id)
        override = self._overrides.get(template_id)
        if not override:
            return base
        try:
            return self._merge_builtin(base, override)
        except TemplateValidationError as exc:
            logger.warning("[模板库] 内置模板覆盖无效，已忽略: %s", exc)
            return base

    @staticmethod
    def _merge_builtin(
        base: PromptTemplate, override: dict[str, Any]
    ) -> PromptTemplate:
        record = template_record(base)
        for key, value in override.items():
            if key == "defaults" and isinstance(value, dict):
                record["defaults"] = {**base.defaults, **value}
            elif key != "updated_at":
                record[key] = value
        if isinstance(override.get("updated_at"), str):
            record["updated_at"] = override["updated_at"]
        record["id"] = base.id
        record["origin"] = "builtin"
        record["created_at"] = base.created_at
        return validate_template_data(record, origin="builtin")

    def _ensure_writable(self) -> None:
        if self._readonly:
            raise TemplateValidationError(
                "schema_version", "模板库版本过新，已停止写入"
            )

    def _commit(
        self, templates: dict[str, PromptTemplate], overrides: dict[str, dict[str, Any]]
    ) -> None:
        payload = self._payload(templates, overrides)
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        if len(encoded) > _MAX_BYTES:
            raise TemplateValidationError("file", "模板库文件不能超过 8 MB")
        self._write_bytes(encoded)
        self._templates = templates
        self._overrides = overrides

    def _payload(
        self, templates: dict[str, PromptTemplate], overrides: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "templates": [template_record(item) for item in templates.values()],
            "builtin_overrides": overrides,
        }

    def _write_bytes(self, encoded: bytes) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_bytes(encoded)
        os.replace(temporary, self.path)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self._read_payload(payload)
        except Exception as exc:
            self._quarantine(exc)

    def _read_payload(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise ValueError("模板库结构不正确")
        version = payload.get("schema_version")
        if not isinstance(version, int):
            raise ValueError("模板库结构不正确")
        if version > SCHEMA_VERSION:
            self._readonly = True
            logger.error("[模板库] 文件版本过新，已改为只读并仅使用内置模板")
            return
        if version != SCHEMA_VERSION:
            raise ValueError("模板库结构不正确")
        templates = payload.get("templates")
        overrides = payload.get("builtin_overrides")
        if not isinstance(templates, list) or not isinstance(overrides, dict):
            raise ValueError("模板库结构不正确")
        loaded: dict[str, PromptTemplate] = {}
        for item in templates:
            try:
                template = validate_template_data(item)
            except TemplateValidationError as exc:
                logger.warning("[模板库] 跳过无效模板: %s", exc)
                continue
            if template.origin == "builtin" or is_builtin_id(template.id):
                logger.warning("[模板库] 跳过与内置模板冲突的记录")
                continue
            loaded[template.id] = template
        loaded_overrides: dict[str, dict[str, Any]] = {}
        for template_id, override in overrides.items():
            if not isinstance(template_id, str) or not is_builtin_id(template_id):
                logger.warning("[模板库] 跳过无效的内置模板覆盖")
                continue
            if not isinstance(override, dict):
                logger.warning("[模板库] 跳过无效的内置模板覆盖")
                continue
            try:
                self._merge_builtin(builtin_template(template_id), override)
            except TemplateValidationError as exc:
                logger.warning("[模板库] 跳过无效的内置模板覆盖: %s", exc)
                continue
            loaded_overrides[template_id] = override
        self._templates = loaded
        self._overrides = loaded_overrides

    def _quarantine(self, exc: Exception) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            os.replace(self.path, backup)
        except OSError as replace_error:
            logger.warning("[模板库] 损坏文件备份失败: %s", replace_error)
        self._templates = {}
        self._overrides = {}
        self._readonly = False
        logger.warning("[模板库] 文件损坏，已改用内置模板: %s", exc)
