"""
LLM 工具定义模块

将图像生成 Tool 拆分为独立类

"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from typing import TYPE_CHECKING, Any

import mcp.types
from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass

from .batch_generation import run_batch_job
from .generation_call import invoke_generation_core
from .openai_image_size import (
    CUSTOM_SIZE_DEFAULT,
    validate_custom_size,
)
from .provider_capabilities import (
    SUPPORTED_ASPECT_RATIOS,
    routing_description,
    routing_mode,
    select_candidates,
)
from .provider_settings import (
    candidate_tool_profile,
    first_provider_tool_profile,
)
from .provider_settings import (
    first_provider_candidate as _first_candidate_from_config,
)
from .thought_signature import log_thought_signature_debug
from .tl_utils import encode_file_to_base64, format_error_message
from .tool_path_guard import filter_reference_paths

if TYPE_CHECKING:
    from ..main import GeminiImageGenerationPlugin


# 参数枚举常量（工具定义和验证共用）
RESOLUTION_OPTIONS = ("1K", "2K", "4K")
ASPECT_RATIO_OPTIONS = SUPPORTED_ASPECT_RATIOS
VALID_RESOLUTIONS = set(RESOLUTION_OPTIONS)
VALID_ASPECT_RATIOS = set(ASPECT_RATIO_OPTIONS)


def _first_provider_candidate(
    plugin: Any,
    provider: Any = None,
    model: Any = None,
) -> Any | None:
    candidates = list(getattr(plugin.cfg, "provider_candidates", []) or [])
    selected = select_candidates(candidates, provider=provider, model=model)
    if selected:
        return selected[0]
    if provider or model:
        return None
    return _first_candidate_from_config(plugin)


def _get_tool_profile_settings(plugin: Any, candidate: Any = None) -> dict[str, Any]:
    profile = (
        candidate_tool_profile(plugin, candidate)
        if candidate is not None
        else first_provider_tool_profile(plugin)
    )
    settings = profile.get("settings", {})
    return settings if isinstance(settings, dict) else {}


def _is_custom_size_tool_mode(plugin: Any, candidate: Any = None) -> bool:
    profile = (
        candidate_tool_profile(plugin, candidate)
        if candidate is not None
        else first_provider_tool_profile(plugin)
    )
    return bool(profile.get("custom_size_mode"))


def _build_tool_base_properties() -> dict[str, Any]:
    return {
        "prompt": {
            "type": "string",
            "description": "单图模式下必填：图像生成或修改的详细描述",
        },
        "provider": {
            "type": "string",
            "description": "可选，限制在指定供应商内部生成",
        },
        "model": {
            "type": "string",
            "description": "可选，指定原始模型名或配置的模型别名",
        },
        "negative_prompt": {
            "type": "string",
            "description": "可选负面提示词；仅支持该参数的候选可参与生成",
        },
        "watermark": {
            "type": "boolean",
            "description": "可选水印开关；省略时使用供应商配置",
        },
        "quality": {
            "type": "string",
            "description": "可选质量档位；合法值通过供应商模型查询工具获取",
        },
        "preserve_reference_image_size": {
            "type": "boolean",
            "description": "有参考图时是否保留参考图尺寸；省略时使用插件配置",
        },
        "use_reference_images": {
            "type": "boolean",
            "description": (
                "是否使用上下文中的参考图片。"
                "当当前请求意图是修改、变换或基于现有图片时设置为true"
            ),
            "default": False,
        },
        "include_user_avatar": {
            "type": "boolean",
            "description": (
                "是否包含用户头像作为参考图像。"
                "当当前请求提到'根据我'、'我的头像'或@某人时设置为true"
            ),
            "default": False,
        },
        "reference_image_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "可选。作为参考图的本地图片路径列表。"
                "默认白名单模式下，路径必须位于插件配置的允许目录内"
                "（默认覆盖各系统 AstrBot 数据目录，如 ~/.astrbot/data、/opt/astrbot/data）。"
                "禁止 .. 穿越。路径不存在、越界、非图片或图片损坏将被静默丢弃并在日志记录。"
                "典型用途：复用上一次工具调用缓存在 data/temp/tool_images/ 的图片。"
            ),
            "default": [],
        },
    }


def _build_forum_property() -> dict[str, Any]:
    return {
        "type": "boolean",
        "description": (
            "是否用于论坛发帖。当当前请求明确要求将生成的图片发到论坛/AstrBook时设置为true。"
            "设置为true时，工具会等待图片生成完成并返回图片路径，不会自动发送给用户。"
            "你需要使用返回的路径调用 upload_image 上传到论坛图床。"
        ),
        "default": False,
    }


def _build_tool_description(plugin: Any) -> str:
    return (
        "使用 Gemini 模型生成或修改图像。"
        "当当前对话需要图像生成、绘画、改图、换风格或手办化时调用此函数。"
        "provider 和 model 均可省略；都省略时按配置轮询，provider 单独指定时仅在该供应商内重试，"
        "model 单独指定时按原始模型名或别名跨供应商轮询，两者同时指定时只使用交集候选。"
        "negative_prompt、watermark、quality 只会路由到明确支持该参数的候选；"
        "可先调用 gemini_image_provider_models 查询可用模型和参数。"
        "此工具会先在前台短时间等待结果，若快速完成则直接返回图片；"
        "若超出等待时间则返回后台任务号并继续生成，完成后自动发送给用户；"
        "可调用 gemini_image_task_status 查询任务状态。"
        "需要批量生成时传 batch_tasks；每项必须包含唯一 name、完整 prompt、image_count，"
        "并至少指定 provider 或 model。批量任务固定进入后台。"
        "判断逻辑：对话中出现'改成'、'变成'、'基于'、'修改'、'改图'等词时，"
        "设置 use_reference_images=true；当前请求提到'根据我'、'我的头像'或@某人时，"
        "设置 use_reference_images=true 和 include_user_avatar=true。"
        "需要控制输出分辨率或长宽比时设置 resolution / aspect_ratio；"
        "各渠道与模型支持的具体取值不要猜测，先调用 gemini_image_provider_models 查询，"
        "不支持的比例会被渠道忽略并记录日志。"
        "【重要】当当前请求明确要求将生成的图片发到论坛/AstrBook时，设置 for_forum=true。"
        "此时工具会等待图片生成完成后返回图片路径，你需要使用 upload_image 工具将图片上传到论坛图床获取URL，"
        "然后在发帖或回复时使用 Markdown 格式 ![描述](URL) 插入图片。"
        "【本地路径参考图】如需基于上一轮工具产出的本地图片改图（如 data/temp/tool_images/ 下缓存图），"
        "用 reference_image_paths 传路径；默认仅允许插件配置的允许目录内文件，禁止 .. 穿越。"
    )


def _build_tool_parameters(plugin: Any) -> dict[str, Any]:
    properties = _build_tool_base_properties()
    properties["resolution"] = {
        "type": "string",
        "description": (
            "输出分辨率，可选；不传则使用插件配置或供应商默认值。具体取值先用 gemini_image_provider_models 查询。"
        ),
        "enum": list(RESOLUTION_OPTIONS),
    }
    properties["aspect_ratio"] = {
        "type": "string",
        "description": (
            "输出长宽比，可选；不传则使用插件配置或供应商默认值。"
            "各模型支持的具体取值先用 gemini_image_provider_models 查询，不支持的比例由渠道忽略。"
        ),
        "enum": list(ASPECT_RATIO_OPTIONS),
    }
    properties["batch_tasks"] = {
        "type": "array",
        "description": "可选。传入后按命名任务直接在后台批量生成，不再使用顶层 prompt。",
        "items": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "description": "唯一任务名称",
                },
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "description": "完整且可独立执行的生图提示词",
                },
                "image_count": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "该提示词需要生成的目标图片数量",
                },
                "provider": {
                    "type": "string",
                    "description": "与 model 至少填写一个",
                },
                "model": {
                    "type": "string",
                    "description": "原始模型名或别名；与 provider 至少填写一个",
                },
                "negative_prompt": {"type": "string"},
                "watermark": {"type": "boolean"},
                "quality": {"type": "string"},
                "use_reference_images": {"type": "boolean", "default": False},
                "include_user_avatar": {"type": "boolean", "default": False},
                "reference_image_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "resolution": {
                    "type": "string",
                    "enum": list(RESOLUTION_OPTIONS),
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": list(ASPECT_RATIO_OPTIONS),
                },
                "preserve_reference_image_size": {"type": "boolean"},
            },
            "required": ["name", "prompt", "image_count"],
        },
    }
    properties["for_forum"] = _build_forum_property()
    return {
        "type": "object",
        "properties": properties,
        "required": [],
    }


def _normalize_tool_resolution(value: Any) -> tuple[str | None, bool]:
    if value is None or not str(value).strip():
        return None, False
    resolution = str(value).strip().upper()
    if resolution not in VALID_RESOLUTIONS:
        logger.warning(f"[工具调用] resolution={value!r} 非法，已退回默认配置")
        return None, True
    return resolution, False


def _normalize_tool_aspect_ratio(value: Any) -> tuple[str | None, bool]:
    if value is None or not str(value).strip():
        return None, False
    aspect_ratio = str(value).strip()
    if aspect_ratio not in VALID_ASPECT_RATIOS:
        logger.warning(f"[工具调用] aspect_ratio={value!r} 非法，已退回默认配置")
        return None, True
    return aspect_ratio, False


def _resolve_tool_size_params(
    plugin: Any,
    *,
    size: Any = None,
    resolution: Any = None,
    aspect_ratio: Any = None,
    provider: Any = None,
    model: Any = None,
) -> tuple[str | None, str | None, str | None]:
    normalized_resolution, invalid_resolution = _normalize_tool_resolution(resolution)
    normalized_aspect_ratio, invalid_aspect_ratio = _normalize_tool_aspect_ratio(
        aspect_ratio
    )

    candidate = _first_provider_candidate(plugin, provider, model)
    if (provider or model) and candidate is None:
        return normalized_resolution, normalized_aspect_ratio, None
    if not _is_custom_size_tool_mode(plugin, candidate):
        if size is not None and str(size).strip():
            logger.warning("[工具调用] 当前模式忽略 legacy size 参数")
        return normalized_resolution, normalized_aspect_ratio, None

    if size is not None and str(size).strip():
        try:
            return validate_custom_size(size, field_name="size"), None, None
        except ValueError as exc:
            logger.warning(f"[工具调用] size={size!r} 非法，已退回默认尺寸: {exc}")

    settings = _get_tool_profile_settings(plugin, candidate)
    if invalid_resolution or invalid_aspect_ratio:
        try:
            default_size = validate_custom_size(
                settings.get("custom_size"),
                field_name="openai_images.custom_size",
            )
        except ValueError as exc:
            default_size = CUSTOM_SIZE_DEFAULT
            logger.warning(
                "[工具调用] openai_images.custom_size 非法，"
                f"已退回默认尺寸 {default_size}: {exc}"
            )
        return default_size, None, None

    if normalized_resolution or normalized_aspect_ratio:
        return normalized_resolution, normalized_aspect_ratio, None

    try:
        default_size = validate_custom_size(
            settings.get("custom_size"),
            field_name="openai_images.custom_size",
        )
    except ValueError as exc:
        default_size = CUSTOM_SIZE_DEFAULT
        logger.warning(
            "[工具调用] openai_images.custom_size 非法，"
            f"已退回默认尺寸 {default_size}: {exc}"
        )
    return default_size, None, None


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
    requested_provider: str | None = None,
    requested_model: str | None = None,
    negative_prompt: str | None = None,
    watermark: bool | None = None,
    quality: str | None = None,
    image_count: int = 1,
    suppress_resolution: bool = False,
) -> asyncio.Task:
    async def _run_generation():
        success, result_data = await invoke_generation_core(
            plugin,
            event=event,
            prompt=prompt,
            reference_images=reference_images,
            avatar_reference=avatar_reference,
            override_resolution=override_resolution,
            override_aspect_ratio=override_aspect_ratio,
            is_tool_call=is_tool_call,
            requested_provider=requested_provider,
            requested_model=requested_model,
            negative_prompt=negative_prompt,
            watermark=watermark,
            quality=quality,
            image_count=image_count,
            suppress_resolution=suppress_resolution,
        )
        return success, result_data, plugin.image_generator.get_request_stats()

    return asyncio.create_task(_run_generation())


async def _dispatch_generation_result(
    plugin: Any,
    event: Any,
    success: bool,
    result_data: Any,
    *,
    scene: str,
    fallback_text: str | None = None,
    force_text_response: bool = False,
) -> bool:
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
            return False
        return True

    error_msg = result_data if isinstance(result_data, str) else "❌ 图像生成失败"
    try:
        await event.send(event.plain_result(error_msg))
    except Exception as exc:
        logger.warning(f"[{scene}] 发送错误消息失败: {exc}")
        return False
    return True


async def _await_generation_task_and_send(
    plugin: Any,
    event: Any,
    generation_task: asyncio.Task,
    *,
    scene: str,
    task_id: str | None = None,
) -> None:
    try:
        success, result_data, stats = await generation_task
        delivered = await _dispatch_generation_result(
            plugin=plugin,
            event=event,
            success=success,
            result_data=result_data,
            scene=scene,
        )
        if task_id:
            item = {
                "name": "single",
                "success": bool(success),
                "provider": stats.get("successful_provider"),
                "model": stats.get("successful_model"),
                "alias": stats.get("successful_model_alias"),
                "candidate_id": stats.get("successful_candidate_id"),
                "delivery_success": delivered,
            }
            if success and delivered:
                final_status = "succeeded"
                final_message = "图片生成已完成并发送"
            elif success:
                final_status = "partial_success"
                final_message = "图片生成已完成，但自动发送失败"
            else:
                final_status = "failed"
                final_message = str(result_data)
            await plugin.background_task_manager.update(
                task_id,
                status=final_status,
                message=final_message,
                completed_items=1,
                succeeded_items=1 if success else 0,
                failed_items=0 if success else 1,
                items=[item],
            )
    except Exception as exc:
        logger.error(f"[{scene}] 后台图像生成异常: {exc}", exc_info=True)
        try:
            await event.send(event.plain_result(format_error_message(exc)))
        except Exception as send_error:
            logger.warning(f"[{scene}] 发送异常消息失败: {send_error}")
        if task_id:
            await plugin.background_task_manager.update(
                task_id,
                status="failed",
                message=f"后台生成异常: {exc}",
                completed_items=1,
                failed_items=1,
            )


def _schedule_generation_delivery(
    plugin: Any,
    event: Any,
    generation_task: asyncio.Task,
    *,
    scene: str,
    task_id: str | None = None,
) -> asyncio.Task:
    coroutine = _await_generation_task_and_send(
        plugin=plugin,
        event=event,
        generation_task=generation_task,
        scene=scene,
        task_id=task_id,
    )
    sender_task = (
        plugin.background_task_manager.attach(task_id, coroutine)
        if task_id
        else asyncio.create_task(coroutine)
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


def _event_session_id(event: Any) -> str:
    return str(getattr(event, "unified_msg_origin", None) or "unknown")


def _background_json(task_id: str, mode: str, message: str) -> str:
    return json.dumps(
        {
            "task_id": task_id,
            "status": "running",
            "routing_mode": mode,
            "message": message,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _runtime_values(
    *,
    negative_prompt: str | None,
    watermark: bool | None,
    quality: str | None,
) -> tuple[set[str], dict[str, Any]]:
    required: set[str] = set()
    values: dict[str, Any] = {}
    if negative_prompt not in (None, ""):
        required.add("negative_prompt")
        values["negative_prompt"] = negative_prompt
    if watermark is not None:
        required.add("watermark")
        values["watermark"] = watermark
    if quality not in (None, ""):
        required.add("quality")
        values["quality"] = quality
    return required, values


def _matching_candidates(
    plugin: Any,
    *,
    provider: str | None,
    model: str | None,
    has_reference_images: bool,
    negative_prompt: str | None,
    watermark: bool | None,
    quality: str | None,
) -> list[Any]:
    required, values = _runtime_values(
        negative_prompt=negative_prompt,
        watermark=watermark,
        quality=quality,
    )
    candidates = list(getattr(plugin.cfg, "provider_candidates", []) or [])
    if not candidates:
        return [None]
    return select_candidates(
        candidates,
        provider=provider,
        model=model,
        has_reference_images=has_reference_images,
        required_parameters=required,
        request_values=values,
    )


def _normalize_reference_paths(plugin: Any, values: Any) -> list[str]:
    raw_paths = values if isinstance(values, list) else [values] if values else []
    cfg = getattr(plugin, "cfg", None)
    path_mode = getattr(cfg, "llm_tool_reference_path_mode", "whitelist")
    allowed_dirs = list(getattr(cfg, "llm_tool_reference_allowed_dirs", []) or [])
    accepted, rejected = filter_reference_paths(
        raw_paths,
        allowed_dirs=allowed_dirs,
        global_mode=(path_mode == "global"),
        log_fn=logger.debug,
    )
    if rejected:
        logger.warning(
            f"[工具调用] reference_image_paths 被拒 {len(rejected)} 条 "
            f"(mode={path_mode})"
        )
    return accepted


async def _prepare_batch_tasks(
    plugin: Any,
    event: Any,
    raw_items: Any,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    if not isinstance(raw_items, list) or not raw_items:
        return None, "batch_tasks 必须是非空数组"
    if len(raw_items) > int(plugin.cfg.batch_max_tasks):
        return None, f"batch_tasks 最多允许 {plugin.cfg.batch_max_tasks} 个命名任务"
    if not all(isinstance(item, dict) for item in raw_items):
        return None, "batch_tasks 中每一项都必须是对象"

    include_any_avatar = any(
        bool(item.get("include_user_avatar")) for item in raw_items
    )
    event_refs, event_avatars = await plugin._fetch_images_from_event(
        event,
        include_at_avatars=include_any_avatar,
    )
    prepared: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, raw_item in enumerate(raw_items, 1):
        name = str(raw_item.get("name") or "").strip()
        prompt = str(raw_item.get("prompt") or "").strip()
        provider = str(raw_item.get("provider") or "").strip() or None
        model = str(raw_item.get("model") or "").strip() or None
        if not name:
            return None, f"第 {index} 个批量任务缺少 name"
        if name in names:
            return None, f"批量任务名称重复：{name}"
        names.add(name)
        if not prompt:
            return None, f"批量任务 {name} 缺少 prompt"
        if not provider and not model:
            return None, f"批量任务 {name} 必须至少指定 provider 或 model"
        try:
            image_count = int(raw_item.get("image_count"))
        except (TypeError, ValueError):
            return None, f"批量任务 {name} 的 image_count 必须是整数"
        if not 1 <= image_count <= int(plugin.cfg.batch_max_images_per_task):
            return None, (
                f"批量任务 {name} 的 image_count 必须在 1-"
                f"{plugin.cfg.batch_max_images_per_task} 之间"
            )

        negative_prompt = (
            str(raw_item.get("negative_prompt") or "").strip()
            if "negative_prompt" in raw_item
            else None
        )
        watermark = bool(raw_item.get("watermark")) if "watermark" in raw_item else None
        quality = (
            str(raw_item.get("quality") or "").strip()
            if "quality" in raw_item
            else None
        )
        size_value = raw_item.get("size")
        resolution_value = raw_item.get("resolution")
        aspect_value = raw_item.get("aspect_ratio")
        resolution, aspect_ratio, _notice = _resolve_tool_size_params(
            plugin,
            size=size_value,
            resolution=resolution_value,
            aspect_ratio=aspect_value,
            provider=provider,
            model=model,
        )

        reference_images = (
            list(event_refs) if raw_item.get("use_reference_images") else []
        )
        avatar_reference = (
            list(event_avatars) if raw_item.get("include_user_avatar") else []
        )
        reference_images.extend(
            _normalize_reference_paths(plugin, raw_item.get("reference_image_paths"))
        )
        has_references = bool(reference_images or avatar_reference)
        preserve_size = (
            bool(raw_item.get("preserve_reference_image_size"))
            if "preserve_reference_image_size" in raw_item
            else bool(getattr(plugin.cfg, "preserve_reference_image_size", False))
        )
        explicit_size = any(
            value is not None and str(value).strip()
            for value in (size_value, resolution_value, aspect_value)
        )
        if has_references and preserve_size and explicit_size:
            return None, (
                f"批量任务 {name} 同时指定 preserve_reference_image_size=true "
                "和尺寸参数"
            )
        suppress_resolution = has_references and preserve_size
        if suppress_resolution:
            resolution = None
            aspect_ratio = None

        candidates = _matching_candidates(
            plugin,
            provider=provider,
            model=model,
            has_reference_images=has_references,
            negative_prompt=negative_prompt,
            watermark=watermark,
            quality=quality,
        )
        if not candidates:
            return None, f"批量任务 {name} 没有匹配所选路由和参数能力的候选模型"

        prepared.append(
            {
                "name": name,
                "prompt": prompt,
                "image_count": image_count,
                "provider": provider,
                "model": model,
                "negative_prompt": negative_prompt,
                "watermark": watermark,
                "quality": quality,
                "reference_images": reference_images,
                "avatar_reference": avatar_reference,
                "resolution": resolution,
                "aspect_ratio": aspect_ratio,
                "suppress_resolution": suppress_resolution,
                "routing_mode": routing_mode(provider, model),
            }
        )
    return prepared, None


@dataclass
class GeminiImageGenerationTool(FunctionTool[AstrAgentContext]):
    """
    Gemini 图像生成工具（触发器模式）

    当当前请求需要图像生成、绘画、改图、换风格或手办化时调用此函数。
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
        event = context.context.event
        plugin = self.plugin
        if not plugin:
            return "❌ 工具未正确初始化，缺少插件实例引用"

        raw_batch_tasks = kwargs.get("batch_tasks")
        is_batch = isinstance(raw_batch_tasks, list) and bool(raw_batch_tasks)
        prompt = str(kwargs.get("prompt") or "").strip()
        if not is_batch and not prompt:
            return "❌ 缺少必填参数：单图模式的图像描述不能为空"
        if is_batch and kwargs.get("for_forum"):
            return "❌ batch_tasks 固定进入后台，不能与 for_forum=true 同时使用"

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

        if is_batch:
            prepared_items, error = await _prepare_batch_tasks(
                plugin,
                event,
                raw_batch_tasks,
            )
            if error or not prepared_items:
                return f"❌ 批量任务参数错误：{error or '没有有效任务'}"
            modes = {item["routing_mode"] for item in prepared_items}
            batch_mode = next(iter(modes)) if len(modes) == 1 else "mixed"
            message = routing_description(batch_mode)
            record = await plugin.background_task_manager.create(
                session_id=_event_session_id(event),
                kind="batch",
                routing_mode=batch_mode,
                message=message,
                total_items=len(prepared_items),
            )
            task_id = record["task_id"]
            plugin.background_task_manager.attach(
                task_id,
                run_batch_job(plugin, event, task_id, prepared_items),
            )
            return _background_json(task_id, batch_mode, message)

        use_reference_images = kwargs.get("use_reference_images", False)
        include_user_avatar = kwargs.get("include_user_avatar", False)
        size = kwargs.get("size") or None
        raw_resolution = kwargs.get("resolution") or None
        raw_aspect_ratio = kwargs.get("aspect_ratio") or None
        provider = str(kwargs.get("provider") or "").strip() or None
        model = str(kwargs.get("model") or "").strip() or None
        negative_prompt = (
            str(kwargs.get("negative_prompt") or "").strip()
            if "negative_prompt" in kwargs
            else None
        )
        watermark = bool(kwargs.get("watermark")) if "watermark" in kwargs else None
        quality = (
            str(kwargs.get("quality") or "").strip() if "quality" in kwargs else None
        )
        for_forum = kwargs.get("for_forum", False)

        # 布尔参数已在工具定义中声明为 boolean 类型，直接使用
        include_avatar = bool(include_user_avatar)
        include_ref_images = bool(use_reference_images)
        resolution, aspect_ratio, config_value_notice = _resolve_tool_size_params(
            plugin,
            size=size,
            resolution=raw_resolution,
            aspect_ratio=raw_aspect_ratio,
            provider=provider,
            model=model,
        )

        # 获取参考图片（需要在启动后台任务前获取，因为 event 可能在之后失效）
        reference_images, avatar_reference = await plugin._fetch_images_from_event(
            event, include_at_avatars=include_avatar
        )

        if not include_ref_images:
            reference_images = []
        if not include_avatar:
            avatar_reference = []

        reference_images = list(reference_images) + _normalize_reference_paths(
            plugin,
            kwargs.get("reference_image_paths"),
        )

        ref_count = len(reference_images)
        avatar_count = len(avatar_reference)
        has_references = bool(ref_count or avatar_count)
        preserve_size = (
            bool(kwargs.get("preserve_reference_image_size"))
            if "preserve_reference_image_size" in kwargs
            else bool(getattr(plugin.cfg, "preserve_reference_image_size", False))
        )
        explicit_size = any(
            value is not None and str(value).strip()
            for value in (size, raw_resolution, raw_aspect_ratio)
        )
        if has_references and preserve_size and explicit_size:
            return (
                "❌ 参数冲突：有参考图并启用 preserve_reference_image_size 时，"
                "不能同时指定 size、resolution 或 aspect_ratio"
            )
        suppress_resolution = has_references and preserve_size
        if suppress_resolution:
            resolution = None
            aspect_ratio = None

        candidates = _matching_candidates(
            plugin,
            provider=provider,
            model=model,
            has_reference_images=has_references,
            negative_prompt=negative_prompt,
            watermark=watermark,
            quality=quality,
        )
        if not candidates:
            return "❌ 没有匹配所选供应商、模型、生成模式和参数能力的候选模型"
        request_routing_mode = routing_mode(provider, model)

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
                success, result_data = await invoke_generation_core(
                    plugin,
                    event=event,
                    prompt=prompt,
                    reference_images=reference_images,
                    avatar_reference=avatar_reference,
                    override_resolution=resolution,
                    override_aspect_ratio=aspect_ratio,
                    is_tool_call=True,
                    requested_provider=provider,
                    requested_model=model,
                    negative_prompt=negative_prompt,
                    watermark=watermark,
                    quality=quality,
                    image_count=1,
                    suppress_resolution=suppress_resolution,
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

        generation_task = _create_generation_task(
            plugin=plugin,
            event=event,
            prompt=prompt,
            reference_images=reference_images,
            avatar_reference=avatar_reference,
            override_resolution=resolution,
            override_aspect_ratio=aspect_ratio,
            is_tool_call=True,
            requested_provider=provider,
            requested_model=model,
            negative_prompt=negative_prompt,
            watermark=watermark,
            quality=quality,
            image_count=1,
            suppress_resolution=suppress_resolution,
        )
        foreground_wait_seconds = _resolve_foreground_wait_seconds(plugin, event)

        try:
            if foreground_wait_seconds <= 0:
                notice = _build_background_start_notice(
                    ref_count=ref_count,
                    avatar_count=avatar_count,
                    resolution=resolution,
                    aspect_ratio=aspect_ratio,
                    llm_notice=config_value_notice,
                )
                message = f"{routing_description(request_routing_mode)}。{notice}"
                record = await plugin.background_task_manager.create(
                    session_id=_event_session_id(event),
                    kind="single",
                    routing_mode=request_routing_mode,
                    message=message,
                )
                task_id = record["task_id"]
                _schedule_generation_delivery(
                    plugin=plugin,
                    event=event,
                    generation_task=generation_task,
                    scene="后台任务",
                    task_id=task_id,
                )
                return _background_json(task_id, request_routing_mode, message)

            logger.debug(f"[前台等待] 最多等待 {foreground_wait_seconds} 秒。")
            try:
                success, result_data, request_stats = await asyncio.wait_for(
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
                    async def _already_done():
                        return (True, result_data, request_stats)

                    done_task = asyncio.create_task(_already_done())
                    result_message = (
                        "图片已生成，发送阶段超过前台等待时间，正在后台发送"
                    )
                    record = await plugin.background_task_manager.create(
                        session_id=_event_session_id(event),
                        kind="single",
                        routing_mode=request_routing_mode,
                        message=result_message,
                    )
                    task_id = record["task_id"]
                    _schedule_generation_delivery(
                        plugin=plugin,
                        event=event,
                        generation_task=done_task,
                        scene="后台任务(代理下载超时回退)",
                        task_id=task_id,
                    )
                    if config_value_notice:
                        result_message += f"\n{config_value_notice}"
                    return _background_json(
                        task_id,
                        request_routing_mode,
                        result_message,
                    )
            else:
                error_msg = (
                    result_data if isinstance(result_data, str) else "❌ 图像生成失败"
                )
                return error_msg
        except asyncio.TimeoutError:
            logger.debug("[前台等待] 等待超时，切换为后台继续生成。")
            notice = _build_background_fallback_notice(
                ref_count=ref_count,
                avatar_count=avatar_count,
                resolution=resolution,
                aspect_ratio=aspect_ratio,
                waited_seconds=foreground_wait_seconds,
                llm_notice=config_value_notice,
            )
            message = f"{routing_description(request_routing_mode)}。{notice}"
            record = await plugin.background_task_manager.create(
                session_id=_event_session_id(event),
                kind="single",
                routing_mode=request_routing_mode,
                message=message,
            )
            task_id = record["task_id"]
            _schedule_generation_delivery(
                plugin=plugin,
                event=event,
                generation_task=generation_task,
                scene="后台任务",
                task_id=task_id,
            )
            return _background_json(task_id, request_routing_mode, message)
        except Exception as e:
            logger.error(f"[前台等待] 图像生成异常：{e}", exc_info=True)
            return f"❌ 图片生成过程中出错：{str(e)}"


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
