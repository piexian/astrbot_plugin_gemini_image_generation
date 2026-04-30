"""HTTP 请求头中 API Key 的读写辅助。

抽取自 ``tl/tl_api.py`` 的 ``GeminiAPIClient`` 内部 staticmethod,
拆为模块级函数以便复用与单元测试。
"""

from __future__ import annotations


def extract_api_key_from_headers(headers: dict[str, str]) -> str | None:
    """从请求头中提取 API Key。

    支持的头格式:
    - ``Authorization: Bearer <key>``
    - ``x-goog-api-key: <key>`` (Google API)
    - ``X-Api-Key`` / ``X-API-Key`` / ``x-api-key`` (通用格式)
    - ``Api-Key`` / ``api-key`` / ``API-Key`` (部分反代使用)
    """
    # Bearer Token 格式(最常见)
    if "Authorization" in headers:
        auth = str(headers.get("Authorization") or "")
        if auth.lower().startswith("bearer "):
            return auth[7:]
    # Google API 格式
    if "x-goog-api-key" in headers:
        return headers.get("x-goog-api-key")
    # 通用 X-Api-Key 格式(大小写变体)
    for k in ("X-Api-Key", "X-API-Key", "x-api-key"):
        if k in headers:
            return headers.get(k)
    # 部分反代使用的 Api-Key 格式
    for k in ("Api-Key", "api-key", "API-Key"):
        if k in headers:
            return headers.get(k)
    return None


def apply_api_key_to_headers(headers: dict[str, str], api_key: str) -> None:
    """将 API Key 应用到请求头中(覆盖现有的 key 相关头,原地修改)。"""
    if "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if "x-goog-api-key" in headers:
        headers["x-goog-api-key"] = api_key
    for k in ("X-Api-Key", "X-API-Key", "x-api-key"):
        if k in headers:
            headers[k] = api_key
    # 部分反代使用的 Api-Key 格式
    for k in ("Api-Key", "api-key", "API-Key"):
        if k in headers:
            headers[k] = api_key
