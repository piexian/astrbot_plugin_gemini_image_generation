"""file URI → 本地路径解析（仅标准库，供各处复用）。"""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from urllib.request import url2pathname


def file_uri_to_path(uri: str) -> Path | None:
    """按 file URI 规则解析本地路径；非 file URI 或非本机主机返回 None。

    正确处理 ``file:///abs``、``file://localhost/abs`` 与百分号编码；
    Windows 下 ``file:///C:/x`` 解析为带盘符的本地路径。
    """
    parsed = urllib.parse.urlparse(str(uri).strip())
    if parsed.scheme != "file":
        return None
    host = (parsed.netloc or "").lower()
    if host and host != "localhost":
        # 远程主机路径（file://server/share）无法按本地文件读取
        return None
    return Path(url2pathname(parsed.path))
