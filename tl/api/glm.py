"""GLM（智谱AI）CogView 图像生成供应商实现。

用于调用智谱 AI 的 CogView 系列模型进行图像生成。
API 文档: https://open.bigmodel.cn/dev/api/image/cogview
"""

from __future__ import annotations

import time
from typing import Any

import aiohttp

from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from ..tl_utils import save_base64_image
from .base import ProviderRequest


class GLMProvider:
    """GLM（智谱AI）CogView 图像生成供应商。

    支持 CogView-4 等系列模型，采用智谱 AI 专有 API 格式。
    """

    name = "glm"

    # GLM CogView API 默认端点
    GLM_API_BASE = "https://open.bigmodel.cn/api/paas/v4"
    DEFAULT_MODEL = "cogView-4-250304"

    # 尺寸映射：将通用尺寸映射到 GLM 支持的格式
    SIZE_MAPPING = {
        # 1:1 比例
        "1K": "1024x1024",
        "1024x1024": "1024x1024",
        "512x512": "512x512",
        "768x768": "768x768",
        # 16:9 比例
        "1920x1080": "1920x1080",
        "1280x720": "1280x720",
        # 9:16 比例
        "1080x1920": "1080x1920",
        "720x1280": "720x1280",
        # 4:3 比例
        "1024x768": "1024x768",
        # 3:4 比例
        "768x1024": "768x1024",
        # 其他常见尺寸
        "2K": "2048x2048",
        "2048x2048": "2048x2048",
    }

    # 长宽比到尺寸的映射
    ASPECT_RATIO_MAPPING = {
        "1:1": "1024x1024",
        "16:9": "1920x1080",
        "9:16": "1080x1920",
        "4:3": "1024x768",
        "3:4": "768x1024",
        "3:2": "1536x1024",
        "2:3": "1024x1536",
    }

    async def build_request(
        self, *, client: Any, config: ApiRequestConfig
    ) -> ProviderRequest:  # noqa: ANN401
        """构建 GLM CogView API 请求。"""
        api_base = (config.api_base or "").rstrip("/")
        if not api_base:
            api_base = self.GLM_API_BASE

        url = f"{api_base}/images/generations"
        logger.debug(f"GLM CogView API URL: {url}")

        # 确定模型
        model = config.model or self.DEFAULT_MODEL

        # 确定尺寸
        size = self._resolve_size(config)

        # 构建请求体
        payload: dict[str, Any] = {
            "model": model,
            "prompt": config.prompt,
        }

        if size:
            payload["size"] = size

        # 构建请求头
        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }

        logger.debug(f"GLM CogView 请求 payload: model={model}, size={size}")
        return ProviderRequest(url=url, headers=headers, payload=payload)

    def _resolve_size(self, config: ApiRequestConfig) -> str | None:
        """解析并映射图像尺寸。"""
        # 优先使用 resolution
        if config.resolution:
            resolution = config.resolution.strip()
            if resolution in self.SIZE_MAPPING:
                return self.SIZE_MAPPING[resolution]
            # 如果是有效的 WxH 格式，直接使用
            if "x" in resolution.lower():
                return resolution.lower()
            logger.debug(f"未知分辨率 '{resolution}'，使用默认 1024x1024")
            return "1024x1024"

        # 其次使用 aspect_ratio
        if config.aspect_ratio:
            aspect = config.aspect_ratio.strip()
            if aspect in self.ASPECT_RATIO_MAPPING:
                return self.ASPECT_RATIO_MAPPING[aspect]
            logger.debug(f"未知长宽比 '{aspect}'，使用默认 1024x1024")
            return "1024x1024"

        # 默认尺寸
        return "1024x1024"

    async def parse_response(
        self,
        *,
        client: Any,
        response_data: dict[str, Any],
        session: aiohttp.ClientSession,
        api_base: str | None = None,
    ) -> tuple[list[str], list[str], str | None, str | None]:  # noqa: ANN401
        """解析 GLM CogView API 响应。

        GLM CogView 响应格式:
        {
            "created": 1234567890,
            "data": [
                {"url": "https://..."}
            ]
        }

        返回: (image_urls, image_paths, text_content, finish_reason)
        """
        image_urls: list[str] = []
        image_paths: list[str] = []
        text_content: str | None = None
        finish_reason: str | None = None

        # 检查错误响应
        if "error" in response_data:
            error_info = response_data["error"]
            error_msg = error_info.get("message", str(error_info))
            raise APIError(f"GLM API 错误: {error_msg}")

        # 解析 data 数组
        data_list = response_data.get("data", [])
        if not data_list:
            logger.warning("GLM CogView 响应中没有图像数据")
            return image_urls, image_paths, text_content, finish_reason

        for idx, item in enumerate(data_list):
            # 处理 URL 格式
            if "url" in item:
                url = item["url"]
                if url:
                    image_urls.append(url)
                    logger.debug(f"GLM CogView 图像 URL [{idx}]: {url[:80]}...")

                    # 下载图像
                    try:
                        _, image_path = await client._download_image(
                            url, session, use_cache=True
                        )
                        if image_path:
                            image_paths.append(image_path)
                            logger.debug(f"GLM CogView 图像已下载: {image_path}")
                    except Exception as e:
                        logger.warning(f"GLM CogView 图像下载失败: {e}")

            # 处理 base64 格式（如果有）
            elif "b64_json" in item:
                b64_data = item["b64_json"]
                if b64_data:
                    try:
                        image_path = await save_base64_image(b64_data, "png")
                        if image_path:
                            image_paths.append(image_path)
                            logger.debug(f"GLM CogView base64 图像已保存: {image_path}")
                    except Exception as e:
                        logger.warning(f"GLM CogView base64 图像保存失败: {e}")

        # 检查 revised_prompt（如果有）
        if data_list and "revised_prompt" in data_list[0]:
            text_content = data_list[0]["revised_prompt"]

        return image_urls, image_paths, text_content, finish_reason
