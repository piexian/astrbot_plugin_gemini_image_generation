"""Studio 限流配置持久化与本体 UMO 会话查询。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .limit_config import (
    GROUP_ACCESS_KEYS,
    LIMIT_KEYS,
    apply_limits,
    normalize_limits,
    pending_migration,
    validate_umo,
)
from .web_studio_service import StudioServiceError


class StudioLimitsService:
    def __init__(self, raw_config, config, limiter, context, data_dir):
        self.raw_config = raw_config
        self.config = config
        self.limiter = limiter
        self.context = context
        self.data_dir = Path(data_dir)
        self._session_lock = asyncio.Lock()
        self._session_cache: list[dict[str, str]] | None = None
        self._session_cache_time = 0.0

    def _revision(self) -> str:
        data = json.dumps(
            self.raw_config.get("limit_settings", {}),
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(data.encode()).hexdigest()

    def get_limits(self) -> dict[str, Any]:
        limits = copy.deepcopy({key: getattr(self.config, key) for key in LIMIT_KEYS})
        limits.update(
            group_limit_mode=self.config.group_limit_mode,
            group_limit_list=sorted(self.config.group_limit_list),
        )
        pending = pending_migration(limits)
        error = getattr(self.config, "limit_config_error", "")
        message = ""
        if error:
            message = f"当前限流配置无效，生成已暂停：{error}。请修正插件配置后重载。"
        elif pending:
            message = "启用的旧群号规则尚未迁移，聊天生图已暂停。请选择完整 UMO 并确认迁移；停用或删除规则也会解除该保护。"
        elif self.limiter.legacy_until > time.time():
            message = "旧群级计数不能精确拆分，聊天请求将在原计数窗口结束后恢复；旧 KV 保留作为备份。"
        return {
            "revision": self._revision(),
            "limits": limits,
            "migration": {
                "pending": pending,
                "message": message,
                "cooldown_until": self.limiter.legacy_until,
            },
        }

    async def load_limits(self) -> dict[str, Any]:
        async with self.limiter._rate_limit_lock:
            if self.limiter._closed:
                raise StudioServiceError("插件正在关闭", status_code=503)
            try:
                await self.limiter._load_from_kv()
            except Exception as exc:
                logger.warning("[限流] 无法读取持久化计数", exc_info=True)
                raise StudioServiceError("限流计数暂时不可用", status_code=503) from exc
            return self.get_limits()

    def _backup(self, previous: dict[str, Any], *, group_access: bool = False) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        name = (
            "group_access_config_backup.json"
            if group_access
            else "rate_limit_config_backup.json"
        )
        keys = GROUP_ACCESS_KEYS if group_access else LIMIT_KEYS
        destination = self.data_dir / name
        if destination.exists():
            return
        fd, name = tempfile.mkstemp(prefix=".rate-limit-backup-", dir=self.data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {key: value for key, value in previous.items() if key in keys},
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(name, destination)
            except FileExistsError:
                pass
        finally:
            os.unlink(name)

    async def save_limits(self, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) != {"revision", "limits"} or not isinstance(
            payload.get("revision"), str
        ):
            raise StudioServiceError("请提交限流配置及其版本")
        try:
            limits = normalize_limits(payload["limits"])
        except ValueError as exc:
            raise StudioServiceError(str(exc)) from exc
        if pending_migration(limits):
            raise StudioServiceError(
                "启用规则中仍有旧群号，请选择 UMO 并确认迁移，或明确停用该规则"
            )
        # 客户端断开不能中断已经开始的磁盘事务，避免磁盘和运行时分叉。
        task = asyncio.create_task(self._save_transaction(payload["revision"], limits))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(task)
            except Exception:
                logger.warning("[限流] 请求取消后的配置事务失败", exc_info=True)
            raise

    async def _save_transaction(
        self, revision: str, limits: dict[str, Any]
    ) -> dict[str, Any]:
        async with self.limiter._rate_limit_lock:
            if self.limiter._closed:
                raise StudioServiceError("插件正在关闭", status_code=503)
            if revision != self._revision():
                raise StudioServiceError(
                    "限流配置已更新，请重新加载后再保存", status_code=409
                )
            if getattr(self.config, "limit_config_error", ""):
                raise StudioServiceError(
                    "现有配置格式无效，请先在插件配置中修正，避免覆盖无法展示的规则",
                    status_code=409,
                )
            save_async = getattr(self.raw_config, "save_config_async", None)
            save_sync = getattr(self.raw_config, "save_config", None)
            if not callable(save_async) and not callable(save_sync):
                raise StudioServiceError(
                    "当前宿主不提供配置保存接口，限流配置只读", status_code=503
                )
            try:
                await self.limiter._load_from_kv()
            except Exception as exc:
                logger.warning("[限流] 保存配置前恢复计数失败", exc_info=True)
                raise StudioServiceError(
                    "限流计数暂时不可用，配置未保存", status_code=503
                ) from exc
            previous = copy.deepcopy(self.raw_config.get("limit_settings", {}))
            merged = {**previous, **copy.deepcopy(limits)}
            for rule in merged["rate_limit_rules"]:
                rule["__template_key"] = "rule"
            try:
                await asyncio.to_thread(self._backup, previous)
                if "group_limit_mode" in limits:
                    await asyncio.to_thread(self._backup, previous, group_access=True)
                update = {"limit_settings": merged}
                if callable(save_async):
                    committed = await save_async(update)
                else:
                    committed = await asyncio.to_thread(save_sync, update)
            except Exception as exc:
                if self.raw_config.get("limit_settings") == merged:
                    self.raw_config["limit_settings"] = previous
                logger.warning("[限流] 配置保存失败，保留原运行时规则", exc_info=True)
                raise StudioServiceError(
                    "保存限流配置失败，原规则保持不变", status_code=500
                ) from exc
            if committed is False or self.raw_config.get("limit_settings") != merged:
                # 宿主自己的更新优先，不回写旧快照覆盖新版本。
                try:
                    apply_limits(
                        self.config,
                        normalize_limits(
                            self.raw_config.get("limit_settings", {}), strict=False
                        ),
                    )
                except ValueError:
                    self.config.limit_config_error = "并发保存后的限流配置无效"
                raise StudioServiceError(
                    "配置已被宿主的新版本更新，请重新加载", status_code=409
                )
            apply_limits(self.config, copy.deepcopy(limits))
            await self.limiter._save_to_kv(force=True)
            logger.info("[限流] Studio 配置已保存并即时生效，保留现有计数")
            return self.get_limits()

    async def _load_sessions(self) -> list[dict[str, str]]:
        from astrbot.core.db.po import ConversationV2
        from sqlmodel import select

        db = self.context.get_db()
        async with db.get_db() as session:
            result = await session.execute(select(ConversationV2.user_id).distinct())
            umos = {str(row[0]) for row in result.fetchall() if row[0]}
        alias_getter = getattr(db, "get_umo_aliases", None)
        aliases = await alias_getter() if callable(alias_getter) else []
        alias_map = {str(alias.umo): alias for alias in aliases if alias.umo}
        umos.update(alias_map)
        records = []
        for raw in sorted(umos):
            try:
                umo = validate_umo(raw)
            except ValueError:
                continue
            platform, message_type, session_id = umo.split(":", 2)
            alias = alias_map.get(raw)
            display = (
                getattr(alias, "user_alias", "")
                or getattr(alias, "auto_name", "")
                or umo
            )
            records.append(
                {
                    "umo": umo,
                    "display_name": str(display)[:1024],
                    "platform": platform,
                    "message_type": message_type,
                    "session_id": session_id,
                }
            )
        return records

    async def sessions(
        self,
        *,
        page: int,
        page_size: int,
        search: str,
        message_type: str,
        platform: str,
    ) -> dict[str, Any]:
        if message_type not in {"all", "group", "private"}:
            raise StudioServiceError("无效的会话类型筛选")
        response = {
            "sessions": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
            "available": True,
            "warning": "",
        }
        try:
            async with self._session_lock:
                if (
                    self._session_cache is None
                    or time.monotonic() - self._session_cache_time >= 10
                ):
                    records = await self._load_sessions()
                    self._session_cache = records
                    self._session_cache_time = time.monotonic()
                records = self._session_cache
        except Exception:
            logger.warning(
                "[限流] 本体会话列表不可用，可手动填写完整 UMO", exc_info=True
            )
            return {
                **response,
                "available": False,
                "warning": "当前无法读取本体已有会话，请稍后重试或手动填写完整 UMO",
            }
        needle = search.casefold()
        types = {"group": "GroupMessage", "private": "FriendMessage"}
        records = [
            record
            for record in records
            if (not platform or record["platform"] == platform)
            and (message_type == "all" or record["message_type"] == types[message_type])
            and (
                not needle
                or needle in record["umo"].casefold()
                or needle in record["display_name"].casefold()
            )
        ]
        start = (page - 1) * page_size
        return {
            **response,
            "sessions": records[start : start + page_size],
            "total": len(records),
        }
