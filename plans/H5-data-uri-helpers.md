# H5 — 抽取 data URI / base64 公共助手 (修正方向)

> 状态: 待实现
> 调整: 原 plan 基于"doubao 与 minimax 行数相同 = 复制粘贴"的假设。实测两文件方法集完全不同(doubao=11 个方法,minimax=20+ 个方法),且对应不同的官方 API(Volcengine Ark Seedream vs MiniMax `/v1/image_generation`),不存在可抽取的"公共 HTTP 调用骨架"。
>
> 真正可抽取的重复:**data URI / base64 处理小工具**,在 doubao / minimax / openai_compat 三处都有近似实现。

## 实际重复

| 助手 | doubao | minimax | openai_compat |
|---|---|---|---|
| 拼接 `data:<mime>;base64,<data>` | `_format_base64_data_uri` (L343) | inline `f"data:{mime};base64,{b64}"` (L383) | inline (L257) |
| 剥离 data URI 前缀取裸 base64 | `_strip_data_uri_prefix` (L358) | inline `value.partition(";base64,")` (L668) | - |
| 检测 base64 字符串 | `_looks_like_base64` (L365) | - | - |

## 方案

新增 `tl/api/data_uri.py`:

```python
"""data URI / base64 公共助手,供各 provider 复用。"""
from __future__ import annotations

import re

_BASE64_PATTERN = re.compile(r"^[A-Za-z0-9+/=_-]+$")


def format_data_uri(b64_data: str, mime_type: str | None = None) -> str:
    """拼接标准 data URI:`data:<mime>;base64,<裸 base64>`。

    若入参已包含 `data:image/...` 前缀则原样返回。
    """
    cleaned = (b64_data or "").strip()
    if cleaned.startswith("data:image/"):
        return cleaned
    return f"data:{mime_type or 'image/png'};base64,{cleaned}"


def strip_data_uri_prefix(value: str) -> str:
    """剥离 data URI 前缀,返回裸 base64 字符串。无前缀时返回原字符串。"""
    cleaned = (value or "").strip()
    if ";base64," in cleaned:
        _, _, cleaned = cleaned.partition(";base64,")
    return cleaned.strip()


def looks_like_base64(value: str, *, min_length: int = 64) -> bool:
    """启发式判断字符串是否像 base64。

    不做严格解码校验(由 provider 侧或服务端兜底),仅排除明显非 base64 输入。
    """
    v = (value or "").strip()
    if not v or len(v) < min_length:
        return False
    if v.startswith(("http://", "https://")):
        return False
    if any(ws in v for ws in (" ", "\n", "\r")):
        v = "".join(v.split())
    return bool(_BASE64_PATTERN.match(v))
```

### 改动点

- `tl/api/doubao.py`:删除 3 个 `@staticmethod` 帮助方法,改为 module-level import(保留 `self.method` 调用语义即可改为模块函数调用)
- `tl/api/minimax.py`:`_to_image_file` 返回前 `format_data_uri(...)` 替代 inline f-string;`_strip_data_uri_prefix` 已用过的 inline 代码替换为 `strip_data_uri_prefix(...)`
- `tl/api/openai_compat.py`:`payload_url = f"data:{mime_type};base64,{cleaned}"` → `format_data_uri(cleaned, mime_type)`

## 不做的事

- 不抽取 doubao/minimax 的"任务轮询"或"参考图处理流程":两者对外 API 形态完全不同
- 不在 `BaseProvider` 上挂这些方法(避免与 Protocol 设计冲突;模块级函数更易测试)

## 验证

1. ruff format + check (CI 规则)
2. compileall
3. 人工 diff 等价性
