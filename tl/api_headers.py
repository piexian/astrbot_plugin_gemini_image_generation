"""HTTP 请求头中 API Key 的读写辅助。

抽取自 ``tl/tl_api.py`` 的 ``GeminiAPIClient`` 内部 staticmethod,
拆为模块级函数以便复用与单元测试。
"""

from __future__ import annotations


def extract_api_key_from_headers(headers: dict[str, str]) -> str | None:
    """从请求头中提取 API Key (键名大小写不敏感)。

    支持的头格式:
    - ``Authorization: Bearer <key>``
    - ``x-goog-api-key: <key>`` (Google API)
    - ``X-Api-Key`` / ``X-API-Key`` / ``x-api-key`` (通用格式)
    - ``Api-Key`` / ``api-key`` / ``API-Key`` (部分反代使用)
    """
    for k, v in headers.items():
        kl = k.lower()
        sval = str(v or "")
        if kl == "authorization":
            if sval.lower().startswith("bearer "):
                return sval[7:].strip() or None
        elif kl == "x-goog-api-key":
            return sval or None
        elif kl in ("x-api-key", "api-key"):
            return sval or None
    return None


def apply_api_key_to_headers(headers: dict[str, str], api_key: str) -> None:
    """将 API Key 应用到请求头中 (覆盖现有的 key 相关头，原地修改，键名大小写不敏感)。"""
    for k in list(headers.keys()):
        kl = k.lower()
        if kl == "authorization":
            headers[k] = f"Bearer {api_key}"
        elif kl in ("x-goog-api-key", "x-api-key", "api-key"):
            headers[k] = api_key
