"""
LLM 工具定义模块

将图像生成 Tool 拆分为独立类

"""

from __future__ import annotations

import asyncio
import math
import os
import re
from typing import TYPE_CHECKING, Any

import mcp.types
from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from .openai_image_size import (
    CUSTOM_SIZE_DEFAULT,
    CUSTOM_SIZE_MAX_EDGE,
    CUSTOM_SIZE_MAX_PIXELS,
    CUSTOM_SIZE_MIN_PIXELS,
    normalize_size_mode,
    validate_custom_size,
)
from .thought_signature import log_thought_signature_debug
from .tl_utils import encode_file_to_base64, format_error_message

if TYPE_CHECKING:
    from ..main import GeminiImageGenerationPlugin


# 参数枚举常量（工具定义和验证共用）
RESOLUTION_OPTIONS = ("1K", "2K", "4K")
ASPECT_RATIO_OPTIONS = (
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
)
VALID_RESOLUTIONS = set(RESOLUTION_OPTIONS)
VALID_ASPECT_RATIOS = set(ASPECT_RATIO_OPTIONS)


def _get_openai_images_settings(plugin: Any) -> dict[str, Any]:
    if not plugin or not getattr(plugin, "cfg", None):
        return {}

    settings = getattr(plugin.cfg, "openai_images_settings", None)
    if isinstance(settings, dict) and settings:
        return settings

    overrides = getattr(plugin.cfg, "provider_overrides", None) or {}
    candidate = overrides.get("openai_images", {})
    return candidate if isinstance(candidate, dict) else {}


def _is_openai_images_custom_size_mode(plugin: Any) -> bool:
    if not plugin or not getattr(plugin, "cfg", None):
        return False

    api_type = str(getattr(plugin.cfg, "api_type", "") or "").strip().lower()
    api_type = api_type.replace("-", "_")
    if api_type != "openai_images":
        return False

    try:
        size_mode = normalize_size_mode(
            _get_openai_images_settings(plugin).get("size_mode")
        )
        return size_mode == "custom"
    except ValueError as exc:
        logger.warning(
            f"[工具定义] openai_images size_mode 非法，回退为预设模式: {exc}"
        )
        return False


def _custom_size_constraints_text() -> str:
    return (
        f"格式必须为 WxH（支持 x 或 ×），例如 {CUSTOM_SIZE_DEFAULT} 或 2048x1152；"
        f"最大边 <= {CUSTOM_SIZE_MAX_EDGE}px，宽高都必须是 16 的倍数，"
        f"长边与短边之比 <= 3:1，总像素必须在 {CUSTOM_SIZE_MIN_PIXELS} 到 "
        f"{CUSTOM_SIZE_MAX_PIXELS} 之间。"
    )


def _build_tool_base_properties() -> dict[str, Any]:
    return {
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
    }


def _build_forum_property() -> dict[str, Any]:
    return {
        "type": "boolean",
        "description": (
            "是否用于论坛发帖。当用户明确表示要将生成的图片发到论坛/AstrBook时设置为true。"
            "设置为true时，工具会等待图片生成完成并返回图片路径，不会自动发送给用户。"
            "你需要使用返回的路径调用 upload_image 上传到论坛图床。"
        ),
        "default": False,
    }


def _build_tool_description(plugin: Any) -> str:
    prefix = (
        "使用 Gemini 模型生成或修改图像。"
        "当用户请求图像生成、绘画、改图、换风格或手办化时调用此函数。"
        "此工具会先在前台短时间等待结果，若快速完成则直接返回图片；"
        "若超出等待时间则自动转为后台生成，完成后自动发送给用户。"
        "判断逻辑：用户说'改成'、'变成'、'基于'、'修改'、'改图'等词时，"
        "设置 use_reference_images=true；用户说'根据我'、'我的头像'或@某人时，"
        "设置 use_reference_images=true 和 include_user_avatar=true。"
    )
    if _is_openai_images_custom_size_mode(plugin):
        return (
            prefix + "当前供应商为 OpenAI Images 且已启用自定义尺寸模式。"
            "如果用户指定尺寸，设置 size，且不要传 resolution 或 aspect_ratio。"
            f"size {_custom_size_constraints_text()}"
            "【重要】当用户明确表示要将生成的图片发到论坛/AstrBook时，设置 for_forum=true。"
            "此时工具会等待图片生成完成后返回图片路径，你需要使用 upload_image 工具将图片上传到论坛图床获取URL，"
            "然后在发帖或回复时使用 Markdown 格式 ![描述](URL) 插入图片。"
        )

    return (
        prefix
        + "用户指定分辨率时设置 resolution（仅限 1K/2K/4K 大写）；"
        + "用户指定比例时设置 aspect_ratio（仅限 1:1/16:9/4:3/3:2/9:16/4:5/5:4/21:9/3:4/2:3）。"
        + "【重要】当用户明确表示要将生成的图片发到论坛/AstrBook时，设置 for_forum=true。"
        + "此时工具会等待图片生成完成后返回图片路径，你需要使用 upload_image 工具将图片上传到论坛图床获取URL，"
        + "然后在发帖或回复时使用 Markdown 格式 ![描述](URL) 插入图片。"
    )


def _build_tool_parameters(plugin: Any) -> dict[str, Any]:
    properties = _build_tool_base_properties()

    if _is_openai_images_custom_size_mode(plugin):
        settings = _get_openai_images_settings(plugin)
        configured_size = (
            str(settings.get("custom_size") or "").strip() or CUSTOM_SIZE_DEFAULT
        )
        properties["size"] = {
            "type": "string",
            "description": (
                "OpenAI Images 自定义尺寸。"
                "如用户未指定尺寸可省略，省略时使用当前插件配置默认值 "
                f"{configured_size}。{_custom_size_constraints_text()}"
            ),
        }
    else:
        properties["resolution"] = {
            "type": "string",
            "description": (
                "图像分辨率，可选参数，留空使用默认配置。"
                "仅支持：1K、2K、4K（必须大写英文）"
            ),
            "enum": list(RESOLUTION_OPTIONS),
        }
        properties["aspect_ratio"] = {
            "type": "string",
            "description": (
                "图像长宽比，可选参数，留空使用默认配置。"
                "仅支持：1:1、16:9、4:3、3:2、9:16、4:5、5:4、21:9、3:4、2:3"
            ),
            "enum": list(ASPECT_RATIO_OPTIONS),
        }

    properties["for_forum"] = _build_forum_property()
    return {
        "type": "object",
        "properties": properties,
        "required": ["prompt"],
    }


def _build_tool_retry_message(message: str, *, custom_size_mode: bool) -> str:
    if custom_size_mode:
        return (
            f"❌ 参数错误：{message}\n"
            "当前工具处于 OpenAI Images 自定义尺寸模式，只能传 size，不要传 resolution 或 aspect_ratio。\n"
            f"size {_custom_size_constraints_text()}\n"
            "请修正参数后重新调用 gemini_image_generation 工具。"
        )

    return (
        f"❌ 参数错误：{message}\n"
        f"resolution 仅支持：{'/'.join(RESOLUTION_OPTIONS)}；"
        f"aspect_ratio 仅支持：{'/'.join(ASPECT_RATIO_OPTIONS)}。\n"
        "请修正参数后重新调用 gemini_image_generation 工具；如果用户没有指定这些参数，可以直接省略。"
    )


def _build_config_size_notice(configured_size: str) -> str:
    return (
        "【提醒】本次未显式传入 size，"
        f"已使用插件配置中的 openai_images.custom_size={configured_size}。"
        "如果后续需要指定尺寸，请在下次调用工具时显式传入合法的 size。"
    )


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
        if re.fullmatch(r"\d+[xX]\d+", resolution):
            parts.append(f"尺寸 {resolution}")
        else:
            parts.append(f"分辨率 {resolution}")
    if aspect_ratio:
        parts.append(f"比例 {aspect_ratio}")
    return f"（{', '.join(parts)}）" if parts else ""


_MIME_TYPE_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".heif": "image/heif",
}


def _image_to_base64_content(image_ref: str) -> mcp.types.ImageContent | None:
    """将图片引用（本地路径、data URI 或 base64 字符串）转换为 ImageContent。"""
    if not image_ref:
        return None

    # data URI
    if image_ref.startswith("data:image/") and ";base64," in image_ref:
        try:
            header, b64_data = image_ref.split(";base64,", 1)
            mime_type = header.replace("data:", "")
            return mcp.types.ImageContent(
                type="image", data=b64_data, mimeType=mime_type
            )
        except Exception:
            return None

    # 本地文件路径
    fs_candidate = image_ref
    if image_ref.startswith("file:///"):
        fs_candidate = image_ref[8:]

    if os.path.exists(fs_candidate):
        try:
            ext = os.path.splitext(fs_candidate)[1].lower()
            mime_type = _MIME_TYPE_MAP.get(ext, "image/png")
            b64_data = encode_file_to_base64(fs_candidate)
            return mcp.types.ImageContent(
                type="image", data=b64_data, mimeType=mime_type
            )
        except Exception as e:
            logger.warning(f"[CallToolResult] 编码图片失败: {e}")
            return None

    # HTTP URL 等无法直接转换的引用
    return None


async def _build_call_tool_result(
    image_urls: list[str] | None,
    image_paths: list[str] | None,
    text_content: str | None,
    message_sender: Any,
    api_client: Any | None = None,
    llm_notice: str | None = None,
) -> mcp.types.CallToolResult:
    """将图像生成结果转换为 AstrBot 官方 CallToolResult 格式（含 ImageContent）。

    当 api_client 配置了代理时，远程 URL 图片会通过代理下载后内联返回，
    避免 AstrBot Core 无法访问需要代理的图片导致缓存失败。
    """
    contents: list[mcp.types.TextContent | mcp.types.ImageContent] = []

    # 合并去重图片
    available_images = message_sender.merge_available_images(image_urls, image_paths)

    # 判断是否需要走代理下载远程 URL
    has_proxy = bool(api_client and getattr(api_client, "proxy", None))

    # 处理图片 → ImageContent，并用 data 哈希做最终去重
    import hashlib

    seen_data_hashes: set[str] = set()
    url_only_images: list[str] = []
    for img in available_images:
        img_content = _image_to_base64_content(img)
        if img_content:
            data_hash = hashlib.sha256(img_content.data.encode("ascii")).hexdigest()
            if data_hash in seen_data_hashes:
                logger.debug(f"[CallToolResult 去重] 跳过内容相同的图片: {img[:80]}")
                continue
            seen_data_hashes.add(data_hash)
            contents.append(img_content)
        elif img.startswith(("http://", "https://")):
            url_only_images.append(img)

    # 对远程 URL：如果配有代理则通过代理下载后内联，否则文本告知模型
    remaining_urls: list[str] = []
    if url_only_images and has_proxy:
        logger.debug(
            f"[CallToolResult] 检测到代理，将下载 {len(url_only_images)} 张远程图片"
        )
        try:
            session = await api_client._get_session()
            for url in url_only_images:
                try:
                    _, local_path = await api_client._download_image(
                        url, session, use_cache=False
                    )
                    if local_path:
                        img_content = _image_to_base64_content(local_path)
                        if img_content:
                            data_hash = hashlib.sha256(
                                img_content.data.encode("ascii")
                            ).hexdigest()
                            if data_hash not in seen_data_hashes:
                                seen_data_hashes.add(data_hash)
                                contents.append(img_content)
                                continue
                            else:
                                logger.debug(
                                    f"[CallToolResult 去重] 下载后内容相同: {url[:80]}"
                                )
                                continue
                except Exception as e:
                    logger.warning(f"[CallToolResult] 代理下载图片失败: {url[:80]} {e}")
                # 下载失败的保留为 URL
                remaining_urls.append(url)
        except Exception as e:
            logger.warning(f"[CallToolResult] 获取下载会话失败: {e}")
            remaining_urls = url_only_images
    else:
        remaining_urls = url_only_images

    if remaining_urls:
        url_lines = [
            f"Image URL ({i + 1}): {url}" for i, url in enumerate(remaining_urls)
        ]
        contents.append(
            mcp.types.TextContent(
                type="text",
                text=(
                    "The following images are available as remote URLs only.\n"
                    + "\n".join(url_lines)
                    + "\nUse send_message_to_user with type='image' and "
                    "url=<image_url> to send them to the user."
                ),
            )
        )

    # 处理文本
    prepared_text = message_sender.prepare_text_content(text_content, available_images)
    text_parts: list[str] = []
    if prepared_text:
        text_parts.append(prepared_text)
    if llm_notice:
        text_parts.append(llm_notice)
    # thought signature 只能留在 Provider 协议层，绝不能拼进 Tool 文本结果。
    # 否则下游 Runner 会把这类超大 opaque 数据重新塞回上下文。
    if text_parts:
        contents.append(mcp.types.TextContent(type="text", text="\n".join(text_parts)))

    if not contents:
        contents.append(
            mcp.types.TextContent(
                type="text",
                text="图片已生成但未能获取到有效的图片数据。",
            )
        )

    return mcp.types.CallToolResult(content=contents)


def _build_background_start_notice(
    ref_count: int,
    avatar_count: int,
    resolution: str | None,
    aspect_ratio: str | None,
    llm_notice: str | None = None,
) -> str:
    ref_info = _build_reference_info(ref_count, avatar_count)
    param_info = _build_param_info(resolution, aspect_ratio)
    message = (
        f"[图像生成任务已启动]{ref_info}{param_info}\n"
        "图片正在后台生成中，通常需要 10-30 秒，高质量生成可能长达几百秒，生成完成后会自动发送给用户。\n"
        "请用你维持原有的人设告诉用户：图片正在生成，请稍等片刻，完成后会自动发送。"
    )
    if llm_notice:
        message += f"\n{llm_notice}"
    return message


def _build_background_fallback_notice(
    ref_count: int,
    avatar_count: int,
    resolution: str | None,
    aspect_ratio: str | None,
    waited_seconds: int,
    llm_notice: str | None = None,
) -> str:
    ref_info = _build_reference_info(ref_count, avatar_count)
    param_info = _build_param_info(resolution, aspect_ratio)
    message = (
        f"[图像生成任务已转入后台]{ref_info}{param_info}\n"
        f"前台等待 {waited_seconds} 秒后仍未完成，已切换为后台继续生成。\n"
        "图片生成完成后会自动发送给用户。\n"
        "请用你维持原有的人设告诉用户：图片正在生成，请稍等片刻，完成后会自动发送。"
    )
    if llm_notice:
        message += f"\n{llm_notice}"
    return message


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
    is_tool_call: bool = False,
) -> asyncio.Task:
    return asyncio.create_task(
        plugin._generate_image_core_internal(
            event=event,
            prompt=prompt,
            reference_images=reference_images,
            avatar_reference=avatar_reference,
            override_resolution=override_resolution,
            override_aspect_ratio=override_aspect_ratio,
            is_tool_call=is_tool_call,
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

        # 代理全链路：后台发送前，将需要代理的远程 URL 下载为本地文件
        # 避免 NapCat 等平台无法访问代理依赖的 URL
        api_client = getattr(plugin, "api_client", None)
        has_proxy = bool(api_client and getattr(api_client, "proxy", None))
        if has_proxy and image_urls:
            downloaded_paths: list[str] = []
            remaining_urls: list[str] = []
            try:
                session = await api_client._get_session()
                for url in image_urls:
                    if not url.startswith(("http://", "https://")):
                        remaining_urls.append(url)
                        continue
                    try:
                        _, local_path = await api_client._download_image(
                            url, session, use_cache=False
                        )
                        if local_path:
                            downloaded_paths.append(local_path)
                        else:
                            remaining_urls.append(url)
                    except Exception as e:
                        logger.warning(
                            f"[{scene}] 代理下载图片失败，保留原 URL: {url[:80]} {e}"
                        )
                        remaining_urls.append(url)
            except Exception as e:
                logger.warning(f"[{scene}] 获取代理下载会话失败: {e}")
                remaining_urls = list(image_urls)
            image_urls = remaining_urls
            image_paths = list(image_paths or []) + downloaded_paths

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
        try:
            await plugin.message_sender.send_results_with_stream_retry(
                event=event,
                image_urls=image_urls,
                image_paths=image_paths,
                text_content=content_text,
                thought_signature=thought_signature,
                scene=scene,
                force_text_response=force_text_response,
                text_content_prepared=True,
            )
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
    description: str = Field(default_factory=str)
    parameters: dict = Field(default_factory=dict)

    # 插件实例引用（在创建时设置）
    plugin: Any = Field(default=None, repr=False)

    def refresh_from_plugin(self) -> None:
        self.description = _build_tool_description(self.plugin)
        self.parameters = _build_tool_parameters(self.plugin)

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        """
        执行图像生成工具（触发器模式）

        Foreground-first hybrid mode for normal chats.
        When for_forum=True, the tool waits synchronously and returns image paths.
        """
        self.refresh_from_plugin()

        prompt = kwargs.get("prompt") or ""
        if not prompt.strip():
            return "❌ 缺少必填参数：图像描述不能为空"

        use_reference_images = kwargs.get("use_reference_images", False)
        include_user_avatar = kwargs.get("include_user_avatar", False)
        size = kwargs.get("size") or None
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
        config_value_notice: str | None = None

        custom_size_mode = _is_openai_images_custom_size_mode(plugin)
        if custom_size_mode:
            if resolution is not None:
                return _build_tool_retry_message(
                    "当前模式不支持 resolution 参数，请改用 size。",
                    custom_size_mode=True,
                )
            if aspect_ratio is not None:
                return _build_tool_retry_message(
                    "当前模式不支持 aspect_ratio 参数，请改用 size。",
                    custom_size_mode=True,
                )
            if size is not None and str(size).strip():
                try:
                    resolution = validate_custom_size(size, field_name="size")
                except ValueError as exc:
                    return _build_tool_retry_message(
                        str(exc),
                        custom_size_mode=True,
                    )
            else:
                settings = _get_openai_images_settings(plugin)
                try:
                    resolution = validate_custom_size(
                        settings.get("custom_size"),
                        field_name="openai_images.custom_size",
                    )
                except ValueError as exc:
                    return f"❌ 插件配置错误：{exc}"
                config_value_notice = _build_config_size_notice(resolution)
                logger.warning(
                    "[工具调用] OpenAI Images 自定义尺寸模式未显式提供 size，"
                    f"已使用插件配置中的 openai_images.custom_size={resolution}"
                )
            aspect_ratio = None
        else:
            if size is not None and str(size).strip():
                return _build_tool_retry_message(
                    "当前模式不支持 size 参数，请使用 resolution 和 aspect_ratio，或直接省略。",
                    custom_size_mode=False,
                )

            if resolution is not None:
                resolution = str(resolution).strip().upper()
                if resolution not in VALID_RESOLUTIONS:
                    return _build_tool_retry_message(
                        f"resolution 仅支持 {'/'.join(RESOLUTION_OPTIONS)}，当前值: {kwargs.get('resolution')!r}",
                        custom_size_mode=False,
                    )
            else:
                resolution = None

            if aspect_ratio is not None:
                aspect_ratio = str(aspect_ratio).strip()
                if aspect_ratio not in VALID_ASPECT_RATIOS:
                    return _build_tool_retry_message(
                        f"aspect_ratio 仅支持 {'/'.join(ASPECT_RATIO_OPTIONS)}，当前值: {kwargs.get('aspect_ratio')!r}",
                        custom_size_mode=False,
                    )
            else:
                aspect_ratio = None

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
            f"尺寸/分辨率={resolution} 比例={aspect_ratio} 发帖模式={for_forum}"
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

                if config_value_notice:
                    result_lines.extend(["", config_value_notice])

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
                    llm_notice=config_value_notice,
                )

            logger.debug(f"[前台等待] 最多等待 {foreground_wait_seconds} 秒。")
            try:
                success, result_data = await asyncio.wait_for(
                    asyncio.shield(generation_task),
                    timeout=foreground_wait_seconds,
                )
            except asyncio.TimeoutError:
                raise  # 让外层 except asyncio.TimeoutError 处理
            if success and isinstance(result_data, tuple):
                image_urls, image_paths, text_content, thought_signature = result_data
                img_count = len(
                    plugin.message_sender.merge_available_images(
                        image_urls, image_paths
                    )
                )
                logger.info(
                    f"[前台等待] 生成完成，通过 CallToolResult 返回 {img_count} 张图片"
                )
                # 构建 CallToolResult（含代理下载），使用剩余前台预算做超时保护
                try:
                    result = await asyncio.wait_for(
                        _build_call_tool_result(
                            image_urls=image_urls,
                            image_paths=image_paths,
                            text_content=text_content,
                            message_sender=plugin.message_sender,
                            api_client=plugin.api_client,
                            llm_notice=config_value_notice,
                        ),
                        timeout=30,  # 下载最多给 30 秒
                    )
                    return result
                except asyncio.TimeoutError:
                    logger.warning(
                        "[前台等待] 构建 CallToolResult 超时（代理下载慢），转后台直发"
                    )
                    # 生成已完成，创建一个立即完成的 task 包装结果，走后台直发
                    cleanup_now = False

                    async def _already_done():
                        return (True, result_data)

                    done_task = asyncio.create_task(_already_done())
                    _schedule_generation_delivery(
                        plugin=plugin,
                        event=event,
                        generation_task=done_task,
                        scene="后台任务(代理下载超时回退)",
                    )
                    result_message = (
                        "[图片生成已完成，正在通过代理下载并发送]\n"
                        "由于代理下载耗时较长，已转为后台发送，完成后会自动发给用户。"
                    )
                    if config_value_notice:
                        result_message += f"\n{config_value_notice}"
                    return result_message
            else:
                error_msg = (
                    result_data if isinstance(result_data, str) else "❌ 图像生成失败"
                )
                return error_msg
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
                llm_notice=config_value_notice,
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
        # thought signature 只允许作为内部调试/协议层元数据存在，不能返回给
        # Tool 调用方，更不能让 Agent 把它当成普通文本继续消费。
        log_thought_signature_debug(thought_signature, scene="Tool结果已丢弃")

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
