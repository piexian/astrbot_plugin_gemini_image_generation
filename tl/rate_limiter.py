"""群限制和限流模块（支持多规则限流）"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from astrbot.api import logger

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

    from .plugin_config import PluginConfig


class RateLimiter:
    """群限制和限流管理器（支持 KV 持久化 + 多规则限流）"""

    KV_KEY = "rate_limit_buckets"

    def __init__(
        self,
        config: PluginConfig,
        *,
        get_kv: Callable[[str, Any], Coroutine[Any, Any, Any]] | None = None,
        put_kv: Callable[[str, Any], Coroutine[Any, Any, None]] | None = None,
    ):
        self.config = config
        self._rate_limit_buckets: dict[str, list[float]] = {}
        self._rate_limit_lock = asyncio.Lock()
        # KV 存储回调
        self._get_kv = get_kv
        self._put_kv = put_kv
        self._loaded = False

    async def _load_from_kv(self) -> None:
        """从 KV 存储加载限流数据"""
        if self._loaded or not self._get_kv:
            return
        try:
            data = await self._get_kv(self.KV_KEY, None)
            if data:
                # 数据格式: {"group_id": [timestamp1, timestamp2, ...], ...}
                if isinstance(data, str):
                    data = json.loads(data)
                if isinstance(data, dict):
                    self._rate_limit_buckets = data
                    logger.debug(f"从 KV 加载限流数据: {len(data)} 个群组")
        except Exception as e:
            logger.warning(f"加载限流数据失败: {e}")
        finally:
            self._loaded = True

    async def _save_to_kv(self) -> None:
        """保存限流数据到 KV 存储"""
        if not self._put_kv:
            return
        try:
            await self._put_kv(self.KV_KEY, self._rate_limit_buckets)
        except Exception as e:
            logger.debug(f"保存限流数据失败: {e}")

    def get_group_id_from_event(self, event: AstrMessageEvent) -> str | None:
        """从事件中解析群ID，仅在群聊场景下返回"""
        try:
            if hasattr(event, "group_id") and event.group_id:
                return str(event.group_id)
            message_obj = getattr(event, "message_obj", None)
            if message_obj and getattr(message_obj, "group_id", ""):
                return str(message_obj.group_id)
        except Exception as e:
            logger.debug(f"获取群ID失败: {e}")
        return None

    def _find_matching_rule(self, group_id: str) -> dict[str, Any] | None:
        """查找匹配当前群的限流规则

        规则匹配优先级：
        1. 按列表顺序，第一个匹配的规则生效
        2. group_ids 为空表示匹配所有群
        3. 如果没有规则匹配，返回 None（使用默认限流或旧版配置）
        """
        for rule in self.config.rate_limit_rules:
            if not rule.get("enabled", True):
                continue
            group_ids = rule.get("group_ids") or []
            # 空列表表示匹配所有群
            if not group_ids or group_id in group_ids:
                return rule
        return None

    async def check_and_consume(
        self, event: AstrMessageEvent
    ) -> tuple[bool, str | None]:
        """
        检查当前事件是否通过群聊黑/白名单和限流校验。

        限流规则匹配优先级：
        1. rate_limit_rules（多规则列表，按顺序匹配）
        2. default_rate_limit（未匹配规则时的默认设置）

        Returns:
            (是否允许继续执行, 不允许时的提示消息)
        """
        group_id = self.get_group_id_from_event(event)

        if not group_id:
            logger.debug("无 group_id，跳过限流")
            return True, None

        # 检查群限制模式
        if self.config.group_limit_mode == "whitelist":
            if (
                self.config.group_limit_list
                and group_id not in self.config.group_limit_list
            ):
                logger.debug("拒绝（不在白名单） group_id=%s", group_id)
                return False, None
        elif self.config.group_limit_mode == "blacklist":
            if (
                self.config.group_limit_list
                and group_id in self.config.group_limit_list
            ):
                logger.debug("拒绝（在黑名单） group_id=%s", group_id)
                return False, None

        # 尝试匹配多规则限流
        matched_rule = self._find_matching_rule(group_id)

        # 确定限流参数
        if matched_rule:
            # 使用匹配的规则
            enabled = matched_rule.get("enabled", True)
            period = matched_rule.get("period_seconds", 60)
            max_requests = matched_rule.get("max_requests", 5)
            rule_name = matched_rule.get("rule_name", "自定义规则")
            logger.debug(f"群 {group_id} 匹配规则: {rule_name}")
        elif self.config.default_rate_limit.get("enabled", False):
            # 使用默认限流设置
            enabled = True
            period = self.config.default_rate_limit.get("period_seconds", 60)
            max_requests = self.config.default_rate_limit.get("max_requests", 5)
            rule_name = "默认限流"
            logger.debug(f"群 {group_id} 使用默认限流设置")
        else:
            # 无限流
            logger.debug("未启用限流 group_id=%s", group_id)
            return True, None

        if not enabled:
            logger.debug("限流规则已禁用 group_id=%s", group_id)
            return True, None

        # 首次使用时从 KV 加载
        await self._load_from_kv()

        now = time.time()  # 使用 time.time() 以便跨重启持久化
        window_start = now - period

        async with self._rate_limit_lock:
            bucket = self._rate_limit_buckets.get(group_id, [])
            bucket = [ts for ts in bucket if ts >= window_start]

            if len(bucket) >= max_requests:
                earliest = bucket[0]
                retry_after = int(earliest + period - now)
                if retry_after < 0:
                    retry_after = 0

                self._rate_limit_buckets[group_id] = bucket
                await self._save_to_kv()
                logger.debug(
                    "触发限流 group_id=%s count=%s/%s retry_after=%s rule=%s",
                    group_id,
                    len(bucket),
                    max_requests,
                    retry_after,
                    rule_name,
                )
                return (
                    False,
                    f"⏱️ 本群在最近 {period} 秒内的生图请求次数已达上限"
                    f"（{max_requests} 次），"
                    f"请约 {retry_after} 秒后再试。",
                )

            bucket.append(now)
            self._rate_limit_buckets[group_id] = bucket
            await self._save_to_kv()

        logger.debug(
            "限流检查通过 group_id=%s 当前计数=%s rule=%s",
            group_id,
            len(self._rate_limit_buckets.get(group_id, [])),
            rule_name,
        )
        return True, None

    async def reset(self) -> None:
        """重置限流状态"""
        self._rate_limit_buckets.clear()
        await self._save_to_kv()
