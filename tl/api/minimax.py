"""MiniMax Image Generation API provider."""

from __future__ import annotations

import base64
import binascii
import re
import urllib.parse
from typing import Any, Final

import aiohttp
from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from ..core.errors import redact_url
from ..tl_utils import save_base64_image
from .base import ProviderRequest
from .data_uri import format_data_uri, strip_data_uri_prefix
from .param_utils import ensure_prompt_length
from .provider_limits import (
    MAX_REFERENCE_IMAGES_MINIMAX,
    MAX_REFERENCE_IMAGES_MINIMAX_H3,
)
from .reference_intake import announce_reference_intake
from .reference_values import normalize_image_mime

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
# MiniMax 官方 n 参数范围为 1-9，与参考图上限无关。
_MAX_IMAGES: Final[int] = 9

_H3_CANONICAL_MODELS: dict[str, str] = {
    "minimax-h3-image": "MiniMax-H3-Image",
    "minimax-h3-image-highspeed": "MiniMax-H3-Image-Highspeed",
}
_H3_MODELS: frozenset[str] = frozenset(_H3_CANONICAL_MODELS)
# H3 编辑内联参考图官方限制：单张与请求体合计均不超过 64MiB
_H3_REFERENCE_MAX_BYTES: Final[int] = 64 * 1024 * 1024
_H3_SUPPORTED_REFERENCE_MIMES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
)
_H3_MAX_IMAGES: Final[int] = 10
_H3_PROMPT_MAX_CHARS: Final[int] = 32_000
_H3_OUTPUT_FORMATS: frozenset[str] = frozenset({"jpeg", "png", "webp"})
_H3_AREAS: frozenset[str] = frozenset({"1K", "2K", "4K"})
# 官方 prompt 硬上限（字符）
_PROMPT_MAX_CHARS: int = 1500


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
        settings: dict[str, Any] = (
            getattr(config, "provider_settings", None)
            or getattr(client, "minimax_settings", None)
            or {}
        )
        raw_api_base = config.api_base or settings.get("api_base")
        base = self._normalize_api_base(raw_api_base)
        model = self._resolve_model(settings, config)
        if self._is_h3_model(model):
            path = (
                "/v1/images/edits"
                if config.reference_images
                else "/v1/images/generations"
            )
            url = f"{base}{path}"
        else:
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
        request_config: ApiRequestConfig | None = None,
        # 共享调度层在 parse_errors_with_provider=True 时传入；本方法不依赖该值
        is_retry: bool = False,
    ) -> tuple[list[str], list[str], str | None, str | None]:  # noqa: ANN401
        model = self._resolve_model(
            getattr(request_config, "provider_settings", None) or {}, request_config
        )
        if self._is_h3_model(model) or isinstance(response_data.get("data"), list):
            return await self._parse_h3_response(
                client=client,
                response_data=response_data,
                session=session,
                http_status=http_status,
                request_config=request_config,
            )

        self._raise_for_base_resp(response_data, http_status)
        if http_status is not None and http_status != 200:
            error = response_data.get("error")
            error_message = (
                str(error.get("message") or f"HTTP {http_status}")
                if isinstance(error, dict)
                else f"HTTP {http_status}"
            )
            if http_status in (429, 402, 403):
                logger.warning("API 配额/权限问题: %s", error_message)
                raise APIError(error_message, http_status, "quota")
            if http_status == 451:
                logger.warning("内容安全拦截 (451): %s", error_message)
                raise APIError(
                    f"内容安全审核未通过: {error_message}",
                    http_status,
                    "safety",
                    retryable=False,
                )
            logger.warning("API 错误: %s", error_message)
            raise APIError(error_message, http_status)

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

        await self._collect_image_data(
            client,
            session,
            data,
            image_urls,
            image_paths,
            request_config,
        )

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

        failed_count = ""
        if isinstance(metadata, dict) and metadata.get("failed_count"):
            failed_count = f"（{metadata.get('failed_count')} 张因内容安全检查未返回）"
        raise APIError(
            f"MiniMax 未返回图片数据{failed_count}",
            http_status,
            "no_image",
            retryable=False,
        )

    async def _parse_h3_response(
        self,
        *,
        client: Any,
        response_data: dict[str, Any],
        session: aiohttp.ClientSession,
        http_status: int | None,
        request_config: ApiRequestConfig | None,
    ) -> tuple[list[str], list[str], str | None, str | None]:  # noqa: ANN401
        usage = response_data.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        logger.info(
            "[minimax] H3 request_id=%s generated_images=%s total_tokens=%s "
            "input_tokens=%s output_tokens=%s",
            response_data.get("request_id"),
            usage.get("generated_images"),
            usage.get("total_tokens"),
            usage.get("input_tokens"),
            usage.get("output_tokens"),
        )

        if http_status is not None and http_status != 200:
            error = response_data.get("error")
            if not isinstance(error, dict):
                data = response_data.get("data")
                error = (
                    next(
                        (
                            item.get("error")
                            for item in data
                            if isinstance(item, dict)
                            and isinstance(item.get("error"), dict)
                        ),
                        None,
                    )
                    if isinstance(data, list)
                    else None
                )
            raise self._h3_api_error(error or f"HTTP {http_status}", http_status)
        if isinstance(response_data.get("error"), dict):
            raise self._h3_api_error(response_data["error"], http_status)

        data = response_data.get("data")
        if not isinstance(data, list):
            raise APIError(
                "MiniMax H3 响应格式不正确，缺少 data 数组",
                http_status,
                "invalid_response",
                retryable=False,
            )

        image_urls: list[str] = []
        image_paths: list[str] = []
        failed_items: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            error = item.get("error")
            if isinstance(error, dict):
                failed_items.append(error)
                continue
            image_url = str(item.get("url") or "").strip()
            if not image_url:
                continue

            image_path = None
            try:
                _, image_path = await client._download_image(
                    image_url,
                    session,
                    use_cache=False,
                    proxy=client._request_http_proxy(request_config),
                )
            except Exception as exc:  # noqa: BLE001 — 下载失败回退直链
                logger.warning(
                    "[minimax] H3 图片下载失败，回退直链: url=%s error=%s",
                    redact_url(image_url),
                    str(exc)[:240],
                )
            if image_path:
                image_urls.append(image_path)
                image_paths.append(image_path)
            else:
                image_urls.append(image_url)

        if failed_items:
            codes = [
                str(
                    item.get("code") or self._extract_h3_error_code(item.get("message"))
                )
                for item in failed_items
            ]
            logger.warning(
                "[minimax] H3 部分图片失败: failed_count=%s error_codes=%s request_id=%s",
                len(failed_items),
                [code for code in codes if code and code != "None"],
                response_data.get("request_id"),
            )

        if image_urls:
            return image_urls, image_paths, None, None
        if failed_items:
            raise self._h3_api_error(failed_items[0], http_status)
        raise APIError(
            "MiniMax H3 未返回图片数据",
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
        model = self._resolve_model(settings, config)
        if self._is_h3_model(model):
            return await self._prepare_h3_payload(
                client=client,
                config=config,
                settings=settings,
                model=model,
            )

        ensure_prompt_length(
            (config.prompt or "").strip(),
            max_chars=_PROMPT_MAX_CHARS,
            provider="MiniMax",
        )
        model = model.lower()
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

        style = self._resolve_style(settings, model)
        if style:
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

    async def _prepare_h3_payload(
        self,
        *,
        client: Any,
        config: ApiRequestConfig,
        settings: dict[str, Any],
        model: str,
    ) -> dict[str, Any]:
        prompt = (config.prompt or "").strip()
        model = _H3_CANONICAL_MODELS.get(model.lower(), model)
        if not prompt:
            raise APIError(
                "MiniMax H3 需要非空 prompt", None, "empty_prompt", retryable=False
            )
        ensure_prompt_length(
            prompt,
            max_chars=_H3_PROMPT_MAX_CHARS,
            provider="MiniMax H3",
        )

        if bool(getattr(config, "suppress_resolution", False)):
            # 保留参考图尺寸：编辑接口按参考图比例决定输出
            aspect_ratio = "auto"
        else:
            aspect_ratio = self._normalize_h3_aspect_ratio(
                getattr(config, "aspect_ratio", None) or settings.get("aspect_ratio")
            )
        area = self._normalize_h3_area(
            getattr(config, "resolution", None) or settings.get("resolution") or "4K"
        )
        output_format = self._normalize_h3_output_format(
            getattr(config, "output_format", None) or settings.get("output_format")
        )
        disable_prompt_enhanced = bool(settings.get("disable_prompt_enhanced", False))
        has_references = bool(config.reference_images)
        if disable_prompt_enhanced and not has_references and aspect_ratio == "auto":
            logger.warning(
                "[minimax] H3 disable_prompt_enhanced=true 时未提供合法比例，已使用 1:1"
            )
            aspect_ratio = "1:1"

        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": self._coerce_image_count(settings.get("n"), maximum=_H3_MAX_IMAGES),
            "area": area,
            "aspect_ratio": aspect_ratio,
            "output_format": output_format,
            "response_format": "url",
            "aigc_watermark": bool(settings.get("aigc_watermark", False)),
        }
        if has_references:
            payload["images"] = await self._build_h3_images(
                client=client,
                config=config,
                settings=settings,
            )
        else:
            payload["disable_prompt_enhanced"] = disable_prompt_enhanced

        logger.debug(
            "[minimax] H3 payload: model=%s endpoint=%s n=%s area=%s aspect_ratio=%s "
            "output_format=%s refs=%s prompt_len=%s",
            model,
            "edits" if has_references else "generations",
            payload["n"],
            area,
            aspect_ratio,
            output_format,
            len(payload.get("images", [])),
            len(prompt),
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
        client: Any,
        session: aiohttp.ClientSession,
        data: dict[str, Any],
        image_urls: list[str],
        image_paths: list[str],
        request_config: ApiRequestConfig | None,
    ) -> None:
        for image_url in self._iter_string_list(data.get("image_urls")):
            if client._request_has_proxy(request_config):
                _, image_path = await client._download_image(
                    image_url,
                    session,
                    use_cache=False,
                    proxy=client._request_http_proxy(request_config),
                )
                if image_path:
                    image_urls.append(image_path)
                    image_paths.append(image_path)
                else:
                    logger.warning(
                        "[minimax] 图片下载未落盘，已跳过该图: url=%s",
                        redact_url(image_url),
                    )
                continue
            image_urls.append(image_url)
            logger.debug("[minimax] 图片 URL: %s", redact_url(image_url))

        for b64_data in self._iter_string_list(data.get("image_base64")):
            image_path = await save_base64_image(
                self._strip_data_uri(b64_data),
                self._detect_image_extension(b64_data),
            )
            if image_path:
                image_urls.append(image_path)
                image_paths.append(image_path)
                logger.debug("[minimax] base64 图片已保存: %s", image_path)

    def _resolve_style(
        self, settings: dict[str, Any], model: str
    ) -> dict[str, Any] | None:
        """组装 style；仅 image-01-live 支持，其他模型忽略并记录日志。"""
        style_type = str(settings.get("style_type") or "").strip()
        if not style_type:
            legacy = settings.get("style")
            if isinstance(legacy, dict) and legacy and model == "image-01-live":
                return legacy
            return None
        if model != "image-01-live":
            logger.info("[minimax] %s 不支持 style，已忽略", model)
            return None
        style: dict[str, Any] = {"style_type": style_type}
        style_weight = self._coerce_style_weight(settings.get("style_weight"))
        if style_weight is not None:
            style["style_weight"] = style_weight
        return style

    @staticmethod
    def _coerce_style_weight(value: Any) -> float | None:
        """画风权重合法范围 (0, 1]；非法或缺省返回 None（交服务端默认 0.8）。"""
        if value in (None, ""):
            return None
        try:
            weight = float(value)
        except (TypeError, ValueError):
            logger.warning("[minimax] style_weight=%r 无效，已忽略", value)
            return None
        if not 0 < weight <= 1:
            logger.warning("[minimax] style_weight=%s 超出 (0,1]，已忽略", weight)
            return None
        return weight

    async def _build_h3_images(
        self,
        *,
        client: Any,
        config: ApiRequestConfig,
        settings: dict[str, Any],
    ) -> list[dict[str, str]]:
        ref_images = config.reference_images or []
        announce_reference_intake(
            ref_images, MAX_REFERENCE_IMAGES_MINIMAX_H3, log_prefix="[minimax] "
        )
        references = [
            {
                "image_url": await self._to_image_file(
                    client,
                    config,
                    image_input,
                    settings,
                    supported_mimes=_H3_SUPPORTED_REFERENCE_MIMES,
                    max_bytes=_H3_REFERENCE_MAX_BYTES,
                )
            }
            for image_input in ref_images[:MAX_REFERENCE_IMAGES_MINIMAX_H3]
        ]
        inline_total = sum(
            len(item["image_url"])
            for item in references
            if item["image_url"].startswith("data:")
        )
        if inline_total > _H3_REFERENCE_MAX_BYTES:
            raise APIError(
                "MiniMax H3 内联参考图总大小超过 64MiB，请将部分参考图改为公网 URL",
                None,
                "invalid_reference_image",
                retryable=False,
            )
        return references

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

        announce_reference_intake(
            ref_images, MAX_REFERENCE_IMAGES_MINIMAX, log_prefix="[minimax] "
        )
        reference_type = str(settings.get("subject_reference_type") or "character")
        if reference_type != "character":
            logger.warning(
                "[minimax] subject_reference_type=%s 官方仅支持 character，已回退为 character",
                reference_type,
            )
            reference_type = "character"
        references: list[dict[str, str]] = []
        for image_input in ref_images[:MAX_REFERENCE_IMAGES_MINIMAX]:
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
        *,
        supported_mimes: frozenset[str] | None = None,
        max_bytes: int | None = None,
    ) -> str:  # noqa: ANN401
        image_str = str(image_input or "").strip()
        if not image_str:
            raise APIError(
                "参考图为空",
                None,
                "invalid_image",
                retryable=False,
            )

        # H3 调用方传入更宽的官方限制；缺省沿用旧接口 10MiB/JPEG/PNG
        normalize_kwargs: dict[str, Any] = {"error_label": "minimax"}
        if supported_mimes is not None:
            normalize_kwargs["supported"] = supported_mimes
        if max_bytes is not None:
            normalize_kwargs["max_bytes"] = max_bytes
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
            b64_data, mime = normalize_image_mime(
                strip_data_uri_prefix(image_str),
                image_str.split(";", 1)[0].removeprefix("data:"),
                **normalize_kwargs,
            )
            return format_data_uri(b64_data, mime)

        mime_type, b64_data = await client._normalize_reference_image_input(
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

        b64_data, mime = normalize_image_mime(
            b64_data, mime_type or "image/png", **normalize_kwargs
        )
        return format_data_uri(b64_data, mime)

    @staticmethod
    def _resolve_model(
        settings: dict[str, Any], config: ApiRequestConfig | None
    ) -> str:
        model = str(
            settings.get("model") or getattr(config, "model", None) or "image-01"
        ).strip()
        return model

    @staticmethod
    def _is_h3_model(model: Any) -> bool:
        return str(model or "").strip().lower() in _H3_CANONICAL_MODELS

    @staticmethod
    def _normalize_h3_area(value: Any) -> str:
        area = str(value or "4K").strip().upper()
        if area in _H3_AREAS:
            return area
        logger.warning("[minimax] H3 resolution=%s 无效，已回退为 4K", value)
        return "4K"

    @staticmethod
    def _normalize_h3_aspect_ratio(value: Any) -> str:
        raw = str(value or "").strip()
        if not raw or raw.lower() == "auto":
            return "auto"
        if not re.fullmatch(r"[0-9]+:[0-9]+", raw):
            logger.warning("[minimax] H3 aspect_ratio=%s 无效，已回退为 auto", value)
            return "auto"
        width, height = (int(part) for part in raw.split(":", 1))
        ratio = width / height if height else 0
        if not (1 <= width <= 300 and 1 <= height <= 300) or not 1 / 3 <= ratio <= 3:
            logger.warning(
                "[minimax] H3 aspect_ratio=%s 超出官方范围，已回退为 auto", value
            )
            return "auto"
        return raw

    @staticmethod
    def _normalize_h3_output_format(value: Any) -> str:
        output_format = str(value or "jpeg").strip().lower()
        if output_format in _H3_OUTPUT_FORMATS:
            return output_format
        logger.warning("[minimax] H3 output_format=%s 无效，已回退为 jpeg", value)
        return "jpeg"

    @staticmethod
    def _extract_h3_error_code(message: Any) -> str | None:
        match = re.search(r"\(([0-9]+)\)\s*$", str(message or ""))
        return match.group(1) if match else None

    @classmethod
    def _h3_api_error(cls, error: Any, http_status: int | None) -> APIError:
        if isinstance(error, dict):
            message = str(error.get("message") or "未知错误")
            error_type = str(error.get("type") or "api_error")
            error_code = str(error.get("code") or "") or cls._extract_h3_error_code(
                message
            )
        else:
            message = str(error or "MiniMax H3 请求失败")
            error_type = "api_error"
            error_code = cls._extract_h3_error_code(message)
        return APIError(
            f"MiniMax H3 图像生成失败: {message}",
            http_status,
            error_type,
            error_code,
            retryable=error_code == "1002",
        )

    @staticmethod
    def _normalize_api_base(value: Any) -> str:
        raw_api_base = str(value or "").strip().rstrip("/")
        if not raw_api_base:
            return "https://api.minimax.cn"

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
    def _coerce_image_count(value: Any, maximum: int = _MAX_IMAGES) -> int:
        try:
            count = int(value)
        except (TypeError, ValueError):
            count = 1
        if count < 1:
            return 1
        if count > maximum:
            logger.warning("[minimax] n=%s 超出上限，已自动降级到 %s", count, maximum)
            return maximum
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
        # 向后兼容别名;新代码请直接引用 data_uri.strip_data_uri_prefix
        return strip_data_uri_prefix(value)

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
