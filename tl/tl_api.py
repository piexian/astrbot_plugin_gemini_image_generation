"""
API客户端模块
提供Google Gemini和OpenAI兼容API的客户端实现
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger

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


@dataclass
class ApiRequestConfig:
    """API 请求配置（基于 Gemini 官方文档）"""

    model: str
    prompt: str
    api_type: str = "openai"
    api_base: str | None = None
    api_key: str | None = None
    resolution: str | None = None
    aspect_ratio: str | None = None
    enable_grounding: bool = False
    response_modalities: str = "TEXT_IMAGE"  # 默认同时返回文本和图像
    max_tokens: int = 1000
    reference_images: list[str] | None = None
    response_text: str | None = None  # 存储文本响应
    enable_smart_retry: bool = True  # 智能重试开关
    enable_text_response: bool = False  # 文本响应开关
    force_resolution: bool = False  # 强制传递分辨率参数
    verbose_logging: bool = False  # 详细日志开关
    image_input_mode: str = "force_base64"  # 参考图统一转 base64

    # 官方文档推荐参数
    temperature: float = 0.7  # 控制生成随机性，0.0-1.0
    seed: int | None = None  # 固定种子以确保一致性
    safety_settings: dict | None = None  # 安全设置

    # 自定义 API 参数名（支持不同 API 的字段命名差异）
    resolution_param_name: str = "image_size"  # 分辨率参数名
    aspect_ratio_param_name: str = "aspect_ratio"  # 长宽比参数名


class APIError(Exception):
    """API 错误基类"""

    def __init__(self, message: str, status_code: int = None, error_type: str = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type


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
        """准备 Google 官方 API 请求负载（遵循官方规范）"""
        logger.debug(
            "[google] 构建 payload: model=%s refs=%s force_b64=%s aspect=%s res=%s",
            config.model,
            len(config.reference_images or []),
            config.image_input_mode,
            config.aspect_ratio,
            config.resolution,
        )
        parts = [{"text": config.prompt}]

        added_refs = 0
        fail_reasons: list[str] = []
        if config.reference_images:
            for idx, image_input in enumerate(config.reference_images[:14]):
                image_str = str(image_input).strip()
                logger.debug(
                    "[google] 处理参考图 idx=%s type=%s preview=%s",
                    idx,
                    type(image_input),
                    image_str[:120],
                )

                # 使用统一的参考图处理方法
                mime_type, data, is_url = await self._process_reference_image(
                    image_input, idx, config.image_input_mode
                )

                if not data:
                    # 转换失败处理
                    if is_url:
                        # URL 下载失败，改用 fileData 格式传输
                        parts.append({"fileData": {"fileUri": image_str}})
                        added_refs += 1
                        logger.info(
                            "[google] URL 下载失败，改用 fileData 传输 idx=%s url=%s",
                            idx,
                            image_str[:80],
                        )
                        continue
                    else:
                        # 非 URL 输入，直接透传原始数据
                        data = image_str
                        mime_type = self._ensure_mime_type(mime_type)
                        logger.debug(
                            "[google] 转换失败，直接透传原始数据 idx=%s preview=%s",
                            idx,
                            image_str[:80],
                        )

                # 校验 base64，失败则透传或回退 URL
                validated_data, is_valid = self._validate_b64_with_fallback(
                    data, context="google-inline"
                )

                if not is_valid and is_url:
                    # URL 转换的数据校验失败，改用 fileData
                    parts.append({"fileData": {"fileUri": image_str}})
                    added_refs += 1
                    logger.info(
                        "[google] base64 校验失败，改用 fileData 传输 idx=%s url=%s",
                        idx,
                        image_str[:80],
                    )
                    continue

                mime_type = self._ensure_mime_type(mime_type)
                logger.debug(
                    "[google] 成功处理参考图 idx=%s mime=%s size=%s",
                    idx,
                    mime_type,
                    len(validated_data) if validated_data else 0,
                )

                parts.append({"inlineData": {"mimeType": mime_type, "data": validated_data}})
                added_refs += 1

        if config.reference_images and added_refs == 0:
            raise APIError(
                "参考图全部无效或下载失败，请重新发送图片后重试。"
                + (f" 详情: {'; '.join(fail_reasons[:3])}" if fail_reasons else ""),
                None,
                "invalid_reference_image",
            )

        contents = [{"role": "user", "parts": parts}]

        generation_config = {"responseModalities": ["TEXT", "IMAGE"]}

        # 根据官方文档，图像生成必须同时包含 TEXT 和 IMAGE modalities
        # 这样可以确保返回图像而不是纯文本
        modalities_map = {
            "TEXT": ["TEXT"],
            "IMAGE": ["IMAGE"],
            "TEXT_IMAGE": ["TEXT", "IMAGE"],
        }

        # 获取配置的模态
        modalities = modalities_map.get(config.response_modalities, ["TEXT", "IMAGE"])

        # 确保包含图像模态
        if "IMAGE" not in modalities:
            logger.warning("配置中缺少 IMAGE modality，自动添加以支持图像生成")
            modalities.append("IMAGE")

        # 确保包含文本模态
        if "TEXT" not in modalities:
            logger.debug("添加 TEXT modality 以提供更好的兼容性")
            modalities.append("TEXT")

        generation_config["responseModalities"] = modalities
        logger.debug(f"响应模态: {modalities}")

        image_config = {}

        # 获取自定义参数名（支持不同 API 的命名差异）
        # 先 strip 再检查是否为空，避免空格字符串导致空键
        _res_key = (config.resolution_param_name or "").strip()
        resolution_key = _res_key if _res_key else "image_size"
        _aspect_key = (config.aspect_ratio_param_name or "").strip()
        aspect_ratio_key = _aspect_key if _aspect_key else "aspect_ratio"

        # 根据官方文档设置图像尺寸
        if config.resolution:
            resolution = config.resolution.upper()

            if resolution in ["1K", "1024X1024"]:
                image_config[resolution_key] = "1K"
                logger.debug(f"设置图像尺寸: 1K (参数名: {resolution_key})")
            elif resolution in ["2K", "2048X2048"]:
                image_config[resolution_key] = "2K"
                logger.debug(f"设置图像尺寸: 2K (参数名: {resolution_key})")
            elif resolution in ["4K", "4096X4096"]:
                image_config[resolution_key] = "4K"
                logger.debug(f"设置图像尺寸: 4K (参数名: {resolution_key})")
            else:
                # 自定义分辨率值，直接透传
                image_config[resolution_key] = config.resolution
                logger.debug(f"设置图像尺寸: {config.resolution} (参数名: {resolution_key})")

        # 设置长宽比（支持任意自定义比例）
        if config.aspect_ratio:
            ratio = config.aspect_ratio.strip()
            image_config[aspect_ratio_key] = ratio
            logger.debug(f"设置长宽比: {ratio} (参数名: {aspect_ratio_key})")

        if image_config:
            generation_config["image_config"] = image_config

        # 添加官方文档推荐参数
        if config.temperature is not None:
            generation_config["temperature"] = config.temperature
        if config.seed is not None:
            generation_config["seed"] = config.seed
        if config.safety_settings:
            generation_config["safetySettings"] = config.safety_settings

        tools = []
        if config.enable_grounding:
            tools.append({"google_search": {}})

        payload = {"contents": contents, "generationConfig": generation_config}

        if tools:
            payload["tools"] = tools

        # 调试：记录 image_config
        if "image_config" in generation_config:
            logger.debug(
                f"实际发送的 image_config: {generation_config['image_config']}"
            )

        return payload

    async def _prepare_openai_payload(self, config: ApiRequestConfig) -> dict[str, Any]:
        """准备 OpenAI API 请求负载"""
        message_content = [
            {"type": "text", "text": f"Generate an image: {config.prompt}"}
        ]

        force_b64 = (
            str(getattr(config, "image_input_mode", "auto")).lower() == "force_base64"
        )

        supported_exts = {
            "jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff", "heic", "avif",
        }

        if config.reference_images:
            # 本地缓存避免重复处理同一引用图，记录耗时便于性能观察
            processed_cache: dict[str, dict[str, Any]] = {}
            total_start = time.perf_counter()

            for idx, image_input in enumerate(config.reference_images[:6]):
                per_start = time.perf_counter()
                image_str = str(image_input).strip()
                if not image_str:
                    logger.warning(f"跳过空白参考图像: idx={idx}")
                    continue

                if "&amp;" in image_str:
                    image_str = image_str.replace("&amp;", "&")

                # 命中缓存直接复用，避免重复 base64 处理
                if image_str in processed_cache:
                    logger.debug(f"参考图像命中缓存: idx={idx}")
                    message_content.append(processed_cache[image_str])
                    continue

                parsed = urllib.parse.urlparse(image_str)
                image_payload: dict[str, Any] | None = None

                try:
                    # 优先处理 http(s) URL，确保 scheme 和 netloc 合法
                    if (
                        parsed.scheme in ("http", "https")
                        and parsed.netloc
                        and not force_b64
                    ):
                        ext = Path(parsed.path).suffix.lower().lstrip(".")
                        if ext and ext not in supported_exts:
                            logger.debug(
                                "参考图像URL扩展名不在常见列表: idx=%s ext=%s url=%s",
                                idx, ext, image_str[:80],
                            )

                        image_payload = {
                            "type": "image_url",
                            "image_url": {"url": image_str},
                        }
                        logger.debug(
                            "OpenAI兼容API使用URL参考图: idx=%s ext=%s url=%s",
                            idx, ext or "unknown", image_str[:120],
                        )

                    # data URL：直接校验 base64，有效则不再重复转码
                    elif image_str.startswith("data:image/") and ";base64," in image_str:
                        header, _, data_part = image_str.partition(";base64,")
                        mime_type = header.replace("data:", "").lower()
                        try:
                            base64.b64decode(data_part, validate=True)
                        except (binascii.Error, ValueError) as e:
                            logger.warning(
                                "跳过无效的 data URL 参考图: idx=%s 错误=%s", idx, e
                            )
                            mime_type = None

                        if mime_type:
                            ext = mime_type.split("/")[-1]
                            if ext and ext not in supported_exts:
                                logger.debug(
                                    "data URL 图片格式不常见: idx=%s mime=%s", idx, mime_type,
                                )
                            image_payload = {
                                "type": "image_url",
                                "image_url": {"url": image_str},
                            }
                            logger.debug(
                                "OpenAI兼容API使用data URL参考图: idx=%s mime=%s", idx, mime_type,
                            )

                    # 其他输入交给规范化逻辑，自动转换为 data URL
                    else:
                        mime_type, data = await self._normalize_image_input(
                            image_input, image_input_mode=config.image_input_mode
                        )
                        if not data:
                            if force_b64:
                                raise APIError(
                                    f"参考图转 base64 失败（force_base64），idx={idx}, type={type(image_input)}",
                                    None, "invalid_reference_image",
                                )
                            logger.warning(
                                "跳过无法识别/读取的参考图像: idx=%s type=%s", idx, type(image_input),
                            )
                            continue

                        if not mime_type or not mime_type.startswith("image/"):
                            logger.debug(
                                "未检测到明确的图片 MIME，默认使用 image/png: idx=%s", idx,
                            )
                            mime_type = "image/png"

                        ext = mime_type.split("/")[-1]
                        if ext and ext not in supported_exts:
                            logger.debug(
                                "规范化后图片格式不常见: idx=%s mime=%s", idx, mime_type
                            )

                        if force_b64:
                            # force_base64 模式：校验后构建 data URL
                            cleaned = data.strip().replace("\n", "")
                            try:
                                base64.b64decode(cleaned, validate=True)
                            except Exception:
                                raise APIError(
                                    f"参考图 base64 校验失败（force_base64），来源: idx={idx}",
                                    None, "invalid_reference_image",
                                )
                            # OpenAI 兼容 API 需要完整的 data URL 格式
                            payload_url = f"data:{mime_type};base64,{cleaned}"
                        else:
                            payload_url = f"data:{mime_type};base64,{data}"

                        image_payload = {
                            "type": "image_url",
                            "image_url": {"url": payload_url},
                        }

                    if image_payload:
                        message_content.append(image_payload)
                        processed_cache[image_str] = image_payload
                        elapsed_ms = (time.perf_counter() - per_start) * 1000
                        logger.debug(
                            "参考图像处理完成: idx=%s 耗时=%.2fms 来源=%s",
                            idx, elapsed_ms, parsed.scheme or "normalized",
                        )
                except Exception as e:
                    logger.warning("处理参考图像时出现异常: idx=%s err=%s", idx, e)
                    continue

            total_elapsed_ms = (time.perf_counter() - total_start) * 1000
            if processed_cache:
                logger.debug(
                    "参考图像处理统计: 总数=%s 总耗时=%.2fms 平均=%.2fms",
                    len(processed_cache), total_elapsed_ms,
                    total_elapsed_ms / len(processed_cache),
                )

        # OpenAI 兼容接口下：
        # - 使用 chat/completions
        # - modalities: ["image", "text"]
        # - image_config: {aspect_ratio, image_size}
        # - tools: [{google_search:{}}]（当启用搜索接地时）
        payload: dict[str, Any] = {
            "model": config.model,
            "messages": [{"role": "user", "content": message_content}],
            "max_tokens": config.max_tokens,
            "temperature": 0.7,
            "modalities": ["image", "text"],
            # 明确关闭流式响应，避免部分 OpenAI 兼容服务默认返回 SSE
            "stream": False,
        }

        # image_config 与 Gemini 3 Pro Image 模型相关的配置
        image_config: dict[str, Any] = {}

        # 获取自定义参数名（支持不同 API 的命名差异）
        # 先 strip 再检查是否为空，避免空格字符串导致空键
        _res_key = (config.resolution_param_name or "").strip()
        resolution_key = _res_key if _res_key else "image_size"
        _aspect_key = (config.aspect_ratio_param_name or "").strip()
        aspect_ratio_key = _aspect_key if _aspect_key else "aspect_ratio"

        # 设置长宽比（支持任意自定义比例）
        if config.aspect_ratio:
            image_config[aspect_ratio_key] = config.aspect_ratio

        # 仅在 Gemini 3 Pro Image 系列模型下传递分辨率到 image_config
        model_name = (config.model or "").lower()
        is_gemini_image_model = (
            "gemini-3-pro-image" in model_name
            or "gemini-3-pro-preview" in model_name
            or config.force_resolution  # 如果强制开启，则忽略模型名称检查
        )

        if is_gemini_image_model and config.resolution:
            # 前端 router 侧直接传递 "1K"/"2K"/"4K"，这里保持一致
            image_config[resolution_key] = config.resolution

        if image_config:
            payload["image_config"] = image_config

        # 兼容 openai_chat_client.py 风格的顶层与 generation_config 参数
        # 仅当用户显式将参数名设置为 image_resolution/image_aspect_ratio 时启用，
        # 避免对其他 OpenAI 官方/兼容服务引入未知字段。
        generation_config: dict[str, Any] = {}

        if resolution_key == "image_resolution" and config.resolution:
            generation_config["image_resolution"] = config.resolution
            payload["image_resolution"] = config.resolution

        if aspect_ratio_key == "image_aspect_ratio" and config.aspect_ratio:
            generation_config["image_aspect_ratio"] = config.aspect_ratio
            payload["image_aspect_ratio"] = config.aspect_ratio

        if generation_config:
            payload["generation_config"] = generation_config

        # 与前端 router 一致：启用搜索接地时，通过 tools.google_search 控制
        if is_gemini_image_model and config.enable_grounding:
            payload["tools"] = [{"google_search": {}}]

        return payload

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
                logger.debug(f"[_process_reference_image] 从本地文件获取成功: idx={idx}")
        except Exception as e:
            logger.debug(f"[_process_reference_image] 本地文件解析失败: idx={idx} err={e}")

        # 2. 尝试规范化转换
        if not data:
            try:
                temp_cache = Path(
                    tempfile.mkdtemp(prefix="gemini_ref_tmp_", dir="/tmp")
                )
                mime_type, data = await self._normalize_image_input(
                    image_input,
                    image_input_mode=image_input_mode,
                    image_cache_dir=temp_cache,
                )
                if data:
                    logger.debug(f"[_process_reference_image] 规范化转换成功: idx={idx} mime={mime_type}")
                else:
                    logger.debug(f"[_process_reference_image] 规范化转换返回空: idx={idx}")
            except Exception as e:
                logger.debug(f"[_process_reference_image] 规范化转换失败: idx={idx} err={e}")

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
        # 确定 API 基础地址（支持反代）
        if config.api_base:
            api_base = config.api_base.rstrip("/")
            logger.debug(f"使用自定义 API Base: {api_base}")
        else:
            if config.api_type == "google":
                api_base = self.GOOGLE_API_BASE
            else:  # openai 兼容格式
                api_base = self.OPENAI_API_BASE

            logger.debug(f"使用默认 API Base ({config.api_type}): {api_base}")

        # 智能构建完整URL，自动添加正确的路径前缀（如果需要的话）
        if config.api_type == "google":
            # Google API 需要版本前缀
            if not config.api_base or api_base == self.GOOGLE_API_BASE:
                # 使用默认官方地址，直接使用完整路径
                url = f"{api_base}/models/{config.model}:generateContent"
            elif not any(api_base.endswith(suffix) for suffix in ["/v1beta", "/v1"]):
                # 自定义地址但没有版本前缀，自动添加
                url = f"{api_base}/v1beta/models/{config.model}:generateContent"
                logger.debug("为Google API自动添加v1beta前缀")
            else:
                # 已经包含版本前缀，直接使用
                url = f"{api_base}/models/{config.model}:generateContent"
                logger.debug("使用已包含版本前缀的Google API地址")

            payload = await self._prepare_google_payload(config)
            headers = {
                "x-goog-api-key": config.api_key,
                "Content-Type": "application/json",
            }
        else:
            # OpenAI 兼容格式
            if not config.api_base or api_base == self.OPENAI_API_BASE:
                # 使用默认地址，需要完整路径
                url = f"{api_base}/chat/completions"
            elif not any(api_base.endswith(suffix) for suffix in ["/v1", "/v1beta"]):
                # 自定义地址但没有版本前缀，自动添加
                url = f"{api_base}/v1/chat/completions"
                logger.debug("为OpenAI兼容API自动添加v1前缀")
            else:
                # 已经包含版本前缀，直接使用
                url = f"{api_base}/chat/completions"
                logger.debug("使用已包含版本前缀的OpenAI兼容API地址")

            payload = await self._prepare_openai_payload(config)
            headers = {
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/astrbot",
                "X-Title": "AstrBot Gemini Image Advanced",
            }

        logger.debug(f"智能构建API URL: {url}")
        return url, headers, payload

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
    ) -> tuple[list[str], list[str], str | None, str | None]:
        """执行 API 请求并处理响应，每个重试有独立的超时控制"""

        current_retry = 0
        last_error = None

        while current_retry < max_retries:
            try:
                # 每个重试使用独立的超时控制，不共享总超时时间
                timeout = aiohttp.ClientTimeout(
                    total=total_timeout, sock_read=total_timeout
                )
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    logger.debug(f"发送请求（重试 {current_retry}/{max_retries - 1}）")
                    return await self._perform_request(
                        session, url, payload, headers, api_type, model
                    )

            except asyncio.CancelledError:
                # 只有框架取消才不重试（这是最顶层的超时）
                logger.debug("请求被框架取消（工具调用总超时），不再重试")
                timeout_msg = "图像生成时间过长，超出了框架限制。请尝试简化图像描述或在框架配置中增加 tool_call_timeout 到 90-120 秒。"
                raise APIError(timeout_msg, None, "cancelled") from None
            except Exception as e:
                error_msg = str(e)
                error_type = self._classify_error(e, error_msg)

                # 判断是否可重试的错误
                if self._is_retryable_error(error_type, e):
                    last_error = APIError(error_msg, None, error_type)
                    logger.warning(
                        f"可重试错误 (重试 {current_retry + 1}/{max_retries}): {error_msg}"
                    )

                    current_retry += 1
                    if current_retry < max_retries:
                        # 指数退避延迟：2秒、4秒、8秒……最大10秒
                        delay = min(2 ** (current_retry + 1), 10)
                        logger.debug(f"等待 {delay} 秒后重试...")
                        await asyncio.sleep(delay)
                        continue  # 继续下一次重试
                    else:
                        logger.error(f"达到最大重试次数 ({max_retries})，生成失败")
                else:
                    # 不可重试的错误，立即抛出
                    logger.error(f"不可重试错误: {error_msg}")
                    raise APIError(error_msg, None, error_type) from None

        # 如果都失败了，返回最后一次错误
        if last_error:
            raise last_error

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
            url, json=payload, headers=headers, proxy=self.proxy
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
                    return await self._parse_openai_response(response_data, session)
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
        import asyncio

        parse_start = asyncio.get_event_loop().time()
        logger.debug("🔍 开始解析API响应数据...")

        image_urls: list[str] = []
        image_paths: list[str] = []
        text_chunks: list[str] = []
        thought_signature = None
        fallback_texts = self._collect_fallback_texts(response_data)

        if "candidates" not in response_data or not response_data["candidates"]:
            logger.warning(
                "Google 响应缺少 candidates 字段，尝试从 fallback 文本提取图像"
            )
            appended = False
            if fallback_texts:
                appended = await self._append_images_from_texts(
                    fallback_texts, image_urls, image_paths
                )
            if appended and (image_urls or image_paths):
                text_content = (
                    " ".join(t.strip() for t in fallback_texts if t and t.strip())
                    or None
                )
                return image_urls, image_paths, text_content, thought_signature

            if "promptFeedback" in response_data:
                feedback = response_data["promptFeedback"]
                logger.warning(f"请求被阻止: {feedback}")
            else:
                logger.error("响应中没有 candidates，fallback 提取也失败")
                logger.debug(f"完整响应: {str(response_data)[:1000]}")
                logger.debug(f"fallback_texts: {fallback_texts}")
            return [], [], None, None

        candidates = response_data["candidates"]
        logger.debug(f"📝 找到 {len(candidates)} 个候选结果")

        for idx, candidate in enumerate(candidates):
            finish_reason = candidate.get("finishReason")
            if finish_reason in ["SAFETY", "RECITATION"]:
                logger.warning(f"候选 {idx} 生成被阻止: {finish_reason}")
                continue

            content = candidate.get("content", {})
            parts = content.get("parts") or []
            logger.debug(f"📋 候选 {idx} 包含 {len(parts)} 个部分")

            for i, part in enumerate(parts):
                try:
                    logger.debug(f"检查候选 {idx} 的第 {i} 个part: {list(part.keys())}")

                    if "thoughtSignature" in part and not thought_signature:
                        thought_signature = part["thoughtSignature"]
                        logger.debug(f"🧠 找到思维签名: {thought_signature[:50]}...")

                    # 累积文本，便于后续从文本中提取 data URI / http(s) 链接
                    if "text" in part and isinstance(part.get("text"), str):
                        text_chunks.append(part.get("text", ""))

                    inline_data = part.get("inlineData") or part.get("inline_data")
                    if inline_data and not part.get("thought", False):
                        mime_type = (
                            inline_data.get("mimeType")
                            or inline_data.get("mime_type")
                            or "image/png"
                        )
                        base64_data = inline_data.get("data", "")

                        logger.debug(
                            f"🎯 找到图像数据 (候选{idx} 第{i + 1}部分): {mime_type}, 大小: {len(base64_data)} 字符"
                        )

                        if base64_data:
                            image_format = (
                                mime_type.split("/")[1] if "/" in mime_type else "png"
                            )

                            logger.debug("💾 开始保存图像文件...")
                            save_start = asyncio.get_event_loop().time()

                            saved_path = await save_base64_image(
                                base64_data, image_format
                            )

                            save_end = asyncio.get_event_loop().time()
                            logger.debug(
                                f"✅ 图像保存完成，耗时: {save_end - save_start:.2f}秒"
                            )

                            if saved_path:
                                image_paths.append(saved_path)
                                image_urls.append(saved_path)
                            else:
                                # 保存失败时尝试宽松解码并写入临时文件，避免误判为无图
                                try:
                                    import tempfile

                                    tmp_path = Path(
                                        tempfile.mktemp(
                                            prefix="gem_inline_", suffix=".png"
                                        )
                                    )
                                    cleaned = base64_data.strip().replace("\n", "")
                                    if ";base64," in cleaned:
                                        _, _, cleaned = cleaned.partition(";base64,")
                                    raw = base64.b64decode(cleaned, validate=False)
                                    tmp_path.write_bytes(raw)
                                    image_paths.append(str(tmp_path))
                                    image_urls.append(str(tmp_path))
                                    logger.debug(
                                        "⚠️ save_base64_image 失败，已使用宽松解码写入临时文件: %s",
                                        tmp_path,
                                    )
                                except Exception as e:
                                    logger.warning(
                                        "候选 %s 第 %s 部分 inlineData 解码失败，跳过：%s",
                                        idx,
                                        i + 1,
                                        e,
                                    )
                        else:
                            logger.warning(
                                f"候选 {idx} 的第 {i} 个part有inlineData但data为空"
                            )
                    elif "thought" in part and part.get("thought", False):
                        logger.debug(f"候选 {idx} 的第 {i} 个part是思考内容")
                    else:
                        logger.debug(
                            f"候选 {idx} 的第 {i} 个part不是图像也不是思考: {list(part.keys())}"
                        )
                except Exception as e:
                    logger.error(
                        f"处理候选 {idx} 的第 {i} 个part时出错: {e}", exc_info=True
                    )

        logger.debug(f"🖼️ 共找到 {len(image_paths)} 张图片")

        # 文本中尝试解析可能的图像URL或Base64（用于只返回文本的情况）
        if text_chunks:
            extracted_urls: list[str] = []
            extracted_paths: list[str] = []
            for chunk in text_chunks:
                # http(s) 图片链接
                extracted_urls.extend(self._find_image_urls_in_text(chunk))
                # data URI / base64
                urls2, paths2 = await self._extract_from_content(chunk)
                extracted_urls.extend(urls2)
                extracted_paths.extend(paths2)

            if extracted_urls or extracted_paths:
                image_urls.extend(extracted_urls)
                image_paths.extend(extracted_paths)

        text_content = (
            " ".join(chunk for chunk in text_chunks if chunk).strip()
            if text_chunks
            else None
        )
        if text_content:
            logger.debug(f"🎯 找到文本内容: {text_content[:100]}...")

        if not (image_paths or image_urls) and fallback_texts:
            appended = await self._append_images_from_texts(
                fallback_texts, image_urls, image_paths
            )
            if appended and not text_content:
                text_content = (
                    " ".join(t.strip() for t in fallback_texts if t and t.strip())
                    or text_content
                )

        if image_paths or image_urls:
            parse_end = asyncio.get_event_loop().time()
            logger.debug(f"🎉 API响应解析完成，总耗时: {parse_end - parse_start:.2f}秒")
            return image_urls, image_paths, text_content, thought_signature

        if text_content:
            logger.warning("API只返回了文本响应，未生成图像，将触发重试")
            logger.debug(f"Google响应内容: {str(response_data)[:1000]}")
            raise APIError(
                f"图像生成失败：API只返回了文本响应，正在重试... | 响应预览: {str(response_data)[:300]}",
                500,
                "no_image_retry",
            )

        logger.warning(f"未在响应中找到图像数据，响应内容: {str(response_data)[:500]}")
        raise APIError(
            f"图像生成失败：响应格式异常，未找到有效的图像数据 | 响应: {str(response_data)[:300]}",
            None,
            "invalid_response",
        )

    async def _parse_openai_response(
        self, response_data: dict, session: aiohttp.ClientSession
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
                    # 对于可访问的 http(s) 链接，直接返回 URL，避免重复下载占用带宽
                    if candidate_url.startswith("http://") or candidate_url.startswith(
                        "https://"
                    ):
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
        return image_urls, image_paths, text_content, thought_signature

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
        raw_pattern = (
            r"(https?://[^\s)]+\.(?:png|jpe?g|gif|webp|bmp|tiff|avif))(?:\b|$)"
        )
        spaced_pattern = r"(https?\s*:\s*/\s*/[^\s)]+)"

        urls: list[str] = []
        seen: set[str] = set()

        def _push(candidate: str):
            cleaned = candidate.strip().replace("&amp;", "&").rstrip(").,;")
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                urls.append(cleaned)

        for pattern in (markdown_pattern, markdown_data_uri_pattern, raw_pattern):
            for match in re.findall(pattern, text, flags=re.IGNORECASE):
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
