"""参考图接收阶段公共助手:统一的限流计数与起始日志。

各 provider 在处理参考图前都需要做"按上限截取 + 打印起始日志"的雷同动作,
本模块抽取出该公共序言,具体的转换/编码逻辑仍由各 provider 自行实现。
"""

from __future__ import annotations

from collections.abc import Sequence

from astrbot.api import logger


def announce_reference_intake(
    references: Sequence[object] | None,
    max_count: int,
    *,
    log_prefix: str = "",
) -> tuple[int, int]:
    """根据上限计算实际处理数量并打印起始日志。

    Args:
        references: 参考图列表(任意可计数序列;None 表示无参考图)
        max_count: 该 provider 允许的最大数量
        log_prefix: 可选日志前缀(如 "[google] ")

    Returns:
        (total_count, processed_count)。total_count 为入参实际长度,
        processed_count = min(total_count, max_count)。
    """
    total = len(references or [])
    processed = min(total, max_count)
    if total <= 0:
        return total, processed
    if total > processed:
        logger.info(
            f"{log_prefix}📎 开始处理 {processed} 张参考图片 "
            f"(共配置 {total} 张,最多处理 {max_count} 张)..."
        )
    else:
        logger.info(f"{log_prefix}开始处理 {processed} 张参考图片...")
    return total, processed
