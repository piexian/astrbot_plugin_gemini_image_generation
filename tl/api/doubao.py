"""Doubao (Volcengine Ark Seedream) image generation provider."""

from __future__ import annotations

import base64
import re
from typing import Any

import aiohttp

from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from ..plugin_config import DOUBAO_SEQUENTIAL_IMAGES_MAX, DOUBAO_SEQUENTIAL_IMAGES_MIN
from ..tl_utils import save_base64_image
from .base import ProviderRequest

# 豆包 API 错误码分类
# 参考文档: https://www.volcengine.com/docs/82379/1299023

# 不可重试的错误码（需要用户修改输入或配置）
NON_RETRYABLE_ERRORS = {
    # 参数错误
    "MissingParameter",
    "InvalidParameter",
    "InvalidEndpoint.ClosedEndpoint",
    # 敏感内容检测
    "InputTextRiskDetection",
    "InputImageRiskDetection",
    "OutputTextRiskDetection",
    "OutputImageRiskDetection",
    "SensitiveContentDetected",
    "SensitiveContentDetected.SevereViolation",
    "SensitiveContentDetected.Violence",
    "InputTextSensitiveContentDetected",
    "InputImageSensitiveContentDetected",
    "InputVideoSensitiveContentDetected",
    "OutputTextSensitiveContentDetected",
    "OutputImageSensitiveContentDetected",
    "OutputVideoSensitiveContentDetected",
    # 图片格式错误
    "InvalidImageURL.EmptyURL",
    "InvalidImageURL.InvalidFormat",
    "InvalidArgumentError.InvalidImageDetail",
    "InvalidArgumentError.InvalidPixelLimit",
    # 认证/权限错误
    "AuthenticationError",
    "InvalidAccountStatus",
    "AccessDenied",
    "OperationDenied.PermissionDenied",
    "OperationDenied.ServiceNotOpen",
    "OperationDenied.ServiceOverdue",
    "AccountOverdueError",
    # 资源不存在
    "InvalidEndpointOrModel.NotFound",
    "ModelNotOpen",
    "InvalidEndpointOrModel.ModelIDAccessDisabled",
    # 订阅问题
    "InvalidSubscription",
    "UnsupportedModel",
}

# 可重试的错误码（临时性错误，可稍后重试）
RETRYABLE_ERRORS = {
    # 速率限制
    "RateLimitExceeded.EndpointRPMExceeded",
    "RateLimitExceeded.EndpointTPMExceeded",
    "ModelAccountRpmRateLimitExceeded",
    "ModelAccountTpmRateLimitExceeded",
    "APIAccountRpmRateLimitExceeded",
    "ModelAccountIpmRateLimitExceeded",
    "AccountRateLimitExceeded",
    "InflightBatchsizeExceeded",
    # 配额超限（部分可重试）
    "QuotaExceeded",
    "SetLimitExceeded",
    # 服务端错误
    "ServerOverloaded",
    "InternalServiceError",
    "ContentSecurityDetectionError",
}

# 错误码到用户友好消息的映射
ERROR_MESSAGES = {
    # 参数错误
    "MissingParameter": "请求缺少必要参数，请检查配置。",
    "InvalidParameter": "请求参数无效，请检查配置。",
    "InvalidEndpoint.ClosedEndpoint": "推理接入点已关闭或暂时不可用，请稍后重试。",
    # 敏感内容
    "InputTextRiskDetection": "输入文本可能包含敏感信息，请修改后重试。",
    "InputImageRiskDetection": "输入图片可能包含敏感信息，请更换后重试。",
    "OutputTextRiskDetection": "生成的文字可能包含敏感信息，请修改输入后重试。",
    "OutputImageRiskDetection": "生成的图片可能包含敏感信息，请修改输入后重试。",
    "SensitiveContentDetected": "输入内容可能包含敏感信息，请使用其他提示词。",
    "SensitiveContentDetected.SevereViolation": "输入内容可能包含严重违规信息，请使用其他提示词。",
    "SensitiveContentDetected.Violence": "输入内容可能包含激进行为相关信息，请使用其他提示词。",
    "InputTextSensitiveContentDetected": "输入文本可能包含敏感信息，请修改后重试。",
    "InputImageSensitiveContentDetected": "输入图像可能包含敏感信息，请更换后重试。",
    "OutputTextSensitiveContentDetected": "生成的文字可能包含敏感信息，请修改输入后重试。",
    "OutputImageSensitiveContentDetected": "生成的图像可能包含敏感信息，请修改输入后重试。",
    # 图片格式
    "InvalidImageURL.EmptyURL": "图片 URL 为空，请提供有效的图片。",
    "InvalidImageURL.InvalidFormat": "图片格式无效或数据损坏，请更换图片。",
    # 认证/权限
    "AuthenticationError": "API 密钥无效，请检查配置。",
    "InvalidAccountStatus": "账号状态异常，请联系管理员。",
    "AccessDenied": "没有访问权限，请检查权限设置。",
    "AccountOverdueError": "账号已欠费，请前往火山引擎费用中心充值。",
    "OperationDenied.ServiceNotOpen": "模型服务未开通，请前往火山方舟控制台开通。",
    "OperationDenied.ServiceOverdue": "账单已逾期，请前往火山费用中心充值。",
    # 资源不存在
    "InvalidEndpointOrModel.NotFound": "模型或推理接入点不存在，请检查配置。",
    "ModelNotOpen": "模型服务未开通，请前往火山方舟控制台开通。",
    # 速率限制
    "RateLimitExceeded.EndpointRPMExceeded": "请求频率超限 (RPM)，请稍后重试。",
    "RateLimitExceeded.EndpointTPMExceeded": "Token 用量超限 (TPM)，请稍后重试。",
    "ModelAccountRpmRateLimitExceeded": "账户模型 RPM 限制已超出，请稍后重试。",
    "ModelAccountTpmRateLimitExceeded": "账户模型 TPM 限制已超出，请稍后重试。",
    "ModelAccountIpmRateLimitExceeded": "账户模型 IPM 限制已超出，请稍后重试。",
    "AccountRateLimitExceeded": "请求频率过高，请降低请求频率后重试。",
    "ServerOverloaded": "服务资源紧张，请稍后重试。",
    "QuotaExceeded": "配额已用尽，请稍后重试或联系管理员。",
    "InternalServiceError": "服务内部错误，请稍后重试。",
}


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

        # Determine API key: from api_keys list (multi-key rotation) or legacy api_key
        api_keys = doubao_settings.get("api_keys") or []
        if not api_keys:
            legacy_key = doubao_settings.get("api_key")
            if legacy_key:
                api_keys = [legacy_key]
        # Use config.api_key which should already be rotated by the client
        api_key = config.api_key or (api_keys[0] if api_keys else "")

        # 检查 API key 是否配置
        if not api_key:
            raise APIError(
                "豆包 API 密钥未配置，请在插件设置里面配置至少一个密钥。",
                None,
                "ConfigurationError",
                "MissingApiKey",
                retryable=False,
            )

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
            "[doubao] build_request: url=%s model=%s size=%s has_image=%s is_retry=%s payload=%s",
            url,
            payload.get("model"),
            payload.get("size"),
            bool(payload.get("image")),
            is_retry,
            {k: v for k, v in payload.items() if k != "image"},  
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

        logger.debug(
            "[doubao] _prepare_payload: doubao_settings keys=%s default_size=%s",
            list(doubao_settings.keys()) if doubao_settings else [],
            doubao_settings.get("default_size"),
        )

        # Model: doubao_settings.endpoint_id > config.model > default
        model = (
            doubao_settings.get("endpoint_id") or config.model or "doubao-seedream-4.5"
        )

        # Response format: url by default, fallback to b64_json on retry
        response_format = "b64_json" if is_retry else "url"

        # Watermark: default to false per plugin docs (schema default is false)
        watermark = doubao_settings.get("watermark")
        if watermark is None:
            watermark = False  # Plugin default is false

        payload: dict[str, Any] = {
            "model": model,
            "prompt": config.prompt,
            "response_format": response_format,
            "watermark": bool(watermark),
        }

        # Size: doubao_settings.default_size > config.resolution > default 2K
        # provider_overrides 条目配置优先级最高
        if doubao_settings.get("default_size"):
            size = self._map_resolution(doubao_settings["default_size"], model)
        elif config.resolution:
            size = self._map_resolution(config.resolution, model)
        else:
            size = "2K"  # Default for 4.5/4.0 models
        if size:
            payload["size"] = size

        if config.seed is not None:
            try:
                payload["seed"] = int(config.seed)
            except Exception:
                logger.debug("[doubao] invalid seed ignored: %r", config.seed)

        # Process reference images (supports 1-14 images for doubao-seedream-4.5/4.0)
        if config.reference_images:
            image_value = await self._process_reference_images(
                client=client,
                config=config,
                image_inputs=config.reference_images,
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
            if (
                max_images
                and isinstance(max_images, int)
                and DOUBAO_SEQUENTIAL_IMAGES_MIN
                <= max_images
                <= DOUBAO_SEQUENTIAL_IMAGES_MAX
            ):
                payload["sequential_image_generation_options"] = {
                    "max_images": max_images
                }

        return payload

    @staticmethod
    def _map_resolution(resolution: str | None, model: str = "") -> str | None:
        """Map plugin resolution to Doubao `size`.

        Supported by Doubao:
        - doubao-seedream-4.5: "2K"/"4K" only (min 2560x1440=3686400 px)
        - doubao-seedream-4.0: "1K"/"2K"/"4K" (min 1280x720=921600 px)
        """
        if not resolution:
            return None

        raw = str(resolution).strip()
        if not raw:
            return None

        normalized = raw.lower().replace(" ", "")
        model_lower = model.lower() if model else ""

        # WxH format - pass through
        if re.match(r"^\d{3,5}x\d{3,5}$", normalized):
            return normalized

        # Normalize model name: 4-5 -> 4.5, 4_5 -> 4.5, etc.
        model_normalized = model_lower.replace("-", ".").replace("_", ".")

        # For doubao-seedream-4.5, only 2K/4K are valid shortcuts
        # 1K is NOT supported - auto upgrade to 2K
        if "4.5" in model_normalized or "seedream.4.5" in model_normalized:
            if normalized in {"2k", "2048"}:
                return "2K"
            if normalized in {"4k", "4096"}:
                return "4K"
            if normalized in {"1k", "1024"}:
                # 4.5 does not support 1K, auto upgrade to 2K
                return "2K"
            # Unknown resolution, default to 2K
            return "2K"

        # For doubao-seedream-4.0, 1K/2K/4K are all valid
        if "4.0" in model_normalized or "seedream.4.0" in model_normalized:
            if normalized in {"1k", "1024"}:
                return "1K"
            if normalized in {"2k", "2048"}:
                return "2K"
            if normalized in {"4k", "4096"}:
                return "4K"
            # Unknown resolution, default to 2K
            return "2K"

        # Default: assume 4.5 compatible (most common case)
        if normalized in {"2k", "2048"}:
            return "2K"
        if normalized in {"4k", "4096"}:
            return "4K"
        if normalized in {"1k", "1024"}:
            # Auto upgrade to 2K for safety
            return "2K"

        # Unknown, default to 2K
        return "2K"

    @staticmethod
    def _format_base64_data_uri(b64_data: str, mime_type: str | None = None) -> str:
        """Format base64 data with proper data URI prefix for Doubao API.

        Doubao API requires: data:image/<format>;base64,<Base64编码>
        """
        cleaned = b64_data.strip()
        # Already has data URI prefix
        if cleaned.startswith("data:image/"):
            return cleaned
        # Guess mime type from base64 header if not provided
        if not mime_type:
            mime_type = "image/png"
        return f"data:{mime_type};base64,{cleaned}"

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

    async def _process_reference_images(
        self, *, client: Any, config: ApiRequestConfig, image_inputs: list[Any]
    ) -> str | list[str] | None:  # noqa: ANN401
        """Prepare Doubao `image` field for i2i.

        Doubao supports:
        - Single image: string (URL or base64 with data URI prefix)
        - Multiple images (2-14): array of strings

        Returns:
        - Single string if only one image
        - List of strings if multiple images
        - None if no valid images
        """
        if not image_inputs:
            return None

        force_b64 = (
            str(getattr(config, "image_input_mode", "auto")).lower() == "force_base64"
        )

        processed_images: list[str] = []

        for image_input in image_inputs[:14]:  # Max 14 reference images
            image_str = str(image_input).strip()
            if not image_str:
                continue

            result = await self._process_single_image(
                client=client,
                config=config,
                image_str=image_str,
                force_b64=force_b64,
            )
            if result:
                processed_images.append(result)

        if not processed_images:
            return None

        # Return single string for one image, array for multiple
        if len(processed_images) == 1:
            return processed_images[0]
        return processed_images

    async def _process_single_image(
        self,
        *,
        client: Any,
        config: ApiRequestConfig,
        image_str: str,
        force_b64: bool,
    ) -> str | None:  # noqa: ANN401
        """Process a single image for Doubao API.

        Returns URL or base64 with data URI prefix.
        """
        if not image_str:
            return None

        # URL input (not forcing base64)
        if image_str.startswith(("http://", "https://")) and not force_b64:
            return image_str

        # Already has proper data URI prefix
        if image_str.startswith("data:image/") and ";base64," in image_str:
            return image_str

        # Raw base64 - add data URI prefix
        if self._looks_like_base64(image_str) and not image_str.startswith("data:"):
            cleaned = self._strip_data_uri_prefix(image_str).replace("\n", "")
            return self._format_base64_data_uri(cleaned)

        # Need to normalize through client
        try:
            mime_type, b64_data = await client._normalize_image_input(
                image_str,
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
        # Best-effort validation
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

        # Format with proper data URI prefix for Doubao API
        formatted = self._format_base64_data_uri(cleaned, mime_type)
        logger.debug(
            "[doubao] prepared i2i image: mime=%s b64_len=%s",
            mime_type,
            len(cleaned),
        )
        return formatted

    async def parse_response(
        self,
        *,
        client: Any,
        response_data: dict[str, Any],
        session: aiohttp.ClientSession,
        api_base: str | None = None,
        http_status: int | None = None,
        is_retry: bool = False,
    ) -> tuple[list[str], list[str], str | None, str | None]:  # noqa: ANN401
        # 防御性检查：response_data 必须是 dict
        if not isinstance(response_data, dict):
            logger.warning(
                "[doubao] 响应格式异常，期望 dict，实际: %s, 内容: %s",
                type(response_data).__name__,
                repr(response_data)[:100],
            )
            raise APIError(
                "豆包 API 返回了非预期格式的响应，请稍后重试。",
                http_status,
                "MalformedResponse",
                "InvalidResponseType",
                retryable=True,
            )

        # 重试时检测 response_format 是否需要降级（由 tl_api._make_request 标记）
        if is_retry:
            logger.debug("[doubao] 重试请求，response_format 降级为 b64_json")

        image_urls: list[str] = []
        image_paths: list[str] = []
        text_content = None
        thought_signature = None

        # 处理顶层错误
        error_obj = response_data.get("error")
        if (
            isinstance(error_obj, dict)
            and error_obj.get("message")
            and not response_data.get("data")
        ):
            error_code = str(error_obj.get("code") or "")
            error_type = str(error_obj.get("type") or "")
            error_message = str(error_obj.get("message") or "")

            # 解析错误并抛出带有详细信息的 APIError
            raise self._build_api_error(
                error_code=error_code,
                error_type=error_type,
                error_message=error_message,
                http_status=http_status,
            )

        data_list = response_data.get("data") or []
        if isinstance(data_list, list):
            for item in data_list:
                if not isinstance(item, dict):
                    continue
                # Check for per-image error
                item_error = item.get("error")
                if isinstance(item_error, dict) and item_error.get("message"):
                    error_code = str(item_error.get("code") or "")
                    error_message = str(item_error.get("message") or "")
                    # 获取用户友好消息
                    friendly_message = self._get_friendly_error_message(
                        error_code, error_message
                    )
                    logger.warning(
                        "[doubao] image generation error: code=%s message=%s",
                        error_code,
                        friendly_message,
                    )
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

        # 如果既没有错误也没有有效数据，抛出错误以便调试
        if not image_urls and not image_paths:
            # 检查顶层错误字段（已在上面处理过 dict 格式的错误）
            error_obj = response_data.get("error")
            # 如果错误字段存在但格式异常（非预期的 dict），也应该报错
            if error_obj is not None and not isinstance(error_obj, dict):
                logger.warning(
                    "[doubao] 错误字段格式异常，期望 dict，实际: %s 值: %s",
                    type(error_obj).__name__,
                    repr(error_obj)[:200],
                )
                raise APIError(
                    f"豆包 API 返回了格式异常的错误信息：{str(error_obj)[:100]}",
                    http_status,
                    "MalformedError",
                    "InvalidErrorFormat",
                    retryable=True,
                )
            # error 是 dict 但缺少 message，也视为异常响应
            if isinstance(error_obj, dict) and not error_obj.get("message"):
                logger.warning(
                    "[doubao] 错误字段缺少 message: %s",
                    repr(error_obj)[:200],
                )
                raise APIError(
                    "豆包 API 返回了不完整的错误信息，请稍后重试。",
                    http_status,
                    "IncompleteError",
                    "MissingErrorMessage",
                    retryable=True,
                )
            # 无错误字段但也无有效数据
            if not error_obj:
                logger.warning(
                    "[doubao] 响应中既无错误也无有效图像数据: %s",
                    repr(response_data)[:500],
                )
                raise APIError(
                    "豆包 API 返回了空响应，未生成图像也未返回错误信息。请稍后重试或检查请求参数。",
                    http_status,
                    "EmptyResponse",
                    "NoDataReturned",
                    retryable=True,
                )

        return image_urls, image_paths, text_content, thought_signature

    @staticmethod
    def _get_friendly_error_message(error_code: str, original_message: str) -> str:
        """获取用户友好的错误消息。"""
        # 精确匹配
        if error_code in ERROR_MESSAGES:
            return ERROR_MESSAGES[error_code]

        # 前缀匹配（处理带参数的错误码如 MissingParameter.xxx）
        for prefix in ERROR_MESSAGES:
            if error_code.startswith(prefix):
                return ERROR_MESSAGES[prefix]

        # 返回原始消息
        return original_message

    @staticmethod
    def _is_retryable_error(error_code: str, http_status: int | None = None) -> bool:
        """判断错误是否可重试。"""
        # 5xx 服务端错误通常可重试
        if http_status and 500 <= http_status < 600:
            return True

        # 429 Too Many Requests 可重试
        if http_status == 429:
            return True

        # 精确匹配
        if error_code in RETRYABLE_ERRORS:
            return True

        # 前缀匹配
        for prefix in RETRYABLE_ERRORS:
            if error_code.startswith(prefix):
                return True

        # 明确不可重试的错误
        if error_code in NON_RETRYABLE_ERRORS:
            return False

        for prefix in NON_RETRYABLE_ERRORS:
            if error_code.startswith(prefix):
                return False

        # 默认不可重试
        return False

    def _build_api_error(
        self,
        *,
        error_code: str,
        error_type: str,
        error_message: str,
        http_status: int | None = None,
    ) -> APIError:
        """构建带有详细信息的 APIError。"""
        friendly_message = self._get_friendly_error_message(error_code, error_message)
        retryable = self._is_retryable_error(error_code, http_status)

        return APIError(
            message=friendly_message,
            status_code=http_status,
            error_type=error_type or "DoubaoError",
            error_code=error_code,
            retryable=retryable,
        )
