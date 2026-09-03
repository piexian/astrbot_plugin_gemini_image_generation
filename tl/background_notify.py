"""后台生图失败时，将结果回灌给发起调用的 LLM 会话。

工具触发且转后台的任务失败时，不再向群内直发原始报错，而是让当前会话的
聊天模型重新组织语言告知用户；通知链任何一步失败仅记日志（静默降级）。
"""

from __future__ import annotations

import json
from typing import Any

from astrbot.api import logger

# 注入给回灌请求的会话历史上限，避免长对话撑爆通知请求
_CONTEXT_HISTORY_LIMIT = 10


def _build_notice_prompt(task_id: str | None, failure_summary: str) -> str:
    tid = f"（任务号 {task_id}）" if task_id else ""
    return (
        f"你之前代表用户发起的后台图片生成任务{tid}已失败。失败摘要：{failure_summary}\n"
        "请用一两句简短友好的中文告知用户生成失败，可建议稍后重试或调整参数；"
        "不要复述内部错误细节。"
    )


def notify_llm_enabled(plugin: Any) -> bool:
    cfg = getattr(plugin, "cfg", None)
    return bool(getattr(cfg, "background_failure_notify_llm", True))


async def _load_conversation_contexts(
    context: Any, umo: str
) -> list[dict[str, Any]] | None:
    """尽力取当前会话历史作为回灌上下文；API 形态不符时返回 None 跳过。"""
    try:
        conv_mgr = getattr(context, "conversation_manager", None)
        if conv_mgr is None:
            return None
        cid = await conv_mgr.get_curr_conversation_id(umo)
        if not cid:
            return None
        conv = await conv_mgr.get_conversation(umo, cid)
        if conv is None:
            return None
        # v1 Conversation.history 为 OpenAI 格式消息列表的 JSON 字符串
        raw = getattr(conv, "history", None)
        history = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(history, list):
            return None
        msgs = [
            m
            for m in history
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")
        ]
        tail = msgs[-_CONTEXT_HISTORY_LIMIT:]
        return tail or None
    except Exception as e:
        logger.debug(f"[后台失败回灌] 会话历史注入跳过: {e}")
        return None


async def _persist_notice_exchange(
    context: Any,
    umo: str,
    prompt: str,
    reply: str,
) -> None:
    """把回灌一问一答写入会话记忆，让 AI 后续轮次记得该失败；不符则跳过。"""
    try:
        conv_mgr = getattr(context, "conversation_manager", None)
        if conv_mgr is None or not hasattr(conv_mgr, "add_message_pair"):
            return
        cid = await conv_mgr.get_curr_conversation_id(umo)
        if not cid:
            return
        await conv_mgr.add_message_pair(
            cid=cid,
            user_message={"role": "user", "content": prompt},
            assistant_message={"role": "assistant", "content": reply},
        )
    except Exception as e:
        logger.debug(f"[后台失败回灌] 会话记忆写入跳过: {e}")


async def notify_llm_background_failure(
    plugin: Any,
    event: Any,
    failure_summary: str,
    *,
    scene: str,
    task_id: str | None = None,
) -> bool:
    """回灌失败通知：LLM 生成用户友好文案后发送；返回是否实际送达。"""
    prompt = _build_notice_prompt(task_id, failure_summary)
    try:
        context = getattr(plugin, "context", None)
        umo = getattr(event, "unified_msg_origin", None)
        if context is None or not umo:
            raise RuntimeError("缺少插件 context 或会话标识")
        provider_id = await context.get_current_chat_provider_id(umo)
        if not provider_id:
            raise RuntimeError("当前会话未配置聊天模型")
        contexts = await _load_conversation_contexts(context, umo)
        # 单向生成、不带任何工具，避免 AI 再次触发生图形成循环
        resp = await context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            contexts=contexts,
        )
        text = str(getattr(resp, "completion_text", "") or "").strip()
        if not text:
            raise RuntimeError("回灌生成结果为空")
        await event.send(event.plain_result(text))
        await _persist_notice_exchange(context, umo, prompt, text)
        return True
    except Exception as e:
        logger.warning(f"[{scene}] 后台失败回灌 LLM 未送达，已静默降级: {e}")
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
