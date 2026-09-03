"""后台生图失败时，走 AstrBot 官方后台任务回灌链路通知用户。

复用框架 _wake_main_agent_for_background_result 的形态：CronMessageEvent +
主 agent（仅挂 send_message_to_user 工具）+ persist_agent_history。
任何一步失败仅记日志（静默降级）；开关关闭或官方 API 缺失时回退直发。
"""

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


def notify_llm_enabled(plugin: Any) -> bool:
    cfg = getattr(plugin, "cfg", None)
    return bool(getattr(cfg, "background_failure_notify_llm", True))


async def notify_llm_background_failure(
    plugin: Any,
    event: Any,
    failure_summary: str,
    *,
    scene: str,
    task_id: str | None = None,
) -> bool:
    """走官方回灌链路把失败结果交给主 agent 告知用户；返回是否送达。"""
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
            streaming_response=False,
            provider_settings=provider_settings,
        )

        req = ProviderRequest()
        conv = await _get_session_conv(event=cron_event, plugin_context=context)
        req.conversation = conv
        history = json.loads(conv.history)
        if history:
            req.contexts = history
            context_dump = req._print_friendly_context()
            req.contexts = []
            req.system_prompt += (
                "\n\nBellow is you and user previous conversation history:\n"
                f"{context_dump}"
            )
        req.system_prompt += BACKGROUND_TASK_RESULT_WOKE_SYSTEM_PROMPT.format(
            background_task_result=json.dumps(
                extras["background_task_result"], ensure_ascii=False
            )
        )
        req.prompt = (
            "请按照系统指令行事，用与用户历史对话一致的语言。"
            "你必须调用 `send_message_to_user` 工具，把这条后台任务的失败结果简短告知用户，"
            "否则用户将看不到任何结果。"
            "若同一任务已连续多次失败，请先询问用户是否继续，再决定是否重试。"
        )
        req.func_tool = ToolSet()
        req.func_tool.add_tool(
            context.get_llm_tool_manager().get_builtin_tool(SendMessageToUserTool)
        )

        result = await build_main_agent(
            event=cron_event, plugin_context=context, config=config, req=req
        )
        if not result:
            raise RuntimeError("主 agent 构建失败")
        runner = result.agent_runner
        async for _ in runner.step_until_done(30):
            # agent 通过 send_message_to_user 工具把结果发给用户
            pass
        await persist_agent_history(
            context.conversation_manager,
            event=cron_event,
            req=req,
            summary_note=(
                f"[BackgroundTask] 图像生成任务 {task_id or '-'} 失败：{failure_summary}"
            ),
        )
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
        return await notify_llm_background_failure(
            plugin, event, failure_summary, scene=scene, task_id=task_id
        )
    try:
        await event.send(event.plain_result(failure_summary))
    except Exception as e:
        logger.warning(f"[{scene}] 发送错误消息失败: {e}")
        return False
    return True
