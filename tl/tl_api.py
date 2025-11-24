"""
API客户端模块 y
提供Google Gemini和OpenAI兼容API的客户端实现
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger

# 导入本地模块
try:
    from .tl_utils import save_base64_image, save_image_data
except ImportError:
    # 如果tl_utils不存在，先创建简单的占位符
    async def save_base64_image(base64_data: str, image_format: str = "png") -> str | None:
        """占位符函数"""
        return None

    async def save_image_data(image_data: bytes, image_format: str = "png") -> str | None:
        """占位符函数"""
        return None


@dataclass
class ApiRequestConfig:
    """API 请求配置"""

    model: str
    prompt: str
    api_type: str = "openai"
    api_base: str | None = None
    api_key: str | None = None
    resolution: str | None = None
    aspect_ratio: str | None = None
    enable_grounding: bool = False
    response_modalities: str = "TEXT_IMAGE"
    max_tokens: int = 1000
    reference_images: list[str] | None = None
    response_text: str | None = None  # 存储文本响应
    enable_smart_retry: bool = True  # 智能重试开关
    enable_text_response: bool = False  # 文本响应开关


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
    - 支持 Google 官方 API 和 OpenRouter API
    - 支持自定义 API Base URL（反代）
    - 支持任意模型名称
    - 遵循官方 Gemini API 规范
    """

    # Google 官方 API 默认地址
    GOOGLE_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

    # OpenRouter API 默认地址
    OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"

    def __init__(self, api_keys: list[str]):
        """
        初始化 API 客户端

        Args:
            api_keys: API 密钥列表
        """
        self.api_keys = api_keys or []
        self.current_key_index = 0
        self._lock = asyncio.Lock()
        logger.debug(f"API 客户端已初始化，支持 {len(self.api_keys)} 个 API 密钥")

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

    @staticmethod
    def _prepare_google_payload(config: ApiRequestConfig) -> dict[str, Any]:
        """准备 Google 官方 API 请求负载（遵循官方规范）"""
        parts = [{"text": config.prompt}]

        if config.reference_images:
            for base64_image in config.reference_images[:14]:
                if not base64_image.startswith("data:image/"):
                    base64_image = f"data:image/png;base64,{base64_image}"

                if ";base64," in base64_image:
                    header, data = base64_image.split(";base64,", 1)
                    mime_type = header.replace("data:", "")
                else:
                    mime_type = "image/png"
                    data = base64_image

                parts.append({"inlineData": {"mimeType": mime_type, "data": data}})

        contents = [{"role": "user", "parts": parts}]

        generation_config = {"responseModalities": []}

        # 响应模态配置，包含降级处理
        modalities_map = {
            "TEXT": ["TEXT"],
            "IMAGE": ["IMAGE"],
            "TEXT_IMAGE": ["TEXT", "IMAGE"],
        }

        # 降级策略：优先使用兼容性更好的模式
        modalities = modalities_map.get(config.response_modalities, ["TEXT", "IMAGE"])
        if "IMAGE" in modalities and "TEXT" not in modalities:
            logger.debug("降级处理：将 IMAGE 模式改为 TEXT_IMAGE 以提供更好的兼容性")
            modalities = ["TEXT", "IMAGE"]

        generation_config["responseModalities"] = modalities
        logger.debug(f"响应模态: {modalities}")

        image_config = {}
        if config.resolution:
            resolution = config.resolution.upper()
            if resolution in ["1K", "2K", "4K"]:
                image_config["imageSize"] = resolution
                logger.debug(f"设置分辨率: {resolution}")
            else:
                logger.warning(f"不支持的分辨率: {config.resolution}，将使用默认分辨率")

        if config.aspect_ratio and ":" in config.aspect_ratio:
            image_config["aspectRatio"] = config.aspect_ratio
            logger.debug(f"设置长宽比: {config.aspect_ratio}")
        elif config.aspect_ratio:
            logger.warning(
                f"不支持的长宽比格式: {config.aspect_ratio}，将使用默认长宽比"
            )

        if image_config:
            generation_config["imageConfig"] = image_config

        tools = []
        if config.enable_grounding:
            tools.append({"google_search": {}})

        payload = {"contents": contents, "generationConfig": generation_config}

        if tools:
            payload["tools"] = tools

        return payload

    @staticmethod
    def _prepare_openrouter_payload(config: ApiRequestConfig) -> dict[str, Any]:
        """准备 OpenRouter API 请求负载"""
        message_content = [
            {"type": "text", "text": f"Generate an image: {config.prompt}"}
        ]

        if config.reference_images:
            for base64_image in config.reference_images[:6]:
                if not base64_image.startswith("data:image/"):
                    base64_image = f"data:image/png;base64,{base64_image}"

                message_content.append(
                    {"type": "image_url", "image_url": {"url": base64_image}}
                )

        payload = {
            "model": config.model,
            "messages": [{"role": "user", "content": message_content}],
            "max_tokens": config.max_tokens,
            "temperature": 0.7,
        }

        return payload

    def _get_api_url(
        self, config: ApiRequestConfig
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """
        根据配置获取 API URL、请求头和负载

        支持自定义 API Base URL（反代）
        """
        # 确定 API 基础地址（支持反代）
        if config.api_base:
            api_base = config.api_base.rstrip("/")
            logger.debug(f"使用自定义 API Base: {api_base}")
        else:
            if config.api_type == "google":
                api_base = self.GOOGLE_API_BASE
            else:  # openai 兼容格式
                api_base = self.OPENROUTER_API_BASE

            logger.debug(f"使用默认 API Base ({config.api_type}): {api_base}")

        # 准备请求
        if config.api_type == "google":
            url = f"{api_base}/models/{config.model}:generateContent"
            payload = self._prepare_google_payload(config)
            headers = {
                "x-goog-api-key": config.api_key,
                "Content-Type": "application/json",
            }
        else:
            url = f"{api_base}/chat/completions"
            payload = self._prepare_openrouter_payload(config)
            headers = {
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/astrbot",
                "X-Title": "AstrBot Gemini Image Advanced",
            }

        logger.debug(f"准备请求到: {url}")

        return url, headers, payload

    async def generate_image(
        self, config: ApiRequestConfig, max_retries: int = 3, total_timeout: int = 120, per_retry_timeout: int = None, max_total_time: int = None
    ) -> tuple[str | None, str | None, str | None]:
        """
        生成图像

        Args:
            config: 请求配置
            max_retries: 最大重试次数
            total_timeout: 总超时时间（秒）

        Returns:
            (image_url, image_path, text_content) 或 (None, None, None) 如果失败
        """
        if not self.api_keys:
            raise ValueError("未配置 API 密钥")

        if not config.api_key:
            config.api_key = await self.get_next_api_key()

        # 获取请求信息
        url, headers, payload = self._get_api_url(config)

        logger.debug(f"使用 {config.model} (通过 {config.api_type}) 生成图像")
        logger.debug(f"API 端点: {url[:80]}...")

        if config.resolution or config.aspect_ratio:
            logger.debug(
                f"分辨率: {config.resolution or '默认'}, 长宽比: {config.aspect_ratio or '默认'}"
            )

        if config.api_base:
            logger.debug(f"使用自定义 API Base: {config.api_base}")

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
    ) -> tuple[str | None, str | None, str | None]:
        """执行 API 请求并处理响应，每个重试有独立的超时控制"""

        current_retry = 0
        last_error = None

        while current_retry < max_retries:
            try:
                # 每个重试使用独立的超时控制，不共享总超时时间
                async with aiohttp.ClientSession() as session:
                    logger.debug(f"发送请求（重试 {current_retry + 1}/{max_retries}）")
                    return await asyncio.wait_for(
                        self._perform_request(session, url, payload, headers, api_type, model),
                        timeout=total_timeout
                    )

            except asyncio.CancelledError:
                # 只有框架取消才不重试（这是最顶层的超时）
                logger.debug("请求被框架取消（工具调用总超时），不再重试")
                timeout_msg = "图像生成时间过长，超出了框架限制。请尝试简化图像描述或在框架配置中增加 tool_call_timeout 到 90-120 秒。"
                raise APIError(timeout_msg, None, "cancelled")
            except Exception as e:
                error_msg = str(e)
                error_type = self._classify_error(e, error_msg)

                # 判断是否可重试的错误
                if self._is_retryable_error(error_type, e):
                    last_error = APIError(error_msg, None, error_type)
                    logger.warning(f"可重试错误 (重试 {current_retry + 1}/{max_retries}): {error_msg}")

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
                    raise APIError(error_msg, None, error_type)

        # 如果都失败了，返回最后一次错误
        if last_error:
            raise last_error

        return None, None, None

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
    ) -> tuple[str | None, str | None, str | None]:
        """执行实际的HTTP请求"""
        logger.debug(f"发送请求到: {url[:100]}...")

        async with session.post(url, json=payload, headers=headers) as response:
            response_text = await response.text()
            logger.debug(f"响应状态: {response.status}")

            response_data = json.loads(response_text) if response_text else {}

            if response.status == 200:
                logger.debug("API 调用成功")
                if api_type == "google":
                    return await self._parse_gresponse(response_data, session)
                else:  # openai 兼容格式
                    return await self._parse_openrouter_response(response_data, session)
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

    async def _parse_gresponse(
        self, response_data: dict, session: aiohttp.ClientSession
    ) -> tuple[str | None, str | None, str | None]:
        """解析 Google 官方 API 响应"""
        import asyncio

        parse_start = asyncio.get_event_loop().time()
        logger.debug("🔍 开始解析API响应数据...")

        if "candidates" not in response_data or not response_data["candidates"]:
            if "promptFeedback" in response_data:
                feedback = response_data["promptFeedback"]
                logger.warning(f"请求被阻止: {feedback}")
            else:
                logger.error(f"响应中没有 candidates: {response_data}")
            return None, None, None

        candidate = response_data["candidates"][0]
        logger.debug(f"📝 找到 {len(response_data['candidates'])} 个候选结果")

        if "finishReason" in candidate and candidate["finishReason"] in [
            "SAFETY",
            "RECITATION",
        ]:
            logger.warning(f"生成被阻止: {candidate['finishReason']}")
            return None, None, None

        if "content" not in candidate or "parts" not in candidate["content"]:
            logger.error("响应格式不正确")
            return None, None, None

        parts = candidate["content"]["parts"]
        logger.debug(f"📋 响应包含 {len(parts)} 个部分")

        # 处理思考过程
        thought_parts = [p for p in parts if "thought" in p and p["thought"] is True]
        if thought_parts:
            logger.debug(f"检测到 {len(thought_parts)} 个思考步骤（Gemini 3）")

        # 查找图像
        image_url = None
        image_path = None
        text_content = None

        logger.debug(f"🖼️ 搜索图像数据... (共 {len(parts)} 个part)")
        for i, part in enumerate(parts):
            logger.debug(f"检查第 {i} 个part: {list(part.keys())}")
            if "inlineData" in part and not part.get("thought", False):
                inline_data = part["inlineData"]
                mime_type = inline_data.get("mimeType", "image/png")
                base64_data = inline_data.get("data", "")

                logger.debug(
                    f"🎯 找到图像数据 (第{i + 1}部分): {mime_type}, 大小: {len(base64_data)} 字符"
                )

                if base64_data:
                    image_format = (
                        mime_type.split("/")[1] if "/" in mime_type else "png"
                    )

                    logger.debug("💾 开始保存图像文件...")
                    save_start = asyncio.get_event_loop().time()

                    image_path = await save_base64_image(base64_data, image_format)

                    save_end = asyncio.get_event_loop().time()
                    logger.debug(
                        f"✅ 图像保存完成，耗时: {save_end - save_start:.2f}秒"
                    )

                    if image_path:
                        image_url = f"file://{Path(image_path).absolute()}"
                else:
                    logger.warning(f"第 {i} 个part有inlineData但data为空")
            elif "thought" in part and part.get("thought", False):
                logger.debug(f"第 {i} 个part是思考内容")
            else:
                logger.debug(f"第 {i} 个part不是图像也不是思考: {list(part.keys())}")

        # 查找文本内容
        logger.debug("📝 搜索文本内容...")
        text_parts = [
            p for p in parts if "text" in p and not p.get("thought", False)
        ]
        if text_parts:
            text_content = " ".join([p["text"] for p in text_parts])
            logger.debug(f"🎯 找到文本内容: {text_content[:100]}...")

        # 如果找到了图像或文本，返回结果
        if image_url or text_content:
            parse_end = asyncio.get_event_loop().time()
            logger.debug(f"🎉 API响应解析完成，总耗时: {parse_end - parse_start:.2f}秒")
            return image_url, image_path, text_content

        # 检查是否只有文本响应（没有图像）
        if text_parts and len(text_parts) == len(
            [p for p in parts if not p.get("thought", False)]
        ):
            # 所有非思考part都是文本，没有图像
            text_content = " ".join([p["text"] for p in text_parts])
            logger.error("API只返回了文本响应，未生成图像")
            logger.error(f"文本内容: {text_content[:200]}...")
            raise APIError(
                "图像生成失败：API只返回了文本响应。请检查模型名称是否正确，可能需要使用支持图像生成的模型（如 gemini-3-pro-image-preview）",
                None,
                "no_image",
            )

        logger.error("未在响应中找到图像数据")
        raise APIError(
            "图像生成失败：响应格式异常，未找到有效的图像数据", None, "invalid_response"
        )

    async def _parse_openrouter_response(
        self, response_data: dict, session: aiohttp.ClientSession
    ) -> tuple[str | None, str | None, str | None]:
        """解析 OpenRouter API 响应"""

        image_url = None
        image_path = None
        text_content = None

        if "choices" in response_data:
            choice = response_data["choices"][0]
            message = choice.get("message", {})
            content = message.get("content", "")

            # 提取文本内容
            if content:
                text_content = content

            # 标准 images 字段
            if "images" in message and message["images"]:
                for image_item in message["images"]:
                    if "image_url" in image_item:
                        image_url = image_item["image_url"]
                        if image_url.startswith("data:image/"):
                            image_url, image_path = await self._parse_data_uri(image_url)
                        else:
                            image_url, image_path = await self._download_image(image_url, session)
                        return image_url, image_path, text_content

            # content 中查找图像
            if isinstance(content, str):
                extracted_url, extracted_path = await self._extract_from_content(content)
                if extracted_url or extracted_path:
                    return extracted_url, extracted_path, text_content

        # OpenAI 格式
        elif "data" in response_data and response_data["data"]:
            for image_item in response_data["data"]:
                if "url" in image_item:
                    image_url, image_path = await self._download_image(image_item["url"], session)
                    return image_url, image_path, text_content
                elif "b64_json" in image_item:
                    image_path = await save_base64_image(image_item["b64_json"], "png")
                    if image_path:
                        image_url = f"file://{Path(image_path).absolute()}"
                        return image_url, image_path, text_content

        # 如果只有文本内容，也返回
        if text_content:
            return None, None, text_content

        logger.warning("OpenRouter 响应格式不支持或未找到图像数据")
        return None, None, None

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
                image_url = f"file://{Path(image_path).absolute()}"
                return image_url, image_path
        except Exception as e:
            logger.error(f"解析 data URI 失败: {e}")

        return None, None

    async def _extract_from_content(self, content: str) -> tuple[str | None, str | None]:
        """从文本内容中提取图像"""
        pattern = r"data:image/([^;]+);base64,([A-Za-z0-9+/=\s]+)"
        matches = re.findall(pattern, content)

        if matches:
            image_format, base64_string = matches[0]
            image_path = await save_base64_image(base64_string, image_format)
            if image_path:
                image_url = f"file://{Path(image_path).absolute()}"
                return image_url, image_path

        return None, None

    async def _download_image(
        self, image_url: str, session: aiohttp.ClientSession
    ) -> tuple[str | None, str | None]:
        """下载并保存图像"""
        try:
            logger.debug(f"正在下载图像: {image_url[:100]}...")

            async with session.get(
                image_url, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status != 200:
                    logger.error(f"下载图像失败: HTTP {response.status}")
                    return None, None

                image_data = await response.read()
                content_type = response.headers.get("Content-Type", "")

                if "/" in content_type:
                    image_format = content_type.split("/")[1]
                else:
                    image_format = "png"

                image_path = await save_image_data(image_data, image_format)
                if image_path:
                    image_url_local = f"file://{Path(image_path).absolute()}"
                    return image_url_local, image_path
        except Exception as e:
            logger.error(f"下载图像失败: {e}")

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