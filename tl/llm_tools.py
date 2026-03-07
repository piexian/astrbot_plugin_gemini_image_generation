"""
LLM 工具定义模块

将图像生成 Tool 拆分为独立类

"""

from __future__ import annotations

import asyncio
import math
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


def _build_reference_info(ref_count: int, avatar_count: int) -> str:
    if ref_count <= 0 and avatar_count <= 0:
        return ""
    ref_info = f"（使用 {ref_count} 张参考图"
    if avatar_count > 0:
        ref_info += f"，{avatar_count} 张头像"
    ref_info += "）"
    return ref_info


def _build_param_info(
    resolution: str | None,
    aspect_ratio: str | None,
) -> str:
    parts: list[str] = []
    if resolution:
        parts.append(f"分辨率 {resolution}")
    if aspect_ratio:
        parts.append(f"比例 {aspect_ratio}")
    return f"（{', '.join(parts)}）" if parts else ""


def _build_background_start_notice(
    ref_count: int,
    avatar_count: int,
    resolution: str | None,
    aspect_ratio: str | None,
) -> str:
    ref_info = _build_reference_info(ref_count, avatar_count)
    param_info = _build_param_info(resolution, aspect_ratio)
    return (
        f"[图像生成任务已启动]{ref_info}{param_info}\n"
        "图片正在后台生成中，通常需要 10-30 秒，高质量生成可能长达几百秒，生成完成后会自动发送给用户。\n"
        "请用你维持原有的人设告诉用户：图片正在生成，请稍等片刻，完成后会自动发送。"
    )


def _build_background_fallback_notice(
    ref_count: int,
    avatar_count: int,
    resolution: str | None,
    aspect_ratio: str | None,
    waited_seconds: int,
) -> str:
    ref_info = _build_reference_info(ref_count, avatar_count)
    param_info = _build_param_info(resolution, aspect_ratio)
    return (
        f"[图像生成任务已转入后台]{ref_info}{param_info}\n"
        f"前台等待 {waited_seconds} 秒后仍未完成，已切换为后台继续生成。\n"
        "图片生成完成后会自动发送给用户。\n"
        "请用你维持原有的人设告诉用户：图片正在生成，请稍等片刻，完成后会自动发送。"
    )


def _resolve_foreground_wait_seconds(plugin: Any, event: Any) -> int:
    reserve_percent = min(
        max(int(getattr(plugin.cfg, "llm_tool_timeout_reserve_percent", 50)), 1),
        100,
    )
    tool_timeout = max(int(plugin.get_tool_timeout(event)), 1)
    session_umo = getattr(event, "unified_msg_origin", None) or "unknown"
    reserved_seconds = math.ceil(tool_timeout * reserve_percent / 100)
    foreground_wait_seconds = max(tool_timeout - reserved_seconds, 0)
    if foreground_wait_seconds <= 0:
        logger.debug(
            "[前台等待] 由于超时预算不足，已禁用前台等待。"
            f"会话={session_umo} 工具超时={tool_timeout}秒 "
            f"预留比例={reserve_percent}%"
        )
        return 0

    logger.debug(
        "[前台等待] 已根据超时预留比例计算前台等待时长。"
        f"会话={session_umo} 工具超时={tool_timeout}秒 "
        f"预留比例={reserve_percent}% 预留时长={reserved_seconds}秒 "
        f"前台等待={foreground_wait_seconds}秒"
    )
    return foreground_wait_seconds


def _create_generation_task(
    plugin: Any,
    event: Any,
    prompt: str,
    reference_images: list[str],
    avatar_reference: list[str],
    override_resolution: str | None = None,
    override_aspect_ratio: str | None = None,
) -> asyncio.Task:
    return asyncio.create_task(
        plugin._generate_image_core_internal(
            event=event,
            prompt=prompt,
            reference_images=reference_images,
            avatar_reference=avatar_reference,
            override_resolution=override_resolution,
            override_aspect_ratio=override_aspect_ratio,
            is_tool_call=True,
        )
    )


async def _cleanup_avatar_cache(plugin: Any, log_prefix: str) -> None:
    try:
        await plugin.avatar_manager.cleanup_used_avatars()
    except Exception as exc:
        logger.debug(f"{log_prefix} 清理头像缓存失败: {exc}")


async def _dispatch_generation_result(
    plugin: Any,
    event: Any,
    success: bool,
    result_data: Any,
    *,
    scene: str,
    fallback_text: str | None = None,
    force_text_response: bool = False,
) -> None:
    if success and isinstance(result_data, tuple):
        image_urls, image_paths, text_content, thought_signature = result_data
        available_images = plugin.message_sender.merge_available_images(
            image_urls,
            image_paths,
        )
        prepared_text = plugin.message_sender.prepare_text_content(
            text_content,
            available_images,
        )
        content_text = prepared_text or fallback_text
        if text_content and not prepared_text and fallback_text:
            logger.info(
                f"[{scene}] Text content only contained image references; using fallback text."
            )
        async for send_res in plugin.message_sender.dispatch_send_results(
            event=event,
            image_urls=image_urls,
            image_paths=image_paths,
            text_content=content_text,
            thought_signature=thought_signature,
            scene=scene,
            force_text_response=force_text_response,
            text_content_prepared=True,
        ):
            try:
                await event.send(send_res)
            except Exception as exc:
                logger.warning(f"[{scene}] 发送结果失败: {exc}")
        return

    error_msg = result_data if isinstance(result_data, str) else "❌ 图像生成失败"
    try:
        await event.send(event.plain_result(error_msg))
    except Exception as exc:
        logger.warning(f"[{scene}] 发送错误消息失败: {exc}")


async def _await_generation_task_and_send(
    plugin: Any,
    event: Any,
    generation_task: asyncio.Task,
    *,
    scene: str,
) -> None:
    try:
        success, result_data = await generation_task
        await _dispatch_generation_result(
            plugin=plugin,
            event=event,
            success=success,
            result_data=result_data,
            scene=scene,
        )
    except Exception as exc:
        logger.error(f"[{scene}] 后台图像生成异常: {exc}", exc_info=True)
        try:
            await event.send(event.plain_result(format_error_message(exc)))
        except Exception as send_error:
            logger.warning(f"[{scene}] 发送异常消息失败: {send_error}")
    finally:
        await _cleanup_avatar_cache(plugin, f"[{scene}]")


def _schedule_generation_delivery(
    plugin: Any,
    event: Any,
    generation_task: asyncio.Task,
    *,
    scene: str,
) -> asyncio.Task:
    sender_task = asyncio.create_task(
        _await_generation_task_and_send(
            plugin=plugin,
            event=event,
            generation_task=generation_task,
            scene=scene,
        )
    )

    def _report_background_exception(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(
                f"[{scene}] 后台发送任务异常终止: {exc}",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    sender_task.add_done_callback(_report_background_exception)
    return sender_task


@dataclass
class GeminiImageGenerationTool(FunctionTool[AstrAgentContext]):
    """
    Gemini 图像生成工具（触发器模式）

    当用户请求图像生成、绘画、改图、换风格或手办化时调用此函数。
    工具会优先在前台短时间等待，快速完成则直接返回结果，超时则转后台继续发送。
    """

    name: str = "gemini_image_generation"
    handler_module_path: str = "astrbot_plugin_gemini_image_generation"
    description: str = (
        "使用 Gemini 模型生成或修改图像。"
        "当用户请求图像生成、绘画、改图、换风格或手办化时调用此函数。"
        "此工具会先在前台短时间等待结果，若快速完成则直接返回图片；"
        "若超出等待时间则自动转为后台生成，完成后自动发送给用户。"
        "判断逻辑：用户说'改成'、'变成'、'基于'、'修改'、'改图'等词时，"
        "设置 use_reference_images=true；用户说'根据我'、'我的头像'或@某人时，"
        "设置 use_reference_images=true 和 include_user_avatar=true。"
        "用户指定分辨率时设置 resolution（仅限 1K/2K/4K 大写）；"
        "用户指定比例时设置 aspect_ratio（仅限 1:1/16:9/4:3/3:2/9:16/4:5/5:4/21:9/3:4/2:3）。"
        "【重要】当用户明确表示要将生成的图片发到论坛/AstrBook时，设置 for_forum=true。"
        "此时工具会等待图片生成完成后返回图片路径，你需要使用 upload_image 工具将图片上传到论坛图床获取URL，"
        "然后在发帖或回复时使用 Markdown 格式 ![描述](URL) 插入图片。"
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
                "for_forum": {
                    "type": "boolean",
                    "description": (
                        "是否用于论坛发帖。当用户明确表示要将生成的图片发到论坛/AstrBook时设置为true。"
                        "设置为true时，工具会等待图片生成完成并返回图片路径，不会自动发送给用户。"
                        "你需要使用返回的路径调用 upload_image 上传到论坛图床。"
                    ),
                    "default": False,
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

        Foreground-first hybrid mode for normal chats.
        When for_forum=True, the tool waits synchronously and returns image paths.
        """
        prompt = kwargs.get("prompt") or ""
        if not prompt.strip():
            return "❌ 缺少必填参数：图像描述不能为空"

        use_reference_images = kwargs.get("use_reference_images", False)
        include_user_avatar = kwargs.get("include_user_avatar", False)
        resolution = kwargs.get("resolution") or None
        aspect_ratio = kwargs.get("aspect_ratio") or None
        for_forum = kwargs.get("for_forum", False)

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
            f"[工具调用] 启动图像生成任务："
            f"提示词长度={prompt_len} 参考图={ref_count} 张 头像={avatar_count} 张 "
            f"分辨率={resolution} 比例={aspect_ratio} 发帖模式={for_forum}"
        )

        # ========== for_forum 模式：同步等待生成完成 ==========
        if for_forum:
            logger.info("[后台任务] 论坛模式：同步等待图片生成完成……")

            try:
                # 直接调用核心生成逻辑，同步等待
                success, result_data = await plugin._generate_image_core_internal(
                    event=event,
                    prompt=prompt,
                    reference_images=reference_images,
                    avatar_reference=avatar_reference,
                    override_resolution=resolution,
                    override_aspect_ratio=aspect_ratio,
                    is_tool_call=True,
                )

                if not success:
                    error_msg = (
                        result_data if isinstance(result_data, str) else "图像生成失败"
                    )
                    return f"❌ 图片生成失败：{error_msg}"

                if not isinstance(result_data, tuple):
                    return "❌ 图片生成返回格式异常"

                image_urls, image_paths, text_content, thought_signature = result_data

                # 优先使用 URL，其次使用本地路径
                available_images = []

                # 先添加 URL（优先级更高）
                if image_urls:
                    for url in image_urls:
                        if url and url.strip():
                            available_images.append(("url", url.strip()))

                # 再添加本地路径
                if image_paths:
                    from pathlib import Path

                    for path in image_paths:
                        if path and Path(path).exists():
                            available_images.append(("path", path))

                if not available_images:
                    return "❌ 图片生成完成，但未获取到有效的图片路径或URL"

                # 构建返回信息
                result_lines = [
                    "[图像生成完成 - 论坛发帖模式]",
                    "",
                    "图片已生成成功！以下是图片信息：",
                    "",
                ]

                for idx, (img_type, img_value) in enumerate(available_images, 1):
                    if img_type == "url":
                        result_lines.append(f"图片{idx} (URL): {img_value}")
                    else:
                        result_lines.append(f"图片{idx} (本地路径): {img_value}")

                result_lines.extend(
                    [
                        "",
                        "【下一步操作】",
                        "1. 使用 upload_image 工具上传图片到论坛图床",
                        "   - 如果有 URL，可以直接使用 URL",
                        "   - 如果只有本地路径，使用本地路径",
                        "2. 获取图床返回的永久 URL",
                        "3. 在发帖或回复时使用 Markdown 格式插入图片：![图片描述](图床URL)",
                    ]
                )

                if text_content:
                    result_lines.extend(
                        ["", f"【AI 生成的图片描述】{text_content[:200]}..."]
                    )

                logger.info(
                    f"[后台任务] 图片生成成功，返回 {len(available_images)} 张图片"
                )
                return "\n".join(result_lines)

            except asyncio.TimeoutError:
                return "❌ 图片生成超时，请稍后重试"
            except Exception as e:
                logger.error(f"[后台任务] 图片生成异常：{e}", exc_info=True)
                return f"❌ 图片生成过程中出错：{str(e)}"
            finally:
                # 清理缓存
                try:
                    await plugin.avatar_manager.cleanup_used_avatars()
                except Exception as e:
                    logger.debug(f"[后台任务] 清理头像缓存失败：{e}")

        generation_task = _create_generation_task(
            plugin=plugin,
            event=event,
            prompt=prompt,
            reference_images=reference_images,
            avatar_reference=avatar_reference,
            override_resolution=resolution,
            override_aspect_ratio=aspect_ratio,
        )
        cleanup_now = True
        foreground_wait_seconds = _resolve_foreground_wait_seconds(plugin, event)

        try:
            if foreground_wait_seconds <= 0:
                cleanup_now = False
                _schedule_generation_delivery(
                    plugin=plugin,
                    event=event,
                    generation_task=generation_task,
                    scene="后台任务",
                )
                return _build_background_start_notice(
                    ref_count=ref_count,
                    avatar_count=avatar_count,
                    resolution=resolution,
                    aspect_ratio=aspect_ratio,
                )

            logger.debug(f"[前台等待] 最多等待 {foreground_wait_seconds} 秒。")
            success, result_data = await asyncio.wait_for(
                asyncio.shield(generation_task),
                timeout=foreground_wait_seconds,
            )
            await _dispatch_generation_result(
                plugin=plugin,
                event=event,
                success=success,
                result_data=result_data,
                scene="前台等待",
                fallback_text="🎨 图片已生成，请查看",
                force_text_response=True,
            )
            return None
        except asyncio.TimeoutError:
            cleanup_now = False
            logger.debug("[前台等待] 等待超时，切换为后台继续生成。")
            _schedule_generation_delivery(
                plugin=plugin,
                event=event,
                generation_task=generation_task,
                scene="后台任务",
            )
            return _build_background_fallback_notice(
                ref_count=ref_count,
                avatar_count=avatar_count,
                resolution=resolution,
                aspect_ratio=aspect_ratio,
                waited_seconds=foreground_wait_seconds,
            )
        except Exception as e:
            logger.error(f"[前台等待] 图像生成异常：{e}", exc_info=True)
            return f"❌ 图片生成过程中出错：{str(e)}"
        finally:
            if cleanup_now:
                await _cleanup_avatar_cache(plugin, "[工具调用]")


async def _background_generate_and_send(
    plugin: GeminiImageGenerationPlugin,
    event: Any,
    prompt: str,
    reference_images: list[str],
    avatar_reference: list[str],
    override_resolution: str | None = None,
    override_aspect_ratio: str | None = None,
) -> None:
    generation_task = _create_generation_task(
        plugin=plugin,
        event=event,
        prompt=prompt,
        reference_images=reference_images,
        avatar_reference=avatar_reference,
        override_resolution=override_resolution,
        override_aspect_ratio=override_aspect_ratio,
    )
    await _await_generation_task_and_send(
        plugin=plugin,
        event=event,
        generation_task=generation_task,
        scene="后台任务",
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
    logger.debug(f"include_user_avatar 参数值：{avatar_value}")
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
        f"[工具调用] 收集到参考图：消息 {len(reference_images)} 张，"
        f"头像 {len(avatar_reference)} 张"
    )

    # 调用核心生成逻辑
    success, result_data = await plugin._generate_image_core_internal(
        event=event,
        prompt=prompt,
        reference_images=reference_images,
        avatar_reference=avatar_reference,
        is_tool_call=True,
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
