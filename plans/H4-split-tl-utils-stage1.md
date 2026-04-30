# H4 — 拆分 tl_utils.py(分阶段;本阶段:format_error_message)

> 状态: 第一阶段实施
> 调整: 原 plan 设想一次拆 5 个文件,但许多内部函数互相引用(`AvatarManager` ↔ `download_qq_avatar` ↔ `get_plugin_data_dir`,缓存 ↔ base64 编码),激进拆分会引入循环 import 与高回归风险。
>
> 改为 **分阶段**:本阶段先抽出最干净、零内部依赖的 `format_error_message`(140 行,纯字符串匹配)。后续阶段(M1/M2/...)按解耦难度依次进行,均保留 tl_utils.py 的 re-export 以维持向后兼容。

## 本阶段范围

新建 `tl/format_error.py`:整体迁移 `format_error_message(error)` 函数及其 docstring。

`tl/tl_utils.py` 改为:
```python
# 向后兼容 re-export;新代码请直接 from .format_error import format_error_message
from .format_error import format_error_message  # noqa: F401
```
位置:文件末尾(替代原函数定义)。

## 不破坏

- `from .tl_utils import format_error_message` 与 `from ..tl_utils import format_error_message` 全部仍有效(经 re-export)
- `main.py` / `tl/llm_tools.py` 现有 import 行无需修改
- 行为完全一致(代码原样移植)

## 后续阶段(本次不做)

| 阶段 | 目标拆出 | 难度 | 阻碍 |
|---|---|---|---|
| H4-2 | `tl/avatar.py` (AvatarManager + download_qq_avatar + get_plugin_data_dir) | 中 | 三函数互相引用,需要一起搬 |
| H4-3 | `tl/image_cache.py` (_check/_save/cleanup_old_images) | 中 | 与 `_build_image_path` / `get_temp_dir` 耦合 |
| H4-4 | `tl/base64_utils.py` (is_valid_base64_image_str / encode_file_to_base64 / save_base64_image) | 中 | 7+ 文件 import |
| H4-5 | `tl/source_extractor.py` (collect_image_sources / resolve_image_source_to_path / normalize_image_input) | 高 | 业务最复杂,~500 行,多处嵌套异步 |

## 验证

1. ruff format + check (CI)
2. compileall
3. grep 确认 `format_error_message` 调用方无失败 import
