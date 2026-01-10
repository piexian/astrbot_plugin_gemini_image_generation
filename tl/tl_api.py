"""
API客户端模块
提供Google Gemini和OpenAI兼容API的客户端实现
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger

from .api import get_api_provider
from .api_types import APIError, ApiRequestConfig

try:
    from .tl_utils import (
        IMAGE_CACHE_DIR,
        SUPPORTED_IMAGE_MIME_TYPES,
        coerce_supported_image,
        coerce_supported_image_bytes,
        encode_file_to_base64,
        get_plugin_data_dir,
        normalize_image_input,
        resolve_image_source_to_path,
        save_base64_image,
        save_image_data,
        save_image_stream,
    )
except ImportError:
    from pathlib import Path

    async def save_base64_image(
        base64_data: str, image_format: str = "png"
    ) -> str | None:
        """占位符函数"""
        return None

    async def save_image_data(
        image_data: bytes, image_format: str = "png"
    ) -> str | None:
        """占位符函数"""
        return None

    async def save_image_stream(
        stream_reader, image_format: str = "png", target_path=None
    ):
        return None

    def encode_file_to_base64(file_path, chunk_size: int = 65536) -> str:
        return ""

    def get_plugin_data_dir() -> Path:
        return Path(".")

    IMAGE_CACHE_DIR = get_plugin_data_dir() / "images" / "download_cache"
    SUPPORTED_IMAGE_MIME_TYPES = {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/heic",
        "image/heif",
    }

    def coerce_supported_image_bytes(mime_type, raw_bytes):
        return None, None

    def coerce_supported_image(mime_type, base64_data):
        return None, None

    async def normalize_image_input(
        image_input: Any, *, image_cache_dir=None, image_input_mode="force_base64"
    ):
        return None, None


class GeminiAPIClient:
    """遵循官方 API 规范的 Gemini API 客户端

    特性：
    - 支持 Google 官方 API 和 OpenAI API
    - 支持自定义 API Base URL（反代）
    - 支持任意模型名称
    - 遵循官方 Gemini API 规范
    """

    # Google 官方 API 默认地址
    GOOGLE_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

    # OpenAI API 默认地址
    OPENAI_API_BASE = "https://api.openai.com/v1"

    def __init__(self, api_keys: list[str]):
        """
        初始化 API 客户端

        Args:
            api_keys: API 密钥列表
        """
        self.api_keys = api_keys or []
        self.current_key_index = 0
        self._lock = asyncio.Lock()
        self.proxy = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
        )
        if self.proxy:
            logger.debug(f"检测到代理配置，使用代理: {self.proxy}")
        logger.debug(f"API 客户端已初始化，支持 {len(self.api_keys)} 个 API 密钥")
        self.verbose_logging: bool = False
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建可复用的 aiohttp 会话"""
        if self._session and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session and not self._session.closed:
                return self._session
            self._session = aiohttp.ClientSession()
            return self._session

    async def close(self):
        """关闭内部复用的 aiohttp 会话"""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    @staticmethod
    def _coerce_supported_image_bytes(
        mime_type: str | None, raw_bytes: bytes
    ) -> tuple[str | None, str | None]:
        return coerce_supported_image_bytes(mime_type, raw_bytes)

    @staticmethod
    def _coerce_supported_image(
        mime_type: str | None, base64_data: str
    ) -> tuple[str | None, str | None]:
        return coerce_supported_image(mime_type, base64_data)

    @staticmethod
    def _validate_and_normalize_b64(
        raw_data: str, *, context: str = "", allow_relaxed_return: bool = False
    ) -> str:
        """
        校验并归一化 base64：
        - 去掉前缀/换行
        - 尝试标准解码失败后回退 urlsafe 解码（补齐 padding）
        - 再失败尝试宽松过滤/自动补齐 padding 后解码重编码
        返回可直接使用的纯 base64 字符串，失败抛出异常。
        """
        cleaned = (raw_data or "").strip().replace("\n", "")
        if ";base64," in cleaned:
            _, _, cleaned = cleaned.partition(";base64,")

        def try_decode(data: str) -> str:
            base64.b64decode(data, validate=True)
            return data

        try:
            return try_decode(cleaned)
        except Exception:
            # 回退 urlsafe base64
            alt = cleaned.replace("-", "+").replace("_", "/")
            pad_len = (-len(alt)) % 4
            if pad_len:
                alt += "=" * pad_len
            try:
                return try_decode(alt)
            except Exception as e:
                # 最后尝试宽松过滤非法字符/补齐 padding 后解码重编码
                relaxed = re.sub(r"[^A-Za-z0-9+/=_-]", "", cleaned)
                pad_len2 = (-len(relaxed)) % 4
                if pad_len2:
                    relaxed += "=" * pad_len2
                try:
                    raw = base64.b64decode(relaxed, validate=False)
                    if raw:
                        return base64.b64encode(raw).decode("utf-8")
                except Exception:
                    pass
                if allow_relaxed_return and relaxed:
                    return relaxed
                if allow_relaxed_return and cleaned:
                    # 仍无法解码时，允许直接回退原始字符串交由下游处理
                    return cleaned
                raise APIError(
                    f"参考图 base64 校验失败{f'（{context}）' if context else ''}，请检查图片后重试。",
                    None,
                    "invalid_reference_image",
                ) from e

    async def get_next_api_key(self) -> str:
        """获取下一个 API 密钥"""
        async with self._lock:
            if not self.api_keys:
                raise ValueError("API 密钥列表不能为空")
            key = self.api_keys[self.current_key_index % len(self.api_keys)]
            return key

    async def rotate_api_key(self):
        """轮换到下一个 API 密钥"""
        async with self._lock:
            if len(self.api_keys) > 1:
                self.current_key_index = (self.current_key_index + 1) % len(
                    self.api_keys
                )
                logger.debug(
                    f"已轮换到下一个 API 密钥，当前索引: {self.current_key_index}"
                )

    async def _prepare_google_payload(self, config: ApiRequestConfig) -> dict[str, Any]:
        """向后兼容：委托给 GoogleProvider 构建 payload。"""
        provider = get_api_provider("google")
        req = await provider.build_request(client=self, config=config)
        return req.payload

    async def _prepare_openai_payload(self, config: ApiRequestConfig) -> dict[str, Any]:
        """向后兼容：委托给 OpenAICompatProvider 构建 payload。"""
        provider = get_api_provider(config.api_type)
        req = await provider.build_request(client=self, config=config)
        return req.payload

    async def _normalize_image_input(
        self,
        image_input: Any,
        image_input_mode: str = "force_base64",
        image_cache_dir=None,
    ) -> tuple[str | None, str | None]:
        """统一调用 tl_utils 的参考图规范化逻辑"""
        return await normalize_image_input(
            image_input,
            image_cache_dir=image_cache_dir or IMAGE_CACHE_DIR,
            image_input_mode=image_input_mode,
        )

    async def _process_reference_image(
        self,
        image_input: Any,
        idx: int,
        image_input_mode: str = "force_base64",
    ) -> tuple[str | None, str | None, bool]:
        """
        统一处理参考图像，返回 (mime_type, data, is_url)。

        处理流程：
        1. 尝试解析为本地文件路径
        2. 尝试规范化转换为 base64
        3. 尝试通过 QQ 下载器获取
        4. 返回处理结果

        Returns:
            (mime_type, data, is_url):
            - mime_type: MIME 类型
            - data: base64 数据或 None（失败时）
            - is_url: 原始输入是否为 URL
        """
        image_str = str(image_input).strip()
        is_url = image_str.startswith(("http://", "https://"))

        data = None
        mime_type = None

        # 1. 尝试解析为本地文件
        try:
            local_path = await resolve_image_source_to_path(
                image_input,
                image_input_mode=image_input_mode,
                api_client=self,
                download_qq_image_fn=None,
            )
            if local_path and Path(local_path).exists():
                suffix = Path(local_path).suffix.lower().lstrip(".") or "png"
                mime_type = f"image/{suffix}"
                data = encode_file_to_base64(local_path)
                logger.debug(
                    f"[_process_reference_image] 从本地文件获取成功: idx={idx}"
                )
        except Exception as e:
            logger.debug(
                f"[_process_reference_image] 本地文件解析失败: idx={idx} err={e}"
            )

        # 2. 尝试规范化转换
        if not data:
            try:
                with tempfile.TemporaryDirectory(prefix="gemini_ref_tmp_") as tmp_dir:
                    temp_cache = Path(tmp_dir)
                    mime_type, data = await self._normalize_image_input(
                        image_input,
                        image_input_mode=image_input_mode,
                        image_cache_dir=temp_cache,
                    )
                if data:
                    logger.debug(
                        f"[_process_reference_image] 规范化转换成功: idx={idx} mime={mime_type}"
                    )
                else:
                    logger.debug(
                        f"[_process_reference_image] 规范化转换返回空: idx={idx}"
                    )
            except Exception as e:
                logger.debug(
                    f"[_process_reference_image] 规范化转换失败: idx={idx} err={e}"
                )

        # 3. QQ 下载器逻辑已整合到 normalize_image_input 和 resolve_image_source_to_path 中

        return mime_type, data, is_url

    def _validate_b64_with_fallback(
        self, data: str, context: str = ""
    ) -> tuple[str, bool]:
        """
        校验 base64 数据，失败时返回透传的原始数据。

        Returns:
            (result, validated): result 是处理后的数据，validated 表示是否通过校验
        """
        try:
            validated = self._validate_and_normalize_b64(
                data, context=context, allow_relaxed_return=True
            )
            return validated, True
        except APIError:
            # 校验失败，透传原始数据（去掉 data URI 前缀）
            raw = str(data).strip()
            if ";base64," in raw:
                _, _, raw = raw.partition(";base64,")
            return raw, False

    @staticmethod
    def _ensure_mime_type(mime_type: str | None, default: str = "image/png") -> str:
        """确保 MIME 类型有效"""
        if mime_type and mime_type.startswith("image/"):
            return mime_type
        return default

    async def _get_api_url(
        self, config: ApiRequestConfig
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """
        根据配置获取 API URL、请求头和负载

        智能处理API路径前缀，无需手动输入/v1或/v1beta
        """
        provider = get_api_provider(config.api_type)
        req = await provider.build_request(client=self, config=config)
        return req.url, req.headers, req.payload

    async def generate_image(
        self,
        config: ApiRequestConfig,
        max_retries: int = 3,
        total_timeout: int = 120,
        per_retry_timeout: int = None,
        max_total_time: int = None,
    ) -> tuple[list[str], list[str], str | None, str | None]:
        """
        生成图像

        Args:
            config: 请求配置
            max_retries: 最大重试次数
            total_timeout: 总超时时间（秒）

        Returns:
            (image_urls, image_paths, text_content, thought_signature)，如果失败则返回空列表和None
        """
        if not self.api_keys:
            raise ValueError("未配置 API 密钥")

        if not config.api_key:
            config.api_key = await self.get_next_api_key()

        enable_smart_retry = bool(getattr(config, "enable_smart_retry", True))

        # 获取请求信息
        url, headers, payload = await self._get_api_url(config)

        logger.debug(f"使用 {config.model} (通过 {config.api_type}) 生成图像")
        logger.debug(f"API 端点: {url[:80]}...")
        logger.debug(
            "请求参数概览: refs=%s prompt_len=%s aspect=%s res=%s",
            len(config.reference_images or []),
            len(config.prompt or ""),
            config.aspect_ratio,
            config.resolution,
        )

        if config.resolution or config.aspect_ratio:
            logger.debug(
                f"分辨率: {config.resolution or '默认'}, 长宽比: {config.aspect_ratio or '默认'}"
            )

        if config.api_base:
            logger.debug(f"使用自定义 API Base: {config.api_base}")

        # 同步详细日志开关，便于在内部网络请求中控制输出粒度
        self.verbose_logging = bool(getattr(config, "verbose_logging", False))

        return await self._make_request(
            url=url,
            payload=payload,
            headers=headers,
            api_type=config.api_type,
            model=config.model,
            max_retries=max_retries,
            total_timeout=total_timeout,
            api_base=config.api_base,
            enable_smart_retry=enable_smart_retry,
            per_retry_timeout=per_retry_timeout,
            max_total_time=max_total_time,
        )

    async def _make_request(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        api_type: str,
        model: str,
        max_retries: int,
        total_timeout: int = 120,
        api_base: str = None,
        enable_smart_retry: bool = True,
        per_retry_timeout: int | None = None,
        max_total_time: int | None = None,
    ) -> tuple[list[str], list[str], str | None, str | None]:
        """执行 API 请求并处理响应（支持重试、总耗时上限与多 Key 轮换）"""

        # 防御性复制 headers，避免并发请求间因 Key 轮换导致的相互影响
        headers = dict(headers)

        def coerce_int(value: Any, default: int) -> int:
            try:
                return int(value)
            except Exception:
                return default

        effective_max_retries = max(coerce_int(max_retries, 1), 1)
        enable_smart_retry = bool(enable_smart_retry)
        if not enable_smart_retry:
            effective_max_retries = 1

        base_timeout = per_retry_timeout if per_retry_timeout is not None else total_timeout
        base_timeout_int = max(coerce_int(base_timeout, total_timeout), 1)

        max_total_time_int = None
        if max_total_time is not None:
            mt = coerce_int(max_total_time, 0)
            if mt > 0:
                max_total_time_int = mt

        session = await self._get_session()
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        last_error: APIError | None = None

        async def rotate_key_if_possible(err: APIError) -> str | None:
            """在可用多 Key 且错误可能与 Key 相关时轮换，并更新 headers。"""
            if not enable_smart_retry or len(self.api_keys) <= 1:
                return None

            status = err.status_code
            if status not in {401, 402, 403, 429} and err.error_type != "quota":
                return None

            try:
                await self.rotate_api_key()
                new_key = await self.get_next_api_key()
            except Exception as e:
                logger.debug(f"轮换 API Key 失败，将继续使用当前 Key: {e}")
                return None

            updated = False
            if "Authorization" in headers:
                auth = str(headers.get("Authorization") or "")
                if auth.lower().startswith("bearer "):
                    headers["Authorization"] = f"Bearer {new_key}"
                    updated = True
            if "x-goog-api-key" in headers:
                headers["x-goog-api-key"] = new_key
                updated = True
            for k in ("X-Api-Key", "X-API-Key", "x-api-key"):
                if k in headers:
                    headers[k] = new_key
                    updated = True

            if not updated:
                logger.debug("已轮换 API Key，但未能识别需要更新的请求头字段")
            else:
                logger.debug(f"已轮换到新的 API Key: {new_key[:12]}...")

            return new_key

        def is_retryable(err: APIError) -> bool:
            """根据错误类型/状态码判断是否值得重试。"""
            if err.error_type == "no_image_retry":
                return True
            if err.error_type in {"timeout", "network"}:
                return True

            status = err.status_code
            if status is None:
                return True
            if 500 <= status < 600:
                return True
            if status in {408, 500, 502, 503, 504}:
                return True
            if status == 429:
                return True
            if status in {401, 402, 403}:
                return len(self.api_keys) > 1
            return False

        for attempt in range(effective_max_retries):
            if max_total_time_int is not None:
                elapsed = loop.time() - started_at
                remaining = max_total_time_int - elapsed
                if remaining <= 0:
                    timeout_msg = (
                        "图像生成时间过长，超出了框架限制。请尝试简化图像描述或在框架配置中增加 tool_call_timeout 到 90-120 秒。"
                    )
                    raise APIError(timeout_msg, None, "timeout") from None

                attempt_timeout_int = max(min(base_timeout_int, int(remaining)), 1)
            else:
                attempt_timeout_int = base_timeout_int

            timeout_cfg = aiohttp.ClientTimeout(
                total=attempt_timeout_int, sock_read=attempt_timeout_int
            )

            try:
                logger.debug(
                    "发送请求（尝试 %s/%s, timeout=%ss）",
                    attempt + 1,
                    effective_max_retries,
                    attempt_timeout_int,
                )
                return await self._perform_request(
                    session,
                    url,
                    payload,
                    headers,
                    api_type,
                    model,
                    timeout=timeout_cfg,
                    api_base=api_base,
                )

            except asyncio.CancelledError:
                # 只有框架取消才不重试（这是最顶层的超时）
                logger.debug("请求被框架取消（工具调用总超时），不再重试")
                timeout_msg = "图像生成时间过长，超出了框架限制。请尝试简化图像描述或在框架配置中增加 tool_call_timeout 到 90-120 秒。"
                raise APIError(timeout_msg, None, "cancelled") from None
            except Exception as e:
                if isinstance(e, APIError):
                    err = e
                    if not err.error_type:
                        err.error_type = self._classify_error(e, err.message)
                else:
                    err_msg = str(e)
                    err_type = self._classify_error(e, err_msg)
                    status_code = getattr(e, "status", None)
                    err = APIError(err_msg, status_code, err_type)

                # 首先检查是否为不可重试错误
                if not is_retryable(err):
                    logger.error(f"不可重试错误: {err.message}")
                    raise err from None

                # 可重试错误，但已用尽重试次数
                if attempt >= effective_max_retries - 1:
                    logger.error(
                        f"达到最大重试次数 ({effective_max_retries})，生成失败: {err.message}"
                    )
                    raise err from None

                last_error = err
                logger.warning(
                    "可重试错误 (尝试 %s/%s): %s",
                    attempt + 1,
                    effective_max_retries,
                    err.message,
                )

                await rotate_key_if_possible(err)

                delay = min(2 ** (attempt + 2), 10)
                logger.debug(f"等待 {delay} 秒后重试...")
                await asyncio.sleep(delay)

        if last_error:
            raise last_error from None

        return [], [], None, None

    def _classify_error(self, exception: Exception, error_msg: str) -> str:
        """分类错误类型"""
        if isinstance(exception, asyncio.TimeoutError):
            return "timeout"
        elif "timeout" in error_msg.lower():
            return "timeout"
        elif "connection" in error_msg.lower():
            return "network"
        elif isinstance(exception, aiohttp.ClientError):
            return "network"
        else:
            return "unknown"

    def _is_retryable_error(self, error_type: str, exception: Exception) -> bool:
        """判断错误是否可重试"""
        # 特殊处理：未生成图像的重试
        if error_type == "no_image_retry":
            return True

        # 可重试的错误：超时、网络错误、服务器错误
        if error_type in ["timeout", "network"]:
            return True

        # HTTP 状态码判断
        if hasattr(exception, "status"):
            status = exception.status
            # 可重试：408, 500, 502, 503, 504
            # 不可重试：401, 402, 403, 422, 429（速率限制）
            if status in [408, 500, 502, 503, 504]:
                return True
            elif status in [401, 402, 403, 422, 429]:
                return False

        return True  # 默认重试未知错误

    async def _perform_request(
        self,
        session: aiohttp.ClientSession,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        api_type: str,
        model: str,
        *,
        timeout: aiohttp.ClientTimeout | None = None,
        api_base: str = None,
    ) -> tuple[list[str], list[str], str | None, str | None]:
        """执行实际的HTTP请求"""
        logger.debug(
            "发送请求: url=%s api_type=%s model=%s payload_keys=%s",
            url[:100],
            api_type,
            model,
            list(payload.keys()),
        )

        async with session.post(
            url,
            json=payload,
            headers=headers,
            proxy=self.proxy,
            timeout=timeout,
        ) as response:
            logger.debug(f"响应状态: {response.status}")
            response_text = await response.text()
            content_type = response.headers.get("Content-Type", "") or ""

            # 解析 JSON 响应，添加错误处理
            try:
                response_data = json.loads(response_text) if response_text else {}
            except json.JSONDecodeError as e:
                # SSE 响应（text/event-stream）需要额外解析
                if (
                    "text/event-stream" in content_type.lower()
                    or response_text.strip().startswith("data:")
                ):
                    try:
                        response_data = self._parse_sse_payload(response_text)
                        logger.debug("检测到 SSE 响应，已完成 JSON 转换")
                    except Exception as sse_error:
                        logger.error(f"SSE 解析失败: {sse_error}")
                        logger.error(f"响应内容前500字符: {response_text[:500]}")
                        raise APIError(
                            f"API 返回了无效的 JSON/SSE 响应: {sse_error}",
                            response.status,
                        ) from None
                else:
                    logger.error(f"JSON 解析失败: {e}")
                    logger.error(f"响应内容前500字符: {response_text[:500]}")
                    raise APIError(
                        f"API 返回了无效的 JSON 响应: {e}", response.status
                    ) from None

            if response.status == 200:
                logger.debug("API 调用成功")
                if api_type == "google":
                    return await self._parse_gresponse(response_data, session)
                else:  # openai 兼容格式
                    return await self._parse_openai_response(
                        response_data, session, api_base
                    )
            elif response.status in [429, 402, 403]:
                error_msg = response_data.get("error", {}).get(
                    "message", f"HTTP {response.status}"
                )
                logger.warning(f"API 配额/权限问题: {error_msg}")
                raise APIError(error_msg, response.status, "quota")
            else:
                error_msg = response_data.get("error", {}).get(
                    "message", f"HTTP {response.status}"
                )
                logger.warning(f"API 错误: {error_msg}")
                raise APIError(error_msg, response.status)

    def _parse_sse_payload(self, raw_text: str) -> dict[str, Any]:
        """解析 text/event-stream 响应，提取最后一个包含有效 payload 的 data 包"""

        events: list[dict[str, Any]] = []
        data_lines: list[str] = []

        def flush_event():
            """将累计的 data 行拼接并解析为一个事件"""
            if not data_lines:
                return
            data_text = "\n".join(data_lines).strip()
            data_lines.clear()
            if not data_text or data_text == "[DONE]":
                return
            try:
                parsed = json.loads(data_text)
                if isinstance(parsed, dict):
                    events.append(parsed)
            except json.JSONDecodeError as e:
                logger.warning(
                    "SSE 事件解析失败: %s | 片段: %s",
                    e,
                    data_text[:160],
                )

        for raw_line in raw_text.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                flush_event()
                continue
            if stripped.startswith(":"):
                # SSE 注释行，直接跳过
                continue
            if stripped.startswith("data:"):
                data_lines.append(stripped.removeprefix("data:").lstrip())
                continue

            # 少数实现会省略前缀，这里尝试兼容
            if stripped and stripped != "[DONE]":
                data_lines.append(stripped)

        flush_event()

        if not events:
            raise ValueError(
                f"SSE 响应中未找到有效的 data 事件 (收到 {len(raw_text)} 字符, 片段: {raw_text[:160]!r})"
            )

        # 优先返回含 candidates/choices/data 字段的事件，避免 STOP 包覆盖有效负载
        for event in reversed(events):
            if not isinstance(event, dict):
                continue
            if event.get("candidates") or event.get("choices") or event.get("data"):
                logger.debug(
                    "SSE 响应共解析 %s 个事件，返回含有效负载的末尾事件",
                    len(events),
                )
                return event

        logger.debug("SSE 响应只包含通用事件，返回最后一个 data 包")
        return events[-1]

    async def _parse_gresponse(
        self, response_data: dict, session: aiohttp.ClientSession
    ) -> tuple[list[str], list[str], str | None, str | None]:
        """解析 Google 官方 API 响应"""
        provider = get_api_provider("google")
        return await provider.parse_response(
            client=self, response_data=response_data, session=session
        )

    async def _parse_openai_response(
        self, response_data: dict, session: aiohttp.ClientSession, api_base: str = None
    ) -> tuple[list[str], list[str], str | None, str | None]:
        """解析 OpenAI API 响应"""

        image_urls: list[str] = []
        image_paths: list[str] = []
        text_content = None
        thought_signature = None
        fail_reasons: list[str] = []
        fallback_texts = self._collect_fallback_texts(response_data)

        message: dict[str, Any] | None = None
        if "choices" in response_data and response_data["choices"]:
            choice = response_data["choices"][0]
            message = choice.get("message", {})
        else:
            message = self._coerce_basic_openai_message(response_data)

        if message:
            if "choices" not in response_data:
                logger.debug(
                    "[openai] 使用非标准字段构造 message，keys=%s",
                    list(response_data.keys())[:5],
                )
            content = message.get("content", "")

            text_chunks: list[str] = []
            image_candidates: list[str] = []
            extracted_urls: list[str] = []

            logger.debug(
                "[openai] 解析响应 choices，content_type=%s images_field=%s",
                type(content),
                bool(message.get("images")),
            )

            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue

                    part_type = part.get("type")
                    if part_type == "text" and "text" in part:
                        text_val = str(part.get("text", ""))
                        text_chunks.append(text_val)
                        extracted_urls.extend(self._find_image_urls_in_text(text_val))
                    elif part_type == "image_url":
                        image_obj = part.get("image_url") or {}
                        if isinstance(image_obj, dict):
                            url_val = image_obj.get("url")
                            if url_val:
                                image_candidates.append(url_val)
            elif isinstance(content, str):
                text_chunks.append(content)
                extracted_urls.extend(self._find_image_urls_in_text(content))

            # 标准 images 字段（兼容 Gemini/OpenAI 混合格式）
            if message.get("images"):
                for image_item in message["images"]:
                    if not isinstance(image_item, dict):
                        continue

                    # 典型格式：{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
                    image_obj = image_item.get("image_url")
                    if isinstance(image_obj, dict):
                        url_val = image_obj.get("url")
                        if isinstance(url_val, str) and url_val:
                            image_candidates.append(url_val)
                    elif isinstance(image_obj, str) and image_obj:
                        image_candidates.append(image_obj)
                    # 退化格式：{"url": "..."}
                    elif isinstance(image_item.get("url"), str):
                        image_candidates.append(image_item["url"])

            # 合并在文本里解析到的图像 URL
            if extracted_urls:
                image_candidates.extend(extracted_urls)

            # 组装文本内容
            if text_chunks:
                text_content = " ".join([t for t in text_chunks if t]).strip() or None

            # 按顺序处理图像候选
            for candidate_url in image_candidates:
                logger.debug("[openai] 处理候选URL: %s", str(candidate_url)[:120])
                if isinstance(candidate_url, str) and candidate_url.startswith(
                    "data:image/"
                ):
                    image_url, image_path = await self._parse_data_uri(candidate_url)
                elif isinstance(candidate_url, str):
                    # grok2api 适配：处理相对路径（如 /images/xxx）
                    if candidate_url.startswith("/") and not candidate_url.startswith(
                        "//"
                    ):
                        if api_base:
                            # 从 api_base 提取 scheme 和 netloc
                            parsed_base = urllib.parse.urlparse(api_base)
                            base_url = f"{parsed_base.scheme}://{parsed_base.netloc}"
                            full_url = urllib.parse.urljoin(base_url, candidate_url)
                            # grok2api 适配：立即下载临时缓存的图片（避免被清理）
                            logger.debug(
                                f"[grok2api 适配] 相对路径转换并下载: {candidate_url} -> {full_url}"
                            )
                            image_url, image_path = await self._download_image(
                                full_url, session, use_cache=False
                            )
                            # 只保留本地路径（_download_image 返回的两个值相同，避免重复）
                            if image_path:
                                image_paths.append(image_path)
                            continue
                        else:
                            logger.warning(
                                f"发现相对路径 URL 但未提供 api_base，跳过: {candidate_url}"
                            )
                            continue
                    # 对于可访问的 http(s) 链接，直接返回 URL，避免重复下载占用带宽
                    if candidate_url.startswith("http://") or candidate_url.startswith(
                        "https://"
                    ):
                        # grok2api 适配：检测临时缓存 URL 并强制下载（避免被清理）
                        # 临时缓存 URL 特征：包含 /images/users- 或 /temp/image/
                        is_temp_cache = (
                            "/images/users-" in candidate_url
                            or "/temp/image/" in candidate_url
                        )
                        if is_temp_cache:
                            logger.debug(
                                f"[grok2api 适配] 检测到临时缓存 URL，强制下载: {candidate_url}"
                            )
                            image_url, image_path = await self._download_image(
                                candidate_url, session, use_cache=False
                            )
                            # 只保留本地路径（_download_image 返回的两个值相同，避免重复）
                            if image_path:
                                image_paths.append(image_path)
                            continue
                        # 其他永久 URL 直接使用
                        image_urls.append(candidate_url)
                        logger.debug(
                            f"🖼️ OpenAI 返回可直接访问的图像链接: {candidate_url}"
                        )
                        continue
                    image_url, image_path = await self._download_image(
                        candidate_url, session, use_cache=False
                    )
                else:
                    logger.warning(f"跳过非字符串类型的图像URL: {type(candidate_url)}")
                    continue

                if image_url or image_path:
                    if image_url:
                        image_urls.append(image_url)
                    if image_path:
                        image_paths.append(image_path)

            # content 中查找内联 data URI（文本里）
            extracted_urls: list[str] = []
            extracted_paths: list[str] = []

            if isinstance(content, str):
                extracted_urls, extracted_paths = await self._extract_from_content(
                    content
                )
            elif text_content:
                extracted_urls, extracted_paths = await self._extract_from_content(
                    text_content
                )

            if extracted_urls or extracted_paths:
                image_urls.extend(extracted_urls)
                image_paths.extend(extracted_paths)

            # 额外在汇总文本中搜索 http(s) 图片链接，兼容只返回文本的情况
            if text_content:
                http_urls = self._find_image_urls_in_text(text_content)
                for url in http_urls:
                    # grok2api 适配：跳过临时缓存 URL（已在上面下载并添加到 image_paths）
                    is_temp_cache = "/images/users-" in url or "/temp/image/" in url
                    if is_temp_cache:
                        logger.debug(
                            f"[grok2api 适配] 跳过文本中的临时缓存 URL（已下载）: {url}"
                        )
                        continue
                    if url not in image_urls:
                        image_urls.append(url)

                # 松散提取 data:image 片段，避免因 Markdown/换行导致遗漏
                loose_matches = re.finditer(
                    r"data:image/([a-zA-Z0-9.+-]+);base64,([-A-Za-z0-9+/=_\\s]+)",
                    text_content,
                    flags=re.IGNORECASE,
                )
                for m in loose_matches:
                    fmt = m.group(1)
                    b64_raw = m.group(2)
                    b64_clean = re.sub(r"\\s+", "", b64_raw)
                    image_path = await save_base64_image(b64_clean, fmt.lower())
                    if image_path:
                        image_urls.append(image_path)
                        image_paths.append(image_path)
                        logger.debug(
                            "[openai] 松散提取 data URI 成功: fmt=%s len=%s",
                            fmt,
                            len(b64_clean),
                        )

        else:
            logger.debug("[openai] 响应缺少可用的 message 字段，尝试 data/b64 解析")

        if not (image_urls or image_paths) and fallback_texts:
            fallback_added = await self._append_images_from_texts(
                fallback_texts, image_urls, image_paths
            )
            if fallback_added and not text_content:
                text_content = (
                    " ".join(t.strip() for t in fallback_texts if t and t.strip())
                    or text_content
                )

        # OpenAI 格式
        if not image_urls and not image_paths and response_data.get("data"):
            for image_item in response_data["data"]:
                if "url" in image_item:
                    image_url, image_path = await self._download_image(
                        image_item["url"], session, use_cache=False
                    )
                    if image_url:
                        image_urls.append(image_url)
                    if image_path:
                        image_paths.append(image_path)
                elif "b64_json" in image_item:
                    image_path = await save_base64_image(image_item["b64_json"], "png")
                    if image_path:
                        # 直接使用文件路径，不使用 file:// URI（根据 AstrBot 文档要求）
                        image_urls.append(image_path)
                        image_paths.append(image_path)

        if image_urls or image_paths:
            logger.debug(
                f"🖼️ OpenAI 收集到 {len(image_paths) or len(image_urls)} 张图片"
            )
            return image_urls, image_paths, text_content, thought_signature

        # 如果只有文本内容，也返回
        if text_content:
            # 如果配置了需要文本响应，且确实没有找到图片，这里应该报错触发重试而不是直接返回文本
            # 除非这是一个纯文本请求（但在生图插件里通常不是）
            detail = (
                f" | 参考图处理提示: {'; '.join(fail_reasons[:3])}"
                if fail_reasons
                else ""
            )
            logger.debug(
                "[openai] 仅返回文本，长度=%s 预览=%s",
                len(text_content),
                text_content[:200],
            )
            logger.warning(f"OpenAI只返回了文本响应，未生成图像，将触发重试{detail}")
            logger.debug(f"OpenAI响应内容: {str(response_data)[:1000]}")
            raise APIError(
                f"图像生成失败：API只返回了文本响应，正在重试... | 响应预览: {str(response_data)[:300]}",
                500,
                "no_image_retry",
            )

        logger.warning(
            f"OpenAI 响应格式不支持或未找到图像数据，响应: {str(response_data)[:500]}"
        )

    def _normalize_message_value(self, raw_value: Any) -> dict[str, Any] | None:
        """归一化任意常见字段为标准 message 结构"""
        if raw_value is None:
            return None

        if isinstance(raw_value, dict):
            if raw_value.get("role") and "content" in raw_value:
                return raw_value

            if "message" in raw_value:
                nested = self._normalize_message_value(raw_value.get("message"))
                if nested:
                    return nested

            for key in ("content", "text", "output", "result", "response"):
                if key in raw_value:
                    nested = self._normalize_message_value(raw_value.get(key))
                    if nested:
                        return nested

            return None

        if isinstance(raw_value, list):
            if raw_value:
                return {"role": "assistant", "content": raw_value}
            return None

        if isinstance(raw_value, str):
            cleaned = raw_value.strip()
            if cleaned:
                return {"role": "assistant", "content": cleaned}
            return None

        return None

    def _coerce_basic_openai_message(
        self, response_data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """从常见兼容格式提取 message，兼容 body/content/text 等字段"""

        primary_keys = [
            "message",
            "content",
            "text",
            "output",
            "result",
            "response",
        ]
        nested_keys = [
            "body",
            "modelOutput",
            "model_output",
            "response_body",
        ]

        for key in primary_keys:
            normalized = self._normalize_message_value(response_data.get(key))
            if normalized:
                return normalized

        for key in nested_keys:
            value = response_data.get(key)
            if isinstance(value, (dict, list, str)):
                normalized = self._normalize_message_value(value)
                if normalized:
                    return normalized

        return None

    def _collect_fallback_texts(self, payload: dict[str, Any]) -> list[str]:
        """收集常见字段中的文本响应，用于兜底提取 Markdown 链接"""
        if not isinstance(payload, dict):
            return []

        candidate_keys = (
            "content",
            "text",
            "output",
            "result",
            "response",
            "message",
        )
        container_keys = (
            "body",
            "response_body",
            "modelOutput",
            "model_output",
            "modelOutputs",
            "model_outputs",
        )

        texts: list[str] = []

        def push(value: Any):
            if value is None:
                return
            if isinstance(value, str):
                cleaned = value.strip()
                if cleaned:
                    texts.append(cleaned)
                return
            if isinstance(value, list):
                for item in value:
                    push(item)
                return
            if isinstance(value, dict):
                for key in candidate_keys:
                    if key in value:
                        push(value.get(key))

        for key in candidate_keys:
            push(payload.get(key))

        for key in container_keys:
            push(payload.get(key))

        # 去重但保持顺序
        seen: set[str] = set()
        ordered: list[str] = []
        for text in texts:
            if text not in seen:
                seen.add(text)
                ordered.append(text)
        return ordered

    async def _append_images_from_texts(
        self,
        texts: list[str],
        image_urls: list[str],
        image_paths: list[str],
    ) -> bool:
        """从额外的文本字段中提取 http(s)/data URI 图像"""

        appended = False
        for text in texts:
            if not text:
                continue

            http_urls = self._find_image_urls_in_text(text)
            for url in http_urls:
                if url not in image_urls:
                    image_urls.append(url)
                    appended = True

            extra_urls, extra_paths = await self._extract_from_content(text)
            for url in extra_urls:
                if url not in image_urls:
                    image_urls.append(url)
                    appended = True
            for path in extra_paths:
                if path not in image_paths:
                    image_paths.append(path)
                    appended = True

        return appended

    async def _parse_data_uri(self, data_uri: str) -> tuple[str | None, str | None]:
        """解析 data URI 格式的图像"""
        try:
            if ";base64," not in data_uri:
                logger.error("无效的 data URI 格式")
                return None, None

            header, base64_data = data_uri.split(";base64,", 1)
            mime_type = header.replace("data:", "")
            format_type = mime_type.split("/")[1] if "/" in mime_type else "png"

            image_path = await save_base64_image(base64_data, format_type)
            if image_path:
                # 直接使用文件路径，不使用 file:// URI（根据 AstrBot 文档要求）
                image_url = image_path
                return image_url, image_path
        except Exception as e:
            logger.error(f"解析 data URI 失败: {e}")

        return None, None

    async def _extract_from_content(self, content: str) -> tuple[list[str], list[str]]:
        """从文本内容中提取所有 data URI 图像，保持顺序"""
        # OpenAI 兼容接口有时会把图片以 Markdown data URI 形式塞进纯文本
        # 为了更鲁棒，允许大小写混排、包含 -/_，并跨多行匹配
        pattern = re.compile(
            r"data\s*:\s*image/([a-zA-Z0-9.+-]+)\s*;\s*base64\s*,\s*([-A-Za-z0-9+/=_\s]+)",
            flags=re.IGNORECASE,
        )
        matches = pattern.findall(content)

        image_urls: list[str] = []
        image_paths: list[str] = []

        for image_format, base64_string in matches:
            # 先简单清洗非法字符，避免因意外插入的符号导致解码失败
            cleaned_b64 = re.sub(r"[^A-Za-z0-9+/=_-]", "", base64_string)
            image_path = await save_base64_image(
                cleaned_b64 or base64_string, image_format.lower()
            )
            if image_path:
                # 直接使用文件路径，不使用 file:// URI（根据 AstrBot 文档要求）
                image_url = image_path
                image_urls.append(image_url)
                image_paths.append(image_path)

        return image_urls, image_paths

    def _find_image_urls_in_text(self, text: str) -> list[str]:
        """从文本/Markdown中提取可用的 http(s) 图片链接"""
        if not text:
            return []

        # Markdown 图片语法与裸露的图片链接
        markdown_pattern = r"!\[[^\]]*\]\((https?://[^)]+)\)"
        # Markdown 图片语法中的 data URI（如 ![image](data:image/png;base64,...)）
        markdown_data_uri_pattern = r"!\[[^\]]*\]\((data:image/[^)]+)\)"
        # grok2api 适配：支持相对路径 (如 ![image](/images/xxx))
        markdown_relative_pattern = r"!\[[^\]]*\]\((/[^)]+|[^/:)]+/[^)]+)\)"
        raw_pattern = (
            r"(https?://[^\s)]+\.(?:png|jpe?g|gif|webp|bmp|tiff|avif))(?:\b|$)"
        )
        spaced_pattern = r"(https?\s*:\s*/\s*/[^\s)]+)"

        urls: list[str] = []
        seen: set[str] = set()

        def _push(candidate: str):
            cleaned = candidate.strip().replace("&amp;", "&").rstrip(").,;")
            # grok2api 适配：移除 URL 两端的引号（单引号或双引号）
            cleaned = cleaned.strip("'\"")
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                urls.append(cleaned)

        for pattern in (markdown_pattern, markdown_data_uri_pattern, raw_pattern):
            for match in re.findall(pattern, text, flags=re.IGNORECASE):
                _push(match)

        # grok2api 适配：提取相对路径
        for match in re.findall(markdown_relative_pattern, text, flags=re.IGNORECASE):
            if not match.startswith(("http://", "https://", "data:")):
                _push(match)

        # 适配带空格的 http:// 片段（如 "http: //1. 2. 3. 4/image.png"）
        for match in re.findall(spaced_pattern, text, flags=re.IGNORECASE):
            compact = re.sub(r"\s+", "", match)
            if compact.lower().startswith(("http://", "https://")):
                _push(compact)

        return urls

    async def _download_image(
        self,
        image_url: str,
        session: aiohttp.ClientSession,
        use_cache: bool = False,
    ) -> tuple[str | None, str | None]:
        """下载并保存图像，可选择是否使用缓存（默认关闭以避免返回旧图）"""
        cleaned_url = (
            image_url.replace("&amp;", "&") if isinstance(image_url, str) else image_url
        )
        parsed = urllib.parse.urlparse(cleaned_url)
        is_http = parsed.scheme in {"http", "https"}
        cache_key = None

        # 针对 CQ 码图服务器增加专用请求头
        headers: dict[str, str] = {}
        if is_http:
            headers.update(
                {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
                    ),
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Connection": "keep-alive",
                }
            )
            if "gchat.qpic.cn" in (parsed.netloc or ""):
                headers["Referer"] = "https://qun.qq.com"
            elif parsed.scheme and parsed.netloc:
                headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}"

        # 缓存命中直接返回，减少重复下载与内存占用
        if cache_key:
            try:
                IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cached = next(IMAGE_CACHE_DIR.glob(f"{cache_key}.*"), None)
                if cached and cached.exists() and cached.stat().st_size > 0:
                    logger.debug(f"图像下载命中缓存: {cleaned_url}")
                    return str(cached), str(cached)
            except Exception as e:
                logger.debug(f"检查图像缓存失败: {e}")

        max_retries = 1
        retry_interval = 1.0

        for attempt in range(1, max_retries + 1):
            try:
                logger.debug(
                    f"正在下载图像: {cleaned_url[:100]}... 尝试 {attempt}/{max_retries}"
                )

                async with session.get(
                    cleaned_url,
                    timeout=aiohttp.ClientTimeout(total=30),
                    proxy=self.proxy,
                    headers=headers or None,
                ) as response:
                    if response.status != 200:
                        try:
                            err_text = await response.text()
                        except Exception:
                            err_text = ""

                        response_reason = response.reason or ""
                        response_content_type = response.headers.get("Content-Type", "")
                        query_params = urllib.parse.parse_qs(parsed.query)
                        param_issues: list[str] = []

                        # 仅在出现 400 错误时进行参数合法性检查
                        if response.status == 400:
                            appid = (query_params.get("appid") or [None])[0]
                            if appid and not re.fullmatch(r"[A-Za-z0-9]+", appid):
                                param_issues.append("appid 格式异常（仅允许字母数字）")

                            fileid = (query_params.get("fileid") or [None])[0]
                            if fileid and not re.fullmatch(r"[A-Za-z0-9._-]+", fileid):
                                param_issues.append(
                                    "fileid 格式异常（仅允许字母数字、.、_、-）"
                                )

                            rkey = (query_params.get("rkey") or [None])[0]
                            if rkey and re.search(r"[^A-Za-z0-9._-]", rkey):
                                param_issues.append("rkey 包含特殊字符")

                            spec = (query_params.get("spec") or [None])[0]
                            if spec and not str(spec).isdigit():
                                param_issues.append("spec 参数应为数字")

                        # 根据响应内容与校验结果给出建议
                        suggestions: list[str] = []
                        if " " in cleaned_url or "%20" in cleaned_url:
                            suggestions.append("URL格式错误 → 检查URL编码")
                        if param_issues:
                            suggestions.append("参数错误 → 检查参数格式")
                        err_lower = err_text.lower() if err_text else ""
                        if any(keyword in err_lower for keyword in ["auth", "key"]):
                            suggestions.append("认证错误 → 检查API密钥")
                        if any(
                            keyword in err_lower
                            for keyword in ["limit", "频率", "限制"]
                        ):
                            suggestions.append("服务器限制 → 建议稍后重试")
                        if not suggestions:
                            suggestions.append("服务器限制 → 建议稍后重试")

                        logger.error(
                            "下载图像失败: HTTP %s %s url=%s 响应摘要=%s 建议=%s",
                            response.status,
                            response_reason,
                            cleaned_url,
                            err_text[:200],
                            "；".join(dict.fromkeys(suggestions)),
                        )

                        if self.verbose_logging:
                            logger.debug(
                                "HTTP 400 参数检查结果: %s",
                                "; ".join(param_issues)
                                if param_issues
                                else "未发现明显异常",
                            )
                            logger.debug("完整请求头: %s", headers or {})
                            logger.debug(
                                "User-Agent: %s", (headers or {}).get("User-Agent", "")
                            )
                            logger.debug(
                                "Content-Type: %s, Accept: %s",
                                (headers or {}).get("Content-Type", "未设置"),
                                (headers or {}).get("Accept", "未设置"),
                            )
                            logger.debug(
                                "服务器响应详情: status=%s, reason=%s, phrase=%s, content-type=%s",
                                response.status,
                                response_reason,
                                getattr(response, "reason", ""),
                                response_content_type,
                            )
                            logger.debug(
                                "服务器响应体预览: %s",
                                err_text[:1000] if err_text else "<empty>",
                            )

                        if response.status == 400 and attempt < max_retries:
                            await asyncio.sleep(retry_interval * attempt)
                            continue
                        return None, None

                    content_type = response.headers.get("Content-Type", "")

                    if "/" in content_type:
                        image_format = content_type.split("/")[1].split(";")[0] or "png"
                    else:
                        image_format = "png"

                    target_path = None
                    if cache_key:
                        target_path = IMAGE_CACHE_DIR / f"{cache_key}.{image_format}"

                    image_path = await save_image_stream(
                        response.content, image_format, target_path=target_path
                    )
                    if image_path:
                        # 直接使用文件路径，不使用 file:// URI（根据 AstrBot 文档要求）
                        image_url_local = image_path
                        return image_url_local, image_path
            except aiohttp.ClientError as e:
                logger.error(f"下载图像发生网络异常: {e}")
            except Exception as e:
                logger.error(f"下载图像失败: {e}")

            if attempt < max_retries:
                await asyncio.sleep(retry_interval * attempt)

        return None, None


# 为了兼容性，创建APIClient别名
APIClient = GeminiAPIClient

# 全局 API 客户端实例
_api_client: GeminiAPIClient | None = None


def get_api_client(api_keys: list[str]) -> GeminiAPIClient:
    """获取或创建 API 客户端实例"""
    global _api_client
    if _api_client is None:
        _api_client = GeminiAPIClient(api_keys)
    return _api_client


def clear_api_client():
    """清除全局 API 客户端实例（用于测试）"""
    global _api_client
    _api_client = None
