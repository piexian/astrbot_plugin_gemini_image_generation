"""Doubao (Volcengine Ark Seedream) image generation provider."""

from __future__ import annotations

import base64
import re
from typing import Any

import aiohttp

from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from ..tl_utils import save_base64_image
from .base import ProviderRequest


class DoubaoProvider:
    name = "doubao"

    # Default Ark base URL
    ARK_API_BASE = "https://ark.cn-beijing.volces.com"

    async def build_request(
        self,
        *,
        client: Any,
        config: ApiRequestConfig,
        is_retry: bool = False,
    ) -> ProviderRequest:  # noqa: ANN401
        # Read doubao_settings from client for API configuration
        doubao_settings = getattr(client, "doubao_settings", None) or {}

        # Determine API base: doubao_settings > config.api_base > default
        api_base = (
            doubao_settings.get("api_base")
            or (config.api_base or "").rstrip("/")
            or self.ARK_API_BASE
        )
        api_base = api_base.rstrip("/")
        url = f"{api_base}/api/v3/images/generations"

        # Determine API key: doubao_settings > config.api_key
        api_key = doubao_settings.get("api_key") or config.api_key or ""

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        payload = await self._prepare_payload(
            client=client,
            config=config,
            doubao_settings=doubao_settings,
            is_retry=is_retry,
        )

        logger.debug(
            "[doubao] build_request: url=%s model=%s size=%s has_image=%s is_retry=%s",
            url,
            payload.get("model"),
            payload.get("size"),
            bool(payload.get("image")),
            is_retry,
        )
        return ProviderRequest(url=url, headers=headers, payload=payload)

    async def _prepare_payload(
        self,
        *,
        client: Any,
        config: ApiRequestConfig,
        doubao_settings: dict[str, Any] | None = None,
        is_retry: bool = False,
    ) -> dict[str, Any]:  # noqa: ANN401
        if doubao_settings is None:
            doubao_settings = getattr(client, "doubao_settings", None) or {}

        # Model: doubao_settings.endpoint_id > config.model > default
        model = (
            doubao_settings.get("endpoint_id") or config.model or "doubao-seedream-4.5"
        )

        # Response format: url by default, fallback to b64_json on retry
        response_format = "b64_json" if is_retry else "url"

        payload: dict[str, Any] = {
            "model": model,
            "prompt": config.prompt,
            "response_format": response_format,
            "watermark": bool(doubao_settings.get("watermark", False)),
        }

        # Size: config.resolution > doubao_settings.default_size
        size = self._map_resolution(config.resolution)
        if not size and doubao_settings.get("default_size"):
            size = self._map_resolution(doubao_settings["default_size"])
        if size:
            payload["size"] = size

        if config.seed is not None:
            try:
                payload["seed"] = int(config.seed)
            except Exception:
                logger.debug("[doubao] invalid seed ignored: %r", config.seed)

        if config.reference_images:
            image_value = await self._process_reference_image(
                client=client,
                config=config,
                image_input=config.reference_images[0],
            )
            if image_value:
                payload["image"] = image_value

        # Prompt optimization mode
        optimize_mode = doubao_settings.get("optimize_prompt_mode")
        if optimize_mode in ("standard", "fast"):
            payload["optimize_prompt_options"] = {"mode": optimize_mode}

        # Sequential image generation (组图功能)
        seq_mode = doubao_settings.get("sequential_image_generation")
        if seq_mode == "auto":
            payload["sequential_image_generation"] = "auto"
            max_images = doubao_settings.get("sequential_max_images")
            if max_images and isinstance(max_images, int) and 1 <= max_images <= 9:
                payload["sequential_image_generation_options"] = {
                    "max_images": max_images
                }

        return payload

    @staticmethod
    def _map_resolution(resolution: str | None) -> str | None:
        """Map plugin resolution to Doubao `size`.

        Supported by Doubao:
        - "2K"/"4K"
        - "WxH" like "2048x2048"
        """
        if not resolution:
            return None

        raw = str(resolution).strip()
        if not raw:
            return None

        normalized = raw.lower().replace(" ", "")

        if re.match(r"^\d{3,5}x\d{3,5}$", normalized):
            # Keep WxH as-is
            return normalized

        if normalized in {"1k", "1024"}:
            # Doubao supports WxH, safest default for 1K
            return "1024x1024"
        if normalized in {"2k", "2048"}:
            return "2K"
        if normalized in {"4k", "4096"}:
            return "4K"

        # Allow advanced users to pass provider-native values directly
        return raw

    @staticmethod
    def _strip_data_uri_prefix(value: str) -> str:
        cleaned = (value or "").strip()
        if ";base64," in cleaned:
            _, _, cleaned = cleaned.partition(";base64,")
        return cleaned.strip()

    @staticmethod
    def _looks_like_base64(value: str) -> bool:
        # Quick heuristic; do not be overly strict to allow provider-side validation.
        v = (value or "").strip()
        if not v:
            return False
        if len(v) < 64:
            return False
        if v.startswith(("http://", "https://")):
            return False
        if " " in v or "\n" in v or "\r" in v:
            # base64 can include newlines, but we treat it as "needs cleaning"
            v = "".join(v.split())
        return re.match(r"^[A-Za-z0-9+/=_-]+$", v) is not None

    async def _process_reference_image(
        self, *, client: Any, config: ApiRequestConfig, image_input: Any
    ) -> str | None:  # noqa: ANN401
        """Prepare Doubao `image` field for i2i.

        - If input is a URL and not forcing base64, pass URL through.
        - Otherwise normalize to base64 (prefer plugin's unified normalizer).
        """
        image_str = str(image_input).strip()
        if not image_str:
            return None

        force_b64 = (
            str(getattr(config, "image_input_mode", "auto")).lower() == "force_base64"
        )

        if image_str.startswith(("http://", "https://")) and not force_b64:
            return image_str

        # If already base64/data URI, prefer using it directly (after cleanup).
        if image_str.startswith("data:image/") and ";base64," in image_str:
            return self._strip_data_uri_prefix(image_str)
        if self._looks_like_base64(image_str) and force_b64:
            return self._strip_data_uri_prefix(image_str)

        try:
            mime_type, b64_data = await client._normalize_image_input(
                image_input,
                image_input_mode=getattr(config, "image_input_mode", "force_base64"),
            )
        except Exception as e:
            logger.debug("[doubao] normalize_image_input failed: %s", e)
            mime_type, b64_data = None, None

        if not b64_data:
            if force_b64:
                raise APIError(
                    "参考图转换失败（doubao/i2i），请检查图片来源后重试。",
                    None,
                    "invalid_reference_image",
                )
            # Fallback: if user supplied a URL and we are not forcing base64, pass through.
            if image_str.startswith(("http://", "https://")):
                return image_str
            return None

        cleaned = self._strip_data_uri_prefix(b64_data).replace("\n", "")
        # Best-effort validation; keep relaxed to avoid blocking on minor padding issues.
        try:
            base64.b64decode(cleaned, validate=True)
        except Exception:
            try:
                base64.b64decode(cleaned, validate=False)
            except Exception:
                if force_b64:
                    raise APIError(
                        "参考图 base64 校验失败（doubao/i2i），请更换图片后重试。",
                        None,
                        "invalid_reference_image",
                    ) from None

        logger.debug(
            "[doubao] prepared i2i image: mime=%s b64_len=%s",
            mime_type,
            len(cleaned),
        )
        return cleaned

    async def parse_response(
        self,
        *,
        client: Any,
        response_data: dict[str, Any],
        session: aiohttp.ClientSession,
        api_base: str | None = None,
    ) -> tuple[list[str], list[str], str | None, str | None]:  # noqa: ANN401
        image_urls: list[str] = []
        image_paths: list[str] = []
        text_content = None
        thought_signature = None

        error_obj = response_data.get("error")
        if (
            isinstance(error_obj, dict)
            and error_obj.get("message")
            and not response_data.get("data")
        ):
            raise APIError(
                str(error_obj.get("message")),
                None,
                str(error_obj.get("code") or "doubao_error"),
            )

        data_list = response_data.get("data") or []
        if isinstance(data_list, list):
            for item in data_list:
                if not isinstance(item, dict):
                    continue
                url = item.get("url")
                if isinstance(url, str) and url:
                    image_urls.append(url)
                    continue
                b64_json = item.get("b64_json")
                if isinstance(b64_json, str) and b64_json:
                    image_path = await save_base64_image(b64_json, "png")
                    if image_path:
                        image_urls.append(image_path)
                        image_paths.append(image_path)

        usage = response_data.get("usage")
        if isinstance(usage, dict):
            logger.debug(
                "[doubao] usage: generated_images=%s output_tokens=%s total_tokens=%s",
                usage.get("generated_images"),
                usage.get("output_tokens"),
                usage.get("total_tokens"),
            )

        return image_urls, image_paths, text_content, thought_signature
