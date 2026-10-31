"""
LLM 工具定义模块

将图像生成 Tool 拆分为独立类

"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
from typing import TYPE_CHECKING, Any, NamedTuple

import mcp.types
from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass

from .background_dispatch import (
    dispatch_generation_result as _dispatch_generation_result,
)
from .background_dispatch import (
    generation_reference_note,
    single_task_record_changes,
)
from .background_notify import report_background_failure
from .batch_generation import run_batch_job
from .core.errors import (
    CoreError,
    QueueFullError,
    QuotaExceededError,
    ServiceClosedError,
)
from .core.states import JobState
from .generation_call import invoke_generation_core
from .generation_errors import api_failure_message
from .generation_tracker import requester_from_event
from .image_generator import DEFAULT_MAX_REFERENCE_IMAGES
from .internal_references import (
    InternalReferenceError,
    ReferenceResolver,
    parse_references,
)
from .llm_output import image_location_list, image_locations, sanitize_text
from .openai_image_size import (
    CUSTOM_SIZE_DEFAULT,
    validate_custom_size,
)
from .prompt_templates import PromptTemplate, render_template
from .provider_capabilities import (
    SUPPORTED_ASPECT_RATIOS,
    candidate_capability,
    parse_model_selector,
    routing_description,
    routing_mode,
    select_candidates,
)
from .provider_runtime import provider_operation
from .provider_settings import (
    candidate_tool_profile,
    first_provider_tool_profile,
)
from .provider_settings import (
    first_provider_candidate as _first_candidate_from_config,
)
from .session_image_dir import bind_session_image_dir, reset_session_image_dir
from .thought_signature import log_thought_signature_debug
from .tl_utils import format_error_message
from .tool_path_guard import filter_reference_paths
from .v2_bridge import build_request as v2_build_request
from .v2_bridge import split_artifacts, v2_failure_message, v2_failure_text
from .v2_tool_delivery import ToolBackgroundDelivery, ToolBatchDelivery

if TYPE_CHECKING:
    from ..main import GeminiImageGenerationPlugin


# 参数枚举常量（工具定义和验证共用）
RESOLUTION_OPTIONS = ("1K", "2K", "4K")
ASPECT_RATIO_OPTIONS = SUPPORTED_ASPECT_RATIOS
VALID_ASPECT_RATIOS = set(ASPECT_RATIO_OPTIONS)


def _candidate_resolution_options(candidate: Any) -> tuple[str, ...]:
    capability = candidate_capability(candidate)
    descriptor = (capability.get("parameters") or {}).get("resolution") or {}
    raw_values = descriptor.get("enum") or ()
    return tuple(
        dict.fromkeys(
            str(value).strip().upper() for value in raw_values if str(value).strip()
        )
    )


def _tool_resolution_options(
    plugin: Any,
    candidate: Any | None = None,
) -> tuple[str, ...]:
    candidates = (
        [candidate]
        if candidate is not None
        else list(getattr(plugin.cfg, "provider_candidates", []) or [])
    )
    values: list[str] = []
    for item in candidates:
        values.extend(_candidate_resolution_options(item))
    if not values:
        return RESOLUTION_OPTIONS

    unique = list(dict.fromkeys(values))

    def resolution_sort_key(value: str) -> tuple[int, int, str]:
        match = re.fullmatch(r"(\d+)K", value)
        return (0, int(match.group(1)), "") if match else (1, 0, value)

    return tuple(sorted(unique, key=resolution_sort_key))


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
            "description": (
                "不用模板时必填：图像生成或修改的详细描述。"
                "使用 template 时只写主体或附加要求"
            ),
        },
        "template": {
            "type": "string",
            "description": (
                "可选。提示词模板 id；先用 gemini_image_lookup（kind=templates）"
                "查询可用模板"
            ),
        },
        "references": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "可选。要作为参考图的图片，每项是下列写法之一："
                "current（当前消息和被引用消息里的图片）、"
                "avatar:sender（发送者头像）、"
                "avatar:mentioned（消息里 @ 到的用户头像）、"
                "avatar:用户ID（发送者或被 @ 用户的头像）、"
                "gen:last（本会话上一次生成的图）、"
                "gen:记录ID 或 gen:记录ID#序号（指定生成记录的第几张，"
                "可用 gemini_image_lookup 的 history 查到）、"
                "本地图片的绝对路径（需位于插件允许的目录内）。"
                '改图传 ["current"]，要在上一张生成图上继续修改传 ["gen:last"]'
            ),
        },
    }


_OPTION_PROPERTIES: dict[str, dict[str, Any]] = {
    "quality": {
        "type": "string",
        "description": "质量档位；合法值用 gemini_image_lookup（kind=models，detail=true）查询",
    },
    "negative_prompt": {
        "type": "string",
        "description": "负面提示词",
    },
    "watermark": {
        "type": "boolean",
        "description": "水印开关；省略时使用供应商配置",
    },
}


def _configured_option_fields(plugin: Any) -> list[str]:
    """已配置候选的能力表里声明过的 options 字段，按固定顺序。"""

    declared: set[str] = set()
    for candidate in (
        getattr(getattr(plugin, "cfg", None), "provider_candidates", []) or []
    ):
        parameters = candidate_capability(candidate).get("parameters") or {}
        declared.update(name for name in _OPTION_PROPERTIES if name in parameters)
    return [name for name in _OPTION_PROPERTIES if name in declared]


def _build_tool_description(plugin: Any) -> str:
    return (
        "生成或修改图像。"
        "当当前对话需要图像生成、绘画、改图、换风格或手办化时调用此函数。"
        "有现成的提示词模板时，先用 gemini_image_lookup 查询，kind 设为 templates，"
        "再用 template 直接套用，prompt 只写主体或附加要求。"
        "基于已有图片生成时传 references：对话中出现“改成”“变成”“基于”“修改”“改图”等词，"
        '传 ["current"]；提到“根据我”“我的头像”传 ["avatar:sender"]；@了某人传 '
        '["avatar:mentioned"]；要在上一张生成图的基础上继续修改传 ["gen:last"]，'
        "也可以用 gemini_image_lookup 的 history 取得 gen: 标识。"
        "模型和参数取值用 gemini_image_lookup 查询，kind 设为 models，不要猜测。"
        "此工具会先在前台短时间等待；超时后转后台，完成后会自动唤醒你，"
        "你必须调用 send_message_to_user 发送图片。"
        "deliver 设为 return 时不发给用户，只返回图片路径，用于需要自己处理图片的场景。"
        "需要批量生成时传 batch_tasks；每项必须包含唯一 name、完整 prompt、image_count，"
        "并至少指定 provider 或 model。批量任务固定进入后台。"
    )


def _build_batch_tasks_property(resolution_options: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "array",
        "description": "可选。传入后按命名任务直接在后台批量生成，忽略顶层的单图参数",
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
                "negative_prompt": {
                    "type": "string",
                    "description": "可选负面提示词；指定后只会使用支持该参数的模型",
                },
                "watermark": {
                    "type": "boolean",
                    "description": "可选水印开关；省略时使用供应商配置",
                },
                "quality": {
                    "type": "string",
                    "description": "可选质量档位；合法值用 gemini_image_lookup（kind=models，detail=true）查询",
                },
                "use_reference_images": {
                    "type": "boolean",
                    "description": "是否使用当前消息和被引用消息里的图片作为参考图",
                    "default": False,
                },
                "include_user_avatar": {
                    "type": "boolean",
                    "description": "是否包含用户头像作为参考图",
                    "default": False,
                },
                "reference_image_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "可选。作为参考图的本地图片绝对路径列表，需位于插件允许的目录内；"
                        "路径无效时停止生成。典型用途：复用上一次工具调用缓存在 "
                        "data/temp/tool_images/ 的图片"
                    ),
                },
                "resolution": {
                    "type": "string",
                    "description": "输出分辨率，可选；不传则使用插件配置或供应商默认值",
                    "enum": list(resolution_options),
                },
                "aspect_ratio": {
                    "type": "string",
                    "description": "输出长宽比，可选；不传则使用插件配置或供应商默认值",
                    "enum": list(ASPECT_RATIO_OPTIONS),
                },
                "preserve_reference_image_size": {
                    "type": "boolean",
                    "description": (
                        "有参考图时是否保留参考图尺寸；省略时使用插件配置。"
                        "如果同时显式传入 resolution 或 aspect_ratio，则以显式尺寸为准。"
                    ),
                },
            },
            "required": ["name", "prompt", "image_count"],
        },
    }


def _build_tool_parameters(plugin: Any) -> dict[str, Any]:
    properties = _build_tool_base_properties()
    resolution_options = _tool_resolution_options(plugin)
    properties["resolution"] = {
        "type": "string",
        "description": (
            "输出分辨率，可选；不传则使用模板默认值、插件配置或供应商默认值。具体取值先用 gemini_image_lookup（kind=models）查询。"
        ),
        "enum": list(resolution_options),
    }
    properties["aspect_ratio"] = {
        "type": "string",
        "description": (
            "输出长宽比，可选；不传则使用模板默认值、插件配置或供应商默认值。"
            "各模型支持的具体取值先用 gemini_image_lookup（kind=models）查询，不支持的比例由渠道忽略。"
        ),
        "enum": list(ASPECT_RATIO_OPTIONS),
    }
    properties["model"] = {
        "type": "string",
        "description": (
            "可选。写法为 供应商/模型、供应商/（只限定供应商）、模型名或模型别名；"
            "无法识别时会报错而不是改用默认路由，请先用 gemini_image_lookup（kind=models）查询"
        ),
    }
    option_fields = _configured_option_fields(plugin)
    if option_fields:
        properties["options"] = {
            "type": "object",
            "description": "可选的模型参数，只填写本次需要覆盖的项；指定后只会使用支持该参数的模型",
            "properties": {
                name: dict(_OPTION_PROPERTIES[name]) for name in option_fields
            },
        }
    properties["deliver"] = {
        "type": "string",
        "enum": ["user", "return", "forum"],
        "description": (
            "user（默认）把图片交给主 AI 发送；"
            "return 不发给用户，只返回图片路径，用于普通文件处理；"
            "forum 同步返回图片位置，并指导调用 upload_image 上传到论坛图床"
        ),
        "default": "user",
    }
    properties["batch_tasks"] = _build_batch_tasks_property(resolution_options)
    return {
        "type": "object",
        "properties": properties,
        "required": [],
    }


def _normalize_tool_resolution(
    value: Any,
    valid_resolutions: tuple[str, ...] = RESOLUTION_OPTIONS,
) -> tuple[str | None, bool]:
    if value is None or not str(value).strip():
        return None, False
    resolution = str(value).strip().upper()
    if resolution not in set(valid_resolutions):
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
    resolution: Any = None,
    aspect_ratio: Any = None,
    provider: Any = None,
    model: Any = None,
) -> tuple[str | None, str | None, str | None]:
    candidate = _first_provider_candidate(plugin, provider, model)
    resolution_options = _tool_resolution_options(plugin, candidate)
    normalized_resolution, invalid_resolution = _normalize_tool_resolution(
        resolution,
        resolution_options,
    )
    normalized_aspect_ratio, invalid_aspect_ratio = _normalize_tool_aspect_ratio(
        aspect_ratio
    )

    if (provider or model) and candidate is None:
        return normalized_resolution, normalized_aspect_ratio, None
    if not _is_custom_size_tool_mode(plugin, candidate):
        return normalized_resolution, normalized_aspect_ratio, None

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


def _image_location_values(
    image_urls: list[str] | None, image_paths: list[str] | None
) -> list[str]:
    """Return safe image locations in path-first order."""

    return image_location_list(image_urls, image_paths)


async def _build_call_tool_result(
    image_urls: list[str] | None,
    image_paths: list[str] | None,
    text_content: str | None,
    message_sender: Any,
    api_client: Any | None = None,
    llm_notice: str | None = None,
) -> mcp.types.CallToolResult:
    """Build a text-only tool result with local paths preferred over URLs."""

    del message_sender, api_client
    locations = _image_location_values(image_urls, image_paths)
    text_parts: list[str] = []
    if locations:
        text_parts.append(
            "生成图片位置（优先使用本地路径）：\n"
            + "\n".join(
                f"图片 {index}: {location}"
                for index, location in enumerate(locations, start=1)
            )
            + "\n需要发送给用户时，请调用 send_message_to_user 的 image 工具；"
            "不要把图片数据重新编码后放入上下文。"
        )
    if text_content:
        text_parts.append(text_content)
    if llm_notice:
        text_parts.append(llm_notice)
    if not text_parts:
        text_parts.append("图片已生成但未能获取到有效的图片路径或 URL。")
    return mcp.types.CallToolResult(
        content=[mcp.types.TextContent(type="text", text="\n".join(text_parts))]
    )


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
        "图片正在后台生成中，通常需要 10-30 秒，高质量生成可能长达几百秒，生成完成后会重新激活你并提供图片 URL/路径，请调用 send_message_to_user 的 image 组件发送图片。\n"
        "请维持原有人设告诉用户图片正在生成，请稍等片刻；收到后台结果后使用主动发送工具交付图片。"
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
        "生成完成后会重新激活你并提供图片 URL/路径，请调用 send_message_to_user 的 image 组件发送图片。\n"
        "请维持原有人设告诉用户图片正在生成，请稍等片刻；收到后台结果后使用主动发送工具交付图片。"
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
    template_id: str | None = None,
) -> asyncio.Task:
    """旧调度路径：`scheduler_v2` 开启时由 v2 调度器替代，稳定后删除，见 docs/architecture/migration.md。"""

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
            template_id=template_id,
        )
        return success, result_data, plugin.image_generator.get_request_stats()

    return asyncio.create_task(_run_generation())


async def _await_generation_task_and_send(
    plugin: Any,
    event: Any,
    generation_task: asyncio.Task,
    *,
    scene: str,
    task_id: str | None = None,
    notify_llm: bool = False,
) -> None:
    """旧调度路径：`scheduler_v2` 开启时由 v2 调度器替代，稳定后删除，见 docs/architecture/migration.md。"""
    try:
        success, result_data, stats = await generation_task
        delivered = await _dispatch_generation_result(
            plugin=plugin,
            event=event,
            success=success,
            result_data=result_data,
            scene=scene,
            notify_llm=notify_llm,
            task_id=task_id,
            reference_note=generation_reference_note(
                plugin, (stats or {}).get("job_id") if success else None
            ),
        )
        if task_id:
            await plugin.background_task_manager.update(
                task_id,
                **single_task_record_changes(
                    success=success,
                    delivered=delivered,
                    result_data=result_data,
                    stats=stats,
                ),
            )
    except Exception as exc:
        logger.error(f"[{scene}] 后台图像生成异常: {exc}", exc_info=True)
        if notify_llm:
            await report_background_failure(
                plugin,
                event,
                format_error_message(exc),
                scene=scene,
                task_id=task_id,
            )
        else:
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
    notify_llm: bool = False,
) -> asyncio.Task:
    """旧调度路径：`scheduler_v2` 开启时由 v2 调度器替代，稳定后删除，见 docs/architecture/migration.md。"""
    coroutine = _await_generation_task_and_send(
        plugin=plugin,
        event=event,
        generation_task=generation_task,
        scene=scene,
        task_id=task_id,
        notify_llm=notify_llm,
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


def _reference_path_policy(plugin: Any) -> tuple[list[str], str]:
    """本地参考图路径的校验配置：(允许目录, 权限模式)。批量与 references 共用。"""

    cfg = getattr(plugin, "cfg", None)
    path_mode = getattr(cfg, "llm_tool_reference_path_mode", "whitelist")
    allowed_dirs = list(getattr(cfg, "llm_tool_reference_allowed_dirs", []) or [])
    return allowed_dirs, path_mode


def _normalize_reference_paths(plugin: Any, values: Any) -> list[str]:
    raw_paths = values if isinstance(values, list) else [values] if values else []
    allowed_dirs, path_mode = _reference_path_policy(plugin)
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


def _reference_admission_error(
    plugin: Any,
    reference_images: list[str],
    avatar_reference: list[str],
    *,
    references_required: bool,
    avatar_required: bool,
) -> str | None:
    """Reject missing required inputs before charging quota or creating a task."""
    if avatar_required and not avatar_reference:
        return "未取得所需用户头像，未启动生成；请检查头像获取结果或减少参考图片数量。"
    if references_required and not (reference_images or avatar_reference):
        return (
            "未取得所需参考图片，未启动生成；请引用、重发图片或提供有效的本地图片路径。"
        )

    max_images = getattr(
        getattr(plugin, "image_generator", None), "max_reference_images", None
    )
    # 核心生成器优先保留普通参考图；本地路径合并后不能挤掉全部必需头像。
    if max_images is not None and (
        (references_required and max_images == 0)
        or (avatar_required and len(reference_images) >= max_images)
    ):
        return "参考图片数量超过上限，无法保留所需参考图或头像，未启动生成；请减少参考图片数量或检查上限配置。"
    return None


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
        resolution_value = raw_item.get("resolution")
        aspect_value = raw_item.get("aspect_ratio")
        resolution, aspect_ratio, _notice = _resolve_tool_size_params(
            plugin,
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
        reference_error = _reference_admission_error(
            plugin,
            reference_images,
            avatar_reference,
            references_required=bool(
                raw_item.get("use_reference_images")
                or raw_item.get("include_user_avatar")
                or raw_item.get("reference_image_paths")
            ),
            avatar_required=bool(raw_item.get("include_user_avatar")),
        )
        if reference_error:
            return None, f"批量任务 {name} {reference_error}"
        has_references = bool(reference_images or avatar_reference)
        preserve_size = (
            bool(raw_item.get("preserve_reference_image_size"))
            if "preserve_reference_image_size" in raw_item
            else bool(getattr(plugin.cfg, "preserve_reference_image_size", False))
        )
        explicit_size = any(
            value is not None and str(value).strip()
            for value in (resolution_value, aspect_value)
        )
        # 模型显式给出尺寸时优先满足本次请求；配置默认值只约束未指定尺寸的请求。
        if explicit_size:
            preserve_size = False
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


def _joined_notice(*parts: str | None) -> str | None:
    text = "\n".join(part for part in parts if part)
    return text or None


def _format_returned_images(
    success: bool,
    result_data: Any,
    config_value_notice: str | None,
    reference_note: str | None = None,
    *,
    forum: bool = False,
) -> str:
    """格式化 deliver=return 或 deliver=forum 的同步结果。"""
    if not success:
        error_msg = result_data if isinstance(result_data, str) else "图像生成失败"
        return f"❌ 图片生成失败：{error_msg}"

    if not isinstance(result_data, tuple):
        return "❌ 图片生成返回格式异常"

    image_urls, image_paths, text_content, thought_signature = result_data
    local_paths, public_urls = image_locations(image_urls, image_paths)
    available_images = [
        *(("path", path) for path in local_paths),
        *(("url", url) for url in public_urls),
    ]

    if not available_images:
        return "❌ 图片生成完成，但未获取到有效的图片路径或URL"

    if forum:
        result_lines = [
            "[图像生成完成 - 论坛发帖准备]",
            "",
            "图片已生成，插件不会直接发帖。请按下面流程完成论坛自动化：",
            "",
        ]
    else:
        result_lines = [
            "[图像生成完成 - 未发送给用户]",
            "",
            "图片已生成，没有发给用户。以下是图片路径，用于需要自己处理图片的场景：",
            "",
        ]

    for idx, (img_type, img_value) in enumerate(available_images, 1):
        label = "URL" if img_type == "url" else "本地路径"
        result_lines.append(f"图片{idx} ({label}): {img_value}")

    if forum:
        result_lines.extend(
            [
                "",
                "【下一步操作】",
                "1. 对每个图片 URL 或本地路径调用 upload_image 上传到论坛图床。",
                "2. 使用 upload_image 返回的永久图片 URL。",
                "3. 在发帖或回复中用 Markdown 格式插入图片：![图片描述](图床URL)。",
            ]
        )

    if text_content:
        result_lines.extend(["", f"【AI 生成的图片描述】{text_content[:200]}..."])

    for extra in (config_value_notice, reference_note):
        if extra:
            result_lines.extend(["", extra])

    logger.info("[后台任务] 图片生成成功，返回 %s 张图片", len(available_images))
    return "\n".join(result_lines)


def _build_image_location_notice(
    images: list[str], llm_notice: str | None = None
) -> str:
    """前台结果构建超时时直接列出图片位置，让 Agent 自行发送（Job 已结束，无法转后台）。"""
    lines = [
        "图片已生成，但前台结果处理超时；请调用 send_message_to_user 的 image 组件"
        "发送这些图片："
    ]
    for idx, image in enumerate(images, 1):
        label = "URL" if image.startswith(("http://", "https://")) else "本地路径"
        lines.append(f"图片{idx} ({label}): {image}")
    if llm_notice:
        lines.append(llm_notice)
    return "\n".join(lines)


async def _start_background_record_v2(
    plugin: Any,
    event: Any,
    delivery: ToolBackgroundDelivery,
    mode: str,
    notice: str,
) -> str:
    """投递方登记成功后建单图后台记录并绑定给它，返回后台 JSON。"""
    message = f"{routing_description(mode)}。{notice}"
    try:
        record = await plugin.background_task_manager.create(
            session_id=_event_session_id(event),
            kind="single",
            routing_mode=mode,
            message=message,
        )
    except BaseException:
        # 记录没建成：别让投递方白等绑定超时，结果仍会经它送达。
        delivery.bind_record(None)
        raise
    task_id = record["task_id"]
    delivery.bind_record(task_id)
    return _background_json(task_id, mode, message)


async def _finish_foreground_v2(
    plugin: Any,
    job_result: Any,
    *,
    config_value_notice: str | None,
    job_id: str | None = None,
) -> Any:
    """前台拿到已结束的 Job：成功构建 CallToolResult，失败返回与旧路径同措辞的提示。"""
    if (
        job_result.state not in (JobState.SUCCEEDED, JobState.PARTIAL)
        or job_result.result is None
    ):
        return v2_failure_message(
            job_result, total_timeout=plugin.image_generator.total_timeout
        )
    result = job_result.result
    image_urls, image_paths = split_artifacts(result.artifacts)
    images = plugin.message_sender.merge_available_images(image_urls, image_paths)
    logger.info(f"[前台等待] 生成完成，通过 CallToolResult 返回 {len(images)} 张图片")
    llm_notice = _joined_notice(
        config_value_notice, generation_reference_note(plugin, job_id)
    )
    try:
        return await asyncio.wait_for(
            _build_call_tool_result(
                image_urls=image_urls,
                image_paths=image_paths,
                text_content=result.text,
                message_sender=plugin.message_sender,
                api_client=plugin.api_client,
                llm_notice=llm_notice,
            ),
            timeout=30,  # 下载最多给 30 秒
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[前台等待] 构建 CallToolResult 超时（代理下载慢），返回图片位置文本"
        )
        return _build_image_location_notice(images, llm_notice)


async def _run_single_v2(
    plugin: Any,
    event: Any,
    service: Any,
    *,
    prompt: str,
    reference_images: list[str],
    avatar_reference: list[str],
    resolution: str | None,
    aspect_ratio: str | None,
    provider: str | None,
    model: str | None,
    negative_prompt: str | None,
    watermark: bool | None,
    quality: str | None,
    suppress_resolution: bool,
    return_images: bool,
    forum_mode: bool,
    config_value_notice: str | None,
    trusted_local: bool = False,
    template_id: str | None = None,
) -> Any:
    """单图 v2：生成与后台投递都由调度器执行，工具侧不创建任何 task、不扣额度。"""
    ref_count = len(reference_images)
    avatar_count = len(avatar_reference)
    request_routing_mode = routing_mode(provider, model)
    generator = plugin.image_generator

    # 日志记录（仅记录长度和参数摘要，避免记录用户原始内容）
    logger.info(
        f"[工具调用] 启动图像生成任务（v2）："
        f"提示词长度={len(prompt)} 参考图={ref_count} 张 头像={avatar_count} 张 "
        f"尺寸/分辨率={resolution} 比例={aspect_ratio} 投递模式={'forum' if forum_mode else 'return' if return_images else 'user'}"
    )
    submission = await generator.submit_v2(
        event=event,
        v2_service=service,
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
        template_id=template_id,
        admission={"event": event},
        quota={"cost": 1},
        trusted_local=trusted_local,
    )
    handle = submission.handle
    if handle is None:
        # 额度拒绝与其他提交失败都带提示；群名单静默拒绝没有提示，沿用旧路径文案。
        return submission.message or "请求过于频繁，请稍后再试"
    job_id = handle.job_id

    # ========== deliver=return：同步等待生成完成 ==========
    if return_images:
        logger.info("[后台任务] 仅返回路径：同步等待图片生成完成……")
        try:
            success, result_data = await generator.wait_v2(
                v2_service=service,
                handle=handle,
                requested_provider=provider,
                requested_model=model,
                image_count=1,
            )
            return _format_returned_images(
                success,
                result_data,
                config_value_notice,
                generation_reference_note(plugin, job_id),
                forum=forum_mode,
            )
        except Exception as e:
            logger.error(f"[后台任务] 图片生成异常：{e}", exc_info=True)
            return f"❌ 图片生成过程中出错：{str(e)}"

    foreground_wait_seconds = _resolve_foreground_wait_seconds(plugin, event)
    delivery = ToolBackgroundDelivery(plugin, event, scene="后台任务")
    result_wait = service.result_wait_timeout(generator.total_timeout)
    job_result = None
    background_notice: str | None = None
    try:
        if foreground_wait_seconds <= 0:
            if service.attach_delivery(job_id, delivery):
                background_notice = _build_background_start_notice(
                    ref_count=ref_count,
                    avatar_count=avatar_count,
                    resolution=resolution,
                    aspect_ratio=aspect_ratio,
                    llm_notice=config_value_notice,
                )
            else:
                job_result = await service.wait(job_id, timeout=result_wait)
        else:
            logger.debug(f"[前台等待] 最多等待 {foreground_wait_seconds} 秒。")
            try:
                job_result = await service.wait(job_id, timeout=foreground_wait_seconds)
            except asyncio.TimeoutError:
                logger.debug("[前台等待] 等待超时，切换为后台继续生成。")
                if service.attach_delivery(job_id, delivery):
                    background_notice = _build_background_fallback_notice(
                        ref_count=ref_count,
                        avatar_count=avatar_count,
                        resolution=resolution,
                        aspect_ratio=aspect_ratio,
                        waited_seconds=foreground_wait_seconds,
                        llm_notice=config_value_notice,
                    )
                else:
                    # 超时的瞬间 Job 已越过投递判定点，结果就绪在即：按前台完成处理。
                    job_result = await service.wait(job_id, timeout=result_wait)
    except asyncio.CancelledError:
        # 取消入口即取消生成：不留下无人投递的后台 Job。
        await asyncio.shield(service.cancel(job_id))
        raise
    except asyncio.TimeoutError:
        await asyncio.shield(service.cancel(job_id))
        return api_failure_message(
            message="生成等待超时",
            status_code=None,
            error_type="timeout",
            total_timeout=generator.total_timeout,
        )
    except Exception as e:
        logger.error(f"[前台等待] 图像生成异常：{e}", exc_info=True)
        await asyncio.shield(service.cancel(job_id))
        return f"❌ 图片生成过程中出错：{str(e)}"

    try:
        if background_notice is not None:
            return await _start_background_record_v2(
                plugin, event, delivery, request_routing_mode, background_notice
            )
        return await _finish_foreground_v2(
            plugin, job_result, config_value_notice=config_value_notice, job_id=job_id
        )
    except Exception as e:
        logger.error(f"[前台等待] 图像生成异常：{e}", exc_info=True)
        return f"❌ 图片生成过程中出错：{str(e)}"


async def _run_batch_v2(
    plugin: Any, event: Any, service: Any, prepared_items: list[dict[str, Any]]
) -> str:
    """批量 v2：父子 Job 由调度器持有，工具只准备请求并创建后台记录。"""

    generator = plugin.image_generator
    modes = {item["routing_mode"] for item in prepared_items}
    batch_mode = next(iter(modes)) if len(modes) == 1 else "mixed"
    message = routing_description(batch_mode)
    delivery = ToolBatchDelivery(plugin, event)
    token = None
    try:
        token = await bind_session_image_dir(event, generator.context)
        children = []
        for item in prepared_items:
            image_count = int(item["image_count"])
            children.append(
                await generator.prepare_v2_request(
                    event=event,
                    v2_service=service,
                    prompt=item["prompt"],
                    reference_images=item.get("reference_images") or [],
                    avatar_reference=item.get("avatar_reference") or [],
                    override_resolution=item.get("resolution"),
                    override_aspect_ratio=item.get("aspect_ratio"),
                    is_tool_call=True,
                    requested_provider=item.get("provider"),
                    requested_model=item.get("model"),
                    negative_prompt=item.get("negative_prompt"),
                    watermark=item.get("watermark"),
                    quality=item.get("quality"),
                    image_count=image_count,
                    suppress_resolution=bool(item.get("suppress_resolution", False)),
                    deadline_seconds=None,
                    source="llm_batch",
                    metadata={
                        "item_name": item["name"],
                        "fill_image_count": True,
                        "quota": False,
                        "deadline_seconds": generator.total_timeout * image_count,
                    },
                    trusted_local=True,
                )
            )
        requester = requester_from_event(event)
        umo = getattr(event, "unified_msg_origin", None)
        if umo:
            requester["umo"] = str(umo)
        parent_prompt = (
            "批量生图：" + "、".join(item["name"] for item in prepared_items)
        )[:500]
        parent = v2_build_request(
            source="llm_batch",
            prompt=parent_prompt,
            resolution=None,
            aspect_ratio=None,
            provider=None,
            model=None,
            reference_images=(),
            deadline_seconds=None,
            requester=requester,
            metadata={
                "batch_parent": True,
                "quota": {"cost": len(prepared_items)},
            },
            image_count=sum(int(item["image_count"]) for item in prepared_items),
        )
        handle = await service.submit_batch(
            parent,
            children,
            concurrency=max(int(plugin.cfg.batch_concurrency), 1),
            admission={"event": event},
            delivery=delivery,
        )
    except asyncio.CancelledError:
        raise
    except QuotaExceededError as error:
        return error.message or "请求过于频繁，请稍后再试"
    except (QueueFullError, ServiceClosedError) as error:
        return api_failure_message(
            message=error.message,
            status_code=None,
            error_type=error.code,
            total_timeout=generator.total_timeout,
        )
    except CoreError as error:
        return v2_failure_text(
            str(getattr(error, "code", "") or ""),
            getattr(error, "message", None) or str(error),
            total_timeout=generator.total_timeout,
        )
    except Exception as exc:
        logger.error(f"[批量任务] v2 提交异常: {exc}", exc_info=True)
        return f"❌ 批量生成任务启动失败：{exc}"
    finally:
        if token is not None:
            reset_session_image_dir(token)

    try:
        record = await plugin.background_task_manager.create(
            session_id=_event_session_id(event),
            kind="batch",
            routing_mode=batch_mode,
            message=message,
            total_items=len(prepared_items),
        )
    except BaseException:
        delivery.bind_record(None)
        await asyncio.shield(service.cancel(handle.job_id))
        raise
    task_id = record["task_id"]
    delivery.bind_record(task_id)
    return _background_json(task_id, batch_mode, message)


# gen: 引用的画廊图在工具层读成 data URI，单文件上限。
_GALLERY_REFERENCE_LIMIT = 20 * 1024 * 1024
_REFERENCE_FAILURE_PREFIX = "❌ 参考图解析失败，未启动生成："


class _ToolReferences(NamedTuple):
    # 顺序：消息图、gen: 画廊图、用户本地文件；头像单独放。
    images: list[str]
    avatars: list[str]
    has_user_files: bool


class _SingleRequest(NamedTuple):
    prompt: str
    template_id: str | None
    provider: str | None
    model: str | None
    negative_prompt: str | None
    watermark: bool | None
    quality: str | None
    reference_images: list[str]
    avatar_reference: list[str]
    trusted_local: bool
    resolution: str | None
    aspect_ratio: str | None
    config_value_notice: str | None
    suppress_resolution: bool
    return_images: bool
    forum_mode: bool


def _blank(value: Any) -> bool:
    return value is None or not str(value).strip()


def _reference_settings(plugin: Any) -> dict[str, Any]:
    cfg = getattr(plugin, "cfg", None)
    tracker = getattr(plugin, "generation_tracker", None)
    generator = getattr(plugin, "image_generator", None)
    allowed_dirs, path_mode = _reference_path_policy(plugin)
    return {
        "cross_session": bool(getattr(cfg, "reference_cross_session", False)),
        "history_enabled": bool(getattr(tracker, "enabled", False)),
        "max_reference_images": getattr(
            generator, "max_reference_images", DEFAULT_MAX_REFERENCE_IMAGES
        ),
        "allowed_dirs": allowed_dirs,
        "global_paths": path_mode == "global",
    }


def _inline_gallery_file(path: str) -> str:
    """把 gen: 解析出的画廊文件读成 data URI，让它和消息图走同一条普通通道。"""

    try:
        with open(path, "rb") as handle:
            raw = handle.read(_GALLERY_REFERENCE_LIMIT + 1)
    except OSError as exc:
        raise ValueError("参考图片不存在或正在清理，请重新选择") from exc
    if len(raw) > _GALLERY_REFERENCE_LIMIT:
        raise ValueError("参考图片超过 20MB")
    mime_type = _MIME_TYPE_MAP.get(os.path.splitext(path)[1].lower(), "image/png")
    return f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}"


async def _resolve_tool_references(
    plugin: Any, event: Any, values: list[str]
) -> tuple[_ToolReferences | None, str | None]:
    """解析 references；任何一项失败都整体失败，错误里带上出错的那一项。"""

    if not values:
        return _ToolReferences([], [], False), None
    resolver = ReferenceResolver(
        image_handler=getattr(plugin, "image_handler", None),
        avatar_handler=getattr(plugin, "avatar_handler", None),
        generation_tracker=getattr(plugin, "generation_tracker", None),
        settings=lambda: _reference_settings(plugin),
    )
    try:
        resolved = await resolver.resolve(parse_references(values), event)
    except InternalReferenceError as exc:
        return None, f"{_REFERENCE_FAILURE_PREFIX}{exc.message}"
    images = list(resolved.message_images)
    gallery_tokens = [
        item["token"] for item in resolved.sources if item["kind"] == "gallery"
    ]
    for token, path in zip(gallery_tokens, resolved.gallery_files, strict=True):
        try:
            images.append(await asyncio.to_thread(_inline_gallery_file, path))
        except ValueError as exc:
            return None, f"{_REFERENCE_FAILURE_PREFIX}{exc}：{token[:64]}"
    images.extend(resolved.user_files)
    return (
        _ToolReferences(
            images, list(resolved.avatar_reference), bool(resolved.user_files)
        ),
        None,
    )


def _apply_template(
    plugin: Any, template_id: str, prompt: str
) -> tuple[PromptTemplate | None, str, str | None]:
    """取模板并渲染提示词；模板不存在或未对模型开放时返回错误文本。"""

    store = getattr(plugin, "prompt_templates", None)
    template = store.get(template_id) if store is not None else None
    if template is None or not template.ai_enabled:
        shown = " ".join(template_id.split())[:48]
        return (
            None,
            prompt,
            f"❌ 模板 {shown} 不存在或未对模型开放；"
            "请用 gemini_image_lookup（kind=templates）查询可用模板",
        )
    cfg = getattr(plugin, "cfg", None)
    rendered = render_template(
        template,
        prompt,
        rows=getattr(cfg, "sticker_grid_rows", 4),
        cols=getattr(cfg, "sticker_grid_cols", 4),
    )
    return template, rendered, None


async def _prepare_single_request(
    plugin: Any, event: Any, kwargs: dict[str, Any], prompt: str
) -> tuple[_SingleRequest | None, str | None]:
    """校验并整理单图调用的全部入参。

    模板、model、references 和 deliver 的任何问题都在这里返回，此时还没有扣额度、
    建任务或发起生成。
    """

    template = None
    template_text = str(kwargs.get("template") or "").strip()
    if template_text:
        template, prompt, error = _apply_template(plugin, template_text, prompt)
        if error:
            return None, error

    deliver = kwargs.get("deliver") or "user"
    if deliver not in ("user", "return", "forum"):
        return None, "❌ deliver 只能是 user、return 或 forum"

    provider = model = None
    model_text = str(kwargs.get("model") or "").strip()
    if model_text:
        provider, model, recognized = parse_model_selector(
            model_text, list(getattr(plugin.cfg, "provider_candidates", []) or [])
        )
        if not recognized:
            return None, (
                "❌ 无法识别 model；请用 gemini_image_lookup（kind=models）"
                "查询可用的供应商和模型"
            )

    # options 只认 schema 里声明过的字段，其余忽略。
    declared = set(_configured_option_fields(plugin))
    options = kwargs.get("options")
    options = options if isinstance(options, dict) else {}
    negative_prompt = (
        str(options.get("negative_prompt") or "").strip()
        if "negative_prompt" in declared and "negative_prompt" in options
        else None
    )
    watermark = (
        bool(options["watermark"])
        if "watermark" in declared and "watermark" in options
        else None
    )
    quality = (
        str(options.get("quality") or "").strip()
        if "quality" in declared and "quality" in options
        else None
    )

    raw_references = kwargs.get("references") or []
    if isinstance(raw_references, str):
        raw_references = [raw_references]
    if not isinstance(raw_references, list):
        return None, "❌ references 必须是字符串数组"
    references, error = await _resolve_tool_references(plugin, event, raw_references)
    if error:
        return None, error
    if (
        template is not None
        and template.reference == "required"
        and not (references.images or references.avatars)
    ):
        return None, (
            f"❌ 模板「{template.name}」需要参考图，未启动生成；"
            '请在 references 里提供，例如 ["current"]'
        )

    raw_resolution = kwargs.get("resolution") or None
    raw_aspect_ratio = kwargs.get("aspect_ratio") or None
    explicit_size = not (_blank(raw_resolution) and _blank(raw_aspect_ratio))
    # 模板默认尺寸只补调用里没有显式给出的那一项。
    template_size = False
    if template is not None:
        if _blank(raw_resolution) and template.defaults.get("resolution"):
            raw_resolution = template.defaults["resolution"]
            template_size = True
        if _blank(raw_aspect_ratio) and template.defaults.get("aspect_ratio"):
            raw_aspect_ratio = template.defaults["aspect_ratio"]
            template_size = True
    resolution, aspect_ratio, config_value_notice = _resolve_tool_size_params(
        plugin,
        resolution=raw_resolution,
        aspect_ratio=raw_aspect_ratio,
        provider=provider,
        model=model,
    )
    has_references = bool(references.images or references.avatars)
    preserve_size = bool(getattr(plugin.cfg, "preserve_reference_image_size", False))
    # 显式尺寸与模板默认尺寸优先满足；配置默认值只约束未指定尺寸的请求。
    if explicit_size or template_size:
        preserve_size = False
    suppress_resolution = has_references and preserve_size
    if suppress_resolution:
        resolution = None
        aspect_ratio = None
    return (
        _SingleRequest(
            prompt=prompt,
            template_id=None if template is None else template.id,
            provider=provider,
            model=model,
            negative_prompt=negative_prompt,
            watermark=watermark,
            quality=quality,
            reference_images=references.images,
            avatar_reference=references.avatars,
            # 只有过了路径守卫的用户本地文件需要受信读取；画廊图已内联为 data URI，
            # 消息图本来就是内联形态。
            trusted_local=references.has_user_files,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            config_value_notice=config_value_notice,
            suppress_resolution=suppress_resolution,
            return_images=deliver in ("return", "forum"),
            forum_mode=deliver == "forum",
        ),
        None,
    )


@dataclass
class GeminiImageGenerationTool(FunctionTool[AstrAgentContext]):
    """
    Gemini 图像生成工具（触发器模式）

    当当前请求需要图像生成、绘画、改图、换风格或手办化时调用此函数。
    工具会优先在前台短时间等待，快速完成则直接返回结果，后台反向激活主 Agent 发送图片。
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

    @provider_operation("tool")
    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        """
        执行图像生成工具（触发器模式）

        Foreground-first hybrid mode for normal chats.
        When deliver="return", the tool waits synchronously and returns image paths.
        """
        self.refresh_from_plugin()
        event = context.context.event
        plugin = self.plugin
        if not plugin:
            return "❌ 工具未正确初始化，缺少插件实例引用"

        raw_batch_tasks = kwargs.get("batch_tasks")
        is_batch = raw_batch_tasks is not None
        prompt = str(kwargs.get("prompt") or "").strip()
        if (
            not is_batch
            and not prompt
            and not str(kwargs.get("template") or "").strip()
        ):
            return "❌ 缺少必填参数：单图模式的图像描述不能为空"
        if is_batch and kwargs.get("deliver") == "return":
            return "❌ batch_tasks 固定进入后台，不能与 deliver=return 同时使用"

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
            v2_service = plugin._v2_service_for(
                getattr(event, "unified_msg_origin", None)
            )
            if v2_service is not None:
                return await _run_batch_v2(plugin, event, v2_service, prepared_items)
            allowed, limit_message = await plugin._check_and_consume_limit(
                event, cost=len(prepared_items)
            )
            if not allowed:
                return limit_message or "请求过于频繁，请稍后再试"
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
            tracker = getattr(plugin, "generation_tracker", None)
            parent_job_id = None
            if tracker is not None:
                try:
                    parent = await tracker.begin(
                        source="llm_batch",
                        prompt="",
                        params={},
                        requester=requester_from_event(event),
                        requested_images=sum(
                            item["image_count"] for item in prepared_items
                        ),
                    )
                    parent_job_id = parent["job_id"]
                except Exception as exc:
                    logger.warning(f"[生成追踪] 创建批量父记录失败: {exc}")
            plugin.background_task_manager.attach(
                task_id,
                run_batch_job(
                    plugin, event, task_id, prepared_items, parent_job_id=parent_job_id
                ),
            )
            return _background_json(task_id, batch_mode, message)

        single, error = await _prepare_single_request(plugin, event, kwargs, prompt)
        if error:
            return error
        prompt = single.prompt
        template_id = single.template_id
        provider = single.provider
        model = single.model
        negative_prompt = single.negative_prompt
        watermark = single.watermark
        quality = single.quality
        reference_images = single.reference_images
        avatar_reference = single.avatar_reference
        resolution = single.resolution
        aspect_ratio = single.aspect_ratio
        config_value_notice = single.config_value_notice
        suppress_resolution = single.suppress_resolution
        return_images = single.return_images
        ref_count = len(reference_images)
        avatar_count = len(avatar_reference)
        has_references = bool(ref_count or avatar_count)
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
        # v2 灰度：额度、生成与后台投递都归调度器；以下 legacy 流程保持原样。
        v2_service = plugin._v2_service_for(getattr(event, "unified_msg_origin", None))
        if v2_service is not None:
            return await _run_single_v2(
                plugin,
                event,
                v2_service,
                prompt=prompt,
                reference_images=reference_images,
                avatar_reference=avatar_reference,
                resolution=resolution,
                aspect_ratio=aspect_ratio,
                provider=provider,
                model=model,
                negative_prompt=negative_prompt,
                watermark=watermark,
                quality=quality,
                suppress_resolution=suppress_resolution,
                return_images=return_images,
                config_value_notice=config_value_notice,
                forum_mode=single.forum_mode,
                trusted_local=single.trusted_local,
                template_id=template_id,
            )
        allowed, limit_message = await plugin._check_and_consume_limit(event)
        if not allowed:
            return limit_message or "请求过于频繁，请稍后再试"
        request_routing_mode = routing_mode(provider, model)

        # 日志记录（仅记录长度和参数摘要，避免记录用户原始内容）
        prompt_len = len(prompt)
        logger.info(
            f"[工具调用] 启动图像生成任务："
            f"提示词长度={prompt_len} 参考图={ref_count} 张 头像={avatar_count} 张 "
            f"尺寸/分辨率={resolution} 比例={aspect_ratio} 仅返回路径={return_images}"
        )

        # ========== deliver=return：同步等待生成完成 ==========
        if return_images:
            logger.info("[后台任务] 仅返回路径：同步等待图片生成完成……")

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
                    template_id=template_id,
                )
                job_id = (
                    plugin.image_generator.get_request_stats().get("job_id")
                    if success
                    else None
                )
                return _format_returned_images(
                    success,
                    result_data,
                    config_value_notice,
                    generation_reference_note(plugin, job_id),
                    forum=single.forum_mode,
                )

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
            template_id=template_id,
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
                    notify_llm=True,
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
                            llm_notice=_joined_notice(
                                config_value_notice,
                                generation_reference_note(
                                    plugin, request_stats.get("job_id")
                                ),
                            ),
                        ),
                        timeout=30,  # 下载最多给 30 秒
                    )
                    return result
                except asyncio.TimeoutError:
                    logger.warning(
                        "[前台等待] 构建 CallToolResult 超时（代理下载慢），转后台反向激活主 Agent 发送图片"
                    )

                    # 生成已完成，创建一个立即完成的 task 包装结果，走后台反向激活主 Agent 发送图片
                    async def _already_done():
                        return (True, result_data, request_stats)

                    done_task = asyncio.create_task(_already_done())
                    result_message = (
                        "图片已生成，前台结果处理超时，转入后台反向激活；"
                        "收到图片 URL/路径后请调用 send_message_to_user 发送图片。"
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
                        notify_llm=True,
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
                notify_llm=True,
            )
            return _background_json(task_id, request_routing_mode, message)
        except Exception as e:
            logger.error(f"[前台等待] 图像生成异常：{e}", exc_info=True)
            return f"❌ 图片生成过程中出错：{str(e)}"


# 保留旧的辅助函数以保持向后兼容（已弃用）
@provider_operation("legacy_tool")
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

    prompt = str(prompt or "").strip()
    if not prompt:
        return ["❌ 图像描述不能为空"]

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

    reference_error = _reference_admission_error(
        plugin,
        reference_images,
        avatar_reference,
        references_required=include_ref_images or include_avatar,
        avatar_required=include_avatar,
    )
    if reference_error:
        return [f"❌ {reference_error}"]

    logger.info(
        f"[工具调用] 收集到参考图：消息 {len(reference_images)} 张，"
        f"头像 {len(avatar_reference)} 张"
    )

    if not _matching_candidates(
        plugin,
        provider=None,
        model=None,
        has_reference_images=bool(reference_images or avatar_reference),
        negative_prompt=None,
        watermark=None,
        quality=None,
    ):
        return ["❌ 没有匹配本次请求能力的供应商或模型"]
    allowed, limit_message = await plugin._check_and_consume_limit(event)
    if not allowed:
        return [limit_message or "请求过于频繁，请稍后再试。"]

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

        log_thought_signature_debug(thought_signature, scene="Tool结果已丢弃")
        locations = image_location_list(image_urls, image_paths)
        results: list[str] = []
        if text_content:
            results.append(sanitize_text(text_content) or "")
        if locations:
            results.append(
                "生成图片位置（优先使用本地路径）：\n"
                + "\n".join(
                    f"图片 {index}: {location}"
                    for index, location in enumerate(locations, start=1)
                )
            )
        return results or ["✅ 图片已生成"]

    # 失败情况
    error_msg = (
        format_error_message(result_data)
        if isinstance(result_data, str)
        else "图像生成失败"
    )
    return [error_msg]
