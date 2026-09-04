"""后台生图失败时，重新激活 AstrBot 主 Agent 处理结果。"""

from __future__ import annotations

import json
from typing import Any

from astrbot.api import logger

try:  # 官方内部 API（_get_session_conv 为私有函数），版本漂移风险防御
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

    _OFFICIAL_API_READY = True
except ImportError as e:  # pragma: no cover - 仅低版本框架触发
    logger.warning(f"[后台失败回灌] AstrBot 官方回灌 API 导入失败，通知将静默降级: {e}")
    _OFFICIAL_API_READY = False
    # except 分支显式绑 None：保证模块符号恒存在，便于调用方/测试安全引用
    ToolSet = MainAgentBuildConfig = _get_session_conv = build_main_agent = None
    BACKGROUND_TASK_RESULT_WOKE_SYSTEM_PROMPT = CronMessageEvent = None
    MessageSession = ProviderRequest = SendMessageToUserTool = None
    persist_agent_history = None


def _build_notice_prompt(task_id: str | None, failure_summary: str) -> str:
    tid = f"（任务号 {task_id}）" if task_id else ""
    return f"后台图片生成任务{tid}失败。失败摘要：{failure_summary}"


def _build_safe_fallback_notice(task_id: str | None) -> str:
    tid = f"（任务号 {task_id}）" if task_id else ""
    return f"后台图片生成任务{tid}失败，请稍后重试。"


def notify_llm_enabled(plugin: Any) -> bool:
    cfg = getattr(plugin, "cfg", None)
    return bool(getattr(cfg, "background_failure_notify_llm", True))


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


async def notify_llm_background_failure(
    plugin: Any,
    event: Any,
    failure_summary: str,
    *,
    scene: str,
    task_id: str | None = None,
) -> bool:
    """按官方后台结果回灌路径唤醒主 Agent；返回是否实际送达。"""
    notice = _build_notice_prompt(task_id, failure_summary)
    try:
        if not _OFFICIAL_API_READY:
            raise RuntimeError("AstrBot 官方回灌 API 不可用")
        context = getattr(plugin, "context", None)
        umo = getattr(event, "unified_msg_origin", None)
        if context is None or not umo:
            raise RuntimeError("缺少插件 context 或会话标识")

        task_result = {"task_id": task_id or "", "result": failure_summary}
        extras = {"background_task_result": task_result}

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
            tool_call_timeout=60,
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
        req.prompt = (
            "Proceed according to your system instructions. "
            "Output using same language as previous conversation. "
            "If you need to deliver the result to the user immediately, "
            "you MUST use `send_message_to_user` tool to send the message directly to the user, "
            "otherwise the user will not see the result. "
            "After completing your task, summarize and output your actions and results. "
        )
        if not req.func_tool:
            req.func_tool = ToolSet()
        req.func_tool.add_tool(
            context.get_llm_tool_manager().get_builtin_tool(SendMessageToUserTool)
        )

        result = await build_main_agent(
            event=cron_event,
            plugin_context=context,
            config=config,
            req=req,
        )
        if not result:
            raise RuntimeError("主 Agent 构建失败")
        async for _ in result.agent_runner.step_until_done(
            _configured_agent_max_steps(cfg)
        ):
            pass

        await persist_agent_history(
            context.conversation_manager,
            event=cron_event,
            req=req,
            summary_note=(
                f"[BackgroundTask] 图像生成任务 {task_id or '-'} 失败：{failure_summary}"
            ),
        )
        if not getattr(cron_event, "_has_send_oper", False):
            logger.warning(f"[{scene}] 后台通知 agent 未成功调用发送工具")
            return False
        return True
    except Exception as e:
        logger.warning(f"[{scene}] 后台失败回灌未送达，已静默降级: {e}")
        return False


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
        await event.send(event.plain_result(direct_notice))
    except Exception as e:
        logger.warning(f"[{scene}] 发送错误消息失败: {e}")
        return False
    return True
