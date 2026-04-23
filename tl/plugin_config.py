"""插件配置加载和管理模块"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from .openai_image_size import (
    normalize_custom_size_input,
    normalize_size_mode,
    validate_custom_size,
)

# 豆包组图数量限制常量
DOUBAO_SEQUENTIAL_IMAGES_MIN = 2
DOUBAO_SEQUENTIAL_IMAGES_MAX = 15


def _validate_openai_images_settings(settings: dict[str, Any]) -> None:
    """校验 openai_images 覆盖配置。"""
    try:
        size_mode = normalize_size_mode(settings.get("size_mode"))
    except ValueError as exc:
        logger.warning(
            f"[配置加载] {exc}；已回退为 preset，以允许插件继续加载并在 WebUI 中修复配置"
        )
        size_mode = "preset"
    settings["size_mode"] = size_mode

    custom_size = settings.get("custom_size")
    if size_mode == "custom":
        settings["custom_size"] = normalize_custom_size_input(custom_size)
        try:
            settings["custom_size"] = validate_custom_size(custom_size)
        except ValueError as exc:
            logger.warning(f"[配置加载] {exc}；已保留当前值，以便在 WebUI 中继续修改")
    elif isinstance(custom_size, str):
        settings["custom_size"] = normalize_custom_size_input(custom_size)


@dataclass
class PluginConfig:
    """插件配置数据类"""

    # API 设置
    provider_id: str = ""
    vision_provider_id: str = ""
    vision_model: str = ""
    api_type: str = ""
    api_base: str = ""
    model: str = ""
    api_keys: list[str] = field(default_factory=list)
    proxy: str | None = None

    # 豆包（Volcengine Ark）专用配置（api_type == doubao 时使用）
    doubao_settings: dict[str, Any] = field(default_factory=dict)

    # 供应商配置覆盖（支持所有 API 类型的多 Key 轮换和限流）
    # 结构：{api_type: {api_keys: [...], daily_limit_per_key: int, ...}}
    provider_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    # 图像生成设置
    resolution: str = "1K"
    aspect_ratio: str = "1:1"
    enable_grounding: bool = False
    max_reference_images: int = 6
    enable_text_response: bool = False
    enable_sticker_split: bool = True
    enable_sticker_zip: bool = False
    preserve_reference_image_size: bool = False
    enable_llm_crop: bool = True
    force_resolution: bool = False
    resolution_param_name: str = "image_size"
    aspect_ratio_param_name: str = "aspect_ratio"
    image_input_mode: str = "force_base64"
    max_inline_image_size_mb: float = 2.0  # 本地图片 base64 编码阈值（MB）
    llm_tool_timeout_reserve_percent: int = 50

    # 表情包设置
    sticker_grid_rows: int = 4
    sticker_grid_cols: int = 4
    sticker_bbox_rows: int = 6
    sticker_bbox_cols: int = 4

    # 快速模式覆盖
    quick_mode_overrides: dict[str, tuple[str | None, str | None]] = field(
        default_factory=dict
    )

    # 重试设置
    max_attempts_per_key: int = 3
    enable_smart_retry: bool = True
    total_timeout: int = 120

    # 服务设置
    nap_server_address: str = "localhost"
    nap_server_port: int = 3658
    auto_avatar_reference: bool = False

    # 帮助页渲染
    help_render_mode: str = "html"
    html_render_options: dict[str, Any] = field(default_factory=dict)

    # 限制设置
    group_limit_mode: str = "none"
    group_limit_list: set[str] = field(default_factory=set)
    # 限流规则列表
    rate_limit_rules: list[dict[str, Any]] = field(default_factory=list)
    # 默认限流设置（未匹配规则时使用）
    default_rate_limit: dict[str, Any] = field(
        default_factory=lambda: {
            "enabled": False,
            "period_seconds": 60,
            "max_requests": 5,
        }
    )

    # 缓存设置
    cache_ttl_minutes: int = 5
    cleanup_interval_minutes: int = 30
    max_cache_files: int = 100


# 快速模式键列表
QUICK_MODES = (
    "avatar",
    "poster",
    "wallpaper",
    "card",
    "mobile",
    "figure",
    "sticker",
)


class ConfigLoader:
    """配置加载器"""

    def __init__(
        self,
        raw_config: dict[str, Any],
        *,
        data_dir: str | None = None,
    ):
        self.raw_config = raw_config
        self._migrated = False
        self._data_dir = data_dir  # 插件持久化存储目录

    def _needs_migration(self) -> list[str]:
        """检测哪些配置需要迁移

        Returns:
            需要迁移的配置项列表
        """
        migrations_needed = []

        # 检查 limit_settings 是否使用旧版格式
        # 旧版格式特征：存在 enable_rate_limit 字段，且不存在 rate_limit_rules
        limit_settings = self.raw_config.get("limit_settings")
        if isinstance(limit_settings, dict):
            has_old_fields = "enable_rate_limit" in limit_settings
            has_new_rules = "rate_limit_rules" in limit_settings
            if has_old_fields and not has_new_rules:
                migrations_needed.append("limit_settings")

        # 检查 quick_mode_settings 是否使用旧版 object 格式
        # 旧版格式特征：是 dict 类型；新版是 list 类型（template_list）
        quick_mode_settings = self.raw_config.get("quick_mode_settings")
        if isinstance(quick_mode_settings, dict):
            migrations_needed.append("quick_mode_settings")

        return migrations_needed

    def _get_migration_marker_path(self) -> str | None:
        """获取迁移标记文件路径"""
        import os

        if not self._data_dir:
            return None
        return os.path.join(self._data_dir, ".migration_v1.9.0.done")

    def _is_migration_done(self) -> bool:
        """检查迁移是否已完成"""
        import os

        marker_path = self._get_migration_marker_path()
        if not marker_path:
            return False
        return os.path.exists(marker_path)

    def _mark_migration_done(self) -> None:
        """标记迁移已完成"""
        import os

        marker_path = self._get_migration_marker_path()
        if not marker_path:
            return
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(marker_path, "w", encoding="utf-8") as f:
                f.write("migration completed\n")
        except Exception as e:
            logger.debug(f"无法写入迁移标记文件: {e}")

    def _backup_config(self, migration_items: list[str]) -> str | None:
        """备份旧配置到插件持久化存储目录

        Args:
            migration_items: 需要迁移的配置项列表

        Returns:
            备份文件路径，失败返回 None
        """
        import json
        import os
        from datetime import datetime

        if not self._data_dir:
            logger.warning("未提供插件数据目录，跳过配置备份")
            return None

        try:
            # 确保目录存在
            os.makedirs(self._data_dir, exist_ok=True)

            # 生成备份文件名（包含迁移前版本信息）
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            # 获取迁移前版本号（从 marker 文件名推断上一个版本）
            from_version = "pre_v1.9.0"
            backup_filename = f"config_backup_{from_version}_{timestamp}.json"
            backup_path = os.path.join(self._data_dir, backup_filename)

            # 写入备份
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump(self.raw_config, f, ensure_ascii=False, indent=2)

            items_str = ", ".join(migration_items)
            logger.info(f"配置已备份到: {backup_path}")
            logger.info(f"将迁移以下配置项: {items_str}")
            return backup_path
        except Exception as e:
            logger.warning(f"配置备份失败: {e}")
            return None

    def _migrate_config(self) -> bool:
        """迁移旧版配置到新版格式

        迁移内容：
        1. limit_settings 旧版字段 -> rate_limit_rules
        2. quick_mode_settings 从 object 格式迁移到 template_list 格式

        Returns:
            bool: 是否进行了迁移
        """
        if self._migrated:
            return False
        self._migrated = True

        # 检查是否已经完成迁移（持久化标记）
        if self._is_migration_done():
            return False

        # 检测需要迁移的项目
        migrations_needed = self._needs_migration()
        if not migrations_needed:
            return False

        # 备份旧配置
        logger.info("检测到旧版配置格式，开始迁移...")
        self._backup_config(migrations_needed)

        migrated = False

        # 迁移旧版限流配置到 rate_limit_rules
        # 旧版格式：enable_rate_limit, rate_limit_period, max_requests_per_group
        # 新版格式：rate_limit_rules (template_list)
        limit_settings = self.raw_config.get("limit_settings")
        if isinstance(limit_settings, dict):
            has_old_fields = "enable_rate_limit" in limit_settings
            has_new_rules = "rate_limit_rules" in limit_settings

            if has_old_fields and not has_new_rules:
                # 旧版启用了限流，迁移为一条规则
                old_enabled = limit_settings.get("enable_rate_limit", False)
                if old_enabled:
                    new_rule = {
                        "__template_key": "rule",
                        "rule_name": "默认规则（迁移）",
                        "group_ids": [],  # 空表示匹配所有
                        "enabled": True,
                        "period_seconds": limit_settings.get("rate_limit_period", 60),
                        "max_requests": limit_settings.get("max_requests_per_group", 5),
                    }
                    limit_settings["rate_limit_rules"] = [new_rule]
                    logger.info("配置迁移: 旧版限流配置 -> rate_limit_rules")
                else:
                    # 旧版未启用限流，设置空规则列表
                    limit_settings["rate_limit_rules"] = []
                    logger.info("配置迁移: 旧版限流未启用，设置空规则列表")
                migrated = True

        # 迁移旧版 quick_mode_settings 从 object 格式到 template_list 格式
        # 旧版格式：{avatar: {resolution: "1K", aspect_ratio: "1:1"}, ...}
        # 新版格式：[{__template_key: "avatar", resolution: "1K", aspect_ratio: "1:1"}, ...]
        quick_mode_settings = self.raw_config.get("quick_mode_settings")
        if isinstance(quick_mode_settings, dict):
            new_quick_mode_list = []
            for mode_key in QUICK_MODES:
                mode_settings = quick_mode_settings.get(mode_key)
                if isinstance(mode_settings, dict):
                    resolution = (mode_settings.get("resolution") or "").strip()
                    aspect_ratio = (mode_settings.get("aspect_ratio") or "").strip()
                    if resolution or aspect_ratio:
                        new_entry = {"__template_key": mode_key}
                        if resolution:
                            new_entry["resolution"] = resolution
                        if aspect_ratio:
                            new_entry["aspect_ratio"] = aspect_ratio
                        new_quick_mode_list.append(new_entry)
            self.raw_config["quick_mode_settings"] = new_quick_mode_list
            logger.info("配置迁移: quick_mode_settings object -> template_list")
            migrated = True

        # 标记迁移完成
        if migrated:
            self._mark_migration_done()

        return migrated

    def load(self) -> PluginConfig:
        """加载配置并返回 PluginConfig 实例"""
        # 先执行配置迁移
        self._migrate_config()

        config = PluginConfig()

        # API 设置
        api_settings = self.raw_config.get("api_settings", {})
        config.provider_id = api_settings.get("provider_id") or ""
        config.vision_provider_id = api_settings.get("vision_provider_id") or ""
        config.vision_model = (api_settings.get("vision_model") or "").strip()
        config.api_type = (api_settings.get("api_type") or "").strip()
        config.api_base = (api_settings.get("custom_api_base") or "").strip()
        config.model = (api_settings.get("model") or "").strip()
        config.proxy = str(api_settings.get("proxy") or "").strip() or None

        # Provider overrides（从 api_settings.provider_overrides 读取）
        # 新结构：api_settings.provider_overrides 是 template_list，每个条目有 __template_key 标识类型
        provider_overrides = api_settings.get("provider_overrides") or []
        doubao_settings = {}

        # 从 provider_overrides 中查找 doubao 配置
        if isinstance(provider_overrides, list):
            for override in provider_overrides:
                if (
                    isinstance(override, dict)
                    and override.get("__template_key") == "doubao"
                ):
                    doubao_settings = override.copy()
                    doubao_settings.pop("__template_key", None)
                    break

        # 兼容旧的 doubao_settings 配置（如果存在）
        if not doubao_settings:
            doubao_settings_raw = self.raw_config.get("doubao_settings")
            if isinstance(doubao_settings_raw, list) and len(doubao_settings_raw) > 0:
                doubao_settings = doubao_settings_raw[0].copy()
                doubao_settings.pop("__template_key", None)
            elif isinstance(doubao_settings_raw, dict):
                doubao_settings = doubao_settings_raw.copy()

        # 处理 api_keys（新格式：列表）
        if "api_keys" in doubao_settings:
            api_keys = doubao_settings.get("api_keys") or []
            if isinstance(api_keys, list):
                # 清理并过滤空字符串
                doubao_settings["api_keys"] = [
                    k.strip() for k in api_keys if isinstance(k, str) and k.strip()
                ]
            else:
                doubao_settings["api_keys"] = []
        # 兼容旧的 api_key（单个 key）
        elif "api_key" in doubao_settings and isinstance(
            doubao_settings["api_key"], str
        ):
            key = doubao_settings["api_key"].strip()
            doubao_settings["api_keys"] = [key] if key else []
            doubao_settings.pop("api_key", None)
        else:
            doubao_settings["api_keys"] = []

        # 处理 daily_limit_per_key
        daily_limit = doubao_settings.get("daily_limit_per_key")
        if daily_limit is not None:
            try:
                doubao_settings["daily_limit_per_key"] = max(int(daily_limit), 0)
            except (TypeError, ValueError):
                doubao_settings["daily_limit_per_key"] = 0
        else:
            doubao_settings["daily_limit_per_key"] = 0

        # 清理字符串类型的配置项
        for key in (
            "endpoint_id",
            "api_base",
            "default_size",
            "optimize_prompt_mode",
            "sequential_image_generation",
        ):
            if isinstance(doubao_settings.get(key), str):
                doubao_settings[key] = doubao_settings[key].strip()

        # 确保 optimize_prompt_mode 默认为 standard
        if not doubao_settings.get("optimize_prompt_mode"):
            doubao_settings["optimize_prompt_mode"] = "standard"

        # 处理 sequential_max_images 类型容错
        max_images = doubao_settings.get("sequential_max_images")
        if max_images is not None:
            try:
                max_images_int = int(max_images)
                if (
                    max_images_int < DOUBAO_SEQUENTIAL_IMAGES_MIN
                    or max_images_int > DOUBAO_SEQUENTIAL_IMAGES_MAX
                ):
                    raise ValueError(
                        f"sequential_max_images 必须在 {DOUBAO_SEQUENTIAL_IMAGES_MIN}-"
                        f"{DOUBAO_SEQUENTIAL_IMAGES_MAX} 之间，当前值: {max_images_int}"
                    )
                doubao_settings["sequential_max_images"] = max_images_int
            except (TypeError, ValueError) as e:
                if isinstance(e, ValueError) and "必须在" in str(e):
                    raise
                raise ValueError(f"sequential_max_images 配置无效: {max_images}") from e

        config.doubao_settings = doubao_settings

        # 解析所有 provider_overrides 并存入 config.provider_overrides
        # 结构：{api_type: {api_keys: [...], daily_limit_per_key: int, ...}}
        all_overrides: dict[str, dict[str, Any]] = {}
        if isinstance(provider_overrides, list):
            for override in provider_overrides:
                if isinstance(override, dict):
                    template_key = override.get("__template_key")
                    if template_key:
                        override_copy = override.copy()
                        override_copy.pop("__template_key", None)
                        # 统一处理 api_keys
                        if "api_keys" in override_copy:
                            api_keys = override_copy.get("api_keys") or []
                            if isinstance(api_keys, list):
                                override_copy["api_keys"] = [
                                    k.strip()
                                    for k in api_keys
                                    if isinstance(k, str) and k.strip()
                                ]
                            else:
                                override_copy["api_keys"] = []
                        # 统一处理 daily_limit_per_key
                        daily_limit = override_copy.get("daily_limit_per_key")
                        if daily_limit is not None:
                            try:
                                override_copy["daily_limit_per_key"] = max(
                                    int(daily_limit), 0
                                )
                            except (TypeError, ValueError):
                                override_copy["daily_limit_per_key"] = 0
                        # 清理 proxy 字段
                        proxy_val = override_copy.get("proxy")
                        if isinstance(proxy_val, str):
                            override_copy["proxy"] = proxy_val.strip() or None
                        else:
                            override_copy["proxy"] = None
                        if template_key == "openai_images":
                            _validate_openai_images_settings(override_copy)
                        all_overrides[template_key] = override_copy
        config.provider_overrides = all_overrides

        # 图像生成设置
        image_settings = self.raw_config.get("image_generation_settings") or {}
        config.resolution = image_settings.get("resolution") or "1K"
        config.aspect_ratio = image_settings.get("aspect_ratio") or "1:1"
        config.enable_grounding = image_settings.get("enable_grounding") or False
        config.max_reference_images = image_settings.get("max_reference_images") or 6
        config.enable_text_response = (
            image_settings.get("enable_text_response") or False
        )
        config.enable_sticker_split = image_settings.get("enable_sticker_split", True)
        config.enable_sticker_zip = image_settings.get("enable_sticker_zip") or False
        config.preserve_reference_image_size = (
            image_settings.get("preserve_reference_image_size") or False
        )
        config.enable_llm_crop = image_settings.get("enable_llm_crop", True)
        config.force_resolution = image_settings.get("force_resolution") or False
        max_size = image_settings.get("max_inline_image_size_mb")
        if max_size is not None:
            try:
                config.max_inline_image_size_mb = max(float(max_size), 0.1)
            except (TypeError, ValueError):
                config.max_inline_image_size_mb = (
                    PluginConfig().max_inline_image_size_mb
                )
        timeout_reserve_percent = image_settings.get("llm_tool_timeout_reserve_percent")
        if timeout_reserve_percent is not None:
            try:
                config.llm_tool_timeout_reserve_percent = min(
                    max(int(timeout_reserve_percent), 1),
                    100,
                )
            except (TypeError, ValueError):
                config.llm_tool_timeout_reserve_percent = (
                    PluginConfig().llm_tool_timeout_reserve_percent
                )

        # 自定义参数名
        _res_param = (image_settings.get("resolution_param_name") or "").strip()
        config.resolution_param_name = _res_param if _res_param else "image_size"
        _aspect_param = (image_settings.get("aspect_ratio_param_name") or "").strip()
        config.aspect_ratio_param_name = (
            _aspect_param if _aspect_param else "aspect_ratio"
        )

        # 表情包网格设置
        grid_raw = str(image_settings.get("sticker_grid") or "4x4").strip()
        m = re.match(r"^\s*(\d{1,2})\s*[xX]\s*(\d{1,2})\s*$", grid_raw)
        if m:
            config.sticker_grid_rows = int(m.group(1))
            config.sticker_grid_cols = int(m.group(2))
        config.sticker_grid_rows = min(max(config.sticker_grid_rows, 1), 20)
        config.sticker_grid_cols = min(max(config.sticker_grid_cols, 1), 20)

        # 快速模式覆盖 - template_list 格式
        quick_mode_settings = self.raw_config.get("quick_mode_settings") or []
        if isinstance(quick_mode_settings, list):
            for mode_entry in quick_mode_settings:
                if isinstance(mode_entry, dict):
                    mode_key = mode_entry.get("__template_key")
                    if mode_key and mode_key in QUICK_MODES:
                        override_res = (mode_entry.get("resolution") or "").strip()
                        override_ar = (mode_entry.get("aspect_ratio") or "").strip()
                        if override_res or override_ar:
                            config.quick_mode_overrides[mode_key] = (
                                override_res or None,
                                override_ar or None,
                            )

        # 重试设置
        retry_settings = self.raw_config.get("retry_settings") or {}
        config.max_attempts_per_key = retry_settings.get("max_attempts_per_key") or 3
        config.enable_smart_retry = retry_settings.get("enable_smart_retry", True)
        config.total_timeout = retry_settings.get("total_timeout") or 120

        # 服务设置
        service_settings = self.raw_config.get("service_settings") or {}
        config.nap_server_address = (
            service_settings.get("nap_server_address") or "localhost"
        )
        config.nap_server_port = service_settings.get("nap_server_port") or 3658
        config.auto_avatar_reference = (
            service_settings.get("auto_avatar_reference") or False
        )

        # 帮助页渲染
        config.help_render_mode = self.raw_config.get("help_render_mode") or "html"
        config.html_render_options = self._load_html_render_options(service_settings)

        # 限制设置
        self._load_limit_settings(config)

        # 缓存设置
        self._load_cache_settings(config)

        return config

    def _load_html_render_options(
        self, service_settings: dict[str, Any]
    ) -> dict[str, Any]:
        """加载 HTML 渲染选项"""
        html_render_options = (
            self.raw_config.get("html_render_options")
            or service_settings.get("html_render_options")
            or {}
        )

        # 设置默认值以确保图片清晰度
        # scale: "device" 使用设备像素比，生成更清晰的图片
        # full_page: True 截取整个页面
        # type: "png" 无损格式
        defaults = {
            "scale": "device",
            "full_page": True,
            "type": "png",
        }
        for key, default_val in defaults.items():
            html_render_options.setdefault(key, default_val)

        try:
            quality_val = html_render_options.get("quality")
            if quality_val is not None:
                quality_int = int(quality_val)
                if 1 <= quality_int <= 100:
                    html_render_options["quality"] = quality_int
                else:
                    logger.warning(
                        "html_render_options.quality 超出范围(1-100)，已忽略"
                    )
                    html_render_options.pop("quality", None)

            type_val = html_render_options.get("type")
            if type_val and str(type_val).lower() not in {"png", "jpeg"}:
                logger.warning("html_render_options.type 仅支持 png/jpeg，已忽略")
                html_render_options.pop("type", None)

            scale_val = html_render_options.get("scale")
            if scale_val and str(scale_val) not in {"css", "device"}:
                logger.warning("html_render_options.scale 仅支持 css/device，已忽略")
                html_render_options.pop("scale", None)
        except Exception:
            logger.warning("解析 html_render_options 失败，已忽略质量设置")
            html_render_options.pop("quality", None)

        return html_render_options

    def _load_limit_settings(self, config: PluginConfig):
        """加载限制设置"""
        limit_settings = self.raw_config.get("limit_settings") or {}

        raw_mode = str(limit_settings.get("group_limit_mode") or "none").lower()
        if raw_mode not in {"none", "whitelist", "blacklist"}:
            raw_mode = "none"
        config.group_limit_mode = raw_mode

        raw_group_list = limit_settings.get("group_limit_list") or []
        config.group_limit_list = {
            str(group_id).strip()
            for group_id in raw_group_list
            if str(group_id).strip()
        }

        # 新版限流规则列表
        rate_limit_rules_raw = limit_settings.get("rate_limit_rules") or []
        config.rate_limit_rules = []
        if isinstance(rate_limit_rules_raw, list):
            for rule in rate_limit_rules_raw:
                if isinstance(rule, dict):
                    rule_copy = rule.copy()
                    rule_copy.pop("__template_key", None)
                    # 处理 group_ids 列表
                    group_ids = rule_copy.get("group_ids") or []
                    if isinstance(group_ids, list):
                        rule_copy["group_ids"] = [
                            str(gid).strip() for gid in group_ids if str(gid).strip()
                        ]
                    else:
                        rule_copy["group_ids"] = []
                    # 确保数值类型正确
                    try:
                        rule_copy["period_seconds"] = max(
                            int(rule_copy.get("period_seconds", 60)), 1
                        )
                    except (TypeError, ValueError):
                        rule_copy["period_seconds"] = 60
                    try:
                        rule_copy["max_requests"] = max(
                            int(rule_copy.get("max_requests", 5)), 1
                        )
                    except (TypeError, ValueError):
                        rule_copy["max_requests"] = 5
                    rule_copy["enabled"] = bool(rule_copy.get("enabled", True))
                    config.rate_limit_rules.append(rule_copy)

        # 默认限流设置
        default_rate_limit = limit_settings.get("default_rate_limit") or {}
        if isinstance(default_rate_limit, dict):
            config.default_rate_limit = {
                "enabled": bool(default_rate_limit.get("enabled", False)),
                "period_seconds": 60,
                "max_requests": 5,
            }
            try:
                config.default_rate_limit["period_seconds"] = max(
                    int(default_rate_limit.get("period_seconds", 60)), 1
                )
            except (TypeError, ValueError):
                pass
            try:
                config.default_rate_limit["max_requests"] = max(
                    int(default_rate_limit.get("max_requests", 5)), 1
                )
            except (TypeError, ValueError):
                pass

    def _load_cache_settings(self, config: PluginConfig):
        """加载缓存设置"""
        cache_settings = self.raw_config.get("cache_settings") or {}

        cache_ttl = cache_settings.get("cache_ttl_minutes")
        if cache_ttl is not None:
            try:
                config.cache_ttl_minutes = max(int(cache_ttl), 0)
            except (TypeError, ValueError):
                config.cache_ttl_minutes = 5

        cleanup_interval = cache_settings.get("cleanup_interval_minutes")
        if cleanup_interval is not None:
            try:
                config.cleanup_interval_minutes = max(int(cleanup_interval), 0)
            except (TypeError, ValueError):
                config.cleanup_interval_minutes = 30

        max_files = cache_settings.get("max_cache_files")
        if max_files is not None:
            try:
                config.max_cache_files = max(int(max_files), 0)
            except (TypeError, ValueError):
                config.max_cache_files = 100
