"""Studio 提示词广场的模板管理与预览图资源。"""

from __future__ import annotations

import asyncio
import copy
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .prompt_templates import (
    PromptTemplateStore,
    SourceAuthError,
    SourceRateLimited,
    SourceRejected,
    TemplateSourceError,
    TemplateValidationError,
    export_package,
    is_builtin_id,
)
from .template_cache import TemplateHubCache
from .template_hub import HubTemplateSource, source_for
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
        hub_mode: str = "cache",
        hub_url: str = "",
        hub_key: str = "",
        source_factory=source_for,
        raw_config: Any = None,
        config_lock: Any = None,
        is_closed: Any = None,
    ) -> None:
        self.store = store
        self.data_dir = Path(data_dir)
        self.asset_dir = self.data_dir / "template_assets"
        self.gallery_dir = Path(gallery_dir) if gallery_dir else None
        self._asset_lock = asyncio.Lock()
        self._hub_mode = hub_mode
        self._hub_url = hub_url
        self._hub_key = hub_key
        self._source_factory = source_factory
        self.raw_config = raw_config
        self._config_lock = config_lock if config_lock is not None else asyncio.Lock()
        self._is_closed = is_closed or (lambda: False)
        self.hub_source: HubTemplateSource | None = None
        self.hub_cache = TemplateHubCache(self.data_dir, mode=hub_mode)
        self.reconfigure_hub(mode=hub_mode, url=hub_url, key=hub_key, sync=False)

    def list_templates(self) -> list[dict[str, Any]]:
        return [_public_record(item) for item in self.store.list_templates()]

    def get(self, template_id: Any):
        """删除前取回模板，用于清理与之绑定的预览图。"""

        found = self.store.get(str(template_id or "").strip())
        return _public_record(found) if found is not None else None

    # --- 在线模板 ---

    def reconfigure_hub(
        self,
        *,
        mode: str | None = None,
        url: str | None = None,
        key: str | None = None,
        sync: bool = True,
    ) -> None:
        """按插件配置重建在线源；键不变时复用同一个客户端。"""

        if mode is not None:
            self._hub_mode = mode
        if url is not None:
            self._hub_url = url
        if key is not None:
            self._hub_key = key
        if self._hub_mode == "off":
            self.hub_source = None
            self.hub_cache.reconfigure(source=None, mode="off")
            return
        if (
            self.hub_source is None
            or self.hub_source.base_url != (self._hub_url or "")
            or self.hub_source.api_key != (self._hub_key or None)
        ):
            previous = self.hub_source
            self.hub_source = self._source_factory(self._hub_url, self._hub_key or None)
            if previous is not None:
                _schedule_close(previous)
        self.hub_cache.reconfigure(source=self.hub_source, mode=self._hub_mode)

    def hub_state(self) -> dict[str, Any]:
        state = self.hub_cache.state
        return {
            "mode": self._hub_mode,
            "enabled": self.hub_cache.enabled,
            "live_search": self.hub_cache.live_search,
            "has_key": bool(self._hub_key),
            "url": self.hub_source.base_url if self.hub_source else "",
            **state.as_dict(),
        }

    async def sync_hub(self, *, force: bool = False) -> dict[str, Any]:
        if not self.hub_cache.enabled:
            return self.hub_state()
        await self.hub_cache.sync(force=force)
        return self.hub_state()

    async def hub_search(
        self, query: str, *, tag: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        """本地缓存检索；online 模式额外合并实时检索结果。"""

        size = max(1, min(int(limit or 20), 200))
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for score, template in self.hub_cache.search(query, tag=tag, limit=size):
            record = _public_record(template)
            record["score"] = round(score, 3)
            record["found_via"] = "cache"
            results.append(record)
            seen.add(record["id"])
        notice = ""
        if self.hub_cache.live_search and self.hub_source is not None:
            try:
                page = await self.hub_source.search(query, tag=tag, limit=size)
                for template in page.templates:
                    record = _public_record(template)
                    if record["id"] in seen:
                        continue
                    record["score"] = 0.0
                    record["found_via"] = "online"
                    results.append(record)
                    seen.add(record["id"])
            except TemplateSourceError as exc:
                notice = _source_message(exc)
        return {
            "templates": results[:size],
            "notice": notice,
            "state": self.hub_state(),
        }

    async def hub_mine(self, *, limit: int = 50) -> dict[str, Any]:
        """自己上传的在线模板，需要密钥。"""

        if not str(self._hub_key or "").strip():
            raise StudioServiceError("尚未填写在线模板密钥", status_code=400)
        try:
            templates = await self._with_source().my_templates(limit=limit)
        except SourceAuthError as exc:
            raise StudioServiceError(_source_message(exc), status_code=401) from exc
        except SourceRateLimited as exc:
            raise StudioServiceError(_source_message(exc), status_code=429) from exc
        except TemplateSourceError as exc:
            raise StudioServiceError(_source_message(exc), status_code=502) from exc
        return {
            "templates": [_public_record(item) for item in templates],
            "state": self.hub_state(),
        }

    async def hub_tags(self, *, query: str = "", limit: int = 20) -> dict[str, Any]:
        """在线标签列表，公开接口。"""

        try:
            tags = await self._with_source().tags(query=query, limit=limit)
        except TemplateSourceError as exc:
            raise StudioServiceError(_source_message(exc), status_code=502) from exc
        return {"tags": tags}

    async def hub_import(self, remote_ids: Any) -> dict[str, Any]:
        """把缓存里的在线模板导入为可编辑的本地副本。"""

        wanted = [str(item).strip() for item in (remote_ids or []) if str(item).strip()]
        if not wanted:
            raise StudioServiceError("请先选择要导入的在线模板", status_code=400)
        if len(wanted) > 100:
            raise StudioServiceError("一次最多导入 100 条模板", status_code=400)
        results = []
        for remote_id in wanted:
            template = self.hub_cache.get(remote_id)
            if template is None:
                results.append(
                    {"id": remote_id, "ok": False, "message": "缓存里没有该模板"}
                )
                continue
            try:
                created = await self.store.create(
                    {
                        "name": template.name,
                        "content": template.content,
                        "ai_description": template.ai_description,
                        "defaults": dict(template.defaults),
                        "reference": template.reference,
                        "tags": list(template.tags),
                        "author": template.author,
                        "source_url": template.source_url,
                    }
                )
                results.append({"id": remote_id, "ok": True, "local_id": created.id})
            except TemplateValidationError as exc:
                results.append({"id": remote_id, "ok": False, "message": str(exc)})
        return {
            "imported": sum(1 for item in results if item["ok"]),
            "failed": sum(1 for item in results if not item["ok"]),
            "results": results,
        }

    async def hub_key_request(self, email: str) -> dict[str, Any]:
        await self._with_source().request_key(email)
        return {"status": "sent"}

    async def hub_key_check(self) -> dict[str, Any]:
        payload = await self._with_source().key_status()
        return {"status": "ok", "key": payload}

    async def hub_key_revoke(self) -> dict[str, Any]:
        await self._with_source().revoke_key()
        return {"status": "revoked"}

    async def save_hub_key(self, key: Any) -> dict[str, Any]:
        """把在线模板密钥写入插件配置；空串表示清除。"""

        value = str(key or "").strip()
        raw = self.raw_config
        if raw is None:
            raise StudioServiceError(
                "当前宿主不提供配置保存接口，密钥只能填写在插件配置里",
                status_code=503,
            )
        save_async = getattr(raw, "save_config_async", None)
        save_sync = getattr(raw, "save_config", None)
        if not callable(save_async) and not callable(save_sync):
            raise StudioServiceError(
                "当前宿主不提供配置保存接口，密钥只能填写在插件配置里",
                status_code=503,
            )
        async with self._config_lock:
            if self._is_closed():
                raise StudioServiceError("插件正在关闭", status_code=503)
            if callable(save_async):
                await save_async({"template_hub_key": value})
            else:
                await asyncio.to_thread(save_sync, {"template_hub_key": value})
            self.reconfigure_hub(key=value)
        return {**self.hub_state(), **self.hub_key_masked()}

    def hub_key_masked(self) -> dict[str, Any]:
        """返回已配置密钥的掩码，用于在页面上确认是否填写。"""

        value = str(self._hub_key or "").strip()
        return {
            "configured": bool(value),
            "masked": _mask_key(value),
        }

    async def hub_key_test(self) -> dict[str, Any]:
        """用已保存的密钥测试连接。"""

        if not str(self._hub_key or "").strip():
            raise StudioServiceError("尚未填写在线模板密钥", status_code=400)
        try:
            payload = await self._with_source().key_status()
        except SourceAuthError as exc:
            raise StudioServiceError(_source_message(exc), status_code=401) from exc
        except TemplateSourceError as exc:
            raise StudioServiceError(_source_message(exc), status_code=502) from exc
        return {"status": "ok", "key": payload}

    async def hub_upload(self, template_id: Any) -> dict[str, Any]:
        """上传本地模板；先展示内容再由前端确认，这里只负责发送。"""

        found = self.store.get(_require_id(template_id))
        if found is None:
            raise StudioServiceError("模板不存在", status_code=404)
        try:
            uploaded = await self._with_source().upload(found)
        except SourceAuthError as exc:
            raise StudioServiceError(_source_message(exc), status_code=401) from exc
        except SourceRateLimited as exc:
            raise StudioServiceError(_source_message(exc), status_code=429) from exc
        except SourceRejected as exc:
            raise StudioServiceError(str(exc), status_code=400) from exc
        except TemplateSourceError as exc:
            raise StudioServiceError(_source_message(exc), status_code=502) from exc
        return _public_record(uploaded)

    def hub_upload_preview(self, template_id: Any) -> dict[str, Any]:
        """上传前展示将发送的完整内容。"""

        found = self.store.get(_require_id(template_id))
        if found is None:
            raise StudioServiceError("模板不存在", status_code=404)
        record = _public_record(found)
        return {
            "id": record["id"],
            "name": record["name"],
            "ai_description": record["ai_description"],
            "content": record["content"],
            "defaults": record["defaults"],
            "reference": record["reference"],
            "tags": record["tags"],
            "author": record["author"],
            "source_url": record["source_url"],
        }

    async def hub_report(self, remote_id: Any, reason: Any) -> dict[str, Any]:
        """举报在线模板；不需要密钥，但要给出原因。"""

        identifier = str(remote_id or "").strip()
        text = str(reason or "").strip()
        if not identifier:
            raise StudioServiceError("缺少在线模板 id", status_code=400)
        if not text:
            raise StudioServiceError("举报需要填写原因", status_code=400)
        try:
            await self._with_source().report(identifier, text)
        except SourceRateLimited as exc:
            raise StudioServiceError(_source_message(exc), status_code=429) from exc
        except SourceRejected as exc:
            raise StudioServiceError(str(exc), status_code=400) from exc
        except TemplateSourceError as exc:
            raise StudioServiceError(_source_message(exc), status_code=502) from exc
        return {"status": "reported"}

    async def hub_update(self, remote_id: Any, changes: Any) -> dict[str, Any]:
        """修改自己上传的在线模板；只接受展示用的可编辑字段。"""

        identifier = str(remote_id or "").strip()
        if not identifier:
            raise StudioServiceError("缺少在线模板 id", status_code=400)
        if not isinstance(changes, dict) or not changes:
            raise StudioServiceError("没有需要修改的字段", status_code=400)
        allowed = {"name", "ai_description", "content", "tags", "reference"}
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise StudioServiceError(
                f"不支持修改的字段：{', '.join(unknown)}", status_code=400
            )
        found = self.store.get(str(changes.get("id") or "").strip())
        if found is None:
            raise StudioServiceError("请提供本地模板 id 作为修改来源", status_code=400)
        draft = copy.deepcopy(found)
        for field, value in changes.items():
            if field == "tags" and isinstance(value, list):
                draft.tags = [str(item).strip() for item in value if str(item).strip()]
                continue
            setattr(draft, field, str(value or "").strip())
        try:
            updated = await self._with_source().update(identifier, draft)
        except SourceAuthError as exc:
            raise StudioServiceError(_source_message(exc), status_code=401) from exc
        except SourceRateLimited as exc:
            raise StudioServiceError(_source_message(exc), status_code=429) from exc
        except SourceRejected as exc:
            raise StudioServiceError(str(exc), status_code=400) from exc
        except TemplateSourceError as exc:
            raise StudioServiceError(_source_message(exc), status_code=502) from exc
        return _public_record(updated)

    async def hub_delete(self, remote_id: Any) -> dict[str, Any]:
        """删除自己上传的在线模板，需要密钥。"""

        identifier = str(remote_id or "").strip()
        if not identifier:
            raise StudioServiceError("缺少在线模板 id", status_code=400)
        try:
            await self._with_source().delete(identifier)
        except SourceAuthError as exc:
            raise StudioServiceError(_source_message(exc), status_code=401) from exc
        except SourceRateLimited as exc:
            raise StudioServiceError(_source_message(exc), status_code=429) from exc
        except SourceRejected as exc:
            raise StudioServiceError(str(exc), status_code=400) from exc
        except TemplateSourceError as exc:
            raise StudioServiceError(_source_message(exc), status_code=502) from exc
        return {"status": "deleted"}

    def _with_source(self) -> HubTemplateSource:
        if self.hub_source is None:
            raise StudioServiceError("在线模板已关闭或未配置服务地址", status_code=400)
        return self.hub_source

    async def close(self) -> None:
        source, self.hub_source = self.hub_source, None
        await _drain(source)

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


def _mask_key(value: str) -> str:
    """只保留首尾各 4 位，避免页面回显完整密钥。"""

    text = str(value or "").strip()
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}{'*' * 8}{text[-4:]}"


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
        "remote_id": template.remote.remote_id if template.remote else None,
        "remote_source": template.remote.source if template.remote else None,
    }


def _source_message(exc: Exception) -> str:
    if isinstance(exc, SourceRateLimited):
        return f"在线服务限流，请 {max(int(exc.retry_after), 1)} 秒后重试"
    if isinstance(exc, SourceAuthError):
        return "在线服务密钥无效或权限不足"
    if isinstance(exc, SourceRejected):
        return str(exc)
    return "在线模板服务暂时不可用"


def _schedule_close(source: Any) -> None:
    """配置变更时关闭旧客户端；无事件循环时只能丢弃引用。"""

    if source is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_drain(source))


async def _drain(source: Any) -> None:
    if source is None:
        return
    try:
        await source.close()
    except Exception as exc:
        logger.warning("[模板广场] 关闭在线源失败: %s", exc)
