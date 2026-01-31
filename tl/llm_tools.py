"""
LLM 工具定义模块

将图像生成 Tool 拆分为独立类

"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from .tl_utils import format_error_message

if TYPE_CHECKING:
    from ..main import GeminiImageGenerationPlugin


# 参数枚举常量（工具定义和验证共用）
VALID_RESOLUTIONS = {"1K", "2K", "4K"}
VALID_ASPECT_RATIOS = {
    "1:1",
    "16:9",
    "4:3",
    "3:2",
    "9:16",
    "4:5",
    "5:4",
    "21:9",
    "3:4",
    "2:3",
}


@dataclass
class GeminiImageGenerationTool(FunctionTool[AstrAgentContext]):
    """
    Gemini 图像生成工具（触发器模式）

    当用户请求图像生成、绘画、改图、换风格或手办化时调用此函数。
    工具会立即返回确认信息，图片在后台生成完成后自动发送。
    """

    name: str = "gemini_image_generation"
    handler_module_path: str = "astrbot_plugin_gemini_image_generation"
    description: str = (
        "使用 Gemini 模型生成或修改图像。"
        "当用户请求图像生成、绘画、改图、换风格或手办化时调用此函数。"
        "此工具会立即返回确认，图片会在后台生成完成后自动发送给用户。"
        "判断逻辑：用户说'改成'、'变成'、'基于'、'修改'、'改图'等词时，"
        "设置 use_reference_images=true；用户说'根据我'、'我的头像'或@某人时，"
        "设置 use_reference_images=true 和 include_user_avatar=true。"
        "用户指定分辨率时设置 resolution（仅限 1K/2K/4K 大写）；"
        "用户指定比例时设置 aspect_ratio（仅限 1:1/16:9/4:3/3:2/9:16/4:5/5:4/21:9/3:4/2:3）。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "图像生成或修改的详细描述",
                },
                "use_reference_images": {
                    "type": "boolean",
                    "description": (
                        "是否使用上下文中的参考图片。"
                        "当用户意图是修改、变换或基于现有图片时设置为true"
                    ),
                    "default": False,
                },
                "include_user_avatar": {
                    "type": "boolean",
                    "description": (
                        "是否包含用户头像作为参考图像。"
                        "当用户说'根据我'、'我的头像'或@某人时设置为true"
                    ),
                    "default": False,
                },
                "resolution": {
                    "type": "string",
                    "description": (
                        "图像分辨率，可选参数，留空使用默认配置。"
                        "仅支持：1K、2K、4K（必须大写英文）"
                    ),
                    "enum": sorted(VALID_RESOLUTIONS),
                },
                "aspect_ratio": {
                    "type": "string",
                    "description": (
                        "图像长宽比，可选参数，留空使用默认配置。"
                        "仅支持：1:1、16:9、4:3、3:2、9:16、4:5、5:4、21:9、3:4、2:3"
                    ),
                    "enum": sorted(VALID_ASPECT_RATIOS),
                },
            },
            "required": ["prompt"],
        }
    )

    # 插件实例引用（在创建时设置）
    plugin: Any = Field(default=None, repr=False)

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        """
        执行图像生成工具（触发器模式）

        立即返回确认信息，图片生成在后台异步执行
        """
        prompt = kwargs.get("prompt") or ""
        if not prompt.strip():
            return "❌ 缺少必填参数：图像描述不能为空"

        use_reference_images = kwargs.get("use_reference_images", False)
        include_user_avatar = kwargs.get("include_user_avatar", False)
        resolution = kwargs.get("resolution") or None
        aspect_ratio = kwargs.get("aspect_ratio") or None

        # 获取事件上下文
        event = context.context.event
        plugin = self.plugin

        if not plugin:
            return "❌ 工具未正确初始化，缺少插件实例引用"

        # 检查限流
        allowed, limit_message = await plugin._check_and_consume_limit(event)
        if not allowed:
            return limit_message or "请求过于频繁，请稍后再试"

        if not plugin.api_client:
            return (
                "❌ 无法生成图像：API 客户端尚未初始化\n"
                "🧐 可能原因：API 密钥未配置或加载失败\n"
                "✅ 建议：在插件配置中填写有效密钥并重启服务"
            )

        # 布尔参数已在工具定义中声明为 boolean 类型，直接使用
        include_avatar = bool(include_user_avatar)
        include_ref_images = bool(use_reference_images)

        # 验证分辨率和比例参数，无效值回退到默认配置
        # 大小写兼容：LLM 有时会输出小写（如 "1k"），统一转换为大写后验证
        if resolution:
            resolution = resolution.upper()
        resolution = resolution if resolution in VALID_RESOLUTIONS else None
        aspect_ratio = aspect_ratio if aspect_ratio in VALID_ASPECT_RATIOS else None

        # 获取参考图片（需要在启动后台任务前获取，因为 event 可能在之后失效）
        reference_images, avatar_reference = await plugin._fetch_images_from_event(
            event, include_at_avatars=include_avatar
        )

        if not include_ref_images:
            reference_images = []
        if not include_avatar:
            avatar_reference = []

        ref_count = len(reference_images)
        avatar_count = len(avatar_reference)

        # 日志记录（仅记录长度和参数摘要，避免记录用户原始内容）
        prompt_len = len(prompt)
        logger.info(
            f"[TOOL-TRIGGER] 启动后台图像生成任务: "
            f"prompt_len={prompt_len} refs={ref_count} avatars={avatar_count} "
            f"resolution={resolution} aspect_ratio={aspect_ratio}"
        )

        # 启动后台任务执行图像生成
        gen_task = asyncio.create_task(
            background_generate_and_send(
                plugin=plugin,
                event=event,
                prompt=prompt,
                reference_images=reference_images,
                avatar_reference=avatar_reference,
                override_resolution=resolution,
                override_aspect_ratio=aspect_ratio,
            )
        )
        # 捕获任务异常，防止静默失败
        gen_task.add_done_callback(
            lambda t: t.exception()
            and logger.error(f"图像生成后台任务异常终止: {t.exception()}")
        )

        # 立即返回确认信息给 AI，提示 AI 告知用户需要等待
        ref_info = ""
        if ref_count > 0 or avatar_count > 0:
            ref_info = f"（使用 {ref_count} 张参考图"
            if avatar_count > 0:
                ref_info += f"，{avatar_count} 张头像"
            ref_info += "）"

        # 分辨率和比例信息
        param_info = ""
        if resolution or aspect_ratio:
            parts = []
            if resolution:
                parts.append(f"分辨率 {resolution}")
            if aspect_ratio:
                parts.append(f"比例 {aspect_ratio}")
            param_info = f"（{', '.join(parts)}）"

        # 返回给 AI 的提示信息，引导 AI 用自己的人格告知用户
        return (
            f"[图像生成任务已启动]{ref_info}{param_info}\n"
            "图片正在后台生成中，通常需要 10-30 秒，高质量生成可能长达几百秒，生成完成后会自动发送给用户。\n"
            "请用你维持原有的人设告诉用户：图片正在生成，请稍等片刻，完成后会自动发送。"
        )


async def background_generate_and_send(
    plugin: GeminiImageGenerationPlugin,
    event: Any,
    prompt: str,
    reference_images: list[str],
    avatar_reference: list[str],
    override_resolution: str | None = None,
    override_aspect_ratio: str | None = None,
) -> None:
    """
    后台执行图像生成并发送结果

    此函数在后台异步执行，不阻塞工具调用
    """
    try:
        logger.debug("[TOOL-BG] 开始后台图像生成...")

        # 调用核心生成逻辑
        success, result_data = await plugin._generate_image_core_internal(
            event=event,
            prompt=prompt,
            reference_images=reference_images,
            avatar_reference=avatar_reference,
            override_resolution=override_resolution,
            override_aspect_ratio=override_aspect_ratio,
        )

        if success and isinstance(result_data, tuple):
            image_urls, image_paths, text_content, thought_signature = result_data

            # 使用 MessageSender 发送结果（和普通指令一样）
            async for send_res in plugin.message_sender.dispatch_send_results(
                event=event,
                image_urls=image_urls,
                image_paths=image_paths,
                text_content=text_content,
                thought_signature=thought_signature,
                scene="LLM工具",
            ):
                # 使用 event 发送结果
                try:
                    await event.send(send_res)
                except Exception as e:
                    logger.warning(f"[TOOL-BG] 发送结果失败: {e}")

            logger.info(
                f"[TOOL-BG] 图像生成成功，已发送 {len(image_paths or [])} 张图片"
            )

        else:
            # 生成失败，发送错误消息
            error_msg = (
                format_error_message(result_data)
                if isinstance(result_data, str)
                else "❌ 图像生成失败"
            )
            try:
                await event.send(event.plain_result(error_msg))
            except Exception as e:
                logger.warning(f"[TOOL-BG] 发送错误消息失败: {e}")

            logger.warning(f"[TOOL-BG] 图像生成失败: {error_msg}")

    except Exception as e:
        logger.error(f"[TOOL-BG] 后台图像生成异常: {e}", exc_info=True)
        try:
            await event.send(event.plain_result(format_error_message(e)))
        except Exception as send_error:
            logger.warning(f"[TOOL-BG] 发送异常消息失败: {send_error}")

    finally:
        # 清理缓存
        try:
            await plugin.avatar_manager.cleanup_used_avatars()
        except Exception as e:
            logger.debug(f"[TOOL-BG] 清理头像缓存: {e}")


# 向后兼容别名（避免外部引用断裂）
_background_generate_and_send = background_generate_and_send


async def execute_gemini_generate_image_tool(
    plugin: GeminiImageGenerationPlugin,
    event: Any,
    prompt: str,
    use_reference_images: bool = False,
    include_user_avatar: bool = False,
    resolution: str = "",
    aspect_ratio: str = "",
) -> str:
    """执行 Gemini 生图工具（触发器模式）"""
    prompt = prompt or ""
    if not prompt.strip():
        return "❌ 缺少必填参数：图像描述不能为空"

    allowed, limit_message = await plugin._check_and_consume_limit(event)
    if not allowed:
        return limit_message or "请求过于频繁，请稍后再试"

    if not plugin.api_client:
        return (
            "❌ 无法生成图像：API 客户端尚未初始化\n"
            "🧐 可能原因：API 密钥未配置或加载失败\n"
            "✅ 建议：在插件配置中填写有效密钥并重启服务"
        )

    def _parse_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, (int, float)):
            return value != 0
        return str(value).strip().lower() in {"true", "1", "yes", "y", "是"}

    include_ref_images = _parse_bool(use_reference_images)
    include_avatar = _parse_bool(include_user_avatar)

    resolution = resolution or None
    aspect_ratio = aspect_ratio or None
    if resolution:
        resolution = resolution.upper()
    resolution = resolution if resolution in VALID_RESOLUTIONS else None
    aspect_ratio = aspect_ratio if aspect_ratio in VALID_ASPECT_RATIOS else None

    reference_images, avatar_reference = await plugin._fetch_images_from_event(
        event, include_at_avatars=include_avatar
    )

    if not include_ref_images:
        reference_images = []
    if not include_avatar:
        avatar_reference = []

    ref_count = len(reference_images)
    avatar_count = len(avatar_reference)

    logger.info(
        f"[TOOL-TRIGGER] 启动后台图像生成任务: "
        f"prompt_len={len(prompt)} refs={ref_count} avatars={avatar_count} "
        f"resolution={resolution} aspect_ratio={aspect_ratio}"
    )

    gen_task = asyncio.create_task(
        background_generate_and_send(
            plugin=plugin,
            event=event,
            prompt=prompt,
            reference_images=reference_images,
            avatar_reference=avatar_reference,
            override_resolution=resolution,
            override_aspect_ratio=aspect_ratio,
        )
    )
    gen_task.add_done_callback(
        lambda t: t.exception()
        and logger.error(f"图像生成后台任务异常终止: {t.exception()}")
    )

    ref_info = ""
    if ref_count > 0 or avatar_count > 0:
        ref_info = f"（使用 {ref_count} 张参考图"
        if avatar_count > 0:
            ref_info += f"，{avatar_count} 张头像"
        ref_info += "）"

    param_info = ""
    if resolution or aspect_ratio:
        parts = []
        if resolution:
            parts.append(f"分辨率 {resolution}")
        if aspect_ratio:
            parts.append(f"比例 {aspect_ratio}")
        param_info = f"（{', '.join(parts)}）"

    return (
        f"[图像生成任务已启动]{ref_info}{param_info}\n"
        "图片正在后台生成中，通常需要 10-30 秒，高质量生成可能长达几百秒，生成完成后会自动发送给用户。\n"
        "请用你维持原有的人设告诉用户：图片正在生成，请稍等片刻，完成后会自动发送。"
    )


# 保留旧的辅助函数以保持向后兼容（已弃用）
async def execute_image_generation_tool(
    plugin: GeminiImageGenerationPlugin,
    event: Any,
    prompt: str,
    use_reference_images: str = "false",
    include_user_avatar: str = "false",
) -> list[Any]:
    """
    执行图像生成工具的辅助函数

    已弃用：请使用 GeminiImageGenerationTool 类代替。
    此函数保留用于向后兼容 @filter.llm_tool 装饰器方式。
    """
    from pathlib import Path

    from astrbot.api.message_components import Image as AstrImage

    # 检查限流
    allowed, limit_message = await plugin._check_and_consume_limit(event)
    if not allowed:
        return [limit_message or "请求过于频繁，请稍后再试。"]

    if not plugin.api_client:
        return [
            "❌ 无法生成图像：API 客户端尚未初始化。\n"
            "🧐 可能原因：API 密钥未配置或加载失败。\n"
            "✅ 建议：在插件配置中填写有效密钥并重启服务。"
        ]

    # 解析参数
    avatar_value = str(include_user_avatar).lower()
    logger.debug(f"include_user_avatar 参数: {avatar_value}")
    include_avatar = avatar_value in {"true", "1", "yes", "y", "是"}
    include_ref_images = str(use_reference_images).lower() in {
        "true",
        "1",
        "yes",
        "y",
        "是",
    }

    # 获取参考图片
    reference_images, avatar_reference = await plugin._fetch_images_from_event(
        event, include_at_avatars=include_avatar
    )

    if not include_ref_images:
        reference_images = []
    if not include_avatar:
        avatar_reference = []

    logger.info(
        f"[TOOL] 收集到参考图: 消息 {len(reference_images)} 张，"
        f"头像 {len(avatar_reference)} 张"
    )

    # 调用核心生成逻辑
    success, result_data = await plugin._generate_image_core_internal(
        event=event,
        prompt=prompt,
        reference_images=reference_images,
        avatar_reference=avatar_reference,
    )

    # 清理缓存
    try:
        await plugin.avatar_manager.cleanup_cache()
    except Exception as e:
        logger.warning(f"清理头像缓存失败: {e}")

    if success and isinstance(result_data, tuple):
        image_urls, image_paths, text_content, thought_signature = result_data

        results: list[Any] = []
        if text_content:
            results.append(text_content)
        if thought_signature:
            results.append(thought_signature)

        # 添加图片
        for img_path in image_paths or []:
            if img_path and Path(img_path).exists():
                results.append(AstrImage.fromFileSystem(img_path))

        # 如果没有本地图片，使用 URL
        if not any(isinstance(r, AstrImage) for r in results):
            for url in image_urls or []:
                if url:
                    results.append(AstrImage(file=url))

        return results if results else ["✅ 图片已生成"]

    # 失败情况
    error_msg = (
        format_error_message(result_data)
        if isinstance(result_data, str)
        else "图像生成失败"
    )
    return [error_msg]
