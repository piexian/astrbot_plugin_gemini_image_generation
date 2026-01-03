"""群限制和限流模块"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from astrbot.api import logger

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

    from .plugin_config import PluginConfig


class RateLimiter:
    """群限制和限流管理器"""

    def __init__(self, config: PluginConfig):
        self.config = config
        self._rate_limit_buckets: dict[str, list[float]] = {}
        self._rate_limit_lock = asyncio.Lock()

    def get_group_id_from_event(self, event: AstrMessageEvent) -> str | None:
        """从事件中解析群ID，仅在群聊场景下返回"""
        try:
            if hasattr(event, "group_id") and event.group_id:
                return str(event.group_id)
            message_obj = getattr(event, "message_obj", None)
            if message_obj and getattr(message_obj, "group_id", ""):
                return str(message_obj.group_id)
        except Exception as e:
            self.config.log_debug(f"获取群ID失败: {e}")
        return None

    async def check_and_consume(
        self, event: AstrMessageEvent
    ) -> tuple[bool, str | None]:
        """
        检查当前事件是否通过群聊黑/白名单和限流校验。

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

        # 检查限流
        if not self.config.enable_rate_limit:
            logger.debug("未启用限流 group_id=%s", group_id)
            return True, None

        now = time.monotonic()
        window_start = now - self.config.rate_limit_period

        async with self._rate_limit_lock:
            bucket = self._rate_limit_buckets.get(group_id, [])
            bucket = [ts for ts in bucket if ts >= window_start]

            if len(bucket) >= self.config.max_requests_per_group:
                earliest = bucket[0]
                retry_after = int(earliest + self.config.rate_limit_period - now)
                if retry_after < 0:
                    retry_after = 0

                self._rate_limit_buckets[group_id] = bucket
                logger.debug(
                    "触发限流 group_id=%s count=%s/%s retry_after=%s",
                    group_id,
                    len(bucket),
                    self.config.max_requests_per_group,
                    retry_after,
                )
                return (
                    False,
                    f"⏱️ 本群在最近 {self.config.rate_limit_period} 秒内的生图请求次数已达上限"
                    f"（{self.config.max_requests_per_group} 次），"
                    f"请约 {retry_after} 秒后再试。",
                )

            bucket.append(now)
            self._rate_limit_buckets[group_id] = bucket

        logger.debug(
            "限流检查通过 group_id=%s 当前计数=%s",
            group_id,
            len(self._rate_limit_buckets.get(group_id, [])),
        )
        return True, None

    def reset(self):
        """重置限流状态"""
        self._rate_limit_buckets.clear()
