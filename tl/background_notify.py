"""后台生图完成后，重新激活 AstrBot 主 Agent 处理结果。"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from typing import Any

from astrbot.api import logger

from . import astrbot_compat
from .core.errors import redact_text
from .core.states import TERMINAL_STATES, JobState
from .llm_output import sanitize_task_result
from .plugin_config import get_session_tool_timeout

try:
    from astrbot.core.star.session_llm_manager import SessionServiceManager
except ImportError:
    SessionServiceManager = None

try:
    from astrbot.core.agent.input import PlainInput
except ImportError:
    PlainInput = None


active_event_registry = None
if astrbot_compat.has_background_stop_registry():
    try:
        from astrbot.core.utils.active_event_registry import active_event_registry
    except ImportError as e:
        logger.warning(
            f"[后台任务] active_event_registry 不可用，跳过停止生命周期注册: {e}"
        )
ToolSet = MainAgentBuildConfig = _get_session_conv = build_main_agent = None
BACKGROUND_TASK_RESULT_WOKE_SYSTEM_PROMPT = CronMessageEvent = None
MessageSession = ProviderRequest = SendMessageToUserTool = None
persist_agent_history = None

if PlainInput is not None:
    _OFFICIAL_API_READY = True
else:
    try:  # AstrBot 4.29 官方内部 API（_get_session_conv 为私有函数）
        from astrbot.core.agent.tool import ToolSet
        from astrbot.core.astr_main_agent import (
            MainAgentBuildConfig,
            _get_session_conv,
            build_main_agent,
        )
        from astrbot.core.astr_main_agent_resources import (
            BACKGROUND_TASK_RESULT_WOKE_SYSTEM_PROMPT,
        )
        from astrbot.core.cron.events import CronMessageEvent
        from astrbot.core.platform.message_session import MessageSession
        from astrbot.core.provider.entites import ProviderRequest
        from astrbot.core.tools.message_tools import SendMessageToUserTool
        from astrbot.core.utils.history_saver import persist_agent_history
    except ImportError as e:  # pragma: no cover - 仅不支持的框架版本触发
        logger.warning(
            f"[后台结果回灌] AstrBot 4.30/4.29 回灌 API 均不可用，无法激活主 Agent: {e}"
        )
        _OFFICIAL_API_READY = False
    else:
        _OFFICIAL_API_READY = True


_BACKGROUND_DELIVERY_PROMPT = (
    "Proceed according to your system instructions. "
    "Output using same language as previous conversation. "
    "If you need to deliver the result to the user immediately, "
    "you MUST use `send_message_to_user` tool to send the message directly to the user, "
    "otherwise the user will not see the result. "
    "For generated images in image_urls/image_paths (including batch items), "
    "you MUST call send_message_to_user with messages containing "
    "{type: 'image', url: '<image URL>'} or {type: 'image', path: '<local path>'}. "
    "Send the generated images to the current session, leaving session empty. "
    "Do not merely describe completion or output Markdown image links. "
    "Use the supplied locations exactly; do not regenerate completed images. "
    "After completing your task, summarize and output your actions and results. "
)


def _build_notice_prompt(task_id: str | None, failure_summary: str) -> str:
    tid = f"（任务号 {task_id}）" if task_id else ""
    return f"后台图片生成任务{tid}失败。失败摘要：{failure_summary}"


def _build_safe_fallback_notice(task_id: str | None) -> str:
    tid = f"（任务号 {task_id}）" if task_id else ""
    return f"后台图片生成任务{tid}失败，请稍后重试。"


def notify_llm_enabled(plugin: Any) -> bool:
    cfg = getattr(plugin, "cfg", None)
    return bool(getattr(cfg, "background_failure_notify_llm", True))


def _event_stopped(event: Any) -> bool:
    get_extra = getattr(event, "get_extra", None)
    if callable(get_extra):
        try:
            if get_extra("agent_stop_requested") is True:
                return True
            signal = get_extra("_background_stop_signal")
            if signal is not None and signal.is_set():
                return True
        except Exception:
            return True
    checker = getattr(event, "is_stopped", None)
    if checker is None:
        return False
    try:
        value = checker() if callable(checker) else checker
        return value is not False
    except Exception:
        return True


async def register_background_job_stop(
    event: Any, service: Any, job_id: str
) -> asyncio.Task[None] | None:
    """把 LLM 后台 Job 的生成与投递阶段登记到 AstrBot 停止生命周期。"""
    if active_event_registry is None:
        return None

    cleanup_started = False

    async def cancel_generation() -> None:
        nonlocal cleanup_started
        if cleanup_started:
            return
        cleanup_started = True
        try:
            snapshot = await service.get_status(job_id)
            state = getattr(snapshot, "state", None)
            if (
                snapshot is None
                or state in TERMINAL_STATES
                or state in {JobState.RESULT_READY, JobState.DELIVERY_PENDING}
            ):
                return
            await asyncio.shield(service.cancel(job_id))
        except Exception as exc:
            logger.warning(
                f"[后台任务] 停止生成失败: job_id={job_id}, error={redact_text(exc)[:240]}"
            )

    if _event_stopped(event):
        await cancel_generation()
        return None

    async def supervise() -> None:
        try:
            await service.wait(job_id)
        except asyncio.CancelledError:
            await cancel_generation()
            raise
        except Exception as exc:
            logger.debug(
                f"[后台任务] 停止监管任务结束异常: job_id={job_id}, error={redact_text(exc)[:240]}"
            )

    task = asyncio.create_task(supervise(), name=f"llm-background-job:{job_id}")
    active_event_registry.register_background_task(event, task)
    # 登记时已经停止的 Task 可能在协程首次运行前被取消，需要补上生成取消。
    await asyncio.sleep(0)
    if task.cancelled():
        await cancel_generation()
    return task


async def _background_delivery_allowed(
    event: Any,
    *,
    require_llm: bool,
    context: Any = None,
    target_umo: str | None = None,
) -> bool:
    """Direct background callbacks do not pass through the session pipeline."""
    umo = target_umo or getattr(event, "unified_msg_origin", None)
    if not isinstance(umo, str) or not umo or _event_stopped(event):
        return False
    if astrbot_compat.ASTRBOT_VERSION is None:
        logger.warning("[后台结果回灌] 无法识别 AstrBot 版本，跳过通知")
        return False
    try:
        if astrbot_compat.has_modern_session_config():
            if context is None:
                return False
            cfg = context.get_config(umo=umo) or {}
            platform_settings = cfg.get("platform_settings") or {}
            blacklist = {
                str(item).strip()
                for item in (platform_settings.get("id_blacklist") or [])
                if item is not None
            }
            blacklist.discard("")
            get_group_id = (
                getattr(event, "get_group_id", None) if target_umo is None else None
            )
            group_id = get_group_id() if callable(get_group_id) else None
            normalized_group_id = str(group_id).strip() if group_id is not None else ""
            if umo in blacklist or (
                normalized_group_id and normalized_group_id in blacklist
            ):
                return False
            provider_settings = cfg.get("provider_settings") or {}
            if require_llm and not provider_settings.get("enable", True):
                return False
        else:
            if SessionServiceManager is None:
                return False
            if astrbot_compat.has_async_session_manager():
                session_enabled = await SessionServiceManager.is_session_enabled(umo)
            else:
                session_enabled = SessionServiceManager.is_session_enabled(umo)
            if session_enabled is not True:
                return False
            if require_llm:
                if astrbot_compat.has_async_session_manager():
                    llm_enabled = (
                        await SessionServiceManager.is_llm_enabled_for_session(umo)
                    )
                else:
                    llm_enabled = SessionServiceManager.is_llm_enabled_for_session(umo)
                if llm_enabled is not True:
                    return False
    except Exception as exc:
        logger.warning(
            f"[后台结果回灌] 无法确认会话服务状态，跳过通知: {redact_text(exc)[:240]}"
        )
        return False
    return not _event_stopped(event)


class _BackgroundSendContext:
    """Delegate only this notification tool's final send to a checked boundary."""

    def __init__(
        self, context: Any, event: Any, receipt: dict[str, bool] | None = None
    ):
        self._context = context
        self._event = event
        self._receipt = receipt

    def __getattr__(self, name: str) -> Any:
        return getattr(self._context, name)

    async def send_message(self, session: Any, message: Any, *args, **kwargs):
        if not await _background_delivery_allowed(
            self._event, context=self._context, require_llm=True
        ):
            return False
        target = str(session)
        if target != self._event.unified_msg_origin:
            if not await _background_delivery_allowed(
                self._event,
                context=self._context,
                require_llm=True,
                target_umo=target,
            ):
                return False
        sent = await self._context.send_message(session, message, *args, **kwargs)
        if (
            sent
            and target == self._event.unified_msg_origin
            and self._receipt is not None
        ):
            self._receipt["image_sent"] |= any(
                getattr(part, "type", None) == "Image"
                for part in (getattr(message, "chain", None) or ())
            )
        return sent


def _guard_background_send_tool(
    tool: Any, event: Any, receipt: dict[str, bool] | None = None
) -> Any:
    """Keep the shared builtin unchanged; the copied tool gets a local context."""
    if getattr(tool, "handler", None) is not None:
        raise RuntimeError("后台发送工具 handler 接口不受支持")
    original_call = getattr(tool, "call", None)
    if not callable(original_call):
        raise RuntimeError("后台发送工具 call 接口不可用")
    guarded = copy.copy(tool)

    async def call(context: Any, **kwargs):
        if not await _background_delivery_allowed(
            event, context=context.context.context, require_llm=True
        ):
            return "error: background session is paused or unavailable"
        wrapped = copy.copy(context)
        wrapped.context = copy.copy(context.context)
        wrapped.context.context = _BackgroundSendContext(
            context.context.context, event, receipt
        )
        return await original_call(wrapped, **kwargs)

    guarded.call = call
    return guarded


def _configured_agent_max_steps(cfg: dict[str, Any]) -> int:
    value = (
        cfg.get("agent_runner", {})
        .get("config", {})
        .get("misc", {})
        .get("max_steps", 30)
    )
    if isinstance(value, bool):
        return 30
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return 30


async def _has_successful_send_tool(run: Any, umo: str) -> bool:
    """仅接受当前会话的 send_message_to_user 成功返回作为图片送达回执。"""
    tool_names: dict[str, str] = {}
    delivered = False
    async for runtime_event in run.events():
        payload = runtime_event.payload
        if runtime_event.type == "tool.started":
            tool_names[payload.execution_id] = payload.name
            continue
        if runtime_event.type != "tool.finished":
            continue
        if tool_names.get(payload.execution_id) != "send_message_to_user":
            continue
        status = getattr(payload, "status", None)
        if getattr(status, "value", status) != "completed":
            continue
        content = getattr(payload, "content", ()) or ()
        result_text = "".join(getattr(part, "text", "") or "" for part in content)
        if result_text.strip() == f"Message sent to session {umo}":
            delivered = True
    return delivered


async def notify_llm_background_failure(
    plugin: Any,
    event: Any,
    failure_summary: str,
    *,
    scene: str,
    task_id: str | None = None,
) -> bool:
    return await notify_llm_background_result(
        plugin,
        event,
        {"task_id": task_id or "", "result": failure_summary},
        notice=_build_notice_prompt(task_id, failure_summary),
        scene=scene,
        require_send_tool=False,
    )


async def notify_llm_background_result(
    plugin: Any,
    event: Any,
    task_result: dict[str, Any],
    *,
    notice: str,
    scene: str,
    require_send_tool: bool = True,
) -> bool:
    """按官方后台结果回灌路径唤醒主 Agent；返回是否确认送达。"""
    task_id = str((task_result or {}).get("task_id") or "") or None
    umo = getattr(event, "unified_msg_origin", None)
    started_at = time.perf_counter()
    logger.info(f"[{scene}] 后台结果回灌开始: task_id={task_id}, umo={umo}")
    try:
        if not _OFFICIAL_API_READY:
            raise RuntimeError("AstrBot 官方回灌 API 不可用")
        context = getattr(plugin, "context", None)
        umo = getattr(event, "unified_msg_origin", None)
        if context is None or not umo:
            raise RuntimeError("缺少插件 context 或会话标识")

        if not await _background_delivery_allowed(
            event, context=context, require_llm=True
        ):
            return False
        extras = {"background_task_result": sanitize_task_result(task_result)}

        if PlainInput is not None:
            agent_service = getattr(context, "agent", None)
            if agent_service is None:
                raise RuntimeError("AstrBot 4.30 AgentService 不可用")
            background_result = json.dumps(
                extras["background_task_result"], ensure_ascii=False
            )
            user_prompt = (
                f"{notice}\n\n"
                "# BACKGROUND TASK CONTEXT\n"
                f"{background_result}\n\n"
                f"{_BACKGROUND_DELIVERY_PROMPT}"
            )
            agent_session = getattr(event, "agent_session", None)
            run = await agent_service.run_turn(
                umo=umo,
                input_message=[PlainInput(text=user_prompt)],
                send_to_user=True,
                source_event=event,
                session_id=getattr(agent_session, "session_id", None),
            )
            await run.result()
            if require_send_tool:
                delivered = await _has_successful_send_tool(run, umo)
            else:
                run_event = getattr(run, "_event", None)
                delivered = bool(getattr(run_event, "_has_send_oper", False))
            if not delivered:
                logger.warning(
                    f"[{scene}] 后台回灌未确认发送: task_id={task_id}, umo={umo}"
                )
                return False
            await _bump_delivery_count(plugin, task_result, scene=scene)
            logger.info(
                f"[{scene}] 后台结果回灌完成: task_id={task_id}, umo={umo}, "
                f"elapsed={time.perf_counter() - started_at:.2f}s, sent=True"
            )
            return True
        session = MessageSession.from_str(umo)
        cron_event = CronMessageEvent(
            context=context,
            session=session,
            message=notice,
            extras=extras,
            message_type=session.message_type,
        )
        cron_event.role = getattr(event, "role", None)

        cfg = context.get_config(umo=umo) or {}
        provider_settings = cfg.get("provider_settings") or {}
        config = MainAgentBuildConfig(
            tool_call_timeout=get_session_tool_timeout(context, umo),
            streaming_response=provider_settings.get("stream", False),
            provider_settings=provider_settings,
        )

        req = ProviderRequest()
        conv = await _get_session_conv(event=cron_event, plugin_context=context)
        req.conversation = conv
        req.contexts = json.loads(conv.history)
        req.system_prompt += BACKGROUND_TASK_RESULT_WOKE_SYSTEM_PROMPT.format(
            background_task_result=json.dumps(
                extras["background_task_result"], ensure_ascii=False
            )
        )
        req.prompt = _BACKGROUND_DELIVERY_PROMPT
        if not req.func_tool:
            req.func_tool = ToolSet()
        req.func_tool.add_tool(
            context.get_llm_tool_manager().get_builtin_tool(SendMessageToUserTool)
        )

        if not await _background_delivery_allowed(
            event, context=context, require_llm=True
        ):
            return False
        result = await build_main_agent(
            event=cron_event,
            plugin_context=context,
            config=config,
            req=req,
        )
        if not result:
            raise RuntimeError("主 Agent 构建失败")
        # Core may re-add the builtin while building; wrap the final request tool.
        send_tool = req.func_tool.get_tool("send_message_to_user")
        receipt = {"image_sent": False}
        req.func_tool.add_tool(_guard_background_send_tool(send_tool, event, receipt))

        def delivered() -> bool:
            if require_send_tool:
                return receipt["image_sent"]
            return bool(getattr(cron_event, "_has_send_oper", False))

        if not await _background_delivery_allowed(
            event, context=context, require_llm=True
        ):
            return False
        async for _ in result.agent_runner.step_until_done(
            _configured_agent_max_steps(cfg)
        ):
            if not await _background_delivery_allowed(
                event, context=context, require_llm=True
            ):
                if delivered():
                    break
                return False

        if not delivered():
            logger.warning(f"[{scene}] 后台通知 agent 未确认当前会话的结果发送")
            return False
        await persist_agent_history(
            context.conversation_manager,
            event=cron_event,
            req=req,
            summary_note=f"[BackgroundTask] {notice}",
        )
        if not getattr(cron_event, "_has_send_oper", False):
            logger.warning(f"[{scene}] 后台通知 agent 未成功调用发送工具")
            logger.warning(
                f"[{scene}] 后台回灌未触发发送工具: task_id={task_id}, umo={umo}"
            )
            return False
        await _bump_delivery_count(plugin, task_result, scene=scene)
        logger.info(
            f"[{scene}] 后台结果回灌完成: task_id={task_id}, umo={umo}, "
            f"elapsed={time.perf_counter() - started_at:.2f}s, sent=True"
        )
        return True
    except Exception as e:
        logger.warning(
            f"[{scene}] 后台结果回灌失败: task_id={task_id}, umo={umo}, error={redact_text(e)[:240]}"
        )
        return False


async def _bump_delivery_count(
    plugin: Any,
    task_result: dict[str, Any],
    *,
    scene: str,
) -> None:
    """回灌 agent 确认发送后累加交付计数；失败不影响发送结果。"""
    manager = getattr(plugin, "background_task_manager", None)
    if manager is None or not hasattr(manager, "record_delivery"):
        return
    task_id = str((task_result or {}).get("task_id") or "").strip()
    if not task_id:
        return
    try:
        await manager.record_delivery(task_id)
    except Exception as exc:
        logger.warning(f"[{scene}] 记录交付次数失败: {redact_text(exc)[:240]}")


async def report_background_failure(
    plugin: Any,
    event: Any,
    failure_summary: str,
    *,
    scene: str,
    task_id: str | None = None,
) -> bool:
    """工具触发的后台失败统一出口：按配置回灌 LLM，否则沿用直发。"""
    if notify_llm_enabled(plugin):
        delivered = await notify_llm_background_failure(
            plugin, event, failure_summary, scene=scene, task_id=task_id
        )
        if delivered:
            return True
        direct_notice = _build_safe_fallback_notice(task_id)
    else:
        direct_notice = failure_summary
    try:
        if not await _background_delivery_allowed(
            event, context=getattr(plugin, "context", None), require_llm=False
        ):
            return False
        await event.send(event.plain_result(direct_notice))
    except Exception as e:
        logger.warning(
            f"[{scene}] 失败结果直发兜底失败: task_id={task_id}, umo={getattr(event, 'unified_msg_origin', None)}, error={redact_text(e)[:240]}"
        )
        return False
    logger.info(
        f"[{scene}] 失败结果直发兜底成功: task_id={task_id}, umo={getattr(event, 'unified_msg_origin', None)}"
    )
    return True
