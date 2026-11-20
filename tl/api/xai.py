"""xAI Images API 供应商实现。

支持：
- /v1/images/generations  文生图（JSON 请求）
- /v1/images/edits        图像编辑（JSON 请求）
- 后台任务 deferred 异步提交 + 轮询（GET /v1/images/{request_id}）

xAI 的图像编辑端点要求 `application/json`，不支持 OpenAI SDK `images.edit()`
使用的 `multipart/form-data`。
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from typing import Any

import aiohttp
from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from ..core.errors import redact_text
from ..tl_utils import save_base64_image
from .base import ProviderRequest
from .reference_pipeline import reference_data_uri

_SUPPORTED_RESOLUTIONS: frozenset[str] = frozenset({"1k", "1.5k", "2k"})
_SUPPORTED_RESPONSE_FORMATS: frozenset[str] = frozenset({"url", "b64_json"})
# 官方多图编辑最多 5 张源图
_MAX_EDIT_IMAGES = 5
_MAX_BATCH_IMAGES = 10
# 官方 aspect_ratio 枚举（现行文档）：不含 4:5/5:4；21:9 与 5:2 已列入
SUPPORTED_ASPECT_RATIOS: frozenset[str] = frozenset(
    {
        "1:1",
        "16:9",
        "9:16",
        "4:3",
        "3:4",
        "3:2",
        "2:3",
        "2:1",
        "1:2",
        "19.5:9",
        "9:19.5",
        "20:9",
        "9:20",
        "21:9",
        "5:2",
        "auto",
    }
)
# quality low/medium/auto（默认 auto，仅 grok-imagine-image-2.0 支持）
_SUPPORTED_QUALITIES: frozenset[str] = frozenset({"low", "medium", "auto"})

# 官方参考图输入仅接受 JPEG/PNG/WebP，其余格式转码为 PNG
_SUPPORTED_INPUT_MIMES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp"}
)
# user 参数默认标识；配置留空则不发送
_DEFAULT_USER = "AstrBot"
# 后台任务来源：LLM 工具单图与批量
_BACKGROUND_SOURCES: frozenset[str] = frozenset({"llm_tool", "llm_batch"})
_DEFAULT_POLL_INTERVAL = 5.0
_DEFAULT_POLL_TIMEOUT = 100.0
# 单次轮询 GET 的独立超时，不依赖 session 默认
_POLL_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class XAIProvider:
    """xAI `/v1/images/*` 端点实现。"""

    name = "xai"

    async def build_request(
        self, *, client: Any, config: ApiRequestConfig
    ) -> ProviderRequest:  # noqa: ANN401
        settings: dict[str, Any] = (
            getattr(config, "provider_settings", None)
            or getattr(client, "xai_settings", None)
            or {}
        )
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
        request_config: ApiRequestConfig | None = None,
    ) -> tuple[list[str], list[str], str | None, str | None]:  # noqa: ANN401
        image_urls: list[str] = []
        image_paths: list[str] = []
        text_content = None
        thought_signature = None

        request_id = str(response_data.get("request_id") or "").strip()
        if request_id and not response_data.get("data"):
            response_data = await self._poll_deferred(
                client=client,
                session=session,
                request_id=request_id,
                api_base=api_base,
                request_config=request_config,
            )

        usage = response_data.get("usage")
        if isinstance(usage, dict):
            cost_ticks = usage.get("cost_in_usd_ticks")
            if isinstance(cost_ticks, (int, float)):
                logger.info("[xai] 本次请求成本: $%.6f", cost_ticks / 1e10)
            if any(
                usage.get(key) is not None
                for key in ("input_tokens", "output_tokens", "total_tokens")
            ):
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
                    if client._request_has_proxy(request_config):
                        _, image_path = await client._download_image(
                            image_url,
                            session,
                            use_cache=False,
                            proxy=client._request_http_proxy(request_config),
                        )
                        if image_path:
                            image_paths.append(image_path)
                            image_urls.append(image_path)
                        continue
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
            settings.get("model") or config.model or "grok-imagine-image-2.0"
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

        self._apply_output_options(payload, config, settings)
        logger.debug(
            "[xai] generations payload: model=%s n=%s aspect_ratio=%s resolution=%s "
            "response_format=%s quality=%s deferred=%s user=%s prompt_len=%s",
            model,
            payload.get("n"),
            payload.get("aspect_ratio"),
            payload.get("resolution"),
            payload.get("response_format"),
            payload.get("quality"),
            payload.get("deferred", False),
            payload.get("user"),
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
            # 与其他 provider 截断约定一致：超限静默截取前 N 张并告警
            logger.warning(
                "[xai] 参考图 %d 张超过 edits 上限 %d，已截取前 %d 张",
                len(ref_images),
                _MAX_EDIT_IMAGES,
                _MAX_EDIT_IMAGES,
            )
            ref_images = ref_images[:_MAX_EDIT_IMAGES]

        model = str(
            settings.get("model") or config.model or "grok-imagine-image-2.0"
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

        self._apply_output_options(payload, config, settings)
        logger.debug(
            "[xai] edits payload: model=%s n=%s ref_images=%s aspect_ratio=%s resolution=%s "
            "response_format=%s quality=%s deferred=%s user=%s prompt_len=%s",
            model,
            payload.get("n"),
            len(image_items),
            payload.get("aspect_ratio"),
            payload.get("resolution"),
            payload.get("response_format"),
            payload.get("quality"),
            payload.get("deferred", False),
            payload.get("user"),
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

        # 共享管道按官方输入白名单转码，HEIC/GIF 等统一转 PNG
        data_uri = await reference_data_uri(
            client,
            config,
            image_str,
            log_prefix="[xai] ",
            error_label="xai",
            supported=_SUPPORTED_INPUT_MIMES,
        )
        if not data_uri:
            raise APIError(
                "无法将参考图转换为 xAI 所需的 data URI",
                None,
                "invalid_image",
                retryable=False,
            )
        return data_uri

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

    def _apply_output_options(
        self,
        payload: dict[str, Any],
        config: ApiRequestConfig,
        settings: dict[str, Any],
    ) -> None:
        """generations 与 edits 共用的输出选项：响应格式、质量、user、deferred。"""
        response_format = self._normalize_response_format(
            settings.get("response_format")
        )
        if response_format:
            payload["response_format"] = response_format

        quality = self._normalize_quality(settings.get("quality"))
        if quality:
            payload["quality"] = quality

        user = self._resolve_user(settings)
        if user:
            payload["user"] = user

        if self._deferred_enabled(config, settings, response_format):
            payload["deferred"] = True

    @staticmethod
    def _resolve_user(settings: dict[str, Any]) -> str | None:
        value = settings.get("user")
        if value is None:
            value = _DEFAULT_USER
        return str(value).strip() or None

    @staticmethod
    def _deferred_enabled(
        config: ApiRequestConfig,
        settings: dict[str, Any],
        response_format: str | None,
    ) -> bool:
        # 后台任务首选 deferred；官方 deferred 仅支持 url 响应格式
        if not bool(settings.get("deferred", True)):
            return False
        if getattr(config, "request_source", None) not in _BACKGROUND_SOURCES:
            return False
        return response_format in (None, "url")

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
        if not aspect_ratio:
            return None
        if aspect_ratio not in SUPPORTED_ASPECT_RATIOS:
            logger.warning("[xai] aspect_ratio=%s 不受官方支持，已忽略", aspect_ratio)
            return None
        return aspect_ratio

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
        logger.warning("[xai] 忽略不支持的 resolution=%s，仅支持 1k/1.5k/2k", value)
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
        if not quality:
            return None
        if quality in _SUPPORTED_QUALITIES:
            return quality
        if quality == "high":
            logger.warning("[xai] quality=high 已不再支持，自动降级为 medium")
            return "medium"
        logger.warning("[xai] 忽略不支持的 quality=%s，仅支持 low/medium/auto", value)
        return None

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

    async def _poll_deferred(
        self,
        *,
        client: Any,
        session: aiohttp.ClientSession,
        request_id: str,
        api_base: str | None,
        request_config: ApiRequestConfig | None,
    ) -> dict[str, Any]:
        """轮询 GET /v1/images/{request_id} 直到 done/failed，返回终态响应体。"""
        settings: dict[str, Any] = (
            getattr(request_config, "provider_settings", None) or {}
        )
        base = (
            self._normalize_api_base(api_base or settings.get("api_base"))
            or "https://api.x.ai"
        )
        url = (
            f"{base}/images/{request_id}"
            if base.endswith("/v1")
            else f"{base}/v1/images/{request_id}"
        )
        interval = self._coerce_positive_number(
            settings.get("poll_interval"), _DEFAULT_POLL_INTERVAL
        )
        poll_timeout = self._coerce_positive_number(
            settings.get("poll_timeout"), _DEFAULT_POLL_TIMEOUT
        )
        request_deadline = getattr(request_config, "request_deadline", None)
        if request_deadline is not None:
            remaining_total = request_deadline - asyncio.get_running_loop().time()
            if remaining_total <= 0:
                raise APIError(
                    "xAI 总超时预算已耗尽；deferred 任务可能仍在服务端运行，"
                    "为避免重复计费将不再重新提交",
                    None,
                    "timeout",
                    retryable=False,
                )
            poll_timeout = min(poll_timeout, remaining_total)
        headers = {
            "Authorization": f"Bearer {getattr(request_config, 'api_key', None)}"
        }
        proxy = client._request_http_proxy(request_config)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + poll_timeout
        logger.info("[xai] deferred 任务已提交: request_id=%s", request_id)

        def _timeout_error() -> APIError:
            logger.warning(
                "[xai] deferred 轮询超时: request_id=%s timeout=%.0fs",
                request_id,
                poll_timeout,
            )
            return APIError(
                f"xAI deferred 任务轮询超时（{poll_timeout:.0f} 秒），"
                "任务可能仍在服务端运行，为避免重复计费将不再重新提交",
                None,
                "timeout",
                retryable=False,
            )

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise _timeout_error()
            # deadline 硬约束：sleep 与单次 GET 超时均按剩余预算封顶
            await asyncio.sleep(min(interval, remaining))
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise _timeout_error()
            get_timeout = aiohttp.ClientTimeout(
                total=min(_POLL_REQUEST_TIMEOUT.total, remaining)
            )
            try:
                async with session.get(
                    url, headers=headers, proxy=proxy, timeout=get_timeout
                ) as response:
                    poll_status = response.status
                    body = await response.text()
            except asyncio.TimeoutError:
                if loop.time() >= deadline:
                    raise _timeout_error() from None
                logger.warning("[xai] deferred 轮询 GET 超时，继续等待")
                continue
            if poll_status == 404:
                raise APIError(
                    "xAI deferred 任务不存在或已过期",
                    poll_status,
                    "api_error",
                    retryable=False,
                )
            if poll_status == 429 or poll_status >= 500:
                logger.warning(
                    "[xai] deferred 轮询瞬时故障 HTTP %s，继续等待", poll_status
                )
                continue
            if poll_status in (401, 403):
                raise APIError(
                    f"xAI deferred 轮询鉴权失败（HTTP {poll_status}），请检查 API Key",
                    poll_status,
                    "auth",
                    retryable=False,
                )
            if 400 <= poll_status < 500:
                message = self._extract_poll_error(body) or f"HTTP {poll_status}"
                logger.warning(
                    "[xai] deferred 轮询请求失败: request_id=%s status=%s detail=%s",
                    request_id,
                    poll_status,
                    redact_text(message)[:240],
                )
                raise APIError(
                    f"xAI deferred 轮询请求失败: {message}",
                    poll_status,
                    "api_error",
                    retryable=False,
                )
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                logger.warning("[xai] deferred 轮询响应非法 JSON，继续等待")
                continue
            if not isinstance(data, dict):
                continue
            status = str(data.get("status") or "").strip().lower()
            if status == "done":
                return data
            if status == "failed":
                error = data.get("error")
                message = (
                    error.get("message")
                    if isinstance(error, dict)
                    else str(error or "")
                ) or "未知原因"
                logger.warning(
                    "[xai] deferred 任务失败: request_id=%s detail=%s",
                    request_id,
                    redact_text(str(message))[:240],
                )
                raise APIError(
                    f"xAI deferred 任务失败: {message}",
                    None,
                    "api_error",
                    error.get("code") if isinstance(error, dict) else None,
                    retryable=False,
                )
            logger.debug(
                "[xai] deferred status=%s，%.0fs 后再查",
                status or "pending",
                interval,
            )

    @staticmethod
    def _extract_poll_error(body: str) -> str:
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return body.strip()[:200]
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()
            message = data.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        return body.strip()[:200]

    @staticmethod
    def _coerce_positive_number(value: Any, default: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if number > 0 else default
