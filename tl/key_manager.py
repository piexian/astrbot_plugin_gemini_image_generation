"""API Key 管理器模块

支持多 Key 轮换和每日限额功能，基于 provider_overrides 配置。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from astrbot.api import logger

if TYPE_CHECKING:
    from .plugin_config import PluginConfig


@dataclass
class KeyUsageRecord:
    """单个 Key 的使用记录"""

    key: str
    usage_count: int = 0
    last_reset_date: str = ""  # YYYY-MM-DD 格式
    is_exhausted: bool = False  # 是否已达到每日限额


@dataclass
class ProviderKeyManager:
    """单个供应商的 Key 管理器"""

    api_type: str
    api_keys: list[str] = field(default_factory=list)
    daily_limit_per_key: int = 0  # 0 表示不限制
    current_index: int = 0
    key_records: dict[str, KeyUsageRecord] = field(default_factory=dict)

    def __post_init__(self):
        # 初始化 Key 记录
        for key in self.api_keys:
            if key not in self.key_records:
                self.key_records[key] = KeyUsageRecord(key=key)


class KeyManager:
    """全局 API Key 管理器

    功能：
    1. 基于候选 ID 的多 Key 轮换
    2. 每日限额追踪（daily_limit_per_key）
    3. Key 耗尽时自动切换到下一个可用 Key
    4. 支持 KV 持久化

    ``provider_overrides`` 在 v1.2.0 后以候选 ID（如 ``google#1``）为 key。
    候选保留独立轮换索引，但同一供应商类型下相同 API Key 的每日使用次数共享，
    避免同一 key 出现在同类多个候选时重复获得额度。
    """

    KV_KEY = "api_key_usage"

    def __init__(
        self,
        config: PluginConfig,
        *,
        get_kv: Callable[[str, Any], Coroutine[Any, Any, Any]] | None = None,
        put_kv: Callable[[str, Any], Coroutine[Any, Any, None]] | None = None,
    ):
        self.config = config
        self._providers: dict[str, ProviderKeyManager] = {}
        self._shared_key_records: dict[tuple[str, str], KeyUsageRecord] = {}
        self._lock = asyncio.Lock()
        self._get_kv = get_kv
        self._put_kv = put_kv
        self._loaded = False

        # 初始化供应商配置
        self._init_providers()

    def _init_providers(self) -> None:
        """从 provider_overrides 初始化候选级 Key 配置。"""
        for key_scope, override in self.config.provider_overrides.items():
            if not isinstance(override, dict):
                continue

            api_keys = override.get("api_keys") or []
            if not api_keys:
                continue

            daily_limit = override.get("daily_limit_per_key", 0)

            provider = ProviderKeyManager(
                api_type=key_scope,
                api_keys=api_keys,
                daily_limit_per_key=daily_limit,
            )
            for api_key in api_keys:
                shared_scope = (key_scope.split("#", 1)[0], api_key)
                record = self._shared_key_records.setdefault(
                    shared_scope, KeyUsageRecord(key=api_key)
                )
                provider.key_records[api_key] = record
            self._providers[key_scope] = provider
            logger.debug(
                f"[KeyManager] 初始化候选 {key_scope}: {len(api_keys)} 个 Key, "
                f"每日限额: {daily_limit or '无限制'}"
            )

    def has_provider(self, key_scope: str) -> bool:
        """检查是否有指定候选作用域的 Key 配置。"""
        return key_scope in self._providers

    def key_count(self, key_scope: str) -> int:
        """返回指定候选作用域配置的 Key 数量。"""
        provider = self._providers.get(key_scope)
        return len(provider.api_keys) if provider else 0

    async def _load_from_kv(self) -> None:
        """从 KV 存储加载使用记录"""
        if self._loaded or not self._get_kv:
            return
        try:
            import json

            data = await self._get_kv(self.KV_KEY, None)
            if data:
                if isinstance(data, str):
                    data = json.loads(data)
                if isinstance(data, dict):
                    self._restore_usage_records(data)
                    logger.debug("[KeyManager] 从 KV 加载使用记录")
        except Exception as e:
            logger.warning(f"[KeyManager] 加载使用记录失败: {e}")
        finally:
            self._loaded = True

    async def _save_to_kv(self) -> None:
        """保存使用记录到 KV 存储"""
        if not self._put_kv:
            return
        try:
            data = self._export_usage_records()
            await self._put_kv(self.KV_KEY, data)
        except Exception as e:
            logger.debug(f"[KeyManager] 保存使用记录失败: {e}")

    def _export_usage_records(self) -> dict[str, Any]:
        """导出使用记录为可序列化格式"""
        result = {}
        for api_type, provider in self._providers.items():
            result[api_type] = {
                "current_index": provider.current_index,
                "keys": {
                    key: {
                        "usage_count": record.usage_count,
                        "last_reset_date": record.last_reset_date,
                    }
                    for key, record in provider.key_records.items()
                },
            }
        return result

    def _restore_usage_records(self, data: dict[str, Any]) -> None:
        """从导出格式恢复使用记录"""
        for api_type, provider_data in data.items():
            if api_type not in self._providers:
                continue

            provider = self._providers[api_type]

            if "current_index" in provider_data:
                provider.current_index = provider_data["current_index"]

            keys_data = provider_data.get("keys", {})
            for key, key_data in keys_data.items():
                if key in provider.key_records:
                    record = provider.key_records[key]
                    saved_date = str(key_data.get("last_reset_date", ""))
                    saved_usage = int(key_data.get("usage_count", 0) or 0)
                    if saved_date > record.last_reset_date:
                        record.last_reset_date = saved_date
                        record.usage_count = saved_usage
                    elif saved_date == record.last_reset_date:
                        record.usage_count = max(record.usage_count, saved_usage)

    def _get_today_date(self) -> str:
        """获取今天的日期字符串 (YYYY-MM-DD)"""
        return time.strftime("%Y-%m-%d", time.localtime())

    def _reset_if_new_day(self, record: KeyUsageRecord) -> None:
        """如果是新的一天，重置使用计数"""
        today = self._get_today_date()
        if record.last_reset_date != today:
            record.usage_count = 0
            record.last_reset_date = today
            record.is_exhausted = False

    async def get_available_key(self, key_scope: str) -> str | None:
        """获取指定候选作用域的可用 Key（预扣除额度，避免竞态条件）

        Args:
            key_scope: 候选 ID（如 "google#1"）

        Returns:
            可用的 API Key，如果没有可用 Key 则返回 None

        Note:
            此方法会预扣除额度（额度已在此处扣除）。
        """
        if key_scope not in self._providers:
            return None

        await self._load_from_kv()

        async with self._lock:
            provider = self._providers[key_scope]

            if not provider.api_keys:
                return None

            # 如果没有每日限额，直接返回当前 Key
            if provider.daily_limit_per_key <= 0:
                key = provider.api_keys[provider.current_index % len(provider.api_keys)]
                return key

            # 有每日限额，需要检查并找到可用 Key
            start_index = provider.current_index
            checked_count = 0

            while checked_count < len(provider.api_keys):
                idx = (start_index + checked_count) % len(provider.api_keys)
                key = provider.api_keys[idx]
                record = provider.key_records.get(key)

                if record:
                    self._reset_if_new_day(record)

                    if record.usage_count < provider.daily_limit_per_key:
                        # 找到可用 Key，预扣除额度并更新当前索引
                        provider.current_index = idx
                        record.usage_count += 1
                        # 检查是否达到限额
                        if record.usage_count >= provider.daily_limit_per_key:
                            record.is_exhausted = True
                            logger.info(
                                f"[KeyManager] Key ***{key[-4:]} 今日额度已用尽 "
                                f"({record.usage_count}/{provider.daily_limit_per_key})"
                            )
                        # 保存到 KV
                        await self._save_to_kv()
                        return key

                checked_count += 1

            # 所有 Key 都已耗尽
            logger.warning(f"[KeyManager] 候选 {key_scope} 的所有 Key 今日额度已用尽")
            return None

    async def rotate_key(self, key_scope: str) -> str | None:
        """轮换到下一个可用 Key

        Args:
            key_scope: 候选 ID

        Returns:
            新的可用 Key，如果没有可用 Key 则返回 None
        """
        if key_scope not in self._providers:
            return None

        async with self._lock:
            provider = self._providers[key_scope]

            if len(provider.api_keys) <= 1:
                return provider.api_keys[0] if provider.api_keys else None

            # 移动到下一个索引
            provider.current_index = (provider.current_index + 1) % len(
                provider.api_keys
            )

        # 获取可用 Key（会自动跳过已耗尽的）
        return await self.get_available_key(key_scope)

    def get_key_status(self, key_scope: str) -> dict[str, Any]:
        """获取指定候选作用域的 Key 状态

        Returns:
            包含各 Key 状态的字典
        """
        if key_scope not in self._providers:
            return {}

        provider = self._providers[key_scope]
        today = self._get_today_date()

        status = {
            "api_type": key_scope,
            "total_keys": len(provider.api_keys),
            "daily_limit_per_key": provider.daily_limit_per_key,
            "keys": [],
        }

        for key in provider.api_keys:
            record = provider.key_records.get(key)
            if record:
                # 检查是否需要重置
                is_today = record.last_reset_date == today
                usage = record.usage_count if is_today else 0

                status["keys"].append(
                    {
                        "key_suffix": f"***{key[-4:]}" if len(key) >= 4 else "***",
                        "usage_today": usage,
                        "is_exhausted": (
                            provider.daily_limit_per_key > 0
                            and usage >= provider.daily_limit_per_key
                        ),
                    }
                )

        return status
