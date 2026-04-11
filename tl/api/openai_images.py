"""OpenAI Images API (`/v1/images/generations`) 供应商实现。

用于调用标准的 OpenAI Images API 端点，如 DALL-E 系列模型。
"""

from __future__ import annotations

from typing import Any

import aiohttp

from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from ..tl_utils import save_base64_image
from .base import ProviderRequest


class OpenAIImagesProvider:
    """OpenAI /v1/images/generations 端点实现"""

    name = "openai_images"

    async def build_request(
        self, *, client: Any, config: ApiRequestConfig
    ) -> ProviderRequest:  # noqa: ANN401
        api_base = (config.api_base or "").rstrip("/")
        default_base = "https://api.openai.com"

        if api_base:
            base = api_base
            logger.debug(f"使用自定义 API Base: {base}")
        else:
            base = default_base
            logger.debug(f"使用默认 API Base: {base}")

        # 构建 /v1/images/generations URL
        if base.endswith("/v1"):
            url = f"{base}/images/generations"
        else:
            url = f"{base}/v1/images/generations"

        payload = await self._prepare_payload(client=client, config=config)
        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }
        logger.debug(f"OpenAI Images API URL: {url}")
        return ProviderRequest(url=url, headers=headers, payload=payload)

    async def parse_response(
        self,
        *,
        client: Any,
        response_data: dict[str, Any],
        session: aiohttp.ClientSession,
        api_base: str | None = None,
        http_status: int | None = None,
    ) -> tuple[list[str], list[str], str | None, str | None]:  # noqa: ANN401
        """解析 OpenAI Images API 响应"""
        image_urls: list[str] = []
        image_paths: list[str] = []
        text_content = None
        thought_signature = None

        # OpenAI Images API 返回格式: {"data": [{"url": "...", "b64_json": "...", "revised_prompt": "..."}]}
        if not response_data.get("data"):
            logger.warning(f"OpenAI Images API 响应无 data 字段: {response_data}")
            raise APIError(
                "API 响应格式不正确，缺少 data 字段",
                None,
                "invalid_response",
                retryable=True,
            )

        for image_item in response_data["data"]:
            if not isinstance(image_item, dict):
                continue

            # 优先处理 URL
            if "url" in image_item:
                image_url = image_item["url"]
                if isinstance(image_url, str) and image_url:
                    image_urls.append(image_url)
                    logger.debug(f"[openai_images] 返回图片 URL: {image_url[:80]}...")

            # 处理 base64 图片
            elif "b64_json" in image_item:
                b64_data = image_item["b64_json"]
                if isinstance(b64_data, str) and b64_data:
                    image_path = await save_base64_image(b64_data, "png")
                    if image_path:
                        image_urls.append(image_path)
                        image_paths.append(image_path)
                        logger.debug(
                            f"[openai_images] 返回 base64 图片: {len(b64_data)} 字节"
                        )

            # 记录修订后的提示词
            if "revised_prompt" in image_item:
                revised = image_item["revised_prompt"]
                if revised:
                    text_content = f"修订提示词: {revised}"
                    logger.debug(f"OpenAI 修订提示词: {revised[:100]}...")

        if image_urls or image_paths:
            logger.debug(
                f"[openai_images] 收集到 {len(image_urls) + len(image_paths)} 张图片"
            )
            return image_urls, image_paths, text_content, thought_signature

        # 如果没有图片，检查是否有错误信息
        error_msg = response_data.get("error", {}).get("message", "未知错误")
        logger.warning(f"OpenAI Images API 未返回图片: {error_msg}")
        raise APIError(
            f"图像生成失败: {error_msg}",
            response_data.get("error", {}).get("code"),
            "no_image",
            retryable=False,
        )

    async def _prepare_payload(
        self, *, client: Any, config: ApiRequestConfig
    ) -> dict[str, Any]:  # noqa: ANN401
        """构建 OpenAI Images API 请求体"""
        # OpenAI Images API 标准参数
        payload: dict[str, Any] = {
            "model": config.model,
            "prompt": config.prompt,
            "n": 1,  # 默认生成 1 张
        }

        # 处理分辨率/尺寸参数
        _res_key = (config.resolution_param_name or "").strip()
        resolution_key = _res_key if _res_key else "size"

        if config.resolution:
            # 将 1K/2K/4K 转换为 OpenAI 格式 (1024x1024, 1792x1024 等)
            size_mapping = {
                "1K": "1024x1024",
                "2K": "1792x1024",  # 或 1024x1792 根据比例
                "4K": "2048x2048",  # DALL-E 3 最大支持
            }
            payload[resolution_key] = size_mapping.get(
                config.resolution, config.resolution
            )

        # 添加其他可选参数
        if config.seed is not None:
            payload["seed"] = config.seed

        # OpenAI Images API 不支持 reference_images（无图生图功能）
        if config.reference_images:
            logger.warning(
                "OpenAI Images API (/v1/images/generations) 不支持参考图，已忽略"
            )

        logger.debug(
            f"OpenAI Images payload: model={config.model}, size={payload.get(resolution_key)}, prompt_len={len(config.prompt)}"
        )
        return payload
