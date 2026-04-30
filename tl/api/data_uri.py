"""data URI / base64 公共助手,供各 provider 复用。

抽取自 doubao/minimax/openai_compat 中重复的 base64 与 data URI 处理片段。
不做严格的解码校验(由 provider 或服务端兜底),仅做形态判断与拼接。
"""

from __future__ import annotations

import re

_BASE64_PATTERN = re.compile(r"^[A-Za-z0-9+/=_-]+$")


def format_data_uri(b64_data: str, mime_type: str | None = None) -> str:
    """拼接标准 data URI:``data:<mime>;base64,<裸 base64>``。

    若入参已包含 ``data:image/`` 前缀则原样返回。
    未指定 ``mime_type`` 时回退为 ``image/png``。
    """
    cleaned = (b64_data or "").strip()
    if cleaned.startswith("data:image/"):
        return cleaned
    return f"data:{mime_type or 'image/png'};base64,{cleaned}"


def strip_data_uri_prefix(value: str) -> str:
    """剥离 data URI 前缀,返回裸 base64 字符串。

    无 ``;base64,`` 分隔符时返回原字符串(已 strip)。
    """
    cleaned = (value or "").strip()
    if ";base64," in cleaned:
        _, _, cleaned = cleaned.partition(";base64,")
    return cleaned.strip()


def looks_like_base64(value: str, *, min_length: int = 64) -> bool:
    """启发式判断字符串是否像 base64。

    不做严格解码校验,仅排除明显非 base64 输入(URL、过短、含空白等)。
    若包含空白字符会先合并再判断字符集。
    """
    v = (value or "").strip()
    if not v or len(v) < min_length:
        return False
    if v.startswith(("http://", "https://")):
        return False
    if any(ws in v for ws in (" ", "\n", "\r")):
        v = "".join(v.split())
    return bool(_BASE64_PATTERN.match(v))
