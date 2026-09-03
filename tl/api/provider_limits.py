"""各 provider 参考图与提示词等关键上限的集中定义。

修改前请确认对应官方文档,避免运行时被服务端拒绝。
"""

from __future__ import annotations

from typing import Final

# 参考图最大数量(含编辑/识图/续图场景)。
MAX_REFERENCE_IMAGES_GOOGLE: Final[int] = 14
# Interactions API 官方上限 14 张（lite 14 / flash 10+4 / pro 6+5+3 按模型分层）
MAX_REFERENCE_IMAGES_GEMINI_INTERACTIONS: Final[int] = 14
MAX_REFERENCE_IMAGES_DOUBAO: Final[int] = 14
MAX_REFERENCE_IMAGES_DOUBAO_SEEDREAM_5_PRO: Final[int] = 10
MAX_REFERENCE_IMAGES_OPENAI_COMPAT: Final[int] = 6
MAX_REFERENCE_IMAGES_MINIMAX: Final[int] = 9
# qwen-image-3.0 系列编辑接口官方限定 1-3 张输入图
MAX_REFERENCE_IMAGES_DASHSCOPE_QWEN3: Final[int] = 3
# sensenova-u1.5-lite 编辑接口官方未给出上限，保守截取
MAX_REFERENCE_IMAGES_SENSENOVA_U15: Final[int] = 4
# ModelScope 编辑接口官方仅证实单图输入，保守默认 1 张（用户可在配置调高）
MAX_REFERENCE_IMAGES_MODELSCOPE: Final[int] = 1
MAX_REFERENCE_IMAGES_DASHSCOPE: Final[int] = 9
# SiliconFlow 编辑模型参考图上限：Qwen-Image-Edit-2509 最多 3 张（经典 Edit 仅 1 张，provider 内分层）
MAX_REFERENCE_IMAGES_SILICONFLOW: Final[int] = 3
