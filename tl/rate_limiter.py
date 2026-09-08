"""共享全局额度、UMO 会话额度与群访问控制。"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import time
import uuid
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from astrbot.api import logger

from .limit_config import MAX_PERIOD, MAX_REQUESTS, validate_umo

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

    from .plugin_config import PluginConfig


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    message: str | None = None
    scope: str | None = None
    retry_after: int = 0
    token: str | None = None


class RateLimiter:
    """在同一锁内检查和扣减所有生效额度。"""

    KV_KEY = "rate_limit_buckets_v2"
    LEGACY_KV_KEY = "rate_limit_buckets"
    SAVE_DEBOUNCE_SECONDS = 1.0
    GLOBAL_KEY = "global"

    def __init__(
        self,
        config: PluginConfig,
        *,
        get_kv: Callable[[str, Any], Coroutine[Any, Any, Any]] | None = None,
        put_kv: Callable[[str, Any], Coroutine[Any, Any, None]] | None = None,
    ):
        self.config = config
        self._rate_limit_buckets: dict[str, list[dict[str, Any]]] = {}
        self._rate_limit_lock = asyncio.Lock()
        self._save_lock = asyncio.Lock()
        self._get_kv = get_kv
        self._put_kv = put_kv
        self._loaded = False
        self._closed = False
        self._last_save_time = 0.0
        self._pending_save_task: asyncio.Task | None = None
        self._pending_save_data: dict[str, Any] = {}
        self.legacy_until = 0.0
        self._last_prune = 0.0

    async def _load_from_kv(self) -> None:
        """仅在准入锁内执行；读取失败不视为成功加载。"""
        if self._loaded:
            return
        if self._get_kv:
            data = await self._get_kv(self.KV_KEY, None)
            if isinstance(data, str):
                data = json.loads(data)
            if data is None:
                legacy = await self._get_kv(self.LEGACY_KV_KEY, None)
                if isinstance(legacy, str):
                    legacy = json.loads(legacy)
                if legacy is not None:
                    if not isinstance(legacy, dict):
                        raise ValueError("Invalid legacy rate-limit state")
                    stamps = [ts for bucket in legacy.values() for ts in bucket]
                    if any(
                        type(ts) not in (int, float) or not math.isfinite(ts)
                        for ts in stamps
                    ):
                        raise ValueError("Invalid legacy timestamps")
                    # 旧群桶无法精确拆分到 UMO，最多等待原规则窗口，不冒然清零。
                    self.legacy_until = max(stamps, default=0) + self._retention()
                    if self.legacy_until <= time.time():
                        self.legacy_until = 0.0
            else:
                if not isinstance(data, dict) or data.get("version") != 2:
                    raise ValueError("Invalid rate-limit state version")
                buckets = data.get("buckets")
                if not isinstance(buckets, dict):
                    raise ValueError("Invalid rate-limit buckets")
                for key, bucket in buckets.items():
                    if key.startswith("plugin/") and ":" not in key:
                        if not key.removeprefix("plugin/").strip():
                            raise ValueError("Invalid plugin rate-limit key")
                    elif key != self.GLOBAL_KEY:
                        validate_umo(key)
                    if not isinstance(bucket, list):
                        raise ValueError("Invalid rate-limit bucket")
                    for item in bucket:
                        if (
                            not isinstance(item, dict)
                            or type(item.get("at")) not in (int, float)
                            or not math.isfinite(item["at"])
                            or type(item.get("count")) is not int
                            or not 1 <= item["count"] <= MAX_REQUESTS
                            or not isinstance(item.get("token"), str)
                        ):
                            raise ValueError("Invalid rate-limit entry")
                until = data.get("legacy_until", 0)
                if type(until) not in (int, float) or not math.isfinite(until):
                    raise ValueError("Invalid migration cooldown")
                self._rate_limit_buckets = copy.deepcopy(buckets)
                self.legacy_until = until
        self._loaded = True

    def _retention(self) -> int:
        policies = [
            getattr(self.config, "global_rate_limit", {}),
            self.config.default_rate_limit,
            *self.config.rate_limit_rules,
        ]
        return min(
            max((p.get("period_seconds", 60) for p in policies), default=60), MAX_PERIOD
        )

    def _prune(self, now: float) -> None:
        if now - self._last_prune < 60:
            return
        cutoff = now - self._retention()
        for key in list(self._rate_limit_buckets):
            bucket = [
                entry for entry in self._rate_limit_buckets[key] if entry["at"] > cutoff
            ]
            if bucket:
                self._rate_limit_buckets[key] = bucket
            else:
                del self._rate_limit_buckets[key]
        self._last_prune = now

    async def _write_to_kv(self) -> None:
        if not self._put_kv:
            return
        try:
            async with self._save_lock:
                await self._put_kv(self.KV_KEY, copy.deepcopy(self._pending_save_data))
                self._last_save_time = time.monotonic()
        except Exception:
            logger.warning(
                "[限流] 计数持久化失败，保留内存计数并在下次保存时重试", exc_info=True
            )

    async def _delayed_save_to_kv(self, delay: float) -> None:
        try:
            await asyncio.sleep(max(delay, 0.0))
            await self._write_to_kv()
        finally:
            if self._pending_save_task is asyncio.current_task():
                self._pending_save_task = None

    async def _save_to_kv(self, *, force: bool = False) -> None:
        if not self._put_kv:
            return
        self._pending_save_data = {
            "version": 2,
            "buckets": copy.deepcopy(self._rate_limit_buckets),
            "legacy_until": self.legacy_until,
        }
        if force:
            task = self._pending_save_task
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            self._pending_save_task = None
            await self._write_to_kv()
            return
        elapsed = time.monotonic() - self._last_save_time
        if elapsed >= self.SAVE_DEBOUNCE_SECONDS:
            await self._write_to_kv()
        elif not self._pending_save_task or self._pending_save_task.done():
            self._pending_save_task = asyncio.create_task(
                self._delayed_save_to_kv(self.SAVE_DEBOUNCE_SECONDS - elapsed)
            )

    def get_group_id_from_event(self, event: AstrMessageEvent) -> str | None:
        """裸群号仅用于保留原有群黑白名单。"""
        try:
            if getattr(event, "group_id", None):
                return str(event.group_id)
            message_obj = getattr(event, "message_obj", None)
            if message_obj and getattr(message_obj, "group_id", ""):
                return str(message_obj.group_id)
        except Exception:
            logger.debug("[限流] 获取群访问控制标识失败", exc_info=True)
        return None

    def _session_policy(self, umo: str) -> dict[str, Any]:
        for rule in self.config.rate_limit_rules:
            if rule.get("enabled", True) and (
                not rule.get("umos") or umo in rule["umos"]
            ):
                return rule
        return self.config.default_rate_limit

    async def acquire(
        self,
        umo: str | None,
        *,
        cost: int = 1,
        event: AstrMessageEvent | None = None,
        plugin_id: str | None = None,
    ) -> LimitDecision:
        """预留逻辑任务额度；无 UMO 的插件按稳定标识使用默认规则。"""
        if type(cost) is not int or not 1 <= cost <= MAX_REQUESTS:
            return LimitDecision(False, "无效的生成任务数量", "input")
        if umo is not None:
            try:
                umo = validate_umo(umo)
            except ValueError:
                return LimitDecision(
                    False, "无法识别完整会话 UMO，已拒绝生成", "session"
                )
        async with self._rate_limit_lock:
            if self._closed:
                return LimitDecision(False, "插件正在关闭", "closed")
            # 等待锁期间名单可能热更新，扣减前重新检查访问权限。
            if event is not None and not self.allows_group(event):
                return LimitDecision(False)
            if getattr(self.config, "limit_config_error", ""):
                return LimitDecision(False, "限流配置无效，请管理员修正配置", "config")
            try:
                await self._load_from_kv()
            except Exception:
                logger.warning("[限流] 无法恢复计数，本次请求未放行", exc_info=True)
                return LimitDecision(False, "限流计数暂时不可用，请稍后重试", "storage")
            now = time.time()
            if umo is not None:
                if any(
                    r.get("enabled", True) and r.get("group_ids")
                    for r in self.config.rate_limit_rules
                ):
                    return LimitDecision(
                        False,
                        "旧群号限流规则待迁移，聊天生图已暂停；请管理员在 Studio 限流控制中确认 UMO",
                        "migration",
                    )
                if self.legacy_until > now:
                    retry = math.ceil(self.legacy_until - now)
                    return LimitDecision(
                        False,
                        f"旧群级计数迁移保护中，请 {retry} 秒后再试",
                        "migration",
                        retry,
                    )
            self._prune(now)
            policies = [
                (
                    self.GLOBAL_KEY,
                    "global",
                    getattr(self.config, "global_rate_limit", {}),
                )
            ]
            if umo is not None:
                policies.append((umo, "session", self._session_policy(umo)))
            elif plugin_id:
                policies.append(
                    (
                        f"plugin/{quote(plugin_id, safe='')}",
                        "plugin",
                        self.config.default_rate_limit,
                    )
                )
            active = [
                (key, scope, p) for key, scope, p in policies if p.get("enabled", False)
            ]
            denials = []
            for key, scope, policy in active:
                period, maximum = policy["period_seconds"], policy["max_requests"]
                label = {"global": "全局", "session": "当前会话", "plugin": "当前插件"}[
                    scope
                ]
                if cost > maximum:
                    return LimitDecision(
                        False,
                        f"本次任务数 {cost} 超过{label}单周期额度 {maximum}，请减少批量条目",
                        scope,
                    )
                entries = sorted(
                    (
                        entry
                        for entry in self._rate_limit_buckets.get(key, [])
                        if entry["at"] > now - period
                    ),
                    key=lambda entry: entry["at"],
                )
                excess = sum(entry["count"] for entry in entries) + cost - maximum
                if excess > 0:
                    for entry in entries:
                        excess -= entry["count"]
                        if excess <= 0:
                            retry = max(math.ceil(entry["at"] + period - now), 1)
                            denials.append(
                                LimitDecision(
                                    False,
                                    f"{label}最近 {period} 秒内的生图请求已达上限（{maximum} 次），请 {retry} 秒后再试",
                                    scope,
                                    retry,
                                )
                            )
                            break
            if denials:
                return max(denials, key=lambda decision: decision.retry_after)
            token = uuid.uuid4().hex if active else None
            for key, _scope, _policy in active:
                self._rate_limit_buckets.setdefault(key, []).append(
                    {"at": now, "count": cost, "token": token}
                )
            try:
                await self._save_to_kv()
            except asyncio.CancelledError:
                self._remove_token(token)
                await self._save_to_kv(force=True)
                raise
            return LimitDecision(True, token=token)

    def _remove_token(self, token: str | None) -> None:
        if token:
            for key, bucket in list(self._rate_limit_buckets.items()):
                remaining = [entry for entry in bucket if entry["token"] != token]
                if remaining:
                    self._rate_limit_buckets[key] = remaining
                else:
                    del self._rate_limit_buckets[key]

    async def refund(self, token: str | None) -> None:
        if not token:
            return
        async with self._rate_limit_lock:
            self._remove_token(token)
            await self._save_to_kv(force=True)

    def allows_group(self, event: AstrMessageEvent) -> bool:
        """仅检查访问权限，不消费额度，供命令发送提示前使用。"""
        group_id = self.get_group_id_from_event(event)
        if group_id and self.config.group_limit_list:
            listed = group_id in self.config.group_limit_list
            if (self.config.group_limit_mode == "whitelist" and not listed) or (
                self.config.group_limit_mode == "blacklist" and listed
            ):
                return False
        return True

    async def check_and_consume(
        self, event: AstrMessageEvent, *, cost: int = 1
    ) -> tuple[bool, str | None]:
        if not self.allows_group(event):
            return False, None
        umo = getattr(event, "unified_msg_origin", "")
        if not umo:
            return False, "无法识别完整会话 UMO，已拒绝生成"
        decision = await self.acquire(umo, cost=cost, event=event)
        return decision.allowed, decision.message

    async def reset(self) -> None:
        async with self._rate_limit_lock:
            await self._load_from_kv()
            self._rate_limit_buckets.clear()
            self.legacy_until = 0.0
            await self._save_to_kv(force=True)

    async def close(self) -> None:
        async with self._rate_limit_lock:
            self._closed = True
            # 未使用的 limiter 不覆盖磁盘上尚未加载的计数。
            if self._loaded:
                await self._save_to_kv(force=True)
