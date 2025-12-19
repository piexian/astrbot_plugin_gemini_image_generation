"""WhatAI 图像编辑供应商实现。

用于调用 WhatAI 的图像编辑 API（multipart/form-data 格式）。
API 端点: https://api.whatai.cc/v1/images/edits
"""

from __future__ import annotations

import mimetypes
import os
from typing import Any

import aiohttp

from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from ..tl_utils import (
    save_base64_image,
    resolve_image_source_to_path,
)
from .base import ProviderRequest


class WhatAIProvider:
    """WhatAI 图像编辑供应商。

    使用 multipart/form-data 格式发送请求，支持多张参考图片。
    """

    name = "whatai"

    # WhatAI API 默认端点
    WHATAI_API_BASE = "https://api.whatai.cc/v1"
    DEFAULT_MODEL = "nano-banana"

    # 尺寸映射
    SIZE_MAPPING = {
        "1K": "1K",
        "2K": "2K",
        "4K": "4K",
        "1024x1024": "1K",
        "2048x2048": "2K",
        "4096x4096": "4K",
    }

    async def build_request(
        self, *, client: Any, config: ApiRequestConfig
    ) -> ProviderRequest:  # noqa: ANN401
        """构建 WhatAI multipart/form-data 请求。"""
        api_base = (config.api_base or "").rstrip("/")
        if not api_base:
            api_base = self.WHATAI_API_BASE

        # 智能构建 URL：如果用户已经填写了完整路径（包含 /images/edits），则不再追加
        if api_base.endswith("/images/edits"):
            url = api_base
        elif "/images/edits" in api_base:
            # 用户可能填写了类似 https://api.whatai.cc/v1/images/edits 的完整路径
            url = api_base
        else:
            url = f"{api_base}/images/edits"
        logger.debug(f"WhatAI API URL: {url}")

        # 确定模型
        model = config.model or self.DEFAULT_MODEL

        # 创建 FormData
        form_data = aiohttp.FormData()

        # 添加模型
        form_data.add_field("model", model)

        # 添加提示词
        form_data.add_field("prompt", config.prompt)

        # 添加参考图片
        if config.reference_images:
            for idx, image_input in enumerate(config.reference_images):
                image_str = str(image_input).strip()
                if not image_str:
                    continue

                try:
                    # 尝试解析图片路径
                    image_path = await self._resolve_image_path(client, image_str)
                    if image_path and os.path.exists(image_path):
                        # 获取 MIME 类型
                        mime_type = mimetypes.guess_type(image_path)[0] or "application/octet-stream"
                        filename = os.path.basename(image_path)

                        # 读取文件内容
                        with open(image_path, "rb") as f:
                            image_data = f.read()

                        form_data.add_field(
                            "image",
                            image_data,
                            filename=filename,
                            content_type=mime_type,
                        )
                        logger.debug(f"WhatAI 添加参考图片 [{idx}]: {filename}")
                    else:
                        logger.warning(f"WhatAI 无法解析参考图片 [{idx}]: {image_str[:50]}...")
                except Exception as e:
                    logger.warning(f"WhatAI 处理参考图片 [{idx}] 失败: {e}")

        # 响应格式
        form_data.add_field("response_format", "url")

        # 图像尺寸
        image_size = self._resolve_size(config)
        if image_size:
            form_data.add_field("image_size", image_size)

        # 长宽比（如果有）
        if config.aspect_ratio:
            form_data.add_field("aspect_ratio", config.aspect_ratio)

        # 构建请求头（不包含 Content-Type，aiohttp 会自动处理 multipart）
        headers = {
            "Authorization": f"Bearer {config.api_key}",
        }

        logger.debug(f"WhatAI 请求: model={model}, image_size={image_size}")

        # 返回 multipart 类型的请求
        return ProviderRequest(
            url=url,
            headers=headers,
            payload={},  # multipart 请求不使用 JSON payload
            request_type="multipart",
            form_data=form_data,
        )

    async def _resolve_image_path(self, client: Any, image_input: str) -> str | None:
        """解析图片输入为本地文件路径。"""
        try:
            # 如果已经是本地路径
            if os.path.exists(image_input):
                return image_input

            # 尝试使用通用解析函数
            result = await resolve_image_source_to_path(image_input)
            if result:
                return result

            # 如果是 URL，尝试下载
            if image_input.startswith(("http://", "https://")):
                session = await client._get_session()
                _, image_path = await client._download_image(
                    image_input, session, use_cache=True
                )
                return image_path

            return None
        except Exception as e:
            logger.debug(f"解析图片路径失败: {e}")
            return None

    def _resolve_size(self, config: ApiRequestConfig) -> str | None:
        """解析并映射图像尺寸。"""
        if config.resolution:
            resolution = config.resolution.strip().upper()
            if resolution in self.SIZE_MAPPING:
                return self.SIZE_MAPPING[resolution]
            # 如果是有效的尺寸格式，直接使用
            if resolution in {"1K", "2K", "4K"}:
                return resolution
            logger.debug(f"未知分辨率 '{resolution}'，使用默认 1K")
            return "1K"
        return "1K"

    async def parse_response(
        self,
        *,
        client: Any,
        response_data: dict[str, Any],
        session: aiohttp.ClientSession,
        api_base: str | None = None,
    ) -> tuple[list[str], list[str], str | None, str | None]:  # noqa: ANN401
        """解析 WhatAI API 响应。

        WhatAI 响应格式:
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
            if isinstance(error_info, dict):
                error_msg = error_info.get("message", str(error_info))
            else:
                error_msg = str(error_info)
            raise APIError(f"WhatAI API 错误: {error_msg}")

        # 解析 data 数组
        data_list = response_data.get("data", [])
        if not data_list:
            logger.warning("WhatAI 响应中没有图像数据")
            return image_urls, image_paths, text_content, finish_reason

        for idx, item in enumerate(data_list):
            # 处理 URL 格式
            if "url" in item:
                url = item["url"]
                if url:
                    image_urls.append(url)
                    logger.debug(f"WhatAI 图像 URL [{idx}]: {url[:80]}...")

                    # 下载图像
                    try:
                        _, image_path = await client._download_image(
                            url, session, use_cache=True
                        )
                        if image_path:
                            image_paths.append(image_path)
                            logger.debug(f"WhatAI 图像已下载: {image_path}")
                    except Exception as e:
                        logger.warning(f"WhatAI 图像下载失败: {e}")

            # 处理 base64 格式（如果有）
            elif "b64_json" in item:
                b64_data = item["b64_json"]
                if b64_data:
                    try:
                        image_path = await save_base64_image(b64_data, "png")
                        if image_path:
                            image_paths.append(image_path)
                            logger.debug(f"WhatAI base64 图像已保存: {image_path}")
                    except Exception as e:
                        logger.warning(f"WhatAI base64 图像保存失败: {e}")

        # 检查 revised_prompt（如果有）
        if data_list and "revised_prompt" in data_list[0]:
            text_content = data_list[0]["revised_prompt"]

        return image_urls, image_paths, text_content, finish_reason
