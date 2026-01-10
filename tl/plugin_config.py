"""插件配置加载和管理模块"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger


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
    verbose_logging: bool = False

    # 帮助页渲染
    help_render_mode: str = "html"
    html_render_options: dict[str, Any] = field(default_factory=dict)

    # 限制设置
    group_limit_mode: str = "none"
    group_limit_list: set[str] = field(default_factory=set)
    enable_rate_limit: bool = False
    rate_limit_period: int = 60
    max_requests_per_group: int = 5

    # 缓存设置
    cache_ttl_minutes: int = 5
    cleanup_interval_minutes: int = 30
    max_cache_files: int = 100

    def log_info(self, message: str):
        """根据配置输出 info 或 debug 级别日志"""
        if self.verbose_logging:
            logger.info(message)
        else:
            logger.debug(message)

    def log_debug(self, message: str):
        """输出 debug 级别日志"""
        logger.debug(message)


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

    def __init__(self, raw_config: dict[str, Any]):
        self.raw_config = raw_config

    def load(self) -> PluginConfig:
        """加载配置并返回 PluginConfig 实例"""
        config = PluginConfig()

        # API 设置
        api_settings = self.raw_config.get("api_settings", {})
        config.provider_id = api_settings.get("provider_id") or ""
        config.vision_provider_id = api_settings.get("vision_provider_id") or ""
        config.vision_model = (api_settings.get("vision_model") or "").strip()
        config.api_type = (api_settings.get("api_type") or "").strip()
        config.api_base = (api_settings.get("custom_api_base") or "").strip()
        config.model = (api_settings.get("model") or "").strip()

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

        # 快速模式覆盖
        quick_mode_settings = self.raw_config.get("quick_mode_settings") or {}
        for mode_key in QUICK_MODES:
            mode_settings = quick_mode_settings.get(mode_key) or {}
            override_res = (mode_settings.get("resolution") or "").strip()
            override_ar = (mode_settings.get("aspect_ratio") or "").strip()
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
        config.verbose_logging = service_settings.get("verbose_logging") or False

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

        config.enable_rate_limit = bool(
            limit_settings.get("enable_rate_limit") or False
        )

        period = limit_settings.get("rate_limit_period") or 60
        max_requests = limit_settings.get("max_requests_per_group") or 5
        try:
            config.rate_limit_period = max(int(period), 1)
        except (TypeError, ValueError):
            config.rate_limit_period = 60
        try:
            config.max_requests_per_group = max(int(max_requests), 1)
        except (TypeError, ValueError):
            config.max_requests_per_group = 5

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
