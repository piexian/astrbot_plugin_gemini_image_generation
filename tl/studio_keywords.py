"""Studio 关键词触发配置：绑定读写、版本冲突保护与触发预演。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .core.errors import redact_text
from .keyword_trigger import (
    MAX_BINDINGS,
    MAX_PROMPT_APPEND_TOTAL,
    REF_ID,
    normalize_binding,
    normalize_trigger_settings,
)
from .web_studio_service import StudioServiceError


def _short_revision(value: Any) -> str:
    return str(value or "")[:8] or "未知"


class StudioKeywordService:
    """关键词配置的读写编排：文件为唯一事实源，保存即刷新运行时匹配器。"""

    def __init__(
        self,
        raw_config,
        config,
        library,
        keyword_trigger,
        max_slots_fn: Callable[[], int],
        data_dir,
        *,
        config_lock=None,
        is_closed=None,
    ):
        self.raw_config = raw_config
        self.config = config
        self.library = library
        self.keyword_trigger = keyword_trigger
        self._max_slots_fn = max_slots_fn
        self.data_dir = Path(data_dir)
        self._config_lock = config_lock if config_lock is not None else asyncio.Lock()
        self._is_closed = is_closed or (lambda: False)

    def _raw_section(self) -> dict[str, Any]:
        section = self.raw_config.get("keyword_trigger")
        return section if isinstance(section, dict) else {}

    def _revision(self) -> str:
        data = json.dumps(self._raw_section(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(data.encode()).hexdigest()

    def _ref_state(self, ref_id: str) -> dict[str, Any]:
        entry = self.library.get(ref_id) if self.library is not None else None
        return {
            "id": ref_id,
            "label": (entry or {}).get("label") or ref_id,
            "missing": entry is None,
        }

    def get_keywords(self) -> dict[str, Any]:
        """页面数据：设置、绑定及每条绑定的参考图标签与缺失态。"""
        raw = copy.deepcopy(self._raw_section())
        settings = normalize_trigger_settings(raw)
        raw_bindings = raw.get("bindings")
        bindings = []
        if isinstance(raw_bindings, list):
            for entry in raw_bindings:
                if not isinstance(entry, dict):
                    continue
                refs = [
                    self._ref_state(str(ref_id))
                    for ref_id in entry.get("ref_ids") or []
                ]
                bindings.append({**entry, "refs": refs})
        return {
            "revision": self._revision(),
            "settings": {
                "mode": settings.mode,
                "wrapper_left": settings.wrapper_left,
                "wrapper_right": settings.wrapper_right,
                "max_refs": settings.max_refs,
            },
            "bindings": bindings,
        }

    def preview(self, text: str, manual_count: int) -> dict[str, Any]:
        """触发预演：与运行时共用同一个匹配器；不返回本地路径。"""
        slots = max(self._max_slots_fn() - max(int(manual_count or 0), 0), 0)
        mode = self.keyword_trigger.mode
        expansion = (
            self.keyword_trigger.expand_auto(text, slots)
            if mode == "auto"
            else self.keyword_trigger.expand_explicit(text, slots)
        )
        if expansion is None:
            return {
                "mode": mode,
                "hits": [],
                "refs": [],
                "prompt_append": "",
                "dropped": [],
                "slots": slots,
            }
        return {
            "mode": mode,
            "hits": expansion.hits,
            "refs": [
                {
                    "id": item["id"],
                    "label": item["label"],
                    "missing": item["missing"],
                }
                for item in expansion.refs
            ],
            "prompt_append": expansion.prompt_append,
            "dropped": expansion.dropped,
            "slots": slots,
        }

    def _validate_settings(self, raw: Any) -> dict[str, Any]:
        source = raw if isinstance(raw, dict) else {}
        left = str(source.get("wrapper_left") or "").strip()[:2]
        right = str(source.get("wrapper_right") or "").strip()[:2]
        if bool(left) != bool(right):
            raise StudioServiceError("包裹符必须左右同空或同有值")
        return {
            "mode": source.get("mode"),
            "wrapper_left": left,
            "wrapper_right": right,
            "max_refs": source.get("max_refs"),
        }

    def _previously_configured_ids(self) -> set[str]:
        ids: set[str] = set()
        for entry in self._raw_section().get("bindings") or []:
            if isinstance(entry, dict):
                for ref_id in entry.get("ref_ids") or []:
                    ids.add(str(ref_id))
        return ids

    def _validate_bindings(self, raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            raise StudioServiceError("bindings 必须是数组")
        if len(raw) > MAX_BINDINGS:
            raise StudioServiceError(f"绑定最多 {MAX_BINDINGS} 条")
        previous_ids = self._previously_configured_ids()
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(raw, 1):
            if not isinstance(entry, dict):
                raise StudioServiceError(f"第 {index} 条绑定必须是对象")
            ref_ids = entry.get("ref_ids") or []
            if not isinstance(ref_ids, list):
                raise StudioServiceError(f"第 {index} 条绑定的 ref_ids 必须是数组")
            for raw_id in ref_ids:
                ref_id = str(raw_id or "")
                if not REF_ID.fullmatch(ref_id):
                    raise StudioServiceError(
                        f"第 {index} 条绑定的参考图条目 ID 非法: {ref_id}"
                    )
                exists = (
                    self.library is not None and self.library.get(ref_id) is not None
                )
                if not exists and ref_id not in previous_ids:
                    raise StudioServiceError(
                        f"第 {index} 条绑定引用的参考图库条目不存在: {ref_id}"
                    )
            binding = normalize_binding(entry, index - 1)
            if binding is None:
                raise StudioServiceError(f"第 {index} 条绑定缺少触发关键词")
            cleaned.append(
                {
                    "__template_key": "binding",
                    "name": binding.name,
                    "words": binding.words,
                    "ref_ids": binding.ref_ids,
                    "prompt_append": binding.prompt_append,
                    "enabled": binding.enabled,
                }
            )
        total_append = sum(len(item["prompt_append"]) for item in cleaned)
        if total_append > MAX_PROMPT_APPEND_TOTAL:
            raise StudioServiceError(
                f"全部绑定的提示词片段合计不能超过 {MAX_PROMPT_APPEND_TOTAL} 字符"
            )
        return cleaned

    async def save_keywords(self, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) != {"revision", "settings", "bindings"} or not isinstance(
            payload.get("revision"), str
        ):
            raise StudioServiceError("请提交关键词设置、绑定及其版本")
        settings = self._validate_settings(payload["settings"])
        bindings = self._validate_bindings(payload["bindings"])
        task = asyncio.create_task(
            self._save_transaction(payload["revision"], settings, bindings)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(task)
            except Exception:
                logger.warning("[关键词] 请求取消后的配置事务失败", exc_info=True)
            raise

    async def _save_transaction(
        self, revision: str, settings: dict[str, Any], bindings: list[dict[str, Any]]
    ) -> dict[str, Any]:
        normalized = normalize_trigger_settings(settings)
        merged = {
            "mode": normalized.mode,
            "wrapper_left": normalized.wrapper_left,
            "wrapper_right": normalized.wrapper_right,
            "max_refs": normalized.max_refs,
            "bindings": bindings,
        }
        async with self._config_lock:
            if self._is_closed():
                raise StudioServiceError("插件正在关闭", status_code=503)
            if revision != self._revision():
                logger.warning(
                    "[关键词] 保存版本冲突: "
                    f"提交 revision={_short_revision(revision)}, "
                    f"实际 revision={_short_revision(self._revision())}"
                )
                raise StudioServiceError(
                    "关键词配置已更新，请重新加载后再保存", status_code=409
                )
            save_async = getattr(self.raw_config, "save_config_async", None)
            save_sync = getattr(self.raw_config, "save_config", None)
            if not callable(save_async) and not callable(save_sync):
                raise StudioServiceError(
                    "当前宿主不提供配置保存接口，关键词配置只读", status_code=503
                )
            previous = copy.deepcopy(self._raw_section())
            try:
                await asyncio.to_thread(self._backup, previous)
                update = {"keyword_trigger": merged}
                if callable(save_async):
                    committed = await save_async(update)
                else:
                    committed = await asyncio.to_thread(save_sync, update)
            except Exception as exc:
                if self._raw_section() == merged:
                    self.raw_config["keyword_trigger"] = previous
                logger.warning(
                    "[关键词] 配置保存失败，保留原运行时配置: "
                    f"原因={redact_text(str(exc))[:240]}",
                    exc_info=True,
                )
                raise StudioServiceError(
                    "保存关键词配置失败，原配置保持不变", status_code=500
                ) from exc
            if committed is False or self._raw_section() != merged:
                logger.warning(
                    "[关键词] 宿主已更新配置，拒绝覆盖: "
                    f"提交 revision={_short_revision(revision)}"
                )
                raise StudioServiceError(
                    "配置已被宿主的新版本更新，请重新加载", status_code=409
                )
            self.config.keyword_trigger = copy.deepcopy(merged)
            self.keyword_trigger.refresh(merged)
            logger.info(
                f"[关键词] Studio 配置已保存并即时生效: "
                f"绑定条数={len(bindings)}, 模式={normalized.mode}"
            )
            return self.get_keywords()

    def _backup(self, previous: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        destination = self.data_dir / "keyword_config_backup.json"
        if destination.exists():
            return
        fd, name = tempfile.mkstemp(prefix=".keyword-backup-", dir=self.data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(previous, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(name, destination)
            except FileExistsError:
                pass
        finally:
            os.unlink(name)
