# M1 — 集中 provider 关键魔法数字

> 状态: 待实现
> 预估收益: 为 H6 铺路;新增 provider 时上限有据可查

## 问题

各 provider 的"参考图最大数量"散落且语义模糊:
- `tl/api/doubao.py` L401: `image_inputs[:14]` + 注释
- `tl/api/google.py` L99: `config.reference_images[:14]`
- `tl/api/openai_compat.py` L130: `config.reference_images[:6]`
- `tl/api/minimax.py` L55: `_MAX_IMAGES = 9` (本地常量)

不一致问题:
1. doubao/google 用裸字面量 14
2. openai_compat 裸字面量 6  
3. minimax 用模块私有常量,但与上述命名风格不同

## 方案

新建 `tl/api/provider_limits.py`(单文件,常量集合):

```python
"""各 provider 参考图与提示词等关键上限的集中定义。

修改前请确认对应官方文档,避免运行时被服务端拒绝。
"""
from __future__ import annotations

from typing import Final

# 参考图最大数量(含编辑/识图/续图)。
MAX_REFERENCE_IMAGES_GOOGLE: Final[int] = 14
MAX_REFERENCE_IMAGES_DOUBAO: Final[int] = 14
MAX_REFERENCE_IMAGES_OPENAI_COMPAT: Final[int] = 6
MAX_REFERENCE_IMAGES_MINIMAX: Final[int] = 9
```

各 provider 文件改为 `from .provider_limits import ...` 并替换字面量。

## 不做的事

- 日志截断 `[:80]` / `[:200]` / `[:1000]` 等保持原样:它们是日志预览,语义上属"省略号长度",而非业务上限。
- `_PROMPT_CHAR_SOFT_LIMIT`(sensenova)与 `DOUBAO_SEQUENTIAL_IMAGES_MAX/MIN` 不动:前者是单 provider 私有,后者已在 `plugin_config.py` 公开。
- minimax 的 `_MAX_IMAGES`:虽然是本地常量,本次将其迁入 `provider_limits.py` 以保持一致;同时保留 `_MAX_IMAGES` 作为模块级别名(向后兼容,避免破坏潜在外部引用)。

## 验证

1. `ruff format .` + `ruff check .`(CI 规则)
2. `python -m compileall tl/api`
3. 字面量 grep 为零:`14|6|9` 在 reference 上下文无遗漏
