"""SenseNova（商汤日日新）图像生成供应商实现。

支持模型：
- ``sensenova-u1-fast``：基于 SenseNova U1 的加速版本，专供信息图（Infographics）生成场景。

"""

from __future__ import annotations

from typing import Any

import aiohttp
from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from ..tl_utils import save_base64_image
from .base import ProviderRequest

# 官方默认 API Base
_DEFAULT_API_BASE: str = "https://token.sensenova.cn"

# 默认模型
_DEFAULT_MODEL: str = "sensenova-u1-fast"

# 11 种官方支持的尺寸（width x height）
_ALLOWED_SIZES: tuple[str, ...] = (
    "1664x2496",  # 2:3
    "2496x1664",  # 3:2
    "1760x2368",  # 3:4
    "2368x1760",  # 4:3
    "1824x2272",  # 4:5
    "2272x1824",  # 5:4
    "2048x2048",  # 1:1
    "2752x1536",  # 16:9
    "1536x2752",  # 9:16
    "3072x1376",  # 21:9
    "1344x3136",  # 9:21
)

# aspect_ratio → size 映射（覆盖插件配置中所有 aspect_ratio 选项）
_ASPECT_TO_SIZE: dict[str, str] = {
    "1:1": "2048x2048",
    "2:3": "1664x2496",
    "3:2": "2496x1664",
    "3:4": "1760x2368",
    "4:3": "2368x1760",
    "4:5": "1824x2272",
    "5:4": "2272x1824",
    "16:9": "2752x1536",
    "9:16": "1536x2752",
    "21:9": "3072x1376",
    "9:21": "1344x3136",
}

_DEFAULT_SIZE: str = "2752x1536"

# prompt 最大 token 数
_PROMPT_CHAR_SOFT_LIMIT: int = 4096


def _normalize_aspect_ratio(value: Any) -> str | None:
    """将 'WxH' / 'W×H' / 'W:H' 归一化为 'W:H'。"""
    if value is None:
        return None
    text = str(value).strip().lower().replace("×", ":").replace("x", ":")
    if ":" not in text:
        return None
    parts = text.split(":", 1)
    try:
        w = int(parts[0].strip())
        h = int(parts[1].strip())
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return f"{w}:{h}"


def _resolve_size(
    *,
    explicit_size: Any,
    aspect_ratio: str | None,
    default_size: str | None,
) -> str:
    """决定最终 size。

    优先级：
    1. ``explicit_size`` 合法且在白名单 → 直接使用
    2. ``aspect_ratio`` 能映射到白名单 → 使用映射结果
    3. ``default_size`` 合法且在白名单 → 使用
    4. 兜底使用 ``_DEFAULT_SIZE``
    """
    if explicit_size:
        text = str(explicit_size).strip().lower().replace("×", "x")
        if text in _ALLOWED_SIZES:
            return text
        logger.warning(
            "[sensenova] 显式 size=%s 不在官方支持列表，将根据 aspect_ratio 重选",
            explicit_size,
        )

    ratio = _normalize_aspect_ratio(aspect_ratio)
    if ratio and ratio in _ASPECT_TO_SIZE:
        return _ASPECT_TO_SIZE[ratio]
    if ratio:
        logger.info(
            "[sensenova] aspect_ratio=%s 不在官方支持列表，将回退到默认尺寸", ratio
        )

    if default_size:
        text = str(default_size).strip().lower().replace("×", "x")
        if text in _ALLOWED_SIZES:
            return text
        logger.warning(
            "[sensenova] sensenova_settings.default_size=%s 非法，已忽略", default_size
        )

    return _DEFAULT_SIZE


def _coerce_n(value: Any, default: int = 1) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, 4))


def _ensure_v1_endpoint(api_base: str) -> str:
    base = (api_base or "").strip().rstrip("/")
    if not base:
        base = _DEFAULT_API_BASE
    if base.endswith("/v1"):
        return f"{base}/images/generations"
    return f"{base}/v1/images/generations"


class SenseNovaProvider:
    """SenseNova ``/v1/images/generations`` 端点实现。"""

    name = "sensenova"

    async def build_request(
        self, *, client: Any, config: ApiRequestConfig
    ) -> ProviderRequest:  # noqa: ANN401
        settings: dict[str, Any] = getattr(client, "sensenova_settings", None) or {}

        api_base = settings.get("api_base") or config.api_base or _DEFAULT_API_BASE
        url = _ensure_v1_endpoint(str(api_base))

        if not config.api_key:
            raise APIError(
                "SenseNova 缺少 API Key，请在 provider_overrides.sensenova.api_keys 中配置",
                None,
                "missing_api_key",
                retryable=False,
            )

        # 参考图不支持，给出明确日志而非静默
        if config.reference_images:
            logger.info(
                "[sensenova] U1 Fast 不支持图像输入，已忽略 %d 张参考图",
                len(config.reference_images),
            )

        prompt = (config.prompt or "").strip()
        if not prompt:
            raise APIError(
                "SenseNova 需要非空 prompt", None, "empty_prompt", retryable=False
            )
        if len(prompt) > _PROMPT_CHAR_SOFT_LIMIT:
            logger.warning(
                "[sensenova] prompt 长度 %d 超过软上限 %d，可能被服务端截断",
                len(prompt),
                _PROMPT_CHAR_SOFT_LIMIT,
            )

        model = (
            config.model or settings.get("model") or _DEFAULT_MODEL
        ).strip() or _DEFAULT_MODEL

        size = _resolve_size(
            explicit_size=settings.get("size"),
            aspect_ratio=config.aspect_ratio,
            default_size=settings.get("default_size"),
        )

        n = _coerce_n(settings.get("n", 1))

        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "n": n,
        }

        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }

        logger.debug(
            "[sensenova] build_request: url=%s model=%s size=%s n=%s prompt_len=%s",
            url,
            model,
            size,
            n,
            len(prompt),
        )
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
        image_urls: list[str] = []
        image_paths: list[str] = []

        data = response_data.get("data")
        if not isinstance(data, list) or not data:
            error_obj = response_data.get("error")
            if error_obj:
                error_msg = (
                    error_obj.get("message", "未知错误")
                    if isinstance(error_obj, dict)
                    else str(error_obj)
                )
                raise APIError(
                    f"SenseNova 图像生成失败: {error_msg}",
                    http_status,
                    "api_error",
                    error_obj.get("code") if isinstance(error_obj, dict) else None,
                    retryable=False,
                )
            raise APIError(
                "SenseNova 响应缺少 data 字段",
                http_status,
                "invalid_response",
                retryable=True,
            )

        for item in data:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if isinstance(url, str) and url:
                image_urls.append(url)
                logger.debug("[sensenova] 图片 URL: %s...", url[:80])
                continue
            b64 = item.get("b64_json")
            if isinstance(b64, str) and b64:
                saved = await save_base64_image(b64, "png")
                if saved:
                    image_urls.append(saved)
                    image_paths.append(saved)

        if not image_urls:
            raise APIError(
                "SenseNova 未返回图片数据",
                http_status,
                "no_image",
                retryable=False,
            )

        logger.debug("[sensenova] 共 %d 张图片", len(image_urls))
        return image_urls, image_paths, None, None
