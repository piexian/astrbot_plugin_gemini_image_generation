"""MiniMax Image Generation API provider."""

from __future__ import annotations

import base64
import binascii
import urllib.parse
from typing import Any

import aiohttp

from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from ..tl_utils import save_base64_image
from .base import ProviderRequest

_SUPPORTED_ASPECT_RATIO_VALUES: tuple[str, ...] = (
    "1:1",
    "16:9",
    "4:3",
    "3:2",
    "2:3",
    "3:4",
    "9:16",
    "21:9",
)
_SUPPORTED_ASPECT_RATIOS: frozenset[str] = frozenset(_SUPPORTED_ASPECT_RATIO_VALUES)
_SUPPORTED_RESPONSE_FORMATS: frozenset[str] = frozenset({"url", "base64"})
_REFERENCE_IMAGE_MODES: frozenset[str] = frozenset({"auto", "url", "base64"})
# MiniMax API width/height 上限为 2048，4K 降级到该上限
_RESOLUTION_MAP: dict[str, int] = {"1K": 1024, "2K": 2048, "4K": 2048}
_UNKNOWN_ERROR_MESSAGES: frozenset[str] = frozenset({"unknown error", "unknown"})
_ERROR_MESSAGES: dict[int, str] = {
    1000: (
        "服务端 unknown error；已启用按比例重试策略，重试时会移除显式 "
        "width/height 并改用 MiniMax 官方 aspect_ratio"
    ),
    1002: "触发限流，请稍后再试",
    1004: "账号鉴权失败，请检查 API Key 是否正确",
    1008: "账号余额不足",
    1026: "图片描述涉及敏感内容",
    2013: "传入参数异常，请检查入参是否按 MiniMax 官方要求填写",
    2049: "无效的 API Key",
}
_ERROR_TYPES: dict[int, str] = {
    1000: "server_error",
    1002: "rate_limit",
    1004: "auth",
    1008: "quota",
    1026: "safety",
    2013: "invalid_request",
    2049: "auth",
}
_RETRYABLE_ERROR_CODES: frozenset[int] = frozenset({1002})
_MAX_IMAGES = 9


class MiniMaxProvider:
    """MiniMax `/v1/image_generation` endpoint implementation."""

    name = "minimax"

    async def build_request(
        self,
        *,
        client: Any,
        config: ApiRequestConfig,
        is_retry: bool = False,
        retry_error: APIError | None = None,
    ) -> ProviderRequest:  # noqa: ANN401
        settings: dict[str, Any] = getattr(client, "minimax_settings", None) or {}
        raw_api_base = config.api_base or settings.get("api_base")
        base = self._normalize_api_base(raw_api_base)
        url = f"{base}/v1/image_generation"
        payload = await self._prepare_payload(
            client=client,
            config=config,
            settings=settings,
            is_retry=is_retry,
            retry_error=retry_error,
        )

        logger.debug(
            "[minimax] URL=%s refs=%s n=%s response_format=%s is_retry=%s",
            url,
            len(config.reference_images or []),
            payload.get("n"),
            payload.get("response_format"),
            is_retry,
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
    ) -> tuple[list[str], list[str], str | None, str | None]:  # noqa: ANN401
        self._raise_for_base_resp(response_data, http_status)

        image_urls: list[str] = []
        image_paths: list[str] = []
        data = response_data.get("data")
        if not isinstance(data, dict):
            raise APIError(
                "MiniMax 响应格式不正确，缺少 data 字段",
                http_status,
                "invalid_response",
                retryable=True,
            )

        await self._collect_image_data(data, image_urls, image_paths)

        metadata = response_data.get("metadata")
        if isinstance(metadata, dict):
            logger.info(
                "[minimax] success_count=%s failed_count=%s task_id=%s",
                metadata.get("success_count"),
                metadata.get("failed_count"),
                response_data.get("id"),
            )

        if image_urls or image_paths:
            return image_urls, image_paths, None, None

        raise APIError(
            "MiniMax 未返回图片数据",
            http_status,
            "no_image",
            retryable=False,
        )

    async def _prepare_payload(
        self,
        *,
        client: Any,
        config: ApiRequestConfig,
        settings: dict[str, Any],
        is_retry: bool = False,
        retry_error: APIError | None = None,
    ) -> dict[str, Any]:
        model = str(settings.get("model") or config.model or "image-01").strip().lower()
        payload: dict[str, Any] = {
            "model": model,
            "prompt": config.prompt,
            "response_format": self._normalize_response_format(
                settings.get("response_format")
            ),
            "n": self._coerce_image_count(settings.get("n")),
        }

        aspect_ratio = self._normalize_aspect_ratio(config.aspect_ratio, model)
        resolution_dim = self._map_resolution(config.resolution)
        # image-01-live 仅支持 aspect_ratio，不支持显式 width/height
        supports_explicit_dims = model != "image-01-live"

        if is_retry and self._should_retry_with_aspect_ratio(retry_error):
            retry_aspect_ratio = self._resolve_retry_aspect_ratio(
                config=config,
                settings=settings,
                model=model,
                normalized_aspect_ratio=aspect_ratio,
            )
            payload["aspect_ratio"] = retry_aspect_ratio
            payload["_is_retry"] = True
            config.retry_note = (
                "MiniMax 1000 unknown error 已移除 width/height 并按比例重试"
                f"（aspect_ratio={retry_aspect_ratio}）"
            )
            logger.warning(
                "[minimax] 1000 unknown error 重试：移除 width/height，改用 aspect_ratio=%s",
                retry_aspect_ratio,
            )
        else:
            self._apply_size_fields(
                payload=payload,
                config=config,
                settings=settings,
                model=model,
                aspect_ratio=aspect_ratio,
                resolution_dim=resolution_dim,
                supports_explicit_dims=supports_explicit_dims,
            )

        seed = self._coerce_optional_int(settings.get("seed"))
        if seed is not None and seed != 0:
            payload["seed"] = seed

        payload["prompt_optimizer"] = bool(settings.get("prompt_optimizer", False))
        payload["aigc_watermark"] = bool(settings.get("aigc_watermark", False))

        style = settings.get("style")
        if isinstance(style, dict) and style and model == "image-01-live":
            payload["style"] = style

        subject_reference = await self._build_subject_reference(
            client=client,
            config=config,
            settings=settings,
        )
        if subject_reference:
            payload["subject_reference"] = subject_reference

        logger.debug(
            "[minimax] payload: model=%s n=%s aspect_ratio=%s width=%s height=%s "
            "response_format=%s refs=%s prompt_len=%s retry=%s",
            model,
            payload.get("n"),
            payload.get("aspect_ratio"),
            payload.get("width"),
            payload.get("height"),
            payload.get("response_format"),
            len(subject_reference),
            len(config.prompt or ""),
            is_retry,
        )
        return payload

    def _apply_size_fields(
        self,
        *,
        payload: dict[str, Any],
        config: ApiRequestConfig,
        settings: dict[str, Any],
        model: str,
        aspect_ratio: str | None,
        resolution_dim: int,
        supports_explicit_dims: bool,
    ) -> None:
        needs_explicit = False
        reason = ""
        if supports_explicit_dims and config.aspect_ratio and not aspect_ratio:
            needs_explicit = True
            reason = f"aspect_ratio {config.aspect_ratio} 不受 MiniMax 支持"
        elif (
            supports_explicit_dims
            and aspect_ratio
            and aspect_ratio != "1:1"
            and resolution_dim > 1024
        ):
            needs_explicit = True
            reason = f"resolution {config.resolution} 需要显式尺寸"

        if needs_explicit and config.aspect_ratio:
            w, h = self._compute_dimensions_from_ratio(
                config.aspect_ratio, resolution_dim
            )
            if w and h:
                payload["width"] = w
                payload["height"] = h
                logger.info("[minimax] %s → %dx%d", reason, w, h)
            elif aspect_ratio:
                payload["aspect_ratio"] = aspect_ratio
                logger.warning(
                    "[minimax] 无法计算尺寸，回退到 aspect_ratio=%s", aspect_ratio
                )
            else:
                logger.warning(
                    "[minimax] 无法为 %s 计算有效尺寸，已省略尺寸参数",
                    config.aspect_ratio,
                )
        elif aspect_ratio:
            payload["aspect_ratio"] = aspect_ratio
        else:
            w, h = self._get_dimensions(settings)
            if supports_explicit_dims and w and h:
                if self._is_unsafe_near_square_dimension(w, h):
                    payload["aspect_ratio"] = "1:1"
                    logger.warning(
                        "[minimax] 忽略 %dx%d 近正方形大尺寸，改用 aspect_ratio=1:1",
                        w,
                        h,
                    )
                else:
                    payload["width"] = w
                    payload["height"] = h
            elif supports_explicit_dims and resolution_dim > 1024:
                payload["aspect_ratio"] = "1:1"
                logger.info(
                    "[minimax] 避免 2048x2048 触发 MiniMax unknown error，改用 aspect_ratio=1:1"
                )
            elif supports_explicit_dims and resolution_dim >= 512:
                payload["width"] = resolution_dim
                payload["height"] = resolution_dim

    async def _collect_image_data(
        self,
        data: dict[str, Any],
        image_urls: list[str],
        image_paths: list[str],
    ) -> None:
        for image_url in self._iter_string_list(data.get("image_urls")):
            image_urls.append(image_url)
            logger.debug("[minimax] 图片 URL: %s...", image_url[:80])

        for b64_data in self._iter_string_list(data.get("image_base64")):
            image_path = await save_base64_image(
                self._strip_data_uri(b64_data),
                self._detect_image_extension(b64_data),
            )
            if image_path:
                image_urls.append(image_path)
                image_paths.append(image_path)
                logger.debug("[minimax] base64 图片已保存: %s", image_path)

    async def _build_subject_reference(
        self,
        *,
        client: Any,
        config: ApiRequestConfig,
        settings: dict[str, Any],
    ) -> list[dict[str, str]]:
        ref_images = config.reference_images or []
        if not ref_images:
            return []

        reference_type = str(settings.get("subject_reference_type") or "character")
        references: list[dict[str, str]] = []
        for image_input in ref_images:
            image_file = await self._to_image_file(
                client, config, image_input, settings
            )
            references.append({"type": reference_type, "image_file": image_file})
        return references

    async def _to_image_file(
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
                "MiniMax reference_image_mode=url 时参考图必须是 http(s) URL",
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
                "无法将参考图转换为 MiniMax 可用的 base64 图片",
                None,
                "invalid_image",
                retryable=False,
            )

        mime = mime_type or "image/png"
        return f"data:{mime};base64,{b64_data}"

    @staticmethod
    def _normalize_api_base(value: Any) -> str:
        raw_api_base = str(value or "").strip().rstrip("/")
        if not raw_api_base:
            return "https://api.minimaxi.com"

        parsed = urllib.parse.urlsplit(raw_api_base)
        if parsed.scheme and parsed.netloc:
            path = parsed.path.rstrip("/")
            if path.endswith("/v1"):
                path = path.removesuffix("/v1")
            return urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, path, "", "")
            ).rstrip("/")
        return raw_api_base.removesuffix("/v1").rstrip("/")

    @staticmethod
    def _normalize_response_format(value: Any) -> str:
        response_format = str(value or "base64").strip().lower()
        if response_format in _SUPPORTED_RESPONSE_FORMATS:
            return response_format
        logger.warning(
            "[minimax] 忽略不支持的 response_format=%s，已回退为 base64",
            value,
        )
        return "base64"

    @staticmethod
    def _normalize_reference_image_mode(value: Any) -> str:
        mode = str(value or "auto").strip().lower()
        if mode in _REFERENCE_IMAGE_MODES:
            return mode
        logger.warning(
            "[minimax] 忽略不支持的 reference_image_mode=%s，已回退为 auto",
            value,
        )
        return "auto"

    @staticmethod
    def _normalize_aspect_ratio(value: Any, model: str) -> str | None:
        aspect_ratio = str(value or "").strip()
        if not aspect_ratio:
            return None
        if aspect_ratio not in _SUPPORTED_ASPECT_RATIOS:
            logger.warning("[minimax] aspect_ratio=%s 不受 MiniMax 原生支持", value)
            return None
        if aspect_ratio == "21:9" and model == "image-01-live":
            logger.warning("[minimax] image-01-live 不支持 21:9，已忽略")
            return None
        return aspect_ratio

    @staticmethod
    def _should_retry_with_aspect_ratio(retry_error: APIError | None) -> bool:
        if retry_error is None:
            return True
        if str(retry_error.error_code or "") != "1000":
            return False
        message = str(retry_error.message or "").lower()
        return any(item in message for item in _UNKNOWN_ERROR_MESSAGES)

    def _resolve_retry_aspect_ratio(
        self,
        *,
        config: ApiRequestConfig,
        settings: dict[str, Any],
        model: str,
        normalized_aspect_ratio: str | None,
    ) -> str:
        if normalized_aspect_ratio:
            return normalized_aspect_ratio

        aspect_ratio = self._nearest_supported_aspect_ratio(config.aspect_ratio, model)
        if aspect_ratio:
            logger.warning(
                "[minimax] 重试时 aspect_ratio=%s 不受原生支持，改用最接近的 %s",
                config.aspect_ratio,
                aspect_ratio,
            )
            return aspect_ratio

        width, height = self._get_dimensions(settings)
        aspect_ratio = self._nearest_supported_aspect_ratio(f"{width}:{height}", model)
        if aspect_ratio:
            logger.warning(
                "[minimax] 重试时根据 width/height=%sx%s 推断 aspect_ratio=%s",
                width,
                height,
                aspect_ratio,
            )
            return aspect_ratio

        logger.warning("[minimax] 重试时无法推断比例，回退到 aspect_ratio=1:1")
        return "1:1"

    @staticmethod
    def _nearest_supported_aspect_ratio(value: Any, model: str) -> str | None:
        ratio = MiniMaxProvider._ratio_value(value)
        if ratio is None:
            return None
        candidates = [
            item
            for item in _SUPPORTED_ASPECT_RATIO_VALUES
            if not (item == "21:9" and model == "image-01-live")
        ]
        return min(
            candidates,
            key=lambda item: abs(MiniMaxProvider._ratio_value(item) - ratio),
        )

    @staticmethod
    def _ratio_value(value: Any) -> float | None:
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
        return width / height

    @staticmethod
    def _map_resolution(value: Any) -> int:
        key = str(value or "").strip().upper()
        result = _RESOLUTION_MAP.get(key, 0)
        if result == 0:
            if key:
                logger.warning("[minimax] 未知 resolution=%s，回退为 1024", value)
            return 1024
        if key == "4K":
            logger.info("[minimax] resolution=4K 降级为 2048（MiniMax 最大 2048）")
        return result

    @staticmethod
    def _compute_dimensions_from_ratio(
        aspect_ratio: str, target_long_edge: int
    ) -> tuple[int | None, int | None]:
        if not aspect_ratio or target_long_edge < 512:
            return None, None
        parts = aspect_ratio.strip().split(":")
        if len(parts) != 2:
            return None, None
        try:
            rw, rh = int(parts[0]), int(parts[1])
        except (TypeError, ValueError):
            return None, None
        if rw <= 0 or rh <= 0:
            return None, None

        if rw >= rh:
            width = target_long_edge
            height = round(target_long_edge * rh / rw)
        else:
            height = target_long_edge
            width = round(target_long_edge * rw / rh)

        width = round(width / 8) * 8
        height = round(height / 8) * 8

        # 短边钳制到 512，重新计算长边以维持比例
        if width < height:
            if width < 512:
                width = 512
                height = max(512, round(512 * rh / rw / 8) * 8)
        else:
            if height < 512:
                height = 512
                width = max(512, round(512 * rw / rh / 8) * 8)

        width = min(2048, width)
        height = min(2048, height)

        if width < 512 or height < 512:
            return None, None
        return width, height

    @staticmethod
    def _get_dimensions(settings: dict[str, Any]) -> tuple[int | None, int | None]:
        width = MiniMaxProvider._coerce_dimension(settings.get("width"))
        height = MiniMaxProvider._coerce_dimension(settings.get("height"))
        if width and height:
            return width, height
        if width or height:
            logger.warning("[minimax] width/height 必须同时设置，已忽略像素尺寸")
        return None, None

    @staticmethod
    def _is_unsafe_near_square_dimension(width: int, height: int) -> bool:
        long_edge = max(width, height)
        short_edge = min(width, height)
        return long_edge > 1024 and short_edge / long_edge >= 0.9

    @staticmethod
    def _coerce_dimension(value: Any) -> int | None:
        dimension = MiniMaxProvider._coerce_optional_int(value)
        if dimension is None or dimension == 0:
            return None
        if dimension < 512 or dimension > 2048 or dimension % 8 != 0:
            logger.warning(
                "[minimax] 忽略非法尺寸 %s，width/height 需为 512-2048 且为 8 的倍数",
                value,
            )
            return None
        return dimension

    @staticmethod
    def _coerce_optional_int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_image_count(value: Any) -> int:
        try:
            count = int(value)
        except (TypeError, ValueError):
            count = 1
        if count < 1:
            return 1
        if count > _MAX_IMAGES:
            logger.warning(
                "[minimax] n=%s 超出上限，已自动降级到 %s", count, _MAX_IMAGES
            )
            return _MAX_IMAGES
        return count

    @staticmethod
    def _raise_for_base_resp(
        response_data: dict[str, Any], http_status: int | None
    ) -> None:
        base_resp = response_data.get("base_resp")
        if not isinstance(base_resp, dict):
            return

        status_code = base_resp.get("status_code")
        try:
            status_code_int = int(status_code)
        except (TypeError, ValueError):
            status_code_int = 0
        if status_code_int == 0:
            return

        raw_status_msg = str(base_resp.get("status_msg") or "").strip()
        raw_status_msg_lower = raw_status_msg.lower()
        is_unknown_server_error = (
            status_code_int == 1000 and raw_status_msg_lower in _UNKNOWN_ERROR_MESSAGES
        )
        mapped_msg = _ERROR_MESSAGES.get(status_code_int)
        if (
            mapped_msg
            and raw_status_msg
            and raw_status_msg.lower() not in _UNKNOWN_ERROR_MESSAGES
        ):
            status_msg = f"{mapped_msg}（{raw_status_msg}）"
        else:
            status_msg = mapped_msg or raw_status_msg or "未知错误"

        raise APIError(
            f"MiniMax 图像生成失败（错误码 {status_code_int}）: {status_msg}",
            http_status,
            _ERROR_TYPES.get(status_code_int, "api_error"),
            str(status_code),
            retryable=status_code_int in _RETRYABLE_ERROR_CODES
            or is_unknown_server_error,
        )

    @staticmethod
    def _iter_string_list(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value] if value else []
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str) and item]

    @staticmethod
    def _strip_data_uri(value: str) -> str:
        if ";base64," in value:
            _, _, value = value.partition(";base64,")
        return value

    @staticmethod
    def _detect_image_extension(value: str) -> str:
        raw_data = MiniMaxProvider._strip_data_uri(value)
        try:
            head = base64.b64decode(raw_data[:128], validate=False)
        except (binascii.Error, ValueError):
            return "jpeg"
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if head.startswith(b"RIFF") and b"WEBP" in head[:16]:
            return "webp"
        if head.startswith(b"\xff\xd8\xff"):
            return "jpeg"
        return "jpeg"
