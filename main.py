"""
AstrBot Gemini 图像生成插件主文件
支持 Google 官方 API 和 OpenAI 兼容格式 API，提供生图和改图功能，支持智能头像参考

本文件只负责业务流程编排，具体实现委托给 tl/ 下的各模块
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any

import yaml

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as AstrImage
from astrbot.api.message_components import Node, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import ProviderType

from .tl import (
    AvatarHandler,
    ConfigLoader,
    ImageGenerator,
    ImageHandler,
    MessageSender,
    RateLimiter,
    VisionHandler,
    create_zip,
    ensure_font_downloaded,
    get_template_path,
    render_local_pillow,
    render_text,
    resolve_split_source_to_path,
    split_image,
)
from .tl.enhanced_prompts import (
    build_quick_prompt,
    get_avatar_prompt,
    get_card_prompt,
    get_figure_prompt,
    get_generation_prompt,
    get_mobile_prompt,
    get_modification_prompt,
    get_poster_prompt,
    get_q_version_sticker_prompt,
    get_sticker_prompt,
    get_style_change_prompt,
    get_wallpaper_prompt,
)
from .tl.llm_tools import GeminiImageGenerationTool
from .tl.tl_api import APIClient, ApiRequestConfig, get_api_client
from .tl.tl_utils import AvatarManager, cleanup_old_images


@register(
    "astrbot_plugin_gemini_image_generation",
    "piexian",
    "Gemini图像生成插件，支持生图和改图，可以自动获取头像作为参考",
    "",
)
class GeminiImageGenerationPlugin(Star):
    """Gemini 图像生成插件主类 - 仅负责业务流程编排"""

    def __init__(self, context: Context, config: dict[str, Any]):
        super().__init__(context)
        self.raw_config = config

        # 读取版本号
        self.version = self._load_version()

        # 初始化状态
        self.api_client: APIClient | None = None
        self._cleanup_task: asyncio.Task | None = None

        # 加载配置
        self.cfg = ConfigLoader.load(config)

        # 初始化各功能模块
        self._init_modules()

        # 注册 LLM 工具
        self._register_llm_tools()

        # 启动定时清理任务
        self._start_cleanup_task()

    def _load_version(self) -> str:
        """从 metadata.yaml 读取版本号"""
        try:
            metadata_path = os.path.join(os.path.dirname(__file__), "metadata.yaml")
            with open(metadata_path, encoding="utf-8") as f:
                metadata = yaml.safe_load(f) or {}
                version = str(metadata.get("version", "")).strip()
                return version if version else "v1.0.0"
        except Exception:
            return "v1.0.0"

    def _init_modules(self):
        """初始化各功能处理模块"""
        # 限流器
        self.rate_limiter = RateLimiter(self.cfg)

        # 头像处理器
        self.avatar_handler = AvatarHandler(
            auto_avatar_reference=self.cfg.auto_avatar_reference,
            log_debug_fn=self.log_debug,
        )

        # 图片处理器
        self.image_handler = ImageHandler(
            api_client=self.api_client,
            max_reference_images=self.cfg.max_reference_images,
            log_debug_fn=self.log_debug,
        )

        # 消息发送器
        self.message_sender = MessageSender(
            enable_text_response=self.cfg.enable_text_response,
            log_debug_fn=self.log_debug,
        )

        # 视觉处理器
        self.vision_handler = VisionHandler(
            context=self.context,
            api_client=self.api_client,
            vision_provider_id=self.cfg.vision_provider_id,
            vision_model=self.cfg.vision_model,
            enable_llm_crop=self.cfg.enable_llm_crop,
            sticker_bbox_rows=self.cfg.sticker_bbox_rows,
            sticker_bbox_cols=self.cfg.sticker_bbox_cols,
        )

        # 图像生成器
        self.image_generator = ImageGenerator(
            context=self.context,
            api_client=self.api_client,
            model=self.cfg.model,
            api_type=self.cfg.api_type,
            api_base=self.cfg.api_base,
            resolution=self.cfg.resolution,
            aspect_ratio=self.cfg.aspect_ratio,
            enable_grounding=self.cfg.enable_grounding,
            enable_smart_retry=self.cfg.enable_smart_retry,
            enable_text_response=self.cfg.enable_text_response,
            force_resolution=self.cfg.force_resolution,
            verbose_logging=self.cfg.verbose_logging,
            resolution_param_name=self.cfg.resolution_param_name,
            aspect_ratio_param_name=self.cfg.aspect_ratio_param_name,
            max_reference_images=self.cfg.max_reference_images,
            total_timeout=self.cfg.total_timeout,
            max_attempts_per_key=self.cfg.max_attempts_per_key,
            nap_server_address=self.cfg.nap_server_address,
            nap_server_port=self.cfg.nap_server_port,
            filter_valid_fn=self.image_handler.filter_valid_reference_images,
            get_tool_timeout_fn=self.get_tool_timeout,
        )

        # 兼容旧代码的 avatar_manager
        self.avatar_manager = AvatarManager()

    def _update_modules_api_client(self):
        """更新各模块的 API 客户端"""
        if self.api_client:
            self.image_handler.update_config(api_client=self.api_client)
            self.vision_handler.update_config(api_client=self.api_client)
            self.image_generator.update_config(api_client=self.api_client)

    def _register_llm_tools(self):
        """注册 LLM 工具到 Context"""
        try:
            tool = GeminiImageGenerationTool(plugin=self)
            self.context.add_llm_tools(tool)
            logger.debug("已注册 GeminiImageGenerationTool 到 LLM 工具列表")
        except Exception as e:
            logger.warning(f"注册 LLM 工具失败: {e}，将使用装饰器方式")

    def _start_cleanup_task(self):
        """启动定时清理任务"""
        if self._cleanup_task and not self._cleanup_task.done():
            return

        # 读取配置的清理间隔和缓存保留时间
        cleanup_interval = self.cfg.cleanup_interval_minutes
        cache_ttl = self.cfg.cache_ttl_minutes
        max_files = self.cfg.max_cache_files

        # 如果清理间隔为 0，禁用定时清理
        if cleanup_interval <= 0:
            logger.debug("定时清理任务已禁用（cleanup_interval_minutes=0）")
            return

        async def cleanup_loop():
            while True:
                try:
                    await cleanup_old_images(ttl_minutes=cache_ttl, max_files=max_files)
                    await asyncio.sleep(cleanup_interval * 60)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(f"清理任务异常: {e}")
                    await asyncio.sleep(300)

        self._cleanup_task = asyncio.create_task(cleanup_loop())
        logger.debug(
            f"定时清理任务已启动（间隔 {cleanup_interval} 分钟，保留 {cache_ttl} 分钟，上限 {max_files} 个）"
        )

    async def terminate(self):
        """插件卸载/重载时调用"""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            logger.debug("定时清理任务已停止")
        if self.api_client and hasattr(self.api_client, "close"):
            try:
                await self.api_client.close()
            except Exception as e:
                logger.debug(f"关闭 API 会话失败: {e}")
        logger.info("🎨 Gemini 图像生成插件已卸载")

    # ===== 配置和客户端管理 =====

    def get_tool_timeout(self, event: AstrMessageEvent | None = None) -> int:
        """获取当前聊天环境的 tool_call_timeout 配置"""
        try:
            if event:
                umo = event.unified_msg_origin
                chat_config = self.context.get_config(umo=umo)
                return chat_config.get("provider_settings", {}).get(
                    "tool_call_timeout", 60
                )
            default_config = self.context.get_config()
            return default_config.get("provider_settings", {}).get(
                "tool_call_timeout", 60
            )
        except Exception as e:
            logger.warning(f"获取 tool_call_timeout 配置失败: {e}，使用默认值 60 秒")
            return 60

    def _ensure_api_client(self, *, quiet: bool = False) -> bool:
        """确保 API 客户端已初始化"""
        if self.api_client:
            return True
        self._load_provider_from_context(quiet=quiet)
        if not self.api_client:
            if not quiet:
                logger.error("✗ API 客户端仍未初始化，请检查 AstrBot 提供商配置")
            return False
        return True

    def _load_provider_from_context(self, *, quiet: bool = False):
        """从 AstrBot 提供商读取模型/密钥并初始化客户端"""
        if not quiet:
            logger.debug("尝试读取 AstrBot 提供商配置")

        api_settings = self.raw_config.get("api_settings", {})
        provider_id = api_settings.get("provider_id") or self.cfg.provider_id
        manual_api_type = (api_settings.get("api_type") or "").strip()
        manual_api_base = (api_settings.get("custom_api_base") or "").strip()
        manual_model = (api_settings.get("model") or "").strip()

        # 只按配置文件决定 API 类型
        if manual_api_type and not self.cfg.api_type:
            self.cfg.api_type = manual_api_type
        elif not self.cfg.api_type:
            if not quiet:
                logger.error(
                    "✗ 未配置 api_settings.api_type（google/openai/zai/grok2api），无法初始化 API 客户端"
                )
            return

        if manual_api_base and not self.cfg.api_base:
            self.cfg.api_base = manual_api_base
        if manual_model and not self.cfg.model:
            self.cfg.model = manual_model

        try:
            provider_mgr = getattr(self.context, "provider_manager", None)
            provider = None
            if provider_mgr:
                if provider_id and hasattr(provider_mgr, "inst_map"):
                    provider = provider_mgr.inst_map.get(provider_id)
                if not provider:
                    provider = provider_mgr.get_using_provider(
                        ProviderType.CHAT_COMPLETION, None
                    )

            if provider:
                if not self.cfg.provider_id:
                    self.cfg.provider_id = provider.provider_config.get("id", "")

                prov_model = provider.get_model() or provider.provider_config.get(
                    "model_config", {}
                ).get("model")
                if prov_model and not manual_model and not self.cfg.model:
                    self.cfg.model = prov_model

                prov_keys = provider.get_keys() or []
                if not self.cfg.api_keys:
                    self.cfg.api_keys = [
                        str(k).strip() for k in prov_keys if str(k).strip()
                    ]

                prov_base = provider.provider_config.get("api_base")
                if prov_base and not manual_api_base and not self.cfg.api_base:
                    self.cfg.api_base = prov_base

                logger.info(
                    f"✓ 已从 AstrBot 提供商读取配置，类型={self.cfg.api_type} 模型={self.cfg.model} 密钥={len(self.cfg.api_keys)}"
                )
            else:
                if not quiet:
                    logger.error(
                        "未找到可用的 AstrBot 提供商，无法读取模型/密钥，请在主配置中选择提供商"
                    )
        except Exception as e:
            logger.error(f"读取 AstrBot 提供商配置失败: {e}")

        if self.cfg.api_keys:
            self.api_client = get_api_client(self.cfg.api_keys)
            self._update_modules_api_client()
            logger.info("✓ API 客户端已初始化")
            logger.info(f"  - 类型: {self.cfg.api_type}")
            logger.info(f"  - 模型: {self.cfg.model}")
            logger.info(f"  - 密钥数量: {len(self.cfg.api_keys)}")
            if self.cfg.api_base:
                logger.info(f"  - 自定义 API Base: {self.cfg.api_base}")
        else:
            if not quiet:
                logger.debug("启动阶段未读取到 API 密钥，等待 AstrBot 加载完成后再尝试")

    # ===== 日志工具 =====

    def log_info(self, message: str):
        """根据配置输出info或debug级别日志"""
        if self.cfg.verbose_logging:
            logger.info(message)
        else:
            logger.debug(message)

    def log_debug(self, message: str):
        """输出debug级别日志"""
        logger.debug(message)

    # ===== 事件处理 =====

    @filter.on_startup()
    async def on_startup(self):
        """插件启动初始化"""
        self._load_provider_from_context(quiet=True)
        if self.cfg.help_render_mode == "local":
            asyncio.create_task(self._ensure_font_for_local_mode())
        if self.api_client:
            logger.info("🎨 Gemini 图像生成插件已加载")
        else:
            logger.debug("启动阶段未能初始化 API 客户端，待 on_astrbot_loaded 再补偿")

    async def _ensure_font_for_local_mode(self):
        """确保 local 渲染模式所需的字体已下载"""
        try:
            await ensure_font_downloaded()
        except Exception as e:
            logger.warning(f"字体下载任务异常: {e}")

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 完成初始化后再次尝试加载提供商"""
        logger.debug("on_astrbot_loaded 触发，尝试确保 api_client")
        if not self.api_client:
            self._load_provider_from_context()
            if self.api_client:
                logger.info("✓ AstrBot 加载完成后已成功初始化 API 客户端")
            else:
                logger.error(
                    "✗ AstrBot 加载完成后仍未初始化 API 客户端，请检查提供商配置"
                )

    # ===== 核心业务方法 =====

    async def _quick_generate_image(
        self,
        event: AstrMessageEvent,
        prompt: str,
        use_avatar: bool = False,
        skip_figure_enhance: bool = False,
        override_resolution: str | None = None,
        override_aspect_ratio: str | None = None,
    ):
        """快捷图像生成"""
        if not self._ensure_api_client():
            yield event.plain_result(
                "❌ API 客户端未初始化。\n"
                "🧐 可能原因：服务启动过快，提供商尚未加载或密钥缺失。\n"
                "✅ 建议：确认 AstrBot 主配置已选择提供商并填写密钥后重试。"
            )
            return

        try:
            ref_images, avatars = await self.image_handler.fetch_images_from_event(
                event, include_at_avatars=use_avatar
            )

            all_ref_images: list[str] = []
            all_ref_images.extend(
                self.image_handler.filter_valid_reference_images(
                    ref_images, source="消息图片"
                )
            )
            if use_avatar:
                all_ref_images.extend(
                    self.image_handler.filter_valid_reference_images(
                        avatars, source="头像"
                    )
                )

            enhanced_prompt, is_modification_request = build_quick_prompt(
                prompt, skip_figure_enhance=skip_figure_enhance
            )

            effective_resolution = (
                override_resolution
                if override_resolution is not None
                else self.cfg.resolution
            )
            effective_aspect_ratio = (
                override_aspect_ratio
                if override_aspect_ratio is not None
                else self.cfg.aspect_ratio
            )

            if (
                self.cfg.preserve_reference_image_size
                and is_modification_request
                and all_ref_images
            ):
                effective_resolution = None
                effective_aspect_ratio = None
                self.log_debug("[MODIFY_DEBUG] 保留参考图尺寸，不覆盖分辨率/比例")

            config = ApiRequestConfig(
                model=self.cfg.model,
                prompt=enhanced_prompt,
                api_type=self.cfg.api_type,
                api_base=self.cfg.api_base if self.cfg.api_base else None,
                resolution=effective_resolution,
                aspect_ratio=effective_aspect_ratio,
                enable_grounding=self.cfg.enable_grounding,
                reference_images=all_ref_images if all_ref_images else None,
                enable_smart_retry=self.cfg.enable_smart_retry,
                enable_text_response=self.cfg.enable_text_response,
                force_resolution=self.cfg.force_resolution,
                verbose_logging=self.cfg.verbose_logging,
                image_input_mode="force_base64",
                resolution_param_name=self.cfg.resolution_param_name,
                aspect_ratio_param_name=self.cfg.aspect_ratio_param_name,
            )

            yield event.plain_result("🎨  生成中...")

            api_start = time.perf_counter()
            (
                image_urls,
                image_paths,
                text_content,
                thought_signature,
            ) = await self.api_client.generate_image(
                config=config,
                max_retries=self.cfg.max_attempts_per_key,
                per_retry_timeout=self.cfg.total_timeout,
                max_total_time=self.cfg.total_timeout * 2,
            )
            api_duration = time.perf_counter() - api_start

            send_start = time.perf_counter()
            async for send_res in self.message_sender.dispatch_send_results(
                event=event,
                image_urls=image_urls,
                image_paths=image_paths,
                text_content=text_content,
                thought_signature=thought_signature,
                scene="快捷生成",
            ):
                yield send_res
            send_duration = time.perf_counter() - send_start

            async for res in self.message_sender.send_api_duration(
                event, api_duration, send_duration
            ):
                yield res

        except Exception as e:
            logger.error(f"快捷生成失败: {e}", exc_info=True)
            yield event.plain_result(
                f"❌ 快速生成时出现异常：{str(e)}\n"
                "🧐 可能原因：网络波动、配置缺失或依赖加载失败。\n"
                "✅ 建议：稍后重试，并检查 API 配置与日志定位具体问题。"
            )
        finally:
            try:
                await self.avatar_manager.cleanup_used_avatars()
            except Exception as e:
                logger.warning(f"清理头像缓存失败: {e}")

    def _resolve_quick_mode_params(
        self, mode_key: str | None, default_resolution: str, default_aspect_ratio: str
    ) -> tuple[str, str]:
        """根据 quick_mode_settings 覆盖快速模式默认参数"""
        if not mode_key:
            return default_resolution, default_aspect_ratio

        override = self.cfg.quick_mode_overrides.get(mode_key)
        if not override:
            return default_resolution, default_aspect_ratio

        override_resolution, override_aspect_ratio = override
        return (
            override_resolution or default_resolution,
            override_aspect_ratio or default_aspect_ratio,
        )

    async def _handle_quick_mode(
        self,
        event: AstrMessageEvent,
        prompt: str,
        resolution: str,
        aspect_ratio: str,
        mode_name: str,
        mode_key: str | None = None,
        prompt_func: Any = None,
        **kwargs,
    ):
        """处理快速模式的通用逻辑"""
        allowed, limit_message = await self.rate_limiter.check_and_consume(event)
        if not allowed:
            if limit_message:
                yield event.plain_result(limit_message)
            return

        effective_resolution, effective_aspect_ratio = self._resolve_quick_mode_params(
            mode_key, resolution, aspect_ratio
        )

        yield event.plain_result(f"🎨 使用{mode_name}模式生成图像...")

        if prompt_func:
            full_prompt = prompt_func(prompt)
        else:
            full_prompt = prompt

        use_avatar = await self.avatar_handler.should_use_avatar_for_prompt(
            event, prompt
        )

        async for result in self._quick_generate_image(
            event,
            full_prompt,
            use_avatar,
            override_resolution=effective_resolution,
            override_aspect_ratio=effective_aspect_ratio,
            **kwargs,
        ):
            yield result

    # ===== 命令处理 =====

    @filter.command("生图")
    async def generate_image(self, event: AstrMessageEvent, prompt: str):
        """生图指令"""
        allowed, limit_message = await self.rate_limiter.check_and_consume(event)
        if not allowed:
            if limit_message:
                yield event.plain_result(limit_message)
            return

        use_avatar = await self.avatar_handler.should_use_avatar(event)
        generation_prompt = get_generation_prompt(prompt)

        yield event.plain_result("🎨 开始生成图像...")

        async for result in self._quick_generate_image(
            event, generation_prompt, use_avatar
        ):
            yield result

    @filter.command_group("快速")
    def quick_mode_group(self):
        """快速模式指令组"""
        pass

    @quick_mode_group.command("头像")
    async def quick_avatar(self, event: AstrMessageEvent, prompt: str):
        """头像快速模式 - 1K分辨率，1:1比例"""
        async for result in self._handle_quick_mode(
            event, prompt, "1K", "1:1", "头像", "avatar", get_avatar_prompt
        ):
            yield result

    @quick_mode_group.command("海报")
    async def quick_poster(self, event: AstrMessageEvent, prompt: str):
        """海报快速模式 - 2K分辨率，16:9比例"""
        async for result in self._handle_quick_mode(
            event, prompt, "2K", "16:9", "海报", "poster", get_poster_prompt
        ):
            yield result

    @quick_mode_group.command("壁纸")
    async def quick_wallpaper(self, event: AstrMessageEvent, prompt: str):
        """壁纸快速模式 - 4K分辨率，16:9比例"""
        async for result in self._handle_quick_mode(
            event, prompt, "4K", "16:9", "壁纸", "wallpaper", get_wallpaper_prompt
        ):
            yield result

    @quick_mode_group.command("卡片")
    async def quick_card(self, event: AstrMessageEvent, prompt: str):
        """卡片快速模式 - 1K分辨率，3:2比例"""
        async for result in self._handle_quick_mode(
            event, prompt, "1K", "3:2", "卡片", "card", get_card_prompt
        ):
            yield result

    @quick_mode_group.command("手机")
    async def quick_mobile(self, event: AstrMessageEvent, prompt: str):
        """手机快速模式 - 2K分辨率，9:16比例"""
        async for result in self._handle_quick_mode(
            event, prompt, "2K", "9:16", "手机", "mobile", get_mobile_prompt
        ):
            yield result

    @quick_mode_group.command("手办化")
    async def quick_figure(self, event: AstrMessageEvent, prompt: str):
        """手办化快速模式 - 树脂收藏级手办效果"""
        style_type = 1
        clean_prompt = prompt

        if prompt:
            p_lower = prompt.lower()
            if p_lower.startswith("1") or "pvc" in p_lower:
                style_type = 1
                clean_prompt = prompt.replace("1", "", 1).replace("pvc", "", 1).strip()
            elif p_lower.startswith("2") or "gk" in p_lower:
                style_type = 2
                clean_prompt = prompt.replace("2", "", 1).replace("gk", "", 1).strip()

        full_prompt = get_figure_prompt(clean_prompt, style_type)

        async for result in self._handle_quick_mode(
            event,
            full_prompt,
            "2K",
            "3:2",
            "手办化",
            "figure",
            None,
            skip_figure_enhance=True,
        ):
            yield result

    @quick_mode_group.command("表情包")
    async def quick_sticker(self, event: AstrMessageEvent, prompt: str = ""):
        """表情包快速模式"""
        allowed, limit_message = await self.rate_limiter.check_and_consume(event)
        if not allowed:
            if limit_message:
                yield event.plain_result(limit_message)
            return

        yield event.plain_result("🎨 使用表情包模式生成图像...")

        use_avatar = await self.avatar_handler.should_use_avatar(event)
        (
            reference_images,
            avatar_reference,
        ) = await self.image_handler.fetch_images_from_event(
            event, include_at_avatars=use_avatar
        )

        stripped_prompt = (prompt or "").strip()
        simple_mode = stripped_prompt.startswith("简单")
        user_prompt = stripped_prompt[len("简单") :].strip() if simple_mode else prompt

        if not reference_images:
            yield event.plain_result(
                "❌ 表情包模式需要参考图才能生成一致的角色。\n"
                "🧐 可能原因：消息中未附带图片，或图片格式/大小不被支持。\n"
                "✅ 建议：请附上一张清晰的角色参考图（如头像或原表情）后再试。"
            )
            return

        sticker_resolution, sticker_aspect_ratio = self._resolve_quick_mode_params(
            "sticker", "4K", "16:9"
        )

        if not self.cfg.enable_sticker_split:
            full_prompt = (
                get_q_version_sticker_prompt(
                    user_prompt,
                    rows=self.cfg.sticker_grid_rows,
                    cols=self.cfg.sticker_grid_cols,
                )
                if simple_mode
                else get_sticker_prompt(
                    user_prompt,
                    rows=self.cfg.sticker_grid_rows,
                    cols=self.cfg.sticker_grid_cols,
                )
            )
            async for result in self._quick_generate_image(
                event,
                full_prompt,
                use_avatar,
                override_resolution=sticker_resolution,
                override_aspect_ratio=sticker_aspect_ratio,
            ):
                yield result
            return

        # 启用切割的表情包生成
        full_prompt = (
            get_q_version_sticker_prompt(
                user_prompt,
                rows=self.cfg.sticker_grid_rows,
                cols=self.cfg.sticker_grid_cols,
            )
            if simple_mode
            else get_sticker_prompt(
                user_prompt,
                rows=self.cfg.sticker_grid_rows,
                cols=self.cfg.sticker_grid_cols,
            )
        )

        api_start_time = time.perf_counter()
        try:
            yield event.plain_result("🎨  生成中...")

            success, result_data = await self.image_generator.generate_image_core(
                event=event,
                prompt=full_prompt,
                reference_images=reference_images,
                avatar_reference=avatar_reference,
                override_resolution=sticker_resolution,
                override_aspect_ratio=sticker_aspect_ratio,
            )
            api_duration = time.perf_counter() - api_start_time

            if not success or not isinstance(result_data, tuple):
                if isinstance(result_data, str):
                    yield event.plain_result(result_data)
                else:
                    yield event.plain_result(
                        "❌ 表情包生成未成功。\n"
                        "🧐 可能原因：模型未返回有效结果或参考图处理失败。\n"
                        "✅ 建议：重新上传参考图或稍后再试。"
                    )
                return

            image_urls, image_paths, text_content, thought_signature = result_data
            primary_image_path = next(
                (p for p in image_paths if p and Path(p).exists()), None
            )
            if not primary_image_path and image_urls:
                primary_image_path = image_urls[0]

            if not primary_image_path:
                yield event.plain_result(
                    "❌ 未获取到可用的表情源图。\n"
                    "🧐 可能原因：模型未返回图像或图像保存失败。\n"
                    "✅ 建议：检查日志后重试，或更换模型/提示词。"
                )
                return

            # 处理远程 URL
            primary_source = primary_image_path
            if primary_image_path.startswith(("http://", "https://")):
                try:
                    if self.api_client and hasattr(self.api_client, "_get_session"):
                        session = await self.api_client._get_session()
                        _, downloaded = await self.api_client._download_image(
                            primary_image_path, session, use_cache=False
                        )
                        if downloaded and Path(downloaded).exists():
                            primary_image_path = downloaded
                        else:
                            raise RuntimeError("下载结果为空")
                except Exception as e:
                    logger.warning(f"表情源图下载失败: {e}")
                    yield event.plain_result(
                        "❌ 表情源图为远程链接，但下载到本地失败，无法切割。"
                    )
                    async for res in self.message_sender.safe_send(
                        event, event.image_result(primary_source)
                    ):
                        yield res
                    return

            # AI 识别网格
            ai_rows = None
            ai_cols = None
            if self.cfg.vision_provider_id:
                ai_res = await self.vision_handler.detect_grid_rows_cols(
                    primary_image_path
                )
                if ai_res:
                    ai_rows, ai_cols = ai_res

            # 切割图片
            yield event.plain_result("✂️ 正在切割图片...")
            split_start_time = time.perf_counter()
            try:
                split_files: list[str] = []
                if self.cfg.enable_llm_crop:
                    split_files = await self.vision_handler.llm_detect_and_split(
                        primary_image_path
                    )
                    if not split_files:
                        split_files = await asyncio.to_thread(
                            split_image,
                            primary_image_path,
                            rows=6,
                            cols=4,
                            use_sticker_cutter=True,
                            ai_rows=ai_rows,
                            ai_cols=ai_cols,
                        )
                else:
                    split_files = await asyncio.to_thread(
                        split_image,
                        primary_image_path,
                        rows=6,
                        cols=4,
                        use_sticker_cutter=True,
                        ai_rows=ai_rows,
                        ai_cols=ai_cols,
                    )
                split_duration = time.perf_counter() - split_start_time
            except Exception as e:
                logger.error(f"切割图片时发生异常: {e}")
                split_files = []
                split_duration = time.perf_counter() - split_start_time

            if not split_files:
                yield event.plain_result("❌ 图片切割失败，无法生成表情包切片。")
                async for res in self.message_sender.safe_send(
                    event, event.image_result(primary_image_path)
                ):
                    yield res
                return

            yield event.plain_result(
                f"⏱️ API耗时 {api_duration:.1f}s，切割耗时 {split_duration:.1f}s"
            )

            # 发送结果
            sent_success = False
            if self.cfg.enable_sticker_zip:
                zip_path = await asyncio.to_thread(create_zip, split_files)
                if zip_path:
                    try:
                        from astrbot.api.message_components import File

                        file_comp = File(file=zip_path, name=os.path.basename(zip_path))
                        async for res in self.message_sender.safe_send(
                            event, event.chain_result([file_comp])
                        ):
                            yield res
                        sent_success = True
                        async for res in self.message_sender.safe_send(
                            event, event.image_result(primary_image_path)
                        ):
                            yield res
                    except Exception as e:
                        logger.warning(f"发送ZIP失败: {e}")
                        yield event.plain_result("⚠️ 压缩包发送失败，降级使用合并转发")
                        sent_success = False

            # 合并转发发送
            if not sent_success:
                node_content = []
                node_content.append(Plain("原图预览："))
                try:
                    node_content.append(AstrImage.fromFileSystem(primary_image_path))
                except Exception:
                    pass
                node_content.append(Plain("表情包切片："))
                node_content.append(
                    Plain('如果切图失败请尝试使用"切图 x x"手动指定行列')
                )
                for file_path in split_files:
                    try:
                        node_content.append(AstrImage.fromFileSystem(file_path))
                    except Exception:
                        node_content.append(Plain(f"[切片发送失败]: {file_path}"))

                sender_id = "0"
                try:
                    if hasattr(event, "message_obj") and event.message_obj:
                        sender_id = getattr(event.message_obj, "self_id", "0") or "0"
                except Exception:
                    pass
                node = Node(
                    uin=sender_id, name="Gemini表情包生成", content=node_content
                )
                yield event.chain_result([node])

        finally:
            try:
                await self.avatar_manager.cleanup_used_avatars()
            except Exception:
                pass

    @filter.command("切图")
    async def split_image_command(
        self, event: AstrMessageEvent, grid: str | None = None
    ):
        """对消息中的图片进行切割"""
        manual_cols: int | None = None
        manual_rows: int | None = None
        use_sticker_cutter = False
        grid_text = grid or ""

        if not grid_text:
            try:
                raw_msg = getattr(
                    getattr(event, "message_obj", None), "raw_message", ""
                )
                if isinstance(raw_msg, str):
                    grid_text = raw_msg
                elif isinstance(raw_msg, dict):
                    grid_text = str(raw_msg.get("message", "")) or str(raw_msg)
            except Exception:
                grid_text = ""

        def _parse_manual_grid(text: str) -> tuple[int | None, int | None]:
            cleaned = text or ""
            cmd_pos = cleaned.find("切图")
            if cmd_pos != -1:
                cleaned = cleaned[cmd_pos + len("切图") :]
            cleaned = re.sub(r"\\[CQ:[^\\]]+\\]", " ", cleaned)
            cleaned = cleaned.replace("[图片]", " ")
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            m = re.match(r"^(\d{1,2})\s*[xX*]\s*(\d{1,2})", cleaned)
            if not m:
                m = re.match(r"^(\d{1,2})\s+(\d{1,2})", cleaned)
            if not m:
                m = re.match(r"^(\d)(\d)$", cleaned)
            if m:
                c, r = int(m.group(1)), int(m.group(2))
                if c > 0 and r > 0:
                    return c, r
            return None, None

        if "吸附" in grid_text:
            use_sticker_cutter = True
            logger.info("检测到吸附关键词，启用主体吸附分割")

        if grid_text:
            try:
                manual_cols, manual_rows = _parse_manual_grid(grid_text)
            except Exception as e:
                logger.debug(f"切图网格参数处理异常: {e}")

        ref_images, _ = await self.image_handler.fetch_images_from_event(
            event, include_at_avatars=False
        )
        if not ref_images:
            yield event.plain_result("❌ 未找到可切割的图片。")
            return

        src = ref_images[0]
        local_path = await resolve_split_source_to_path(
            src,
            image_input_mode="force_base64",
            api_client=self.api_client,
            download_qq_image_fn=self.image_handler.download_qq_image,
            logger_obj=logger,
        )

        if not local_path:
            yield event.plain_result("❌ 图片下载/解析失败，无法进行切割。")
            return

        # AI 识别网格
        ai_rows: int | None = None
        ai_cols: int | None = None
        ai_detected = False
        if (
            not (manual_cols and manual_rows)
            and not use_sticker_cutter
            and self.cfg.vision_provider_id
        ):
            ai_res = await self.vision_handler.detect_grid_rows_cols(local_path)
            if ai_res:
                ai_rows, ai_cols = ai_res
                ai_detected = True

        if manual_cols and manual_rows:
            yield event.plain_result(
                f"✂️ 按 {manual_cols}x{manual_rows} 网格切割图片..."
            )
        elif ai_detected and ai_rows and ai_cols:
            yield event.plain_result(
                f"🤖 AI 识别到 {ai_cols}x{ai_rows} 网格，优先切割..."
            )
        elif use_sticker_cutter:
            yield event.plain_result("✂️ 使用主体吸附分割算法切图...")
        else:
            yield event.plain_result("✂️ 正在切割图片...")

        split_files: list[str] = []
        try:
            split_start_time = time.perf_counter()
            split_files = await asyncio.to_thread(
                split_image,
                local_path,
                rows=6,
                cols=4,
                manual_rows=manual_rows,
                manual_cols=manual_cols,
                use_sticker_cutter=use_sticker_cutter,
                ai_rows=ai_rows,
                ai_cols=ai_cols,
            )
            split_duration = time.perf_counter() - split_start_time
        except Exception as e:
            logger.error(f"切割图片时发生异常: {e}")
            split_files = []
            split_duration = None

        if not split_files:
            yield event.plain_result("❌ 图片切割失败，未生成有效切片。")
            return

        if split_duration is not None:
            yield event.plain_result(f"⏱️ 切割耗时 {split_duration:.1f}s")

        node_content = [Plain("切片：")]
        for file_path in split_files:
            try:
                node_content.append(AstrImage.fromFileSystem(file_path))
            except Exception:
                node_content.append(Plain(f"[切片发送失败]: {file_path}"))

        sender_id = "0"
        try:
            if hasattr(event, "message_obj") and getattr(event, "message_obj", None):
                sender_id = getattr(event.message_obj, "self_id", "0")
        except Exception:
            pass

        node = Node(uin=sender_id, name="Gemini切图", content=node_content)
        yield event.chain_result([node])

    @filter.command("生图帮助")
    async def show_help(self, event: AstrMessageEvent):
        """显示插件使用帮助"""
        group_id = self.rate_limiter.get_group_id_from_event(event)
        if group_id and self.cfg.group_limit_list:
            if (
                self.cfg.group_limit_mode == "blacklist"
                and group_id in self.cfg.group_limit_list
            ):
                return
            if (
                self.cfg.group_limit_mode == "whitelist"
                and group_id not in self.cfg.group_limit_list
            ):
                return

        grounding_status = "✓ 启用" if self.cfg.enable_grounding else "✗ 禁用"
        smart_retry_status = "✓ 启用" if self.cfg.enable_smart_retry else "✗ 禁用"
        avatar_status = "✓ 启用" if self.cfg.auto_avatar_reference else "✗ 禁用"

        limit_settings = self.raw_config.get("limit_settings", {})
        enable_rate_limit = limit_settings.get("enable_rate_limit", False)
        rate_limit_period = limit_settings.get("rate_limit_period", 60)
        max_requests = limit_settings.get("max_requests_per_group", 5)
        rate_limit_status = (
            f"✓ {max_requests}次/{rate_limit_period}秒"
            if enable_rate_limit
            else "✗ 禁用"
        )

        tool_timeout = self.get_tool_timeout(event)
        timeout_warning = ""
        if tool_timeout < 90:
            timeout_warning = (
                f"⚠️ LLM工具超时时间较短({tool_timeout}秒)，建议设置为90-120秒"
            )

        template_data = {
            "title": f"Gemini 图像生成插件 {self.version}",
            "model": self.cfg.model,
            "api_type": self.cfg.api_type,
            "resolution": self.cfg.resolution,
            "aspect_ratio": self.cfg.aspect_ratio or "默认",
            "api_keys_count": len(self.cfg.api_keys),
            "grounding_status": grounding_status,
            "avatar_status": avatar_status,
            "smart_retry_status": smart_retry_status,
            "tool_timeout": tool_timeout,
            "rate_limit_status": rate_limit_status,
            "timeout_warning": timeout_warning if timeout_warning else "",
            "enable_sticker_split": self.cfg.enable_sticker_split,
        }

        templates_dir = os.path.join(os.path.dirname(__file__), "templates")
        service_settings = self.raw_config.get("service_settings", {})
        theme_settings = service_settings.get("theme_settings", {})

        if self.cfg.help_render_mode == "text":
            yield event.plain_result(render_text(template_data))
            return

        if self.cfg.help_render_mode == "local":
            try:
                img_bytes = render_local_pillow(
                    templates_dir, theme_settings, template_data
                )
                from .tl.tl_utils import _build_image_path

                img_path = _build_image_path("png", "help")
                with open(img_path, "wb") as f:
                    f.write(img_bytes)
                yield event.image_result(str(img_path))
                logger.info("本地 Pillow 帮助图片生成成功")
                return
            except Exception as e:
                logger.error(f"本地 Pillow 渲染失败: {e}")
                yield event.plain_result(render_text(template_data))
                return

        try:
            template_path = get_template_path(templates_dir, theme_settings, ".html")
            with open(template_path, encoding="utf-8") as f:
                jinja2_template = f.read()

            render_opts = {}
            if self.cfg.html_render_options.get("quality") is not None:
                render_opts["quality"] = self.cfg.html_render_options["quality"]
            for key in (
                "type",
                "full_page",
                "omit_background",
                "scale",
                "animations",
                "caret",
                "timeout",
            ):
                if key in self.cfg.html_render_options:
                    render_opts[key] = self.cfg.html_render_options[key]

            try:
                html_image_url = await self.html_render(
                    jinja2_template, template_data, options=render_opts or None
                )
            except TypeError:
                html_image_url = await self.html_render(jinja2_template, template_data)
            logger.info(f"HTML帮助图片生成成功 (使用模板: {template_path.name})")
            yield event.image_result(html_image_url)

        except Exception as e:
            logger.error(f"HTML帮助图片生成失败: {e}")
            yield event.plain_result(render_text(template_data))

    @filter.command("改图")
    async def modify_image(self, event: AstrMessageEvent, prompt: str):
        """根据提示词修改或重做图像"""
        allowed, limit_message = await self.rate_limiter.check_and_consume(event)
        if not allowed:
            if limit_message:
                yield event.plain_result(limit_message)
            return

        modification_prompt = get_modification_prompt(prompt)

        yield event.plain_result("🎨 开始修改图像...")

        use_avatar = await self.avatar_handler.should_use_avatar(event)

        async for result in self._quick_generate_image(
            event, modification_prompt, use_avatar
        ):
            yield result

    @filter.command("换风格")
    async def change_style(self, event: AstrMessageEvent, style: str, prompt: str = ""):
        """改变图像风格"""
        allowed, limit_message = await self.rate_limiter.check_and_consume(event)
        if not allowed:
            if limit_message:
                yield event.plain_result(limit_message)
            return

        full_prompt = get_style_change_prompt(style, prompt)

        combined_prompt = f"{style} {prompt}".strip()
        use_avatar = await self.avatar_handler.should_use_avatar_for_prompt(
            event, combined_prompt
        )
        (
            reference_images,
            avatar_reference,
        ) = await self.image_handler.fetch_images_from_event(
            event, include_at_avatars=use_avatar
        )

        yield event.plain_result("🎨 开始转换风格...")

        api_start = time.perf_counter()
        success, result_data = await self.image_generator.generate_image_core(
            event=event,
            prompt=full_prompt,
            reference_images=reference_images,
            avatar_reference=avatar_reference,
        )
        api_duration = time.perf_counter() - api_start

        send_start = time.perf_counter()
        if success and result_data:
            image_urls, image_paths, text_content, thought_signature = result_data
            async for send_res in self.message_sender.dispatch_send_results(
                event=event,
                image_urls=image_urls,
                image_paths=image_paths,
                text_content=text_content,
                thought_signature=thought_signature,
                scene="换风格",
            ):
                yield send_res
        else:
            yield event.plain_result(result_data)
        send_duration = time.perf_counter() - send_start

        async for res in self.message_sender.send_api_duration(
            event, api_duration, send_duration
        ):
            yield res

    # ===== 兼容性方法（供 LLM 工具等外部调用）=====

    async def get_avatar_reference(self, event: AstrMessageEvent) -> list[str]:
        """兼容旧 API：获取头像作为参考图像"""
        return await self.avatar_handler.get_avatar_reference(event)

    async def should_use_avatar(self, event: AstrMessageEvent) -> bool:
        """兼容旧 API：判断是否应该使用头像作为参考"""
        return await self.avatar_handler.should_use_avatar(event)

    async def should_use_avatar_for_prompt(
        self, event: AstrMessageEvent, prompt: str
    ) -> bool:
        """兼容旧 API：根据提示词判断是否应该使用头像作为参考"""
        return await self.avatar_handler.should_use_avatar_for_prompt(event, prompt)

    async def parse_mentions(self, event: AstrMessageEvent) -> list[int]:
        """兼容旧 API：解析消息中的@用户"""
        return await self.avatar_handler.parse_mentions(event)

    def _filter_valid_reference_images(
        self, images: list[str] | None, source: str
    ) -> list[str]:
        """兼容旧 API：过滤有效参考图片"""
        return self.image_handler.filter_valid_reference_images(images, source)

    async def _fetch_images_from_event(
        self, event: AstrMessageEvent, include_at_avatars: bool = False
    ) -> tuple[list[str], list[str]]:
        """兼容旧 API：从事件中获取图片"""
        return await self.image_handler.fetch_images_from_event(
            event, include_at_avatars
        )

    async def _generate_image_core_internal(
        self,
        event: AstrMessageEvent,
        prompt: str,
        reference_images: list[str],
        avatar_reference: list[str],
        override_resolution: str | None = None,
        override_aspect_ratio: str | None = None,
    ):
        """兼容旧 API：核心图像生成方法"""
        return await self.image_generator.generate_image_core(
            event=event,
            prompt=prompt,
            reference_images=reference_images,
            avatar_reference=avatar_reference,
            override_resolution=override_resolution,
            override_aspect_ratio=override_aspect_ratio,
        )

    async def _dispatch_send_results(
        self,
        event: AstrMessageEvent,
        image_urls: list[str] | None,
        image_paths: list[str] | None,
        text_content: str | None,
        thought_signature: str | None = None,
        scene: str = "默认",
    ):
        """兼容旧 API：发送结果"""
        async for res in self.message_sender.dispatch_send_results(
            event=event,
            image_urls=image_urls,
            image_paths=image_paths,
            text_content=text_content,
            thought_signature=thought_signature,
            scene=scene,
        ):
            yield res

    async def _check_and_consume_limit(
        self, event: AstrMessageEvent
    ) -> tuple[bool, str | None]:
        """兼容旧 API：检查限流"""
        return await self.rate_limiter.check_and_consume(event)

    def _get_group_id_from_event(self, event: AstrMessageEvent) -> str | None:
        """兼容旧 API：获取群ID"""
        return self.rate_limiter.get_group_id_from_event(event)

    async def _download_qq_image(
        self, url: str, event: AstrMessageEvent | None = None
    ) -> str | None:
        """兼容旧 API：下载QQ图片"""
        return await self.image_handler.download_qq_image(url, event)

    async def _llm_detect_and_split(self, image_path: str) -> list[str]:
        """兼容旧 API：LLM 识别并切割"""
        return await self.vision_handler.llm_detect_and_split(image_path)

    async def _detect_grid_rows_cols(self, image_path: str) -> tuple[int, int] | None:
        """兼容旧 API：检测网格行列"""
        return await self.vision_handler.detect_grid_rows_cols(image_path)

    # 兼容属性
    @property
    def auto_avatar_reference(self) -> bool:
        return self.cfg.auto_avatar_reference

    @property
    def verbose_logging(self) -> bool:
        return self.cfg.verbose_logging

    @property
    def max_reference_images(self) -> int:
        return self.cfg.max_reference_images

    @property
    def api_keys(self) -> list[str]:
        return self.cfg.api_keys

    @property
    def model(self) -> str:
        return self.cfg.model

    @property
    def api_type(self) -> str:
        return self.cfg.api_type

    @property
    def api_base(self) -> str:
        return self.cfg.api_base

    @property
    def resolution(self) -> str:
        return self.cfg.resolution

    @property
    def aspect_ratio(self) -> str:
        return self.cfg.aspect_ratio

    @property
    def enable_sticker_split(self) -> bool:
        return self.cfg.enable_sticker_split

    @property
    def enable_sticker_zip(self) -> bool:
        return self.cfg.enable_sticker_zip

    @property
    def enable_grounding(self) -> bool:
        return self.cfg.enable_grounding

    @property
    def enable_smart_retry(self) -> bool:
        return self.cfg.enable_smart_retry

    @property
    def enable_text_response(self) -> bool:
        return self.cfg.enable_text_response

    @property
    def enable_llm_crop(self) -> bool:
        return self.cfg.enable_llm_crop

    @property
    def vision_provider_id(self) -> str:
        return self.cfg.vision_provider_id

    @property
    def sticker_grid_rows(self) -> int:
        return self.cfg.sticker_grid_rows

    @property
    def sticker_grid_cols(self) -> int:
        return self.cfg.sticker_grid_cols

    @property
    def total_timeout(self) -> int:
        return self.cfg.total_timeout

    @property
    def max_attempts_per_key(self) -> int:
        return self.cfg.max_attempts_per_key

    @property
    def nap_server_address(self) -> str:
        return self.cfg.nap_server_address

    @property
    def nap_server_port(self) -> int:
        return self.cfg.nap_server_port

    @property
    def preserve_reference_image_size(self) -> bool:
        return self.cfg.preserve_reference_image_size

    @property
    def image_input_mode(self) -> str:
        return "force_base64"

    @property
    def resolution_param_name(self) -> str:
        return self.cfg.resolution_param_name

    @property
    def aspect_ratio_param_name(self) -> str:
        return self.cfg.aspect_ratio_param_name

    @property
    def force_resolution(self) -> bool:
        return self.cfg.force_resolution

    @property
    def group_limit_mode(self) -> str:
        return self.cfg.group_limit_mode

    @property
    def group_limit_list(self) -> set[str]:
        return self.cfg.group_limit_list

    @property
    def help_render_mode(self) -> str:
        return self.cfg.help_render_mode

    @property
    def html_render_options(self) -> dict:
        return self.cfg.html_render_options

    @property
    def config(self) -> dict:
        return self.raw_config
