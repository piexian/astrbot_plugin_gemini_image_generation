# H6 — 抽取参考图"接收阶段"公共助手

> 状态: 待实现
> 调整: 原始 plan 设想抽公共 `build_reference_parts`,但 google 用 `inlineData/fileData`、openai_compat 用 `image_url` 结构差异极大,合并出来的函数会变成带大量分支的"假抽象"。本次仅抽取 **真正重复的"接收阶段"** ——计数 + 限流 + 起始日志输出。

## 重复的代码片段

`tl/api/google.py` ≈ L86–L96 与 `tl/api/openai_compat.py` ≈ L120–L131 与 后续会接入的 provider 都有以下 4 行雷同结构:

```python
total_ref_count = len(config.reference_images or [])
processed_ref_count = min(total_ref_count, MAX_X)
if total_ref_count > processed_ref_count:
    logger.info(f"📎 开始处理 {processed_ref_count} 张参考图片 (共配置 {total_ref_count} 张，最多处理 {MAX_X} 张)...")
else:
    logger.info(f"开始处理 {processed_ref_count} 张参考图片...")
```

## 方案

新增 `tl/api/reference_intake.py`:

```python
"""参考图接收阶段公共助手:统一的限流计数与起始日志。"""
from __future__ import annotations

from typing import Sequence

from astrbot.api import logger


def announce_reference_intake(
    references: Sequence[object] | None,
    max_count: int,
    *,
    log_prefix: str = "",
) -> tuple[int, int]:
    """计算实际处理数量并打印起始日志。

    Returns:
        (total_count, processed_count)
    """
    total = len(references or [])
    processed = min(total, max_count)
    if total <= 0:
        return total, processed
    if total > processed:
        logger.info(
            f"{log_prefix}📎 开始处理 {processed} 张参考图片 "
            f"(共配置 {total} 张，最多处理 {max_count} 张)..."
        )
    else:
        logger.info(f"{log_prefix}开始处理 {processed} 张参考图片...")
    return total, processed
```

并在 google.py / openai_compat.py 中调用替换。

## 不做的事

- **不**抽取参考图实际转换/MIME 校验/base64 编码逻辑:google 和 openai_compat 输出 schema 不同,共用会引入大量配置/分支,得不偿失
- doubao/sensenova 暂不接入(doubao 内部用 `_process_single_image` 已有自己的封装,sensenova 是文生图)
- 不修改 minimax(参考图处理逻辑差异较大)

## 验证

1. ruff format/check
2. compileall
3. 人工 diff 等价性: 仅替换计数 + 起始日志,不动循环体
