"""SenseNova（商汤日日新）图像生成供应商实现。

支持两个模型分支：
- ``sensenova-u1-fast``：文生图专用，11 种官方固定尺寸，n 上限 4
- ``sensenova-u1.5-lite``：生成与编辑一体，size 为 32 倍数自由尺寸，n 仅 1，
  编辑走独立的 ``/v1/images/edits`` 接口
"""

from __future__ import annotations

from typing import Any

import aiohttp
from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from ..tl_utils import save_base64_image
from .base import ProviderRequest
from .param_utils import ensure_prompt_length
from .provider_limits import MAX_REFERENCE_IMAGES_SENSENOVA_U15
from .reference_values import resolve_reference_api_values

# 官方默认 API Base
_DEFAULT_API_BASE: str = "https://token.sensenova.cn"

# 默认模型
_DEFAULT_MODEL: str = "sensenova-u1.5-lite"

# u1.5-lite 分支前缀
_U15_MODEL_PREFIX: str = "sensenova-u1.5"

# u1-fast 官方固定 prompt 上限（token）
_PROMPT_CHAR_SOFT_LIMIT: int = 4096

# u1.5-lite prompt 硬上限
_U15_PROMPT_MAX_CHARS: int = 4096

# u1-fast 的 11 种官方支持尺寸（width x height）
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

# u1.5-lite 尺寸约束：32 的倍数，[512, 4096]，比例上限 3:1
_U15_DIM_MIN: int = 512
_U15_DIM_MAX: int = 4096
_U15_DIM_STEP: int = 32
_U15_MAX_RATIO: float = 3.0

# u1.5-lite 分辨率档位 → 像素预算
_U15_TIER_PIXELS: dict[str, int] = {
    "1K": 1024 * 1024,
    "2K": 2048 * 2048,
    "4K": 4096 * 4096,
}


def _is_u15(model: str) -> bool:
    return (model or "").strip().lower().startswith(_U15_MODEL_PREFIX)


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
    """u1-fast：决定最终 size。

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


def _round_to_step(value: float, step: int, lo: int, hi: int) -> int:
    clamped = max(lo, min(int(round(value / step)) * step, hi))
    return max(lo, clamped)


def _resolve_u15_size(
    *,
    resolution: Any,
    aspect_ratio: str | None,
) -> str:
    """u1.5-lite：按分辨率档位 + 长宽比换算 32 倍数尺寸。

    约束：宽高为 [512, 4096] 内的 32 倍数，比例钳位到 3:1 以内。
    """
    tier = str(resolution or "2K").strip().upper()
    budget = _U15_TIER_PIXELS.get(tier)
    if budget is None:
        logger.warning("[sensenova] resolution=%s 非法，已回退为 2K", resolution)
        budget = _U15_TIER_PIXELS["2K"]

    ratio = _normalize_aspect_ratio(aspect_ratio)
    if ratio:
        w_str, h_str = ratio.split(":", 1)
        w_h = int(w_str) / int(h_str)
    else:
        w_h = 1.0
    w_h = max(1.0 / _U15_MAX_RATIO, min(w_h, _U15_MAX_RATIO))

    width = _round_to_step(
        (budget * w_h) ** 0.5, _U15_DIM_STEP, _U15_DIM_MIN, _U15_DIM_MAX
    )
    height = _round_to_step(
        (budget / w_h) ** 0.5, _U15_DIM_STEP, _U15_DIM_MIN, _U15_DIM_MAX
    )
    # 单边触顶（如 4K 非方形）时按请求比例重算另一边，避免独立钳位导致比例失真
    if width == _U15_DIM_MAX and height < _U15_DIM_MAX:
        height = _round_to_step(width / w_h, _U15_DIM_STEP, _U15_DIM_MIN, _U15_DIM_MAX)
    elif height == _U15_DIM_MAX and width < _U15_DIM_MAX:
        width = _round_to_step(height * w_h, _U15_DIM_STEP, _U15_DIM_MIN, _U15_DIM_MAX)
    return f"{width}x{height}"


def _coerce_n(value: Any, default: int = 1) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, 4))


def _ensure_v1_endpoint(api_base: str, resource: str = "images/generations") -> str:
    base = (api_base or "").strip().rstrip("/")
    if not base:
        base = _DEFAULT_API_BASE
    if base.endswith("/v1"):
        return f"{base}/{resource}"
    return f"{base}/v1/{resource}"


class SenseNovaProvider:
    name = "sensenova"

    async def build_request(
        self, *, client: Any, config: ApiRequestConfig
    ) -> ProviderRequest:  # noqa: ANN401
        settings: dict[str, Any] = (
            getattr(config, "provider_settings", None)
            or getattr(client, "sensenova_settings", None)
            or {}
        )

        api_base = config.api_base or settings.get("api_base") or _DEFAULT_API_BASE

        if not config.api_key:
            raise APIError(
                "SenseNova 缺少 API Key，请在 provider_overrides.sensenova.api_keys 中配置",
                None,
                "missing_api_key",
                retryable=False,
            )

        prompt = (config.prompt or "").strip()
        if not prompt:
            raise APIError(
                "SenseNova 需要非空 prompt", None, "empty_prompt", retryable=False
            )

        model = (
            config.model or settings.get("model") or _DEFAULT_MODEL
        ).strip() or _DEFAULT_MODEL

        if _is_u15(model):
            if config.reference_images:
                images = await self._resolve_edit_images(client=client, config=config)
                return self._build_u15_edit_request(
                    api_base=str(api_base),
                    settings=settings,
                    config=config,
                    model=model,
                    prompt=prompt,
                    images=images,
                )
            return self._build_u15_generation_request(
                api_base=str(api_base),
                settings=settings,
                config=config,
                model=model,
                prompt=prompt,
            )
        return self._build_u1_fast_request(
            settings=settings, config=config, model=model, prompt=prompt
        )

    # ------------------------------------------------------------------
    # sensenova-u1-fast（文生图专用）
    # ------------------------------------------------------------------

    def _build_u1_fast_request(
        self,
        *,
        settings: dict[str, Any],
        config: ApiRequestConfig,
        model: str,
        prompt: str,
    ) -> ProviderRequest:
        api_base = config.api_base or settings.get("api_base") or _DEFAULT_API_BASE
        url = _ensure_v1_endpoint(str(api_base))

        # 防护：u1-fast 不支持图像输入，直接报错避免无效消耗
        if config.reference_images:
            raise APIError(
                "sensenova-u1-fast 不支持参考图，请改用 sensenova-u1.5-lite 或去掉参考图",
                None,
                "invalid_reference_image",
                retryable=False,
            )

        if len(prompt) > _PROMPT_CHAR_SOFT_LIMIT:
            logger.warning(
                "[sensenova] prompt 长度 %d 超过软上限 %d，可能被服务端截断",
                len(prompt),
                _PROMPT_CHAR_SOFT_LIMIT,
            )

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

    # ------------------------------------------------------------------
    # sensenova-u1.5-lite（生成 + 编辑）
    # ------------------------------------------------------------------

    def _u15_common_payload(
        self,
        *,
        settings: dict[str, Any],
        model: str,
        prompt: str,
    ) -> dict[str, Any]:
        ensure_prompt_length(
            prompt, max_chars=_U15_PROMPT_MAX_CHARS, provider="SenseNova"
        )
        return {
            "model": model,
            "prompt": prompt,
            # 服务端默认添加水印，按插件约定默认关闭并显式发送
            "watermark": bool(settings.get("watermark", False)),
            "response_format": str(settings.get("response_format") or "b64_json"),
            "prompt_extend": bool(settings.get("prompt_extend", False)),
        }

    def _build_u15_generation_request(
        self,
        *,
        api_base: str,
        settings: dict[str, Any],
        config: ApiRequestConfig,
        model: str,
        prompt: str,
    ) -> ProviderRequest:
        url = _ensure_v1_endpoint(api_base)
        payload = self._u15_common_payload(
            settings=settings, model=model, prompt=prompt
        )
        payload["size"] = _resolve_u15_size(
            resolution=config.resolution, aspect_ratio=config.aspect_ratio
        )

        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }
        logger.debug(
            "[sensenova] build_request: url=%s model=%s size=%s prompt_len=%s",
            url,
            model,
            payload["size"],
            len(prompt),
        )
        return ProviderRequest(url=url, headers=headers, payload=payload)

    def _build_u15_edit_request(
        self,
        *,
        api_base: str,
        settings: dict[str, Any],
        config: ApiRequestConfig,
        model: str,
        prompt: str,
        images: list[str],
    ) -> ProviderRequest:
        if not images:
            raise APIError(
                "SenseNova 编辑接口需要至少一张有效参考图",
                None,
                "invalid_reference_image",
                retryable=False,
            )
        url = _ensure_v1_endpoint(api_base, resource="images/edits")
        payload = self._u15_common_payload(
            settings=settings, model=model, prompt=prompt
        )
        payload["images"] = [{"image_url": value} for value in images]

        # 编辑接口：size 留空时由服务端自动适配主图
        if not getattr(config, "suppress_resolution", False):
            payload["size"] = _resolve_u15_size(
                resolution=config.resolution, aspect_ratio=config.aspect_ratio
            )

        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }
        return ProviderRequest(url=url, headers=headers, payload=payload)

    async def _resolve_edit_images(
        self, *, client: Any, config: ApiRequestConfig
    ) -> list[str]:
        return await resolve_reference_api_values(
            client,
            config,
            config.reference_images or [],
            max_count=MAX_REFERENCE_IMAGES_SENSENOVA_U15,
            log_prefix="[sensenova] ",
            error_label="sensenova",
        )

    # ------------------------------------------------------------------
    # parse_response
    # ------------------------------------------------------------------

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
                if client._request_has_proxy(request_config):
                    _, image_path = await client._download_image(
                        url,
                        session,
                        use_cache=False,
                        proxy=client._request_http_proxy(request_config),
                    )
                    if image_path:
                        image_urls.append(image_path)
                        image_paths.append(image_path)
                    continue
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
