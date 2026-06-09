"""Agnes AI image generation provider."""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

import aiohttp
from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from ..tl_utils import save_base64_image
from .base import ProviderRequest
from .data_uri import format_data_uri

_DEFAULT_API_BASE = "https://apihub.agnes-ai.com"
_DEFAULT_MODEL = "agnes-image-2.1-flash"
_SUPPORTED_RESPONSE_FORMATS: frozenset[str] = frozenset({"url", "b64_json"})
_REFERENCE_IMAGE_MODES: frozenset[str] = frozenset({"auto", "base64", "url"})
_RESOLUTION_MAP: dict[str, int] = {"1K": 1024, "2K": 2048, "4K": 3840}
_SIZE_RE = re.compile(r"^\s*(\d{2,5})\s*[xX×]\s*(\d{2,5})\s*$")


class AgnesAIProvider:
    """Agnes AI `/v1/images/generations` implementation."""

    name = "agnes_ai"

    async def build_request(
        self, *, client: Any, config: ApiRequestConfig
    ) -> ProviderRequest:  # noqa: ANN401
        settings: dict[str, Any] = (
            getattr(config, "provider_settings", None)
            or getattr(client, "agnes_ai_settings", None)
            or {}
        )
        raw_api_base = config.api_base or settings.get("api_base")
        base = self._normalize_api_base(raw_api_base) or _DEFAULT_API_BASE
        url = (
            f"{base}/images/generations"
            if self._api_base_includes_version_path(base)
            else f"{base}/v1/images/generations"
        )
        payload = await self._prepare_payload(
            client=client,
            config=config,
            settings=settings,
        )

        logger.debug(
            f"[agnes_ai] URL={url} refs={len(config.reference_images or [])} "
            f"response_format={settings.get('response_format') or 'url'}"
        )
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
        request_config: ApiRequestConfig | None = None,
    ) -> tuple[list[str], list[str], str | None, str | None]:  # noqa: ANN401
        image_urls: list[str] = []
        image_paths: list[str] = []
        text_content = None

        data = response_data.get("data")
        if not isinstance(data, list) or not data:
            error_obj = response_data.get("error")
            if error_obj:
                error_msg = (
                    error_obj.get("message", "未知错误")
                    if isinstance(error_obj, dict)
                    else str(error_obj)
                )
                logger.warning(f"[agnes_ai] API 返回错误: {error_msg}")
                raise APIError(
                    f"图像生成失败: {error_msg}",
                    http_status,
                    "api_error",
                    error_obj.get("code") if isinstance(error_obj, dict) else None,
                    retryable=False,
                )
            logger.warning(f"[agnes_ai] 响应无 data 字段: {response_data}")
            raise APIError(
                "API 响应格式不正确，缺少 data 字段",
                http_status,
                "invalid_response",
                retryable=True,
            )

        resp_output_format = str(response_data.get("output_format") or "").lower()
        default_save_ext = (
            resp_output_format
            if resp_output_format in {"png", "jpeg", "webp"}
            else "png"
        )

        for image_item in data:
            if not isinstance(image_item, dict):
                continue

            image_url = image_item.get("url")
            if isinstance(image_url, str) and image_url:
                if self._request_has_proxy(client, request_config):
                    image_path = await self._download_image(
                        client, image_url, session, request_config
                    )
                    if image_path:
                        image_urls.append(image_path)
                        image_paths.append(image_path)
                        continue
                image_urls.append(image_url)
                logger.debug(f"[agnes_ai] 图片 URL: {image_url[:80]}...")

            b64_data = image_item.get("b64_json")
            if isinstance(b64_data, str) and b64_data:
                save_ext = (
                    self._extension_from_mime_type(image_item.get("mime_type"))
                    or default_save_ext
                )
                image_path = await save_base64_image(b64_data, save_ext)
                if image_path:
                    image_urls.append(image_path)
                    image_paths.append(image_path)
                    logger.debug(f"[agnes_ai] base64 图片已保存: {image_path}")

            revised = image_item.get("revised_prompt")
            if isinstance(revised, str) and revised:
                text_content = f"修订提示词: {revised}"

        if image_urls or image_paths:
            return image_urls, image_paths, text_content, None

        error_obj = response_data.get("error") or {}
        error_msg = (
            error_obj.get("message", "未知错误")
            if isinstance(error_obj, dict)
            else str(error_obj)
        )
        logger.warning(f"[agnes_ai] 未返回图片: {error_msg}")
        raise APIError(
            f"图像生成失败: {error_msg}",
            http_status,
            "no_image",
            error_obj.get("code") if isinstance(error_obj, dict) else None,
            retryable=False,
        )

    async def _prepare_payload(
        self,
        *,
        client: Any,
        config: ApiRequestConfig,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        model = str(settings.get("model") or config.model or _DEFAULT_MODEL).strip()
        payload: dict[str, Any] = {
            "model": model,
            "prompt": config.prompt,
        }
        if not config.suppress_resolution:
            payload["size"] = self._resolve_size(
                settings.get("size") or config.resolution,
                config.aspect_ratio or settings.get("aspect_ratio"),
            )

        response_format = self._normalize_response_format(
            settings.get("response_format")
        )
        extra_body: dict[str, Any] = {}

        ref_images = config.reference_images or []
        if ref_images:
            extra_body["image"] = [
                await self._to_image_input(client, config, image_input, settings)
                for image_input in ref_images
            ]
            extra_body["response_format"] = response_format
        elif response_format == "b64_json":
            payload["return_base64"] = True
        else:
            extra_body["response_format"] = response_format

        if extra_body:
            payload["extra_body"] = extra_body

        logger.debug(
            f"[agnes_ai] payload: model={model} size={payload.get('size')} "
            f"refs={len(ref_images)} response_format={response_format}"
        )
        return payload

    async def _to_image_input(
        self,
        client: Any,
        config: ApiRequestConfig,
        image_input: Any,
        settings: dict[str, Any],
    ) -> str:  # noqa: ANN401
        image_str = str(image_input or "").strip()
        if not image_str:
            raise APIError(
                "参考图为空",
                None,
                "invalid_image",
                retryable=False,
            )

        mode = self._normalize_reference_image_mode(
            settings.get("reference_image_mode")
        )
        is_url = image_str.startswith(("http://", "https://"))
        if mode == "url" and not is_url:
            raise APIError(
                "Agnes AI reference_image_mode=url 时参考图必须是 http(s) URL",
                None,
                "invalid_image",
                retryable=False,
            )
        if is_url and mode in {"auto", "url"}:
            return image_str
        if image_str.startswith("data:image/") and ";base64," in image_str:
            return image_str

        mime_type, b64_data = await client._normalize_image_input(
            image_str,
            image_input_mode=getattr(config, "image_input_mode", "force_base64"),
        )
        if not b64_data:
            raise APIError(
                "无法将参考图转换为 Agnes AI 可用的 data URI",
                None,
                "invalid_image",
                retryable=False,
            )
        return format_data_uri(b64_data, mime_type or "image/png")

    @staticmethod
    def _normalize_api_base(value: Any) -> str | None:
        raw_api_base = str(value or "").strip().rstrip("/")
        if not raw_api_base:
            return None
        for suffix in ("/v1/images/generations", "/images/generations"):
            if raw_api_base.endswith(suffix):
                raw_api_base = raw_api_base[: -len(suffix)]
                break
        return raw_api_base.rstrip("/")

    @staticmethod
    def _api_base_includes_version_path(value: str) -> bool:
        path = urllib.parse.urlsplit(value).path.rstrip("/")
        return path.startswith("/v1") or path.endswith("/v1")

    @staticmethod
    def _normalize_response_format(value: Any) -> str:
        response_format = str(value or "url").strip().lower()
        if response_format in _SUPPORTED_RESPONSE_FORMATS:
            return response_format
        logger.warning(f"[agnes_ai] 忽略不支持的 response_format={value}，已回退为 url")
        return "url"

    @staticmethod
    def _normalize_reference_image_mode(value: Any) -> str:
        mode = str(value or "base64").strip().lower()
        if mode in _REFERENCE_IMAGE_MODES:
            return mode
        logger.warning(
            f"[agnes_ai] 忽略不支持的 reference_image_mode={value}，已回退为 base64"
        )
        return "base64"

    @staticmethod
    def _resolve_size(resolution: Any, aspect_ratio: Any) -> str:
        size = AgnesAIProvider._normalize_explicit_size(resolution)
        if size:
            return size

        resolution_key = str(resolution or "1K").strip().upper()
        long_edge = _RESOLUTION_MAP.get(resolution_key)
        if long_edge is None:
            if resolution_key:
                logger.warning(f"[agnes_ai] 未知 resolution={resolution}，已回退为 1K")
            long_edge = _RESOLUTION_MAP["1K"]

        ratio = AgnesAIProvider._parse_ratio(aspect_ratio or "1:1")
        if ratio is None:
            logger.warning(
                f"[agnes_ai] 无法解析 aspect_ratio={aspect_ratio}，已回退为 1:1"
            )
            ratio = (1.0, 1.0)
        width, height = AgnesAIProvider._compute_dimensions(ratio, long_edge)
        return f"{width}x{height}"

    @staticmethod
    def _normalize_explicit_size(value: Any) -> str | None:
        match = _SIZE_RE.match(str(value or ""))
        if not match:
            return None
        width = int(match.group(1))
        height = int(match.group(2))
        if width <= 0 or height <= 0:
            return None
        return f"{width}x{height}"

    @staticmethod
    def _parse_ratio(value: Any) -> tuple[float, float] | None:
        raw = str(value or "").strip().lower().replace("×", "x")
        separator = ":" if ":" in raw else "x"
        parts = raw.split(separator)
        if len(parts) != 2:
            return None
        try:
            width = float(parts[0])
            height = float(parts[1])
        except (TypeError, ValueError):
            return None
        if width <= 0 or height <= 0:
            return None
        return width, height

    @staticmethod
    def _compute_dimensions(
        ratio: tuple[float, float], long_edge: int
    ) -> tuple[int, int]:
        ratio_width, ratio_height = ratio
        if ratio_width >= ratio_height:
            width = long_edge
            height = round(long_edge * ratio_height / ratio_width)
        else:
            height = long_edge
            width = round(long_edge * ratio_width / ratio_height)
        return AgnesAIProvider._align_dimension(
            width
        ), AgnesAIProvider._align_dimension(height)

    @staticmethod
    def _align_dimension(value: int) -> int:
        return max(16, value - (value % 16))

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

    @staticmethod
    def _request_has_proxy(
        client: Any, request_config: ApiRequestConfig | None
    ) -> bool:
        has_proxy = getattr(client, "_request_has_proxy", None)
        return bool(has_proxy(request_config)) if callable(has_proxy) else False

    @staticmethod
    async def _download_image(
        client: Any,
        image_url: str,
        session: aiohttp.ClientSession,
        request_config: ApiRequestConfig | None,
    ) -> str | None:
        downloader = getattr(client, "_download_image", None)
        if not callable(downloader):
            return None
        proxy_getter = getattr(client, "_request_http_proxy", None)
        proxy = proxy_getter(request_config) if callable(proxy_getter) else None
        _, image_path = await downloader(
            image_url,
            session,
            use_cache=False,
            proxy=proxy,
        )
        return image_path
