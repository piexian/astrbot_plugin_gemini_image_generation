"""安全处理 thought signature 的辅助函数。"""

from __future__ import annotations

from astrbot.api import logger

THOUGHT_SIGNATURE_DEBUG_PREVIEW_CHARS = 80


def log_thought_signature_debug(
    thought_signature: str | None,
    *,
    scene: str,
) -> None:
    """仅输出受限预览，禁止将 thought signature 当普通文本透传。

    thought signature 是上游模型返回的 opaque 元数据，只能用于调试或在
    Provider 协议层按原样续传，不能拼进用户可见文本、Tool 结果或 LLM 上下文。
    某些模型会返回超大签名，误透传会直接导致上下文爆炸。
    """
    if not thought_signature:
        return

    preview = thought_signature[:THOUGHT_SIGNATURE_DEBUG_PREVIEW_CHARS]
    suffix = (
        "..." if len(thought_signature) > THOUGHT_SIGNATURE_DEBUG_PREVIEW_CHARS else ""
    )
    logger.debug(
        f"[{scene}] 思维签名仅调试预览: "
        f"length={len(thought_signature)} preview={preview}{suffix}"
    )
