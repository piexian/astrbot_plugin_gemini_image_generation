"""
AstrBot Gemini 图像生成插件主文件
支持 Google 官方 API 和 OpenAI 兼容格式 API，提供生图和改图功能，支持智能头像参考
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

import aiohttp
import yaml
from PIL import Image as PILImage

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Reply
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import ProviderType

from .tl import (
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
    enhance_prompt_for_figure,
    get_avatar_prompt,
    get_card_prompt,
    get_figure_prompt,
    get_generation_prompt,
    get_grid_detect_prompt,
    get_mobile_prompt,
    get_modification_prompt,
    get_poster_prompt,
    get_q_version_sticker_prompt,
    get_sticker_bbox_prompt,
    get_sticker_prompt,
    get_style_change_prompt,
    get_wallpaper_prompt,
)
from .tl.llm_tools import GeminiImageGenerationTool
from .tl.tl_api import (
    APIClient,
    APIError,
    ApiRequestConfig,
    get_api_client,
)
from .tl.tl_utils import (
    AvatarManager,
    cleanup_old_images,
    download_qq_avatar,
    encode_file_to_base64,
    send_file,
)
from .tl.tl_utils import (
    is_valid_base64_image_str as util_is_valid_base64_image_str,
)


@register(
    "astrbot_plugin_gemini_image_generation",
    "piexian",
    "Gemini图像生成插件，支持生图和改图，可以自动获取头像作为参考",
    "",
)
class GeminiImageGenerationPlugin(Star):
    # 头像相关关键词（类级别常量，避免每次调用重新创建）
    AVATAR_KEYWORDS = (
        "头像",
        "按照我",
        "根据我",
        "基于我",
        "参考我",
        "我的",
        "avatar",
        "my face",
        "my photo",
        "像我",
        "本人",
    )

    # 快速模式键（集中管理）
    QUICK_MODES = (
        "avatar",
        "poster",
        "wallpaper",
        "card",
        "mobile",
        "figure",
        "sticker",
    )

    def __init__(self, context: Context, config: dict[str, Any]):
        super().__init__(context)
        self.config = config
        # 从 metadata.yaml 读取版本号
        try:
            metadata_path = os.path.join(os.path.dirname(__file__), "metadata.yaml")
            with open(metadata_path, encoding="utf-8") as f:
                metadata = yaml.safe_load(f) or {}
                self.version = str(metadata.get("version", "")).strip()
        except Exception:
            self.version = ""
        if not self.version:
            self.version = "v1.0.0"
        self.api_client: APIClient | None = None
        self.avatar_manager = AvatarManager()
        self._cleanup_task: asyncio.Task | None = None

        # 加载配置
        self._load_config()

        # 注册 LLM 工具
        self._register_llm_tools()

        # 启动定时清理任务
        self._start_cleanup_task()

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

        async def cleanup_loop():
            while True:
                try:
                    await cleanup_old_images()
                    # 每30分钟执行一次
                    await asyncio.sleep(1800)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(f"清理任务异常: {e}")
                    await asyncio.sleep(300)

        self._cleanup_task = asyncio.create_task(cleanup_loop())
        logger.debug("定时清理任务已启动")

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

    def get_tool_timeout(self, event: AstrMessageEvent | None = None) -> int:
        """获取当前聊天环境的 tool_call_timeout 配置"""
        try:
            # 如果提供了事件，尝试获取特定聊天环境的配置
            if event:
                umo = event.unified_msg_origin
                chat_config = self.context.get_config(umo=umo)
                return chat_config.get("provider_settings", {}).get(
                    "tool_call_timeout", 60
                )

            # 否则使用默认配置
            default_config = self.context.get_config()
            return default_config.get("provider_settings", {}).get(
                "tool_call_timeout", 60
            )
        except Exception as e:
            logger.warning(f"获取 tool_call_timeout 配置失败: {e}，使用默认值 60 秒")
            return 60

    async def get_avatar_reference(self, event: AstrMessageEvent) -> list[str]:
        """获取头像作为参考图像，支持用户头像（直接HTTP下载）"""
        avatar_images: list[str] = []
        download_tasks: list[asyncio.Task | asyncio.Future] = []

        # 仅包裹群头像关键词解析，避免小错误影响后续头像获取
        if hasattr(event, "group_id") and event.group_id:
            try:
                group_id = str(event.group_id)
                prompt = (getattr(event, "message_str", "") or "").lower()

                group_avatar_keywords = [
                    "群头像",
                    "本群",
                    "我们的群",
                    "这个群",
                    "群标志",
                    "群图标",
                ]
                explicit_group_request = any(
                    keyword in prompt for keyword in group_avatar_keywords
                )

                should_get_group_avatar = explicit_group_request or (
                    self.auto_avatar_reference
                    and any(
                        keyword in prompt
                        for keyword in [
                            "生图",
                            "绘图",
                            "画图",
                            "生成图片",
                            "制作图片",
                            "改图",
                            "修改",
                        ]
                    )
                )

                if should_get_group_avatar:
                    if explicit_group_request:
                        logger.debug(
                            f"检测到明确的群头像关键词，准备获取群 {group_id} 的头像"
                        )
                    else:
                        logger.debug(
                            f"群聊中生图指令触发，自动获取群 {group_id} 的头像作为参考"
                        )
                    logger.debug("群头像功能暂未实现，跳过")
            except Exception as e:
                logger.debug(f"群头像关键词解析失败: {e}")

        # 获取头像：优先获取@用户头像，如果无@用户则获取发送者头像
        mentioned_users = await self.parse_mentions(event)

        if mentioned_users:
            for user_id in mentioned_users:
                logger.debug(f"获取@用户头像: {user_id}")
                download_tasks.append(
                    download_qq_avatar(
                        str(user_id), f"mentioned_{user_id}", event=event
                    )
                )
        else:
            if (
                hasattr(event, "message_obj")
                and hasattr(event.message_obj, "sender")
                and hasattr(event.message_obj.sender, "user_id")
            ):
                sender_id = str(event.message_obj.sender.user_id)
                logger.debug(f"获取发送者头像: {sender_id}")
                download_tasks.append(
                    download_qq_avatar(sender_id, f"sender_{sender_id}", event=event)
                )

        if download_tasks:
            logger.debug(f"开始并发下载 {len(download_tasks)} 个头像...")
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*download_tasks, return_exceptions=True),
                    timeout=8.0,
                )
                for result in results:
                    if isinstance(result, str) and result:
                        avatar_images.append(result)
                    elif isinstance(result, Exception):
                        logger.warning(f"头像下载任务失败: {result}")
                logger.debug(
                    f"头像下载完成，成功获取 {len(avatar_images)} 个头像，即将返回"
                )
            except asyncio.TimeoutError:
                logger.warning("头像下载总体超时，跳过剩余头像下载")
            except Exception as e:
                logger.warning(f"并发下载头像时发生错误: {e}")

        return avatar_images

    async def should_use_avatar(self, event: AstrMessageEvent) -> bool:
        """判断是否应该使用头像作为参考（只有在有@用户时才使用）"""
        logger.debug(f"检查 auto_avatar_reference: {self.auto_avatar_reference}")
        if not self.auto_avatar_reference:
            return False

        # 检查是否有@用户
        mentioned_users = await self.parse_mentions(event)
        logger.debug(f"@用户数量: {len(mentioned_users)}")

        # 只有当有@用户时才获取头像
        return len(mentioned_users) > 0

    @staticmethod
    def _prompt_contains_avatar_keywords(prompt: str) -> bool:
        """检查提示词中是否包含头像相关关键词"""
        if not prompt:
            return False
        prompt_lower = prompt.lower()
        return any(
            kw in prompt_lower for kw in GeminiImageGenerationPlugin.AVATAR_KEYWORDS
        )

    async def should_use_avatar_for_prompt(
        self, event: AstrMessageEvent, prompt: str
    ) -> bool:
        """
        根据提示词判断是否应该使用头像作为参考
        只有当提示词明确包含头像相关关键词或有@用户时才获取头像
        """
        if not self.auto_avatar_reference:
            return False

        # 检查是否有@用户
        mentioned_users = await self.parse_mentions(event)
        if mentioned_users:
            logger.info(f"检测到@用户，启用头像参考: {len(mentioned_users)} 人")
            return True

        # 检查提示词是否包含头像关键词
        if self._prompt_contains_avatar_keywords(prompt):
            logger.info("提示词包含头像关键词，启用头像参考")
            return True

        logger.info("提示词不含头像关键词且无@用户，跳过头像获取")
        return False

    async def parse_mentions(self, event: AstrMessageEvent) -> list[int]:
        """解析消息中的@用户，返回用户ID列表"""
        mentioned_users = []

        try:
            # 使用框架提供的方法获取消息组件
            messages = event.get_messages()

            for msg_component in messages:
                # 检查是否是@组件
                if hasattr(msg_component, "qq") and str(msg_component.qq) != str(
                    event.get_self_id()
                ):
                    mentioned_users.append(int(msg_component.qq))
                    self.log_debug(f"解析到@用户: {msg_component.qq}")

        except Exception as e:
            logger.warning(f"解析@用户失败: {e}")

        return mentioned_users

    def _load_config(self):
        """从配置加载所有设置"""
        api_settings = self.config.get("api_settings", {})
        provider_id = api_settings.get("provider_id") or ""
        self.provider_id = provider_id
        self.vision_provider_id = api_settings.get("vision_provider_id") or ""
        # 视觉识别模型留空则使用提供商默认模型，这里不强制覆盖
        self.vision_model = (api_settings.get("vision_model") or "").strip()
        # 预先读取用户显式覆盖（如选择 openai、自定义 api_base/model）
        manual_api_type = (api_settings.get("api_type") or "").strip()
        manual_api_base = (api_settings.get("custom_api_base") or "").strip()
        manual_model = (api_settings.get("model") or "").strip()
        self.api_type = manual_api_type or ""
        self.api_base = manual_api_base
        self.model = manual_model or ""
        # 统一从 AstrBot 提供商读取密钥/端点/模型
        self.api_keys: list[str] = []

        # 尝试从 AstrBot 提供商读取动态配置（可能在启动初期尚未就绪）
        self._load_provider_from_context(quiet=True)

        image_settings = self.config.get("image_generation_settings") or {}
        self.resolution = image_settings.get("resolution") or "1K"
        self.aspect_ratio = image_settings.get("aspect_ratio") or "1:1"
        self.enable_grounding = image_settings.get("enable_grounding") or False
        self.max_reference_images = image_settings.get("max_reference_images") or 6
        self.enable_text_response = image_settings.get("enable_text_response") or False
        self.enable_sticker_split = image_settings.get(
            "enable_sticker_split", True
        )  # 默认True，需保留
        self.enable_sticker_zip = image_settings.get("enable_sticker_zip") or False
        self.preserve_reference_image_size = (
            image_settings.get("preserve_reference_image_size") or False
        )
        self.enable_llm_crop = image_settings.get(
            "enable_llm_crop", True
        )  # 默认True，需保留

        # 表情包提示词相关设置
        grid_raw = str(image_settings.get("sticker_grid") or "4x4").strip()
        m = re.match(r"^\s*(\d{1,2})\s*[xX]\s*(\d{1,2})\s*$", grid_raw)
        if m:
            self.sticker_grid_rows = int(m.group(1))
            self.sticker_grid_cols = int(m.group(2))
        else:
            self.sticker_grid_rows = 4
            self.sticker_grid_cols = 4
        self.sticker_grid_rows = min(max(self.sticker_grid_rows, 1), 20)
        self.sticker_grid_cols = min(max(self.sticker_grid_cols, 1), 20)

        # 表情包裁剪识别行列：不暴露为配置项，使用内部默认值即可
        self.sticker_bbox_rows = 6
        self.sticker_bbox_cols = 4

        # 从配置中读取强制分辨率设置，默认为False
        self.force_resolution = image_settings.get("force_resolution") or False
        # 自定义 API 参数名（支持不同 API 的命名差异）
        # 先 strip 再检查是否为空，避免空格字符串导致空键
        _res_param = (image_settings.get("resolution_param_name") or "").strip()
        self.resolution_param_name = _res_param if _res_param else "image_size"
        _aspect_param = (image_settings.get("aspect_ratio_param_name") or "").strip()
        self.aspect_ratio_param_name = (
            _aspect_param if _aspect_param else "aspect_ratio"
        )
        # 参考图传输统一使用 base64，移除格式可选项
        self.image_input_mode = "force_base64"

        # 快速模式参数覆盖（可选）
        quick_mode_settings = self.config.get("quick_mode_settings") or {}
        self.quick_mode_overrides: dict[str, tuple[str | None, str | None]] = {}
        for mode_key in self.QUICK_MODES:
            mode_settings = quick_mode_settings.get(mode_key) or {}
            override_res = (mode_settings.get("resolution") or "").strip()
            override_ar = (mode_settings.get("aspect_ratio") or "").strip()
            if override_res or override_ar:
                self.quick_mode_overrides[mode_key] = (
                    override_res or None,
                    override_ar or None,
                )

        retry_settings = self.config.get("retry_settings") or {}
        self.max_attempts_per_key = retry_settings.get("max_attempts_per_key") or 3
        self.enable_smart_retry = retry_settings.get(
            "enable_smart_retry", True
        )  # 默认True，需保留
        self.total_timeout = retry_settings.get("total_timeout") or 120

        service_settings = self.config.get("service_settings") or {}
        self.nap_server_address = (
            service_settings.get("nap_server_address") or "localhost"
        )
        self.nap_server_port = service_settings.get("nap_server_port") or 3658
        self.auto_avatar_reference = (
            service_settings.get("auto_avatar_reference") or False
        )
        self.verbose_logging = service_settings.get("verbose_logging") or False
        # 帮助页渲染模式: html / local / text
        self.help_render_mode = self.config.get("help_render_mode") or "html"
        # html_render_options 在配置中为顶级字段，兼容历史位置（service_settings 下）
        self.html_render_options = (
            self.config.get("html_render_options")
            or service_settings.get("html_render_options")
            or {}
        )
        try:
            quality_val = self.html_render_options.get("quality")
            if quality_val is not None:
                quality_int = int(quality_val)
                if 1 <= quality_int <= 100:
                    self.html_render_options["quality"] = quality_int
                else:
                    logger.warning(
                        "html_render_options.quality 超出范围(1-100)，已忽略"
                    )
                    self.html_render_options.pop("quality", None)
            type_val = self.html_render_options.get("type")
            if type_val and str(type_val).lower() not in {"png", "jpeg"}:
                logger.warning("html_render_options.type 仅支持 png/jpeg，已忽略")
                self.html_render_options.pop("type", None)
            scale_val = self.html_render_options.get("scale")
            if scale_val and str(scale_val) not in {"css", "device"}:
                logger.warning("html_render_options.scale 仅支持 css/device，已忽略")
                self.html_render_options.pop("scale", None)
        except Exception:
            logger.warning("解析 html_render_options 失败，已忽略质量设置")
            self.html_render_options.pop("quality", None)

        limit_settings = self.config.get("limit_settings") or {}
        raw_mode = str(limit_settings.get("group_limit_mode") or "none").lower()
        if raw_mode not in {"none", "whitelist", "blacklist"}:
            raw_mode = "none"
        self.group_limit_mode: str = raw_mode

        raw_group_list = limit_settings.get("group_limit_list") or []
        # 统一使用字符串形式保存群号，便于与 NapCat/QQ 等平台的群 ID 对齐
        self.group_limit_list: set[str] = {
            str(group_id).strip()
            for group_id in raw_group_list
            if str(group_id).strip()
        }

        self.enable_rate_limit: bool = bool(
            limit_settings.get("enable_rate_limit") or False
        )
        # 限流周期与次数做基础防御，避免异常配置导致错误
        period = limit_settings.get("rate_limit_period") or 60
        max_requests = limit_settings.get("max_requests_per_group") or 5
        try:
            self.rate_limit_period: int = max(int(period), 1)
        except (TypeError, ValueError):
            self.rate_limit_period = 60
        try:
            self.max_requests_per_group: int = max(int(max_requests), 1)
        except (TypeError, ValueError):
            self.max_requests_per_group = 5

        # 内部限流状态：按群维度统计请求时间戳
        self._rate_limit_buckets: dict[str, list[float]] = {}
        self._rate_limit_lock = asyncio.Lock()

    async def _llm_detect_and_split(self, image_path: str) -> list[str]:
        """使用视觉 LLM 识别裁剪框后切割，失败返回空列表"""
        if not self.enable_llm_crop:
            logger.debug("[LLM_CROP] 已关闭视觉裁剪开关，跳过识别")
            return []

        # 若未单独配置视觉识别提供商，则不启用，以免占用生图模型
        if not self.vision_provider_id:
            logger.debug("[LLM_CROP] 未配置 vision_provider_id，跳过视觉裁剪")
            return []

        try:
            # 读取图片尺寸用于提示
            with PILImage.open(image_path) as img:
                width, height = img.size
            prompt = get_sticker_bbox_prompt(
                rows=self.sticker_bbox_rows,
                cols=self.sticker_bbox_cols,
            )

            # 若图过大，先生成压缩副本以提升识别成功率
            image_urls: list[str] = []
            vision_input_path = image_path
            try:
                max_side = max(width, height)
                if max_side > 1200:
                    ratio = 1200 / max_side
                    new_w = int(width * ratio)
                    new_h = int(height * ratio)
                    img = img.resize((new_w, new_h))
                    tmp_path = Path("/tmp") / f"vision_crop_{Path(image_path).stem}.png"
                    img.save(tmp_path, format="PNG")
                    vision_input_path = str(tmp_path)
                    logger.debug(
                        f"[LLM_CROP] 生成压缩副本用于识别: {vision_input_path} ({new_w}x{new_h})"
                    )
            except Exception as e:
                logger.debug(f"[LLM_CROP] 压缩副本生成失败，使用原图: {e}")

            image_urls = [vision_input_path] if vision_input_path else []
            logger.debug(
                f"[LLM_CROP] 调用视觉模型裁剪: provider={self.vision_provider_id}"
            )
            resp = await self.context.llm_generate(
                chat_provider_id=self.vision_provider_id,
                prompt=prompt,
                image_urls=image_urls,
                max_output_tokens=600,
                timeout=120,
                on_llm_request=self._inject_vision_system_prompt,
            )
            text = self._extract_llm_text(resp)
            if not text:
                return []

            # 尝试解析 JSON 数组

            match = re.search(r"\[.*\]", text, re.S)
            json_str = match.group(0) if match else text
            json_str = json_str.replace("```json", "").replace("```", "").strip()
            bboxes = json.loads(json_str)
            if not isinstance(bboxes, list):
                return []

            # 过滤有效框
            clean_boxes = []
            for box in bboxes:
                try:
                    x = int(box.get("x", 0))
                    y = int(box.get("y", 0))
                    w = int(box.get("width", 0))
                    h = int(box.get("height", 0))
                except Exception:
                    continue
                if w > 0 and h > 0:
                    clean_boxes.append({"x": x, "y": y, "width": w, "height": h})

            if not clean_boxes:
                return []

            # 调用裁剪工具
            return await asyncio.to_thread(
                split_image,
                image_path,
                rows=6,
                cols=4,
                bboxes=clean_boxes,
            )
        except Exception as e:
            logger.debug(f"视觉识别裁剪失败: {e}")
            return []

    def _is_aioqhttp_event(self, event: AstrMessageEvent) -> bool:
        """判断事件是否来自aiocqhttp平台"""
        try:
            platform_name = event.get_platform_name()
            return platform_name == "aiocqhttp"
        except AttributeError as e:
            logger.debug(f"判断平台类型失败: {e}")
            return False

    async def _inject_vision_system_prompt(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """为视觉裁剪请求注入 system_prompt，提示返回 JSON 裁剪框"""
        extra = (
            "你是视觉裁剪助手，只需按要求返回 JSON 数组，每个元素包含 x,y,width,height（像素）。"
            "禁止输出除 JSON 之外的任何内容。"
        )
        try:
            if req.system_prompt:
                req.system_prompt += "\n" + extra
            else:
                req.system_prompt = extra
        except Exception:
            pass

    def log_info(self, message: str):
        """根据配置输出info或debug级别日志"""
        if self.verbose_logging:
            logger.info(message)
        else:
            logger.debug(message)

    def log_debug(self, message: str):
        """输出debug级别日志"""
        logger.debug(message)

    async def _safe_send(self, event: AstrMessageEvent, payload):
        """包装发送，若平台发送失败则提示用户"""
        try:
            yield payload
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            yield event.plain_result("⚠️ 消息发送失败，请稍后重试或检查网络/权限。")

    async def _send_api_duration(self, event: AstrMessageEvent, start_time: float):
        """发送 API 耗时消息"""
        try:
            api_duration = time.perf_counter() - start_time
            async for res in self._safe_send(
                event, event.plain_result(f"⏱️ API耗时 {api_duration:.1f}s")
            ):
                yield res
        except Exception:
            pass

    @staticmethod
    def _is_valid_base64_image_str(value: str) -> bool:
        """委托统一的工具方法判断 base64 图像有效性"""
        return util_is_valid_base64_image_str(value)

    @staticmethod
    def _clean_text_content(text: str) -> str:
        """清理文本内容，移除 markdown 图片链接等不可发送的内容"""
        if not text:
            return text

        text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
        text = text.strip()

        return text

    @staticmethod
    def _extract_llm_text(resp: Any) -> str:
        """
        兼容 AstrBot LLMResponse 文本提取：
        - 优先 result_chain 中的 Plain 文本
        - 其次 output_text / response
        """
        try:
            if getattr(resp, "result_chain", None):
                chain = getattr(resp.result_chain, "chain", None)
                if isinstance(chain, list):
                    parts: list[str] = []
                    for comp in chain:
                        text_val = getattr(comp, "text", None)
                        if text_val:
                            parts.append(str(text_val))
                    if parts:
                        return " ".join(parts).strip()

            if getattr(resp, "output_text", None):
                return (resp.output_text or "").strip()
            if getattr(resp, "response", None):
                return (resp.response or "").strip()
        except Exception:
            return ""
        return ""

    def _filter_valid_reference_images(
        self, images: list[str] | None, source: str
    ) -> list[str]:
        """
        过滤出合法的参考图像。

        支持的格式：
        - http(s):// URL（由 tl_api 下载转换）
        - data:image/xxx;base64,... 格式
        - 纯 base64 字符串（需通过魔数校验为图片）

        NapCat 等平台的图片 file_id（例如 D127D0...jpg）会在这里被过滤掉，
        避免传给 Gemini 导致 Base64 解码错误。
        """
        if not images:
            return []

        valid: list[str] = []
        for img in images:
            if not isinstance(img, str) or not img:
                self.log_debug(f"跳过非字符串参考图像({source}): {type(img)}")
                continue

            cleaned = img.strip()

            # URL 形式的图片，交给 tl_api 下载处理
            if cleaned.lower().startswith("http://") or cleaned.lower().startswith(
                "https://"
            ):
                valid.append(cleaned)
                self.log_debug(f"保留 URL 参考图像({source}): {cleaned[:64]}...")
                continue

            if self._is_valid_base64_image_str(cleaned):
                valid.append(cleaned)
            elif cleaned.lower().startswith("data:image/") and ";base64," in cleaned:
                valid.append(cleaned)
            else:
                self.log_debug(f"跳过非支持格式参考图像({source}): {cleaned[:64]}...")

        return valid

    def _get_group_id_from_event(self, event: AstrMessageEvent) -> str | None:
        """从事件中解析群ID，仅在群聊场景下返回"""
        try:
            if hasattr(event, "group_id") and event.group_id:
                return str(event.group_id)
            message_obj = getattr(event, "message_obj", None)
            if message_obj and getattr(message_obj, "group_id", ""):
                return str(message_obj.group_id)
        except Exception as e:
            self.log_debug(f"获取群ID失败: {e}")
        return None

    async def _check_and_consume_limit(
        self, event: AstrMessageEvent
    ) -> tuple[bool, str | None]:
        """
        检查当前事件是否通过群聊黑/白名单和限流校验。

        返回:
            (是否允许继续执行, 不允许时的提示消息)
        """
        group_id = self._get_group_id_from_event(event)

        if not group_id:
            logger.debug("无 group_id，跳过限流")
            return True, None

        if self.group_limit_mode == "whitelist":
            if self.group_limit_list and group_id not in self.group_limit_list:
                logger.debug("拒绝（不在白名单） group_id=%s", group_id)
                return False, None
        elif self.group_limit_mode == "blacklist":
            if self.group_limit_list and group_id in self.group_limit_list:
                logger.debug("拒绝（在黑名单） group_id=%s", group_id)
                return False, None

        if not self.enable_rate_limit:
            logger.debug("未启用限流 group_id=%s", group_id)
            return True, None

        now = time.monotonic()
        window_start = now - self.rate_limit_period

        async with self._rate_limit_lock:
            bucket = self._rate_limit_buckets.get(group_id, [])
            bucket = [ts for ts in bucket if ts >= window_start]

            if len(bucket) >= self.max_requests_per_group:
                earliest = bucket[0]
                retry_after = int(earliest + self.rate_limit_period - now)
                if retry_after < 0:
                    retry_after = 0

                self._rate_limit_buckets[group_id] = bucket
                logger.debug(
                    "触发限流 group_id=%s count=%s/%s retry_after=%s",
                    group_id,
                    len(bucket),
                    self.max_requests_per_group,
                    retry_after,
                )
                return (
                    False,
                    f"⏱️ 本群在最近 {self.rate_limit_period} 秒内的生图请求次数已达上限（{self.max_requests_per_group} 次），请约 {retry_after} 秒后再试。",
                )

            bucket.append(now)
            self._rate_limit_buckets[group_id] = bucket

        logger.debug(
            "限流检查通过 group_id=%s 当前计数=%s",
            group_id,
            len(self._rate_limit_buckets.get(group_id, [])),
        )
        return True, None

    async def initialize(self):
        """插件初始化"""
        # 如果配置为 local 渲染模式，检查并下载字体
        if self.help_render_mode == "local":
            asyncio.create_task(self._ensure_font_for_local_mode())

        # 启动早期 provider_manager 可能尚未就绪，优先等待 on_astrbot_loaded 再初始化
        if self.api_client:
            logger.debug("initialize 已有 api_client，跳过")
            return

        provider_mgr = getattr(self.context, "provider_manager", None)
        ready = False
        if provider_mgr:
            inst_map = getattr(provider_mgr, "inst_map", {}) or {}
            ready = bool(inst_map)
        if not ready:
            logger.debug("启动阶段未检测到可用提供商，等待 AstrBot 完成加载后再初始化")
            return

        logger.debug("尝试加载提供商配置")
        self._load_provider_from_context(quiet=True)
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
        """AstrBot 完成初始化后再次尝试加载提供商，解决启动顺序导致的配置未读问题"""
        logger.debug("on_astrbot_loaded 触发，尝试确保 api_client")
        if not self.api_client:
            self._load_provider_from_context()
            if self.api_client:
                logger.info("✓ AstrBot 加载完成后已成功初始化 API 客户端")
            else:
                logger.error(
                    "✗ AstrBot 加载完成后仍未初始化 API 客户端，请检查提供商配置"
                )

    def _ensure_api_client(self, *, quiet: bool = False) -> bool:
        """确保 API 客户端已初始化，启动初期 provider_mgr 可能尚未就绪"""
        if self.api_client:
            logger.debug("api_client 已就绪")
            return True
        self._load_provider_from_context(quiet=quiet)
        if not self.api_client:
            if quiet:
                logger.debug("API 客户端仍未初始化（quiet=True，跳过错误日志）")
            else:
                logger.error("✗ API 客户端仍未初始化，请检查 AstrBot 提供商配置")
            return False
        return True

    def _load_provider_from_context(self, *, quiet: bool = False):
        """从 AstrBot 提供商读取模型/密钥并初始化客户端，可多次调用用于补偿启动时机"""
        if not quiet:
            logger.debug("尝试读取 AstrBot 提供商配置")
        api_settings = self.config.get("api_settings", {})
        provider_id = api_settings.get("provider_id") or self.provider_id
        manual_api_type = (api_settings.get("api_type") or "").strip()
        manual_api_base = (api_settings.get("custom_api_base") or "").strip()
        manual_model = (api_settings.get("model") or "").strip()

        # 只按配置文件决定 API 类型
        if manual_api_type and not self.api_type:
            self.api_type = manual_api_type
        elif not self.api_type:
            if not quiet:
                logger.error(
                    "✗ 未配置 api_settings.api_type（google/openai/zai/grok2api），无法初始化 API 客户端"
                )
            return

        if manual_api_base and not self.api_base:
            self.api_base = manual_api_base
        if manual_model and not self.model:
            self.model = manual_model

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
                # 补全 provider_id，便于后续视觉识别调用
                if not self.provider_id:
                    self.provider_id = provider.provider_config.get("id", "")

                prov_model = provider.get_model() or provider.provider_config.get(
                    "model_config", {}
                ).get("model")
                # 若用户未手填模型，则使用提供商模型
                if prov_model and not manual_model and not self.model:
                    self.model = prov_model

                prov_keys = provider.get_keys() or []
                # 避免重复覆盖已有非空密钥
                if not self.api_keys:
                    self.api_keys = [
                        str(k).strip() for k in prov_keys if str(k).strip()
                    ]

                prov_base = provider.provider_config.get("api_base")
                # 若用户未手填自定义 base，则使用提供商 base
                if prov_base and not manual_api_base and not self.api_base:
                    self.api_base = prov_base

                logger.info(
                    f"✓ 已从 AstrBot 提供商读取配置，类型={self.api_type} 模型={self.model} 密钥={len(self.api_keys)}"
                )
            else:
                if not quiet:
                    logger.error(
                        "未找到可用的 AstrBot 提供商，无法读取模型/密钥，请在主配置中选择提供商"
                    )
        except Exception as e:
            logger.error(f"读取 AstrBot 提供商配置失败: {e}")

        if self.api_keys:
            self.api_client = get_api_client(self.api_keys)
            logger.info("✓ API 客户端已初始化")
            logger.info(f"  - 类型: {self.api_type}")
            logger.info(f"  - 模型: {self.model}")
            logger.info(f"  - 密钥数量: {len(self.api_keys)}")
            if self.api_base:
                logger.info(f"  - 自定义 API Base: {self.api_base}")
        else:
            if not quiet:
                # 启动阶段可能尚未加载 provider，不再输出 error，交由 on_astrbot_loaded 补偿
                logger.debug("启动阶段未读取到 API 密钥，等待 AstrBot 加载完成后再尝试")

    async def _download_qq_image(
        self, url: str, event: AstrMessageEvent | None = None
    ) -> str | None:
        """对QQ图床/nt.qq.com做特殊处理，优先通过适配器取二进制，失败再走HTTP"""
        try:
            parsed = urllib.parse.urlparse(url)

            # 优先使用适配器API拉取原始图片，避免直链失效
            try:

                async def _call_get_image(client, **kwargs):
                    # 兼容 client.api.call_action 和 client.call_action 两种写法
                    if hasattr(client, "api") and hasattr(client.api, "call_action"):
                        return await client.api.call_action("get_image", **kwargs)
                    if hasattr(client, "call_action"):
                        return await client.call_action("get_image", **kwargs)
                    return None

                bot_client = getattr(event, "bot", None) if event else None

                if bot_client:
                    # 先尝试完整URL
                    resp = await _call_get_image(bot_client, file=url)
                    if isinstance(resp, dict):
                        if resp.get("base64"):
                            return resp["base64"]
                        if resp.get("url"):
                            return resp["url"]
                        if resp.get("file") and Path(resp["file"]).exists():
                            mime_guess = f"image/{Path(resp['file']).suffix.lstrip('.') or 'png'}"
                            data = encode_file_to_base64(resp["file"])
                            return f"data:{mime_guess};base64,{data}"

                    file_id = None
                    qs = urllib.parse.parse_qs(parsed.query or "")
                    if "fileid" in qs and qs["fileid"]:
                        file_id = qs["fileid"][0]
                    if not file_id:
                        file_id = parsed.path.rsplit("/", 1)[-1]

                    if file_id:
                        resp = await _call_get_image(
                            bot_client, file_id=file_id, file=file_id
                        )
                        if isinstance(resp, dict):
                            if resp.get("base64"):
                                return resp["base64"]
                            if resp.get("url"):
                                return resp["url"]
                            if resp.get("file") and Path(resp["file"]).exists():
                                mime_guess = f"image/{Path(resp['file']).suffix.lstrip('.') or 'png'}"
                                data = encode_file_to_base64(resp["file"])
                                return f"data:{mime_guess};base64,{data}"

                # 再尝试通过 context 拿到平台实例调用
                if not bot_client and event:
                    try:
                        from astrbot.core.star.filter.platform_adapter_type import (
                            PlatformAdapterType,
                        )

                        platform = self.context.get_platform(
                            PlatformAdapterType.AIOCQHTTP
                        )
                        client = getattr(platform, "get_client", lambda: None)()
                        if client:
                            resp = await _call_get_image(client, file=url)
                            if isinstance(resp, dict) and resp.get("base64"):
                                return resp["base64"]
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"适配器获取 nt.qq/qpic 图片失败，回退HTTP: {e}")

            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0"
                ),
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Connection": "keep-alive",
            }
            if parsed.netloc:
                headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}"
            if "qpic.cn" in (parsed.netloc or ""):
                headers["Referer"] = "https://qun.qq.com"
            if "nt.qq.com" in (parsed.netloc or ""):
                headers["Referer"] = "https://qun.qq.com"
                headers["Origin"] = "https://qun.qq.com"

            timeout = aiohttp.ClientTimeout(total=12, connect=5)

            async def _http_fetch(target_url: str) -> str | None:
                try:
                    async with aiohttp.ClientSession(
                        headers=headers, trust_env=True
                    ) as session:
                        async with session.get(target_url, timeout=timeout) as resp:
                            if resp.status != 200:
                                logger.warning(
                                    f"QQ图片下载失败: HTTP {resp.status} {resp.reason} | {target_url[:80]}"
                                )
                                return None
                            data = await resp.read()
                            if not data:
                                logger.warning(f"QQ图片为空: {target_url[:80]}")
                                return None
                            mime = resp.headers.get("Content-Type", "image/jpeg")
                            if ";" in mime:
                                mime = mime.split(";", 1)[0]
                            base64_data = base64.b64encode(data).decode("utf-8")
                            return f"data:{mime};base64,{base64_data}"
                except Exception as e:
                    logger.warning(f"QQ图片下载异常: {e} | {target_url[:80]}")
                    return None

            # 先 https，失败再试 http
            data_url = await _http_fetch(url)
            if not data_url and url.startswith("https://"):
                data_url = await _http_fetch("http://" + url[len("https://") :])
            return data_url
        except Exception as e:
            logger.warning(f"QQ图片下载异常: {e} | {url[:80]}")
            return None

    async def _fetch_images_from_event(
        self, event: AstrMessageEvent, include_at_avatars: bool = False
    ) -> tuple[list[str], list[str]]:
        """
        综合提取事件中的图片：当前消息、引用消息及手动@用户头像

        返回 (消息/引用图片, 头像图片)
        """
        message_images: list[str] = []
        avatar_images: list[str] = []
        seen_sources: set[str] = set()
        seen_users: set[str] = set()
        conversion_cache: dict[str, str] = {}
        image_mode = self.image_input_mode  # 统一为 force_base64
        max_images = self.max_reference_images

        if not hasattr(event, "message_obj") or not event.message_obj:
            return message_images, avatar_images

        try:
            message_chain = event.get_messages()
        except Exception:
            message_chain = getattr(event.message_obj, "message", []) or []

        if not message_chain:
            return message_images, avatar_images

        self_id = None
        try:
            self_id = str(event.get_self_id())
        except Exception:
            try:
                self_id = str(getattr(event.message_obj, "self_id", None))
            except Exception:
                self_id = None

        def _is_auto_at(comp: At) -> bool:
            """区分自动@，兼容多种属性命名"""
            flags = [
                getattr(comp, "is_auto", None),
                getattr(comp, "auto", None),
                getattr(comp, "auto_at", None),
                getattr(comp, "autoAt", None),
            ]
            for flag in flags:
                if isinstance(flag, str):
                    flag_val = flag.lower() in {"true", "1", "yes", "y"}
                else:
                    flag_val = bool(flag)
                if flag_val:
                    return True
            return False

        async def convert_image_source(img_source: str, origin: str) -> str | None:
            """
            统一转换图片源为 base64（不再透传 URL）
            """
            if not img_source:
                return None
            if img_source in conversion_cache:
                return conversion_cache[img_source]

            source_str = str(img_source).strip()
            if not source_str:
                return None

            parsed_host = ""
            try:
                parsed_host = urllib.parse.urlparse(source_str).netloc or ""
            except Exception:
                parsed_host = ""

            force_b64 = True

            def _extract_base64_only(val: str) -> str | None:
                """提取纯 base64 数据，剥离 data URL 前缀"""
                try:
                    if ";base64," in val:
                        _, _, b64_part = val.partition(";base64,")
                        base64.b64decode(b64_part, validate=True)
                        return b64_part
                    base64.b64decode(val, validate=True)
                    return val
                except Exception:
                    return None

            # 直接返回已是 base64/data URL 的输入
            if self._is_valid_base64_image_str(source_str):
                b64 = _extract_base64_only(source_str) if force_b64 else source_str
                if b64:
                    conversion_cache[img_source] = b64
                    return b64

            async def to_data_url(candidate: str) -> str | None:
                """统一转为 base64（force 时只返回纯 base64，否则 data URL）"""
                try:
                    if not self.api_client:
                        logger.warning("API 客户端未初始化，无法转换图片为base64")
                        return None
                    (
                        mime_type,
                        base64_data,
                    ) = await self.api_client._normalize_image_input(
                        candidate, image_input_mode=image_mode
                    )
                    if base64_data:
                        data_url = (
                            base64_data
                            if force_b64
                            else (
                                f"data:{mime_type};base64,{base64_data}"
                                if mime_type
                                else base64_data
                            )
                        )
                        conversion_cache[img_source] = data_url
                        return data_url
                    logger.debug(
                        f"跳过无法识别的图片源({origin}): {str(candidate)[:80]}..."
                    )
                except Exception as e:
                    logger.warning(
                        f"转换图片为base64失败({origin}): {repr(e)} | Source: {str(candidate)[:80]}"
                    )
                return None

            # QQ 图床优先转 base64，避免直链失效（含 nt.qq.com）
            if parsed_host and ("qpic.cn" in parsed_host or "nt.qq.com" in parsed_host):
                qq_data = await self._download_qq_image(source_str, event=event)
                if qq_data:
                    if force_b64 and ";base64," in qq_data:
                        qq_data = qq_data.split(";base64,", 1)[1]
                    conversion_cache[img_source] = qq_data
                    return qq_data
                logger.warning(f"QQ图片直链处理失败，尝试通用流程: {source_str[:80]}")
                fallback = await to_data_url(source_str)
                if fallback:
                    return fallback
                # prefer_url 模式下回退为直链；force_base64 直接放弃
                if force_b64:
                    return None
                conversion_cache[img_source] = source_str
                return source_str

            # 统一转 base64，所有参考图不再直接透传 URL
            return await to_data_url(source_str)

        async def handle_image_component(component, origin: str):
            if len(message_images) >= max_images:
                return

            force_b64 = image_mode == "force_base64"
            img_source = None
            if isinstance(component, Image):
                if getattr(component, "url", None):
                    img_source = component.url
                elif getattr(component, "file", None):
                    img_source = component.file
            else:
                if getattr(component, "url", None):
                    img_source = component.url
                elif getattr(component, "file", None):
                    img_source = component.file

            if not img_source:
                return

            if img_source in seen_sources:
                self.log_debug(f"跳过重复图片源({origin}): {str(img_source)[:120]}")
                return

            seen_sources.add(img_source)

            # 优先使用组件自带的 base64 转换能力，避免依赖直链截断
            if hasattr(component, "convert_to_base64"):
                try:
                    raw_b64 = await component.convert_to_base64()
                    if raw_b64:
                        ref_val = (
                            raw_b64
                            if force_b64
                            else f"data:image/jpeg;base64,{raw_b64}"
                        )
                        check_val = raw_b64 if force_b64 else ref_val
                        if self._is_valid_base64_image_str(check_val):
                            conversion_cache[img_source] = ref_val
                            message_images.append(ref_val)
                            self.log_debug(
                                f"✓ 直接从消息组件转换图片 (当前: {len(message_images)}/{max_images})"
                            )
                            return
                        self.log_debug(
                            f"组件转换结果未通过校验，尝试通用流程({origin})"
                        )
                except Exception as e:
                    logger.debug(f"组件 convert_to_base64 异常，回退通用流程: {e}")

            ref_img = await convert_image_source(str(img_source), origin)
            if ref_img:
                message_images.append(ref_img)
                self.log_debug(
                    f"✓ 从{origin}提取图片 (当前: {len(message_images)}/{max_images})"
                )

        async def handle_at_component(component: At, origin: str):
            if not include_at_avatars:
                return

            if _is_auto_at(component):
                self.log_debug(f"跳过自动@用户（{origin}）")
                return

            user_id = getattr(component, "qq", None) or getattr(
                component, "user_id", None
            )
            if not user_id:
                return

            user_id = str(user_id)
            if self_id and user_id == self_id:
                return
            if user_id in seen_users:
                return

            avatar_b64 = await self.avatar_manager.get_avatar(
                user_id, f"at_{user_id}", event=event
            )
            if avatar_b64:
                avatar_images.append(avatar_b64)
                seen_users.add(user_id)
                self.log_debug(f"✓ 获取@用户头像({origin}): {user_id}")
            else:
                self.log_debug(f"✗ 获取@用户头像失败({origin}): {user_id}")

        # 当前消息体处理
        for component in message_chain:
            try:
                if isinstance(component, Image):
                    await handle_image_component(component, "当前消息")
                elif isinstance(component, At):
                    await handle_at_component(component, "当前消息")
                elif isinstance(component, Reply) and component.chain:
                    for reply_comp in component.chain:
                        if isinstance(reply_comp, Image):
                            await handle_image_component(reply_comp, "引用消息")
                        elif isinstance(reply_comp, At):
                            await handle_at_component(reply_comp, "引用消息")
            except Exception as e:
                logger.warning(f"处理消息组件异常: {e}")

        # 如果需要头像但没有@，尝试回退到发送者头像
        if include_at_avatars and not avatar_images:
            try:
                sender_id = None
                if hasattr(event, "message_obj") and hasattr(
                    event.message_obj, "sender"
                ):
                    sender = event.message_obj.sender
                    sender_id = getattr(sender, "user_id", None) or getattr(
                        sender, "userId", None
                    )
                if sender_id and str(sender_id) not in seen_users:
                    sender_id = str(sender_id)
                    avatar_b64 = await self.avatar_manager.get_avatar(
                        sender_id, f"sender_{sender_id}", event=event
                    )
                    if avatar_b64:
                        avatar_images.append(avatar_b64)
                        seen_users.add(sender_id)
                        self.log_debug(f"✓ 回退获取发送者头像: {sender_id}")
            except Exception as e:
                logger.debug(f"回退获取发送者头像失败: {e}")

        # 截断数量，优先保留消息图片，再补充头像
        if len(message_images) > max_images:
            message_images = message_images[:max_images]
        remaining_slots = max(max_images - len(message_images), 0)
        if len(avatar_images) > remaining_slots:
            avatar_images = avatar_images[:remaining_slots]

        if message_images or avatar_images:
            logger.info(
                f"已收集图片: 消息 {len(message_images)} 张，头像 {len(avatar_images)} 张"
            )
        else:
            logger.info("未收集到有效参考图片，若需参考图可直接发送图片或检查网络权限")

        return message_images, avatar_images

    async def _generate_image_core_internal(
        self,
        event: AstrMessageEvent,
        prompt: str,
        reference_images: list[str],
        avatar_reference: list[str],
        override_resolution: str | None = None,
        override_aspect_ratio: str | None = None,
    ) -> tuple[bool, tuple[list[str], list[str], str | None, str | None] | str]:
        """
        内部核心图像生成方法，不发送消息，只返回结果

        Returns:
            tuple[bool, tuple[list[str], list[str], str | None, str | None] | str]:
            (是否成功, (图片URL列表, 图片路径列表, 文本内容, 思维签名) 或错误消息)
        """
        if not self._ensure_api_client():
            return False, (
                "❌ 无法生成图像：API 客户端尚未初始化。\n"
                "🧐 可能原因：服务启动过快，提供商尚未加载或 API 配置/密钥缺失。\n"
                "✅ 建议：先在配置文件中填写有效的 API 密钥并重启服务。"
            )

        valid_msg_images = self._filter_valid_reference_images(
            reference_images, source="消息图片"
        )
        valid_avatar_images = self._filter_valid_reference_images(
            avatar_reference, source="头像"
        )
        all_reference_images = valid_msg_images + valid_avatar_images

        if (
            all_reference_images
            and len(all_reference_images) > self.max_reference_images
        ):
            logger.warning(
                f"参考图片数量 ({len(all_reference_images)}) 超过限制 ({self.max_reference_images})，将截取前 {self.max_reference_images} 张"
            )
            all_reference_images = all_reference_images[: self.max_reference_images]

        # 计算截断后的数量
        final_msg_count = min(len(valid_msg_images), len(all_reference_images))
        final_avatar_count = len(all_reference_images) - final_msg_count

        if final_avatar_count > 0:
            prompt += f"""

[System Note]
The last {final_avatar_count} image(s) provided are User Avatars (marked as optional reference). You may use them for character consistency if needed, but they are NOT mandatory if they conflict with the requested style."""

        response_modalities = "TEXT_IMAGE" if self.enable_text_response else "IMAGE"
        effective_resolution = (
            override_resolution if override_resolution is not None else self.resolution
        )
        effective_aspect_ratio = (
            override_aspect_ratio
            if override_aspect_ratio is not None
            else self.aspect_ratio
        )
        request_config = ApiRequestConfig(
            model=self.model,
            prompt=prompt,
            api_type=self.api_type,
            api_base=self.api_base,
            resolution=effective_resolution,
            aspect_ratio=effective_aspect_ratio,
            enable_grounding=self.enable_grounding,
            response_modalities=response_modalities,
            reference_images=all_reference_images if all_reference_images else None,
            enable_smart_retry=self.enable_smart_retry,
            enable_text_response=self.enable_text_response,
            force_resolution=self.force_resolution,
            verbose_logging=self.verbose_logging,
            image_input_mode=self.image_input_mode,
            resolution_param_name=self.resolution_param_name,
            aspect_ratio_param_name=self.aspect_ratio_param_name,
        )

        logger.info("🎨 图像生成请求:")
        logger.info(f"  模型: {self.model}")
        logger.info(f"  API 类型: {self.api_type}")
        logger.info(
            f"  参考图片: {len(all_reference_images) if all_reference_images else 0} 张"
        )

        try:
            logger.info("🚀 开始调用API生成图像...")
            start_time = asyncio.get_event_loop().time()

            tool_timeout = self.get_tool_timeout(event)
            per_retry_timeout = min(self.total_timeout, tool_timeout)
            max_total_time = tool_timeout
            logger.debug(
                f"超时配置: tool_call_timeout={tool_timeout}s, per_retry_timeout={per_retry_timeout}s, max_retries={self.max_attempts_per_key}, max_total_time={max_total_time}s"
            )

            (
                image_urls,
                image_paths,
                text_content,
                thought_signature,
            ) = await self.api_client.generate_image(
                config=request_config,
                max_retries=self.max_attempts_per_key,
                per_retry_timeout=per_retry_timeout,
                max_total_time=max_total_time,
            )

            end_time = asyncio.get_event_loop().time()
            api_duration = end_time - start_time
            logger.info(f"✅ API调用完成，耗时: {api_duration:.2f}秒")
            logger.info(
                f"🖼️ API 返回图片数量: {len(image_paths)}, URL 数量: {len(image_urls)}"
            )

            if thought_signature:
                logger.debug(f"🧠 思维签名: {thought_signature[:50]}...")

            resolved_paths: list[str] = []
            for idx, img_path in enumerate(image_paths):
                if not img_path:
                    continue
                if Path(img_path).exists():
                    resolved_path = img_path
                    if (
                        self.nap_server_address
                        and self.nap_server_address != "localhost"
                    ):
                        logger.debug(f"开始传输第 {idx + 1} 张图片到远程服务器...")
                        try:
                            remote_path = await asyncio.wait_for(
                                send_file(
                                    img_path,
                                    host=self.nap_server_address,
                                    port=self.nap_server_port,
                                ),
                                timeout=10.0,
                            )
                            if remote_path:
                                resolved_path = remote_path
                        except asyncio.TimeoutError:
                            logger.warning("⚠️ 文件传输超时，使用本地文件")
                        except Exception as e:
                            logger.warning(f"⚠️ 文件传输失败: {e}，将使用本地文件")
                    resolved_paths.append(resolved_path)
                else:
                    logger.warning(f"⚠️ 图像文件不存在或不可访问: {img_path}")
                    resolved_paths.append(img_path)

            image_paths = resolved_paths

            available_paths = [p for p in image_paths if p]
            available_urls = [u for u in image_urls if u]
            if available_paths or available_urls:
                logger.debug(
                    f"图像生成完成，准备返回结果，文件路径 {len(available_paths)} 张，URL {len(available_urls)} 张"
                )
                return True, (
                    image_urls,
                    image_paths,
                    text_content,
                    thought_signature,
                )

            error_msg = (
                "❌ 图像文件未找到，无法返回结果。\n"
                "🧐 可能原因：生成后保存文件失败，或远程传输路径无效。\n"
                "✅ 建议：检查临时目录写入权限与磁盘空间，必要时重试。"
            )
            logger.error(error_msg)
            return False, error_msg

        except APIError as e:
            status_part = (
                f"（状态码 {e.status_code}）" if e.status_code is not None else ""
            )
            error_msg = f"❌ 图像生成失败{status_part}：{e.message}"
            message_lower = (e.message or "").lower()
            api_base_lower = (self.api_base or "").lower()
            if e.status_code == 429:
                error_msg += "\n🧐 可能原因：请求过于频繁或额度已用完。\n✅ 建议：稍等片刻再试，或在配置中增加可用额度/开启智能重试。"
            elif e.status_code == 402:
                error_msg += "\n🧐 可能原因：账户余额不足或套餐到期。\n✅ 建议：充值或更换一组可用的 API 密钥后再试。"
            elif e.status_code == 403:
                error_msg += "\n🧐 可能原因：API 密钥无效、权限不足或访问受限。\n✅ 建议：核对密钥权限、检查 IP 白名单，必要时重新生成密钥。"
            elif e.status_code and 500 <= e.status_code < 600:
                error_msg += "\n🧐 可能原因：上游服务暂时不可用。\n✅ 建议：稍后重试，若频繁出现请联系服务提供方确认故障。"
                # t2i 公共服务繁忙提示
                if ("t2i" in message_lower) or ("t2i" in api_base_lower):
                    error_msg += (
                        "\n⚠️ t2i 公共服务器当前可能繁忙，建议稍后再试；"
                        "如需稳定产能可参考 https://docs.astrbot.app/others/self-host-t2i.html 自建。"
                    )
            else:
                error_msg += "\n🧐 可能原因：请求参数异常或服务返回未知错误。\n✅ 建议：简化提示词/减少参考图后重试，并查看日志获取更多细节。"
            logger.error(error_msg)
            return False, error_msg

        except Exception as e:
            logger.error(f"生成图像时发生未预期的错误: {e}", exc_info=True)
            return False, f"❌ 生成图像时发生错误: {str(e)}"

    def _merge_available_images(
        self, image_paths: list[str] | None, image_urls: list[str] | None
    ) -> list[str]:
        """合并路径与URL，保持顺序并去重，避免同一图重复发送"""
        merged: list[str] = []
        seen: set[str] = set()

        for img in (image_paths or []) + (image_urls or []):
            if not img:
                continue
            if img in seen:
                continue
            seen.add(img)
            merged.append(img)

        return merged

    async def _detect_grid_rows_cols(self, image_path: str) -> tuple[int, int] | None:
        """使用视觉提供商识别网格行列数；失败返回 None"""
        if not self.vision_provider_id:
            return None

        # 视觉识别前确保拿到可读取的本地文件路径（URL 需要先下载）
        local_path = image_path
        if isinstance(image_path, str) and image_path.startswith(
            ("http://", "https://")
        ):
            try:
                if self.api_client and hasattr(self.api_client, "_get_session"):
                    session = await self.api_client._get_session()
                    _, downloaded = await self.api_client._download_image(
                        image_path, session, use_cache=False
                    )
                    if downloaded and Path(downloaded).exists():
                        local_path = downloaded
            except Exception as e:
                logger.debug(f"[GRID_DETECT] 下载图片失败，回退使用原始URL: {e}")

        try:
            with PILImage.open(local_path) as img:
                width, height = img.size
                max_side = max(width, height)
                vision_input_path = local_path
                if max_side > 1200:
                    ratio = 1200 / max_side
                    new_w = int(width * ratio)
                    new_h = int(height * ratio)
                    img = img.resize((new_w, new_h))
                    tmp_path = Path("/tmp") / f"grid_detect_{Path(local_path).stem}.png"
                    img.save(tmp_path, format="PNG")
                    vision_input_path = str(tmp_path)
        except Exception as e:
            logger.debug(f"[GRID_DETECT] 读取/压缩图片失败，使用原图: {e}")
            vision_input_path = local_path

        prompt = get_grid_detect_prompt()

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=self.vision_provider_id,
                prompt=prompt,
                image_urls=[vision_input_path],
                max_output_tokens=200,
                timeout=60,
            )
            text = self._extract_llm_text(resp)
            if not text:
                return None

            match = re.search(r"\{.*\}", text, re.S)
            json_str = match.group(0) if match else text
            data = json.loads(json_str)
            rows = int(data.get("rows", 0))
            cols = int(data.get("cols", 0))
            if (rows == 0 and cols == 0) or rows < 0 or cols < 0:
                logger.debug("[GRID_DETECT] AI 返回 0x0，使用回退切割")
                return None
            # 兼容 AI 返回字符串如 \"4x4\"
            if rows <= 0 or cols <= 0:
                num_match = re.search(r"(\\d{1,2})\\s*[xX]\\s*(\\d{1,2})", text)
                if num_match:
                    cols = int(num_match.group(1))
                    rows = int(num_match.group(2))
            if rows > 0 and cols > 0 and rows <= 20 and cols <= 20:
                logger.debug(f"[GRID_DETECT] AI 行列: {cols} x {rows}")
                return rows, cols
        except Exception as e:
            logger.debug(f"[GRID_DETECT] 视觉识别失败: {e}")

        return None

    def _build_forward_image_component(self, image: str, *, force_base64: bool = False):
        """根据来源构造合并转发图片组件，优先使用本地文件。

        force_base64=True 时，若图片来源为本地文件/data URL，会强制转换为 base64:// 以适配
        NapCat/OneBotv11 等无法直接访问 AstrBot 宿主文件系统的场景。
        """
        from astrbot.api.message_components import Image as AstrImage
        from astrbot.api.message_components import Plain

        try:
            if not image:
                raise ValueError("空的图片地址")
            if image.startswith("data:image/") and ";base64," in image:
                if force_base64:
                    _, _, b64_part = image.partition(";base64,")
                    cleaned = "".join(b64_part.split())
                    if cleaned:
                        return AstrImage(file=f"base64://{cleaned}")
                return AstrImage(file=image)
            if self._is_valid_base64_image_str(image):
                return AstrImage(file=f"base64://{image}")

            fs_candidate = image
            if image.startswith("file:///"):
                fs_candidate = image[8:]

            if os.path.exists(fs_candidate):
                if force_base64:
                    b64_data = encode_file_to_base64(fs_candidate)
                    return AstrImage(file=f"base64://{b64_data}")
                return AstrImage.fromFileSystem(fs_candidate)
            if image.startswith(("http://", "https://")):
                return AstrImage.fromURL(image)

            return AstrImage(file=image)
        except Exception as e:
            logger.warning(f"构造图片组件失败: {e}")
            return Plain(f"[图片不可用: {image[:48]}]")

    async def _dispatch_send_results(
        self,
        event: AstrMessageEvent,
        image_urls: list[str] | None,
        image_paths: list[str] | None,
        text_content: str | None,
        thought_signature: str | None = None,
        scene: str = "默认",
    ):
        """
        根据内容数量选择发送模式：
        - 单图：链式富媒体发送（文本+图一起）
        - 总数<=4：链式富媒体发送（文本+多图一起）
        - 总数>4：合并转发
        """

        cleaned_text = self._clean_text_content(text_content) if text_content else ""
        text_to_send = (
            cleaned_text if (self.enable_text_response and cleaned_text) else ""
        )

        available_images = self._merge_available_images(image_paths, image_urls)
        total_items = len(available_images) + (1 if text_to_send else 0)
        is_aioqhttp = self._is_aioqhttp_event(event)

        logger.debug(
            f"[SEND] 场景={scene}，图片={len(available_images)}，文本={'1' if text_to_send else '0'}，总计={total_items}"
        )

        if not available_images:
            if cleaned_text:
                async for res in self._safe_send(
                    event,
                    event.plain_result(
                        "⚠️ 当前模型只返回了文本，请检查模型配置或者重试"
                    ),
                ):
                    yield res
                if text_to_send:
                    async for res in self._safe_send(
                        event, event.plain_result(f"📝 {text_to_send}")
                    ):
                        yield res
            else:
                async for res in self._safe_send(
                    event,
                    event.plain_result(
                        "❌ 未能成功生成图像。\n"
                        "🧐 可能原因：模型返回空结果、提示词冲突或参考图处理异常。\n"
                        "✅ 建议：简化描述、减少参考图后重试，或稍后重试。"
                    ),
                ):
                    yield res
            return

        # 单图直发
        if len(available_images) == 1:
            logger.debug("[SEND] 采用单图直发模式")
            if text_to_send:
                async for res in self._safe_send(
                    event,
                    event.chain_result(
                        [
                            Comp.Plain(f"\u200b📝 {text_to_send}"),
                            self._build_forward_image_component(
                                available_images[0], force_base64=is_aioqhttp
                            ),
                        ]
                    ),
                ):
                    yield res
            else:
                if is_aioqhttp:
                    img_component = self._build_forward_image_component(
                        available_images[0], force_base64=True
                    )
                    async for res in self._safe_send(
                        event, event.chain_result([img_component])
                    ):
                        yield res
                else:
                    async for res in self._safe_send(
                        event, event.image_result(available_images[0])
                    ):
                        yield res
            if thought_signature:
                logger.debug(f"🧠 思维签名: {thought_signature[:50]}...")
            return

        # AIOCQHTTP 逐图发送（base64）
        if is_aioqhttp:
            logger.debug("[SEND] AIOCQHTTP 平台，采用逐图发送（base64）")
            start_idx = 0
            if text_to_send:
                first_img = self._build_forward_image_component(
                    available_images[0], force_base64=True
                )
                async for res in self._safe_send(
                    event,
                    event.chain_result(
                        [Comp.Plain(f"\u200b📝 {text_to_send}"), first_img]
                    ),
                ):
                    yield res
                start_idx = 1

            for img in available_images[start_idx:]:
                img_component = self._build_forward_image_component(
                    img, force_base64=True
                )
                async for res in self._safe_send(
                    event, event.chain_result([img_component])
                ):
                    yield res

            if thought_signature:
                logger.debug(f"🧠 思维签名: {thought_signature[:50]}...")
            return

        # 短链富媒体发送
        if total_items <= 4:
            logger.debug("[SEND] 采用短链富媒体发送模式")
            chain: list = []
            if text_to_send:
                chain.append(Comp.Plain(f"\u200b📝 {text_to_send}"))
            for img in available_images:
                chain.append(self._build_forward_image_component(img))
            if chain:
                async for res in self._safe_send(event, event.chain_result(chain)):
                    yield res
            if thought_signature:
                logger.debug(f"🧠 思维签名: {thought_signature[:50]}...")
            return

        # 合并转发
        logger.debug("[SEND] 采用合并转发模式")
        from astrbot.api.message_components import Node, Plain

        node_content = []
        if text_to_send:
            node_content.append(Plain(f"📝 {text_to_send}"))

        for idx, img in enumerate(available_images, 1):
            node_content.append(Plain(f"图片 {idx}:"))

            try:
                img_component = self._build_forward_image_component(img)
                node_content.append(img_component)
            except Exception as e:
                logger.warning(f"构造合并转发图片节点失败: {e}")
                node_content.append(Plain(f"[图片不可用: {img[:48]}]"))

        sender_id = "0"
        sender_name = "Gemini图像生成"
        try:
            if hasattr(event, "message_obj") and getattr(event, "message_obj", None):
                sender_id = getattr(event.message_obj, "self_id", "0")
        except Exception:
            pass

        node = Node(uin=sender_id, name=sender_name, content=node_content)

        async for res in self._safe_send(event, event.chain_result([node])):
            yield res

        if thought_signature:
            logger.debug(f"🧠 思维签名: {thought_signature[:50]}...")

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
            ref_images, avatars = await self._fetch_images_from_event(
                event, include_at_avatars=use_avatar
            )

            all_ref_images: list[str] = []
            all_ref_images.extend(
                self._filter_valid_reference_images(ref_images, source="消息图片")
            )
            if use_avatar:
                all_ref_images.extend(
                    self._filter_valid_reference_images(avatars, source="头像")
                )

            enhanced_prompt, is_modification_request = build_quick_prompt(
                prompt, skip_figure_enhance=skip_figure_enhance
            )

            effective_resolution = (
                override_resolution
                if override_resolution is not None
                else self.resolution
            )
            effective_aspect_ratio = (
                override_aspect_ratio
                if override_aspect_ratio is not None
                else self.aspect_ratio
            )

            if (
                self.preserve_reference_image_size
                and is_modification_request
                and all_ref_images
            ):
                effective_resolution = None
                effective_aspect_ratio = None
                self.log_debug("[MODIFY_DEBUG] 保留参考图尺寸，不覆盖分辨率/比例")

            config = ApiRequestConfig(
                model=self.model,
                prompt=enhanced_prompt,
                api_type=self.api_type,
                api_base=self.api_base if self.api_base else None,
                resolution=effective_resolution,
                aspect_ratio=effective_aspect_ratio,
                enable_grounding=self.enable_grounding,
                reference_images=all_ref_images if all_ref_images else None,
                enable_smart_retry=self.enable_smart_retry,
                enable_text_response=self.enable_text_response,
                force_resolution=self.force_resolution,
                verbose_logging=self.verbose_logging,
                image_input_mode=self.image_input_mode,
                resolution_param_name=self.resolution_param_name,
                aspect_ratio_param_name=self.aspect_ratio_param_name,
            )

            self.log_debug("[MODIFY_DEBUG] API请求配置:")
            self.log_debug(f"  - 提示词: {enhanced_prompt[:100]}...")
            self.log_debug(
                f"  - 参考图片数量: {len(all_ref_images) if all_ref_images else 0}"
            )
            self.log_debug(f"  - 是否改图请求: {is_modification_request}")
            self.log_debug(f"  - 模型: {self.model}")

            yield event.plain_result("🎨  生成中...")

            (
                image_urls,
                image_paths,
                text_content,
                thought_signature,
            ) = await self.api_client.generate_image(
                config=config,
                max_retries=self.max_attempts_per_key,
                per_retry_timeout=self.total_timeout,
                max_total_time=self.total_timeout * 2,
            )

            async for send_res in self._dispatch_send_results(
                event=event,
                image_urls=image_urls,
                image_paths=image_paths,
                text_content=text_content,
                thought_signature=thought_signature,
                scene="快捷生成",
            ):
                yield send_res

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

    def _enhance_prompt_for_figure(self, prompt: str) -> str:
        """手办化提示词增强（已废弃，保留兼容性）"""
        return enhance_prompt_for_figure(prompt)

    @filter.command("生图")
    async def generate_image(self, event: AstrMessageEvent, prompt: str):
        """
        生图指令

        Args:
            prompt: 图像描述
        """
        allowed, limit_message = await self._check_and_consume_limit(event)
        if not allowed:
            if limit_message:
                yield event.plain_result(limit_message)
            return

        use_avatar = await self.should_use_avatar(event)

        generation_prompt = get_generation_prompt(prompt)

        yield event.plain_result("🎨 开始生成图像...")
        api_start_time = time.perf_counter()

        async for result in self._quick_generate_image(
            event, generation_prompt, use_avatar
        ):
            yield result
        async for res in self._send_api_duration(event, api_start_time):
            yield res

    def _resolve_quick_mode_params(
        self, mode_key: str | None, default_resolution: str, default_aspect_ratio: str
    ) -> tuple[str, str]:
        """根据 quick_mode_settings 覆盖快速模式默认参数（留空则回落默认）"""
        if not mode_key:
            return default_resolution, default_aspect_ratio

        overrides = getattr(self, "quick_mode_overrides", None) or {}
        override = overrides.get(mode_key)
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
        allowed, limit_message = await self._check_and_consume_limit(event)
        if not allowed:
            if limit_message:
                yield event.plain_result(limit_message)
            return

        effective_resolution, effective_aspect_ratio = self._resolve_quick_mode_params(
            mode_key, resolution, aspect_ratio
        )

        yield event.plain_result(f"🎨 使用{mode_name}模式生成图像...")
        api_start_time = time.perf_counter()

        try:
            if prompt_func:
                full_prompt = prompt_func(prompt)
            else:
                full_prompt = prompt

            use_avatar = await self.should_use_avatar_for_prompt(event, prompt)

            async for result in self._quick_generate_image(
                event,
                full_prompt,
                use_avatar,
                override_resolution=effective_resolution,
                override_aspect_ratio=effective_aspect_ratio,
                **kwargs,
            ):
                yield result

        finally:
            async for res in self._send_api_duration(event, api_start_time):
                yield res

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
        """表情包快速模式 - 4K分辨率，16:9比例，Q版LINE风格

        功能受配置文件控制：
        - enable_sticker_split: 是否自动切割图片
        - enable_sticker_zip: 是否打包发送（如果发送失败则使用合并转发）
        """
        allowed, limit_message = await self._check_and_consume_limit(event)
        if not allowed:
            if limit_message:
                yield event.plain_result(limit_message)
            return

        yield event.plain_result("🎨 使用表情包模式生成图像...")

        use_avatar = await self.should_use_avatar(event)
        reference_images, avatar_reference = await self._fetch_images_from_event(
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
        if not self.enable_sticker_split:
            full_prompt = (
                get_q_version_sticker_prompt(
                    user_prompt,
                    rows=self.sticker_grid_rows,
                    cols=self.sticker_grid_cols,
                )
                if simple_mode
                else get_sticker_prompt(
                    user_prompt,
                    rows=self.sticker_grid_rows,
                    cols=self.sticker_grid_cols,
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

        full_prompt = (
            get_q_version_sticker_prompt(
                user_prompt,
                rows=self.sticker_grid_rows,
                cols=self.sticker_grid_cols,
            )
            if simple_mode
            else get_sticker_prompt(
                user_prompt,
                rows=self.sticker_grid_rows,
                cols=self.sticker_grid_cols,
            )
        )
        api_start_time = time.perf_counter()
        try:
            yield event.plain_result("🎨  生成中...")
            sent_success = False
            split_files: list[str] = []

            success, result_data = await self._generate_image_core_internal(
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
                    else:
                        raise RuntimeError("API 客户端不可用")
                except Exception as e:
                    logger.warning(f"表情源图下载失败: {e}")
                    yield event.plain_result(
                        "❌ 表情源图为远程链接，但下载到本地失败，无法切割。\n"
                        "🧐 可能原因：图片链接临时失效/网络受限/上游防盗链。\n"
                        "✅ 建议：稍后重试，或在群内直接发送图片再试。"
                    )
                    async for res in self._safe_send(
                        event, event.image_result(primary_source)
                    ):
                        yield res
                    return

            ai_rows = None
            ai_cols = None
            if self.vision_provider_id:
                ai_res = await self._detect_grid_rows_cols(primary_image_path)
                if ai_res:
                    ai_rows, ai_cols = ai_res

            # 1. 切割图片
            yield event.plain_result("✂️ 正在切割图片...")
            split_start_time = time.perf_counter()
            try:
                split_files: list[str] = []
                if self.enable_llm_crop:
                    split_files = await self._llm_detect_and_split(primary_image_path)
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
                async for res in self._safe_send(
                    event,
                    event.plain_result(
                        "❌ 图片切割失败，无法生成表情包切片。\n"
                        "🧐 可能原因：源图尺寸异常、裁剪依赖缺失或磁盘空间不足。\n"
                        "✅ 建议：尝试降低分辨率重新生成，检查本地裁剪依赖与磁盘空间后再试。"
                    ),
                ):
                    yield res
                async for res in self._safe_send(
                    event, event.image_result(primary_image_path)
                ):
                    yield res
                return

            try:
                yield event.plain_result(
                    f"⏱️ API耗时 {api_duration:.1f}s，切割耗时 {split_duration:.1f}s"
                )
            except Exception:
                pass

            # 2. 准备发送逻辑

            sent_success = False
            # 2a. 如果开启ZIP，尝试打包发送
            if self.enable_sticker_zip:
                zip_path = await asyncio.to_thread(create_zip, split_files)
                if zip_path:
                    try:
                        from astrbot.api.message_components import File

                        file_comp = File(file=zip_path, name=os.path.basename(zip_path))
                        async for res in self._safe_send(
                            event, event.chain_result([file_comp])
                        ):
                            yield res
                        sent_success = True

                        async for res in self._safe_send(
                            event, event.image_result(primary_image_path)
                        ):
                            yield res
                    except Exception as e:
                        logger.warning(f"发送ZIP失败: {e}")
                        async for res in self._safe_send(
                            event,
                            event.plain_result("⚠️ 压缩包发送失败，降级使用合并转发"),
                        ):
                            yield res
                        sent_success = False
                else:
                    async for res in self._safe_send(
                        event,
                        event.plain_result(
                            "❌ 压缩包创建失败，已尝试改用合并转发。\n"
                            "🧐 可能原因：临时目录无写权限或磁盘空间不足。\n"
                            "✅ 建议：清理磁盘或调整临时目录权限后重试，如仍失败可关闭 ZIP 发送。"
                        ),
                    ):
                        yield res
                    sent_success = False

            # 3. 如果没开启ZIP或者ZIP发送失败，发送合并转发
            if not sent_success:
                from astrbot.api.message_components import Image as AstrImage
                from astrbot.api.message_components import Node, Plain

                node_content = []

                node_content.append(Plain("原图预览："))
                try:
                    node_content.append(AstrImage.fromFileSystem(primary_image_path))
                except Exception:
                    pass
                node_content.append(Plain("表情包切片："))
                node_content.append(
                    Plain("如果切图失败请尝试使用“切图 x x”手动指定行列，例如 切图 4 6")
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
                    uin=sender_id,
                    name="Gemini表情包生成",
                    content=node_content,
                )

                async for res in self._safe_send(event, event.chain_result([node])):
                    yield res

        finally:
            try:
                await self.avatar_manager.cleanup_used_avatars()
            except Exception:
                pass

    @filter.command("切图")
    async def split_image_command(
        self, event: AstrMessageEvent, grid: str | None = None
    ):
        """对消息中的图片进行切割；支持手动指定网格，例如“切图 46”表示横4列竖6行"""
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
            """只解析紧跟在“切图”指令后的数字，支持 4 4 / 44 / 4x4 格式"""
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

        ref_images, _ = await self._fetch_images_from_event(
            event, include_at_avatars=False
        )
        if not ref_images:
            async for res in self._safe_send(
                event,
                event.plain_result(
                    "❌ 未找到可切割的图片。\n"
                    "🧐 可能原因：消息中未包含图片、引用消息或合并转发内无图片。\n"
                    "✅ 建议：请在指令中附带图片，或回复/引用包含图片的消息后再试。"
                ),
            ):
                yield res
            return

        src = ref_images[0]
        local_path = await resolve_split_source_to_path(
            src,
            image_input_mode=self.image_input_mode,
            api_client=self.api_client,
            download_qq_image_fn=self._download_qq_image,
            logger_obj=logger,
        )

        if not local_path:
            async for res in self._safe_send(
                event,
                event.plain_result(
                    "❌ 图片下载/解析失败，无法进行切割。\n"
                    "🧐 可能原因：图片链接失效、群文件无权限或格式不受支持。\n"
                    "✅ 建议：重新发送清晰可访问的图片后再试。"
                ),
            ):
                yield res
            return

        ai_rows: int | None = None
        ai_cols: int | None = None
        ai_detected = False
        if (
            not (manual_cols and manual_rows)
            and not use_sticker_cutter
            and self.vision_provider_id
        ):
            ai_res = await self._detect_grid_rows_cols(local_path)
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
            tip = "✂️ 正在切割图片..."
            if grid:
                tip += "（网格参数未解析，已使用智能切割）"
            yield event.plain_result(tip)

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
            async for res in self._safe_send(
                event,
                event.plain_result(
                    "❌ 图片切割失败，未生成有效切片。\n"
                    "🧐 可能原因：图片格式/尺寸异常，或切割依赖缺失。\n"
                    "✅ 建议：尝试更换图片或检查依赖后重试。"
                ),
            ):
                yield res
            return

        from astrbot.api.message_components import Image as AstrImage
        from astrbot.api.message_components import Node, Plain

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
        group_id = self._get_group_id_from_event(event)
        if group_id and self.group_limit_list:
            if (
                self.group_limit_mode == "blacklist"
                and group_id in self.group_limit_list
            ):
                return
            if (
                self.group_limit_mode == "whitelist"
                and group_id not in self.group_limit_list
            ):
                return

        grounding_status = "✓ 启用" if self.enable_grounding else "✗ 禁用"
        smart_retry_status = "✓ 启用" if self.enable_smart_retry else "✗ 禁用"
        avatar_status = "✓ 启用" if self.auto_avatar_reference else "✗ 禁用"

        limit_settings = self.config.get("limit_settings", {})
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

        try:
            metadata_path = os.path.join(os.path.dirname(__file__), "metadata.yaml")
            with open(metadata_path, encoding="utf-8") as f:
                metadata = yaml.safe_load(f)
                version = metadata.get("version", "v1.3.0")
        except Exception:
            version = "v1.3.0"

        template_data = {
            "title": f"Gemini 图像生成插件 {version}",
            "model": self.model,
            "api_type": self.api_type,
            "resolution": self.resolution,
            "aspect_ratio": self.aspect_ratio or "默认",
            "api_keys_count": len(self.api_keys),
            "grounding_status": grounding_status,
            "avatar_status": avatar_status,
            "smart_retry_status": smart_retry_status,
            "tool_timeout": tool_timeout,
            "rate_limit_status": rate_limit_status,
            "timeout_warning": timeout_warning if timeout_warning else "",
            "enable_sticker_split": self.enable_sticker_split,
        }

        templates_dir = os.path.join(os.path.dirname(__file__), "templates")
        service_settings = self.config.get("service_settings", {})
        theme_settings = service_settings.get("theme_settings", {})

        if self.help_render_mode == "text":
            yield event.plain_result(render_text(template_data))
            return

        if self.help_render_mode == "local":
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
            if self.html_render_options.get("quality") is not None:
                render_opts["quality"] = self.html_render_options["quality"]
            for key in (
                "type",
                "full_page",
                "omit_background",
                "scale",
                "animations",
                "caret",
                "timeout",
            ):
                if key in self.html_render_options:
                    render_opts[key] = self.html_render_options[key]

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
        """
        根据提示词修改或重做图像（默认命令）

        Args:
            prompt: 修改描述，如"把头发改成红色"、"换个背景"、"画成动漫风格"等
        """
        allowed, limit_message = await self._check_and_consume_limit(event)
        if not allowed:
            if limit_message:
                yield event.plain_result(limit_message)
            return

        # 构造改图专用提示词，确保修改意图明确
        modification_prompt = get_modification_prompt(prompt)

        yield event.plain_result("🎨 开始修改图像...")
        api_start_time = time.perf_counter()

        # 根据配置决定是否使用头像参考
        use_avatar = await self.should_use_avatar(event)

        async for result in self._quick_generate_image(
            event, modification_prompt, use_avatar
        ):
            yield result
        async for res in self._send_api_duration(event, api_start_time):
            yield res

    @filter.command("换风格")
    async def change_style(self, event: AstrMessageEvent, style: str, prompt: str = ""):
        """
        改变图像风格

        Args:
            style: 风格描述，如"动漫"、"写实"、"水彩"、"油画"等
            prompt: 额外的修改要求（可选）
        """
        allowed, limit_message = await self._check_and_consume_limit(event)
        if not allowed:
            if limit_message:
                yield event.plain_result(limit_message)
            return

        full_prompt = get_style_change_prompt(style, prompt)

        # 只有提示词包含头像关键词或有@用户时才获取头像
        combined_prompt = f"{style} {prompt}".strip()
        use_avatar = await self.should_use_avatar_for_prompt(event, combined_prompt)
        reference_images, avatar_reference = await self._fetch_images_from_event(
            event, include_at_avatars=use_avatar
        )

        yield event.plain_result("🎨 开始转换风格...")
        api_start_time = time.perf_counter()

        success, result_data = await self._generate_image_core_internal(
            event=event,
            prompt=full_prompt,
            reference_images=reference_images,
            avatar_reference=avatar_reference,
        )

        if success and result_data:
            image_urls, image_paths, text_content, thought_signature = result_data
            async for send_res in self._dispatch_send_results(
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
        async for res in self._send_api_duration(event, api_start_time):
            yield res
