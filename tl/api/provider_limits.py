"""各 provider 参考图与提示词等关键上限的集中定义。

修改前请确认对应官方文档,避免运行时被服务端拒绝。
"""

from __future__ import annotations

from typing import Final

# 参考图最大数量(含编辑/识图/续图场景)。
MAX_REFERENCE_IMAGES_GOOGLE: Final[int] = 14
MAX_REFERENCE_IMAGES_DOUBAO: Final[int] = 14
MAX_REFERENCE_IMAGES_OPENAI_COMPAT: Final[int] = 6
MAX_REFERENCE_IMAGES_MINIMAX: Final[int] = 9
