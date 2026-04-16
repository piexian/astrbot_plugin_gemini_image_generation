"""xAI Images API 供应商实现。

支持：
- /v1/images/generations  文生图（JSON 请求）
- /v1/images/edits        图像编辑（JSON 请求）

xAI 的图像编辑端点要求 `application/json`，不支持 OpenAI SDK `images.edit()`
使用的 `multipart/form-data`。
"""

from __future__ import annotations

from typing import Any
import urllib.parse

import aiohttp

from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from ..tl_utils import save_base64_image
from .base import ProviderRequest

_SUPPORTED_RESOLUTIONS: frozenset[str] = frozenset({"1k", "2k"})
_SUPPORTED_RESPONSE_FORMATS: frozenset[str] = frozenset({"url", "b64_json"})
_MAX_EDIT_IMAGES = 5
_MAX_BATCH_IMAGES = 10


class XAIProvider:
    """xAI `/v1/images/*` 端点实现。"""

    name = "xai"

    async def build_request(
        self, *, client: Any, config: ApiRequestConfig
    ) -> ProviderRequest:  # noqa: ANN401
        settings: dict[str, Any] = getattr(client, "xai_settings", None) or {}
        raw_api_base = config.api_base or settings.get("api_base") or ""
        base = self._normalize_api_base(raw_api_base) or "https://api.x.ai"

        has_ref_images = bool(config.reference_images)
        endpoint = "images/edits" if has_ref_images else "images/generations"
        url = f"{base}/{endpoint}" if base.endswith("/v1") else f"{base}/v1/{endpoint}"

        if has_ref_images:
            payload = await self._prepare_edits_payload(
                client=client,
                config=config,
                settings=settings,
            )
        else:
            payload = await self._prepare_generations_payload(
                config=config,
                settings=settings,
            )

        logger.debug("[xai] URL: %s (edits=%s)", url, has_ref_images)
        return ProviderRequest(
            url=url,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            payload=payload,
        )

    async def parse_response(
        self,
        *,
        client: Any,
        response_data: dict[str, Any],
        session: aiohttp.ClientSession,
        api_base: str | None = None,
        http_status: int | None = None,
    ) -> tuple[list[str], list[str], str | None, str | None]:  # noqa: ANN401
        image_urls: list[str] = []
        image_paths: list[str] = []
        text_content = None
        thought_signature = None

        usage = response_data.get("usage")
        if isinstance(usage, dict):
            in_tok = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            total_tok = usage.get("total_tokens", in_tok + out_tok)
            logger.info(
                "[xai] Token 用量: input=%s output=%s total=%s",
                in_tok,
                out_tok,
                total_tok,
            )

        moderation_states: list[bool] = []
        respect_moderation = response_data.get("respect_moderation")
        if isinstance(respect_moderation, bool):
            moderation_states.append(respect_moderation)

        resp_output_format = response_data.get("output_format") or ""
        default_save_ext = (
            resp_output_format
            if resp_output_format in {"png", "jpeg", "webp"}
            else "png"
        )

        if not response_data.get("data"):
            error_obj = response_data.get("error")
            if error_obj:
                error_msg = (
                    error_obj.get("message", "未知错误")
                    if isinstance(error_obj, dict)
                    else str(error_obj)
                )
                logger.warning("[xai] API 返回错误: %s", error_msg)
                raise APIError(
                    f"图像生成失败: {error_msg}",
                    http_status,
                    "api_error",
                    error_obj.get("code") if isinstance(error_obj, dict) else None,
                    retryable=False,
                )
            logger.warning("[xai] 响应无 data 字段: %s", response_data)
            raise APIError(
                "API 响应格式不正确，缺少 data 字段",
                None,
                "invalid_response",
                retryable=True,
            )

        for image_item in response_data["data"]:
            if not isinstance(image_item, dict):
                continue

            item_moderation = image_item.get("respect_moderation")
            if isinstance(item_moderation, bool):
                moderation_states.append(item_moderation)

            if "url" in image_item:
                image_url = image_item["url"]
                if isinstance(image_url, str) and image_url:
                    image_urls.append(image_url)
                    logger.debug("[xai] 图片 URL: %s...", image_url[:80])
            elif "b64_json" in image_item:
                b64_data = image_item["b64_json"]
                if isinstance(b64_data, str) and b64_data:
                    item_save_ext = self._extension_from_mime_type(
                        image_item.get("mime_type")
                    )
                    image_path = await save_base64_image(
                        b64_data, item_save_ext or default_save_ext
                    )
                    if image_path:
                        image_urls.append(image_path)
                        image_paths.append(image_path)
                        logger.debug(
                            "[xai] base64 图片 (%s): %s 字节",
                            item_save_ext or default_save_ext,
                            len(b64_data),
                        )

            revised = image_item.get("revised_prompt")
            if revised:
                text_content = f"修订提示词: {revised}"
                logger.debug("[xai] 修订提示词: %s...", revised[:100])

        if moderation_states:
            logger.info("[xai] respect_moderation=%s", all(moderation_states))

        if image_urls or image_paths:
            logger.debug("[xai] 共 %s 张图片", len(image_urls))
            return image_urls, image_paths, text_content, thought_signature

        error_obj = response_data.get("error") or {}
        error_msg = (
            error_obj.get("message", "未知错误")
            if isinstance(error_obj, dict)
            else str(error_obj)
        )
        logger.warning("[xai] 未返回图片: %s", error_msg)
        raise APIError(
            f"图像生成失败: {error_msg}",
            http_status,
            "no_image",
            error_obj.get("code") if isinstance(error_obj, dict) else None,
            retryable=False,
        )

    async def _prepare_generations_payload(
        self,
        *,
        config: ApiRequestConfig,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        model = str(
            settings.get("model") or config.model or "grok-imagine-image"
        ).strip()
        payload: dict[str, Any] = {
            "model": model,
            "prompt": config.prompt,
            "n": self._get_requested_image_count(config, settings),
        }

        aspect_ratio = self._normalize_aspect_ratio(config.aspect_ratio)
        if aspect_ratio:
            payload["aspect_ratio"] = aspect_ratio

        resolution = self._normalize_resolution(config.resolution)
        if resolution:
            payload["resolution"] = resolution

        response_format = self._normalize_response_format(
            settings.get("response_format")
        )
        if response_format:
            payload["response_format"] = response_format

        quality = self._normalize_quality(settings.get("quality"))
        if quality:
            payload["quality"] = quality

        logger.debug(
            "[xai] generations payload: model=%s n=%s aspect_ratio=%s resolution=%s "
            "response_format=%s quality=%s prompt_len=%s",
            model,
            payload.get("n"),
            payload.get("aspect_ratio"),
            payload.get("resolution"),
            payload.get("response_format"),
            payload.get("quality"),
            len(config.prompt),
        )
        return payload

    async def _prepare_edits_payload(
        self,
        *,
        client: Any,
        config: ApiRequestConfig,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        ref_images = config.reference_images or []
        if not ref_images:
            raise APIError(
                "/v1/images/edits 需要至少一张参考图",
                None,
                "missing_image",
                retryable=False,
            )
        if len(ref_images) > _MAX_EDIT_IMAGES:
            raise APIError(
                f"xAI /v1/images/edits 最多支持 {_MAX_EDIT_IMAGES} 张参考图",
                None,
                "too_many_images",
                retryable=False,
            )

        model = str(
            settings.get("model") or config.model or "grok-imagine-image"
        ).strip()
        payload: dict[str, Any] = {
            "model": model,
            "prompt": config.prompt,
            "n": self._get_requested_image_count(config, settings),
        }

        image_items = [
            {"type": "image_url", "url": await self._to_image_url(client, config, ref)}
            for ref in ref_images
        ]
        if len(image_items) == 1:
            payload["image"] = image_items[0]
        else:
            payload["images"] = image_items

        if len(image_items) > 1:
            aspect_ratio = self._normalize_aspect_ratio(config.aspect_ratio)
            if aspect_ratio:
                payload["aspect_ratio"] = aspect_ratio
        elif config.aspect_ratio:
            logger.debug("[xai] 单图 edits 忽略 aspect_ratio，输出比例跟随输入图")

        resolution = self._normalize_resolution(config.resolution)
        if resolution:
            payload["resolution"] = resolution

        response_format = self._normalize_response_format(
            settings.get("response_format")
        )
        if response_format:
            payload["response_format"] = response_format

        quality = self._normalize_quality(settings.get("quality"))
        if quality:
            payload["quality"] = quality

        logger.debug(
            "[xai] edits payload: model=%s n=%s ref_images=%s aspect_ratio=%s resolution=%s "
            "response_format=%s quality=%s prompt_len=%s",
            model,
            payload.get("n"),
            len(image_items),
            payload.get("aspect_ratio"),
            payload.get("resolution"),
            payload.get("response_format"),
            payload.get("quality"),
            len(config.prompt),
        )
        return payload

    async def _to_image_url(
        self, client: Any, config: ApiRequestConfig, image_input: str
    ) -> str:  # noqa: ANN401
        image_str = str(image_input or "").strip()
        if not image_str:
            raise APIError(
                "参考图为空",
                None,
                "invalid_image",
                retryable=False,
            )

        if image_str.startswith("data:image/") and ";base64," in image_str:
            return image_str

        mime_type, b64_data = await client._normalize_image_input(
            image_str,
            image_input_mode=getattr(config, "image_input_mode", "force_base64"),
        )
        if not b64_data:
            raise APIError(
                "无法将参考图转换为 xAI 所需的 data URI",
                None,
                "invalid_image",
                retryable=False,
            )

        mime = mime_type or "image/png"
        return f"data:{mime};base64,{b64_data}"

    @staticmethod
    def _coerce_image_count(value: Any) -> int:
        try:
            count = int(value)
        except (TypeError, ValueError):
            count = 1
        if count < 1:
            return 1
        if count > _MAX_BATCH_IMAGES:
            logger.warning(
                "[xai] n=%s 超出上限，已自动降级到 %s",
                count,
                _MAX_BATCH_IMAGES,
            )
            return _MAX_BATCH_IMAGES
        return count

    @staticmethod
    def _get_requested_image_count(
        config: ApiRequestConfig, settings: dict[str, Any]
    ) -> int:
        config_n = getattr(config, "n", None)
        return XAIProvider._coerce_image_count(
            config_n if config_n is not None else settings.get("n")
        )

    @staticmethod
    def _normalize_api_base(value: Any) -> str | None:
        raw_api_base = str(value or "").strip().rstrip("/")
        if not raw_api_base:
            return None

        parsed = urllib.parse.urlsplit(raw_api_base)
        if parsed.scheme and parsed.netloc:
            path = parsed.path.rstrip("/")
            normalized_path = "/v1" if path.startswith("/v1") else ""
            normalized = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, normalized_path, "", "")
            )
            if normalized != raw_api_base:
                logger.debug(
                    "[xai] 规范化 api_base: %s -> %s",
                    raw_api_base,
                    normalized,
                )
            return normalized

        return raw_api_base

    @staticmethod
    def _normalize_aspect_ratio(value: Any) -> str | None:
        aspect_ratio = str(value or "").strip()
        return aspect_ratio or None

    @staticmethod
    def _normalize_resolution(value: Any) -> str | None:
        resolution = str(value or "").strip().lower()
        if not resolution:
            return None
        if resolution in _SUPPORTED_RESOLUTIONS:
            return resolution
        if resolution == "4k":
            logger.warning("[xai] 4K 不受支持，已自动降级为 2k")
            return "2k"
        logger.warning("[xai] 忽略不支持的 resolution=%s，仅支持 1k/2k", value)
        return None

    @staticmethod
    def _normalize_response_format(value: Any) -> str | None:
        response_format = str(value or "").strip().lower()
        if not response_format:
            return None
        if response_format in _SUPPORTED_RESPONSE_FORMATS:
            return response_format
        logger.warning(
            "[xai] 忽略不支持的 response_format=%s，仅支持 url/b64_json",
            value,
        )
        return None

    @staticmethod
    def _normalize_quality(value: Any) -> str | None:
        quality = str(value or "").strip().lower()
        return quality or None

    @staticmethod
    def _extension_from_mime_type(value: Any) -> str | None:
        mime_type = str(value or "").strip().lower()
        if not mime_type.startswith("image/"):
            return None
        ext = mime_type.split(";", 1)[0].split("/")[-1]
        if ext == "jpg":
            return "jpeg"
        if ext in {"jpeg", "png", "webp"}:
            return ext
        return None
