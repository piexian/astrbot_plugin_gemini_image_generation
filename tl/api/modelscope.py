"""ModelScope（魔搭社区）API-Inference 供应商 — 插件首个异步任务制 provider。

链路（官方文档，2026-09 核对）：
- 提交 ``POST {base}/v1/images/generations``，请求头 ``X-ModelScope-Async-Mode: true``，
  立即返回 ``{"task_id": ...}``；
- 轮询 ``GET {base}/v1/tasks/{task_id}``，请求头 ``X-ModelScope-Task-Type: image_generation``；
- 终态 ``task_status`` = SUCCEED（``output_images``）/ FAILED。
免费单并发定位（非主渠道）；官方无批量参数，单任务单图。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Final

import aiohttp
from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from .base import ProviderRequest
from .param_utils import ensure_prompt_length
from .provider_limits import MAX_REFERENCE_IMAGES_MODELSCOPE
from .reference_values import resolve_reference_api_values

_DEFAULT_API_BASE: Final[str] = "https://api-inference.modelscope.cn"
_SUBMIT_PATH: Final[str] = "/v1/images/generations"
_TASK_PATH: Final[str] = "/v1/tasks"
_PROMPT_MAX_CHARS: Final[int] = 2000

_TIER_PIXELS: Final[dict[str, int]] = {"1K": 1024 * 1024, "2K": 2048 * 2048}
_DIM_STEP: Final[int] = 8
# 模型族尺寸上下限（官方参数表分层，模型名小写后匹配）
_QWEN_IMAGE_BOUNDS: Final[tuple[int, int]] = (64, 1664)
_Z_IMAGE_BOUNDS: Final[tuple[int, int]] = (512, 2048)
_FLUX_BOUNDS: Final[tuple[int, int]] = (64, 1024)
_SD_BOUNDS: Final[tuple[int, int]] = (64, 2048)
_UNKNOWN_BOUNDS: Final[tuple[int, int]] = (512, 1024)

_STEPS_BOUNDS: Final[tuple[int, int]] = (1, 100)
_GUIDANCE_BOUNDS: Final[tuple[float, float]] = (1.5, 20.0)
_SEED_MAX: Final[int] = 2**31 - 1

_DEFAULT_POLL_INTERVAL: Final[float] = 5.0
_DEFAULT_POLL_TIMEOUT: Final[float] = 100.0
# 单次轮询 GET 的独立超时，不依赖 session 默认
_POLL_REQUEST_TIMEOUT: Final[aiohttp.ClientTimeout] = aiohttp.ClientTimeout(total=15)

# sd 段级边界匹配：token 等于 sd/sdxl，或形如 sd3/sd15/sd21，避免无关 ID 误入
_SD_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"^(sd\d*|sdxl)$")


def _model_bounds(model: str) -> tuple[int, int]:
    """按模型名小写匹配尺寸上下限；未知模型走保守档。"""
    text = (model or "").strip().lower()
    if "qwen-image" in text:
        return _QWEN_IMAGE_BOUNDS
    if "z-image" in text:
        return _Z_IMAGE_BOUNDS
    if "flux" in text:
        return _FLUX_BOUNDS
    if "majicflus" in text:
        return _SD_BOUNDS
    tokens = re.split(r"[^a-z0-9]+", text)
    if any(_SD_TOKEN_RE.fullmatch(token) for token in tokens):
        return _SD_BOUNDS
    return _UNKNOWN_BOUNDS


def _is_edit_model(model: str) -> bool:
    return "edit" in (model or "").strip().lower()


def _normalize_ratio(value: Any) -> float | None:
    """将 'W:H' / 'WxH' / 'W×H' 归一化为宽高比浮点数；非法返回 None。"""
    if not value:
        return None
    text = str(value).strip().replace("×", ":").replace("x", ":").replace("*", ":")
    parts = text.split(":", 1)
    if len(parts) != 2:
        return None
    try:
        width, height = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return width / height


def _round_to_step(value: float, step: int, lo: int, hi: int) -> int:
    clamped = max(lo, min(int(round(value / step)) * step, hi))
    return max(lo, clamped)


def _resolve_size(*, resolution: Any, aspect_ratio: Any, model: str) -> str:
    """按 档位像素预算 × 长宽比 换算 8 倍数尺寸，并按模型族钳制。"""
    tier = str(resolution or "1K").strip().upper()
    budget = _TIER_PIXELS.get(tier)
    if budget is None:
        logger.warning("[modelscope] resolution=%s 非法，已回退为 1K", resolution)
        budget = _TIER_PIXELS["1K"]

    lo, hi = _model_bounds(model)
    ratio = _normalize_ratio(aspect_ratio) or 1.0

    width = _round_to_step((budget * ratio) ** 0.5, _DIM_STEP, lo, hi)
    height = _round_to_step((budget / ratio) ** 0.5, _DIM_STEP, lo, hi)
    # 单边触界时按比例重算另一边，避免独立钳位导致比例失真
    if width == hi and height < hi:
        height = _round_to_step(width / ratio, _DIM_STEP, lo, hi)
    elif height == hi and width < hi:
        width = _round_to_step(height * ratio, _DIM_STEP, lo, hi)
    return f"{width}x{height}"


def _optional_int(value: Any, lo: int, hi: int, label: str) -> int | None:
    """0/空/非法值不传；有效值钳位 [lo, hi]。"""
    if value is None or value == "":
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        logger.warning("[modelscope] %s=%r 非法，已忽略", label, value)
        return None
    if number == 0:
        return None
    return max(lo, min(number, hi))


def _optional_float(
    value: Any, bounds: tuple[float, float], label: str
) -> float | None:
    lo, hi = bounds
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        logger.warning("[modelscope] %s=%r 非法，已忽略", label, value)
        return None
    if number == 0:
        return None
    return max(lo, min(number, hi))


def _resolve_loras(value: Any) -> str | dict[str, Any] | None:
    """string 原样透传；JSON 对象串解析为 dict；非法值忽略并告警。"""
    if value is None:
        return None
    if isinstance(value, dict):
        return value or None
    text = str(value).strip()
    if not text:
        return None
    if not text.startswith("{"):
        return text
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("[modelscope] loras=%s 非法 JSON，已忽略", text[:80])
        return None
    if isinstance(parsed, dict) and parsed:
        return parsed
    logger.warning("[modelscope] loras=%s 非对象或为空，已忽略", text[:80])
    return None


def _coerce_positive_number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


class ModelScopeProvider:
    """ModelScope AIGC 提交 + 轮询实现。"""

    name = "modelscope"

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
            or getattr(client, "modelscope_settings", None)
            or {}
        )
        prompt = (config.prompt or "").strip()
        if not prompt:
            raise APIError(
                "ModelScope 需要非空 prompt", None, "empty_prompt", retryable=False
            )
        ensure_prompt_length(prompt, max_chars=_PROMPT_MAX_CHARS, provider="ModelScope")

        model = str(settings.get("model") or config.model or "").strip()
        if not model:
            raise APIError(
                "ModelScope 缺少模型名", None, "invalid_model", retryable=False
            )

        payload: dict[str, Any] = {"model": model, "prompt": prompt}
        if not getattr(config, "suppress_resolution", False):
            payload["size"] = _resolve_size(
                resolution=config.resolution,
                aspect_ratio=config.aspect_ratio,
                model=model,
            )

        negative_prompt = str(settings.get("negative_prompt") or "").strip()
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt

        # 编辑门控 defense-in-depth：非 edit 模型收到参考图直接失败，不静默丢弃
        refs = config.reference_images or []
        if refs:
            if not _is_edit_model(model):
                raise APIError(
                    f"ModelScope 模型 {model} 不支持参考图，"
                    "请改用含 edit 的编辑模型（如 Qwen/Qwen-Image-Edit）或去掉参考图",
                    None,
                    "invalid_reference_image",
                    retryable=False,
                )
            max_count = (
                _optional_int(
                    settings.get("max_reference_images"),
                    1,
                    99,
                    "max_reference_images",
                )
                or MAX_REFERENCE_IMAGES_MODELSCOPE
            )
            payload["image_url"] = await resolve_reference_api_values(
                client,
                config,
                refs,
                max_count=max_count,
                log_prefix="[modelscope] ",
                error_label="modelscope",
            )

        seed = _optional_int(settings.get("seed"), 0, _SEED_MAX, "seed")
        if seed:
            payload["seed"] = seed
        steps = _optional_int(settings.get("steps"), *_STEPS_BOUNDS, "steps")
        if steps:
            payload["steps"] = steps
        guidance = _optional_float(
            settings.get("guidance"), _GUIDANCE_BOUNDS, "guidance"
        )
        if guidance:
            payload["guidance"] = guidance
        loras = _resolve_loras(settings.get("loras"))
        if loras:
            payload["loras"] = loras

        base = self._normalize_api_base(config.api_base or settings.get("api_base"))
        url = f"{base}{_SUBMIT_PATH}"
        logger.debug(
            "[modelscope] build_request: url=%s model=%s size=%s refs=%s prompt_len=%s",
            url,
            model,
            payload.get("size"),
            len(refs),
            len(prompt),
        )
        return ProviderRequest(
            url=url,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
                "X-ModelScope-Async-Mode": "true",
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
        is_retry: bool = False,
    ) -> tuple[list[str], list[str], str | None, str | None]:  # noqa: ANN401
        # parse_errors_with_provider=True：非 200 一律由本方法解析错误体
        if http_status is not None and http_status != 200:
            raise self._error_from_body(response_data, http_status)

        task_id = str(response_data.get("task_id") or "").strip()
        if not task_id:
            raise APIError(
                "ModelScope 提交响应缺少 task_id",
                http_status,
                "invalid_response",
                retryable=True,
            )

        settings: dict[str, Any] = (
            getattr(request_config, "provider_settings", None) or {}
        )
        base = self._normalize_api_base(api_base or settings.get("api_base"))
        interval = _coerce_positive_number(
            settings.get("poll_interval"), _DEFAULT_POLL_INTERVAL
        )
        poll_timeout = _coerce_positive_number(
            settings.get("poll_timeout"), _DEFAULT_POLL_TIMEOUT
        )
        image_urls, image_paths = await self._poll_until_done(
            client=client,
            session=session,
            base=base,
            task_id=task_id,
            api_key=getattr(request_config, "api_key", None),
            interval=interval,
            poll_timeout=poll_timeout,
            request_config=request_config,
        )
        return image_urls, image_paths, None, None

    async def _poll_until_done(
        self,
        *,
        client: Any,
        session: aiohttp.ClientSession,
        base: str,
        task_id: str,
        api_key: str | None,
        interval: float,
        poll_timeout: float,
        request_config: ApiRequestConfig | None,
    ) -> tuple[list[str], list[str]]:
        url = f"{base}{_TASK_PATH}/{task_id}"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "X-ModelScope-Task-Type": "image_generation",
        }
        proxy = client._request_http_proxy(request_config)
        deadline = asyncio.get_running_loop().time() + poll_timeout
        while True:
            if asyncio.get_running_loop().time() >= deadline:
                raise APIError(
                    f"ModelScope 任务轮询超时（{poll_timeout:.0f} 秒），"
                    "任务仍在服务端运行，重试将重新提交并再次消耗额度",
                    None,
                    "timeout",
                    retryable=True,
                )
            await asyncio.sleep(interval)
            async with session.get(
                url, headers=headers, proxy=proxy, timeout=_POLL_REQUEST_TIMEOUT
            ) as response:
                poll_status = getattr(response, "status", None)
                body = await response.text()
            if poll_status in (401, 403):
                # 鉴权失败空转到超时只会白扣魔粒，直接失败
                raise APIError(
                    f"ModelScope 轮询鉴权失败（HTTP {poll_status}），请检查 Access Token",
                    poll_status,
                    "auth",
                    retryable=False,
                )
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                logger.warning("[modelscope] 轮询响应非法 JSON，继续等待")
                continue
            if not isinstance(data, dict):
                continue
            status = str(data.get("task_status") or "").strip().upper()
            if status == "SUCCEED":
                return await self._collect_output_images(
                    client, session, data, request_config
                )
            if status == "FAILED":
                message = self._extract_error_message(data) or "未知原因"
                raise APIError(
                    f"ModelScope 任务失败: {message}",
                    None,
                    "api_error",
                    retryable=False,
                )
            logger.debug(
                "[modelscope] task_status=%s，%.0fs 后再查",
                status or "PENDING",
                interval,
            )

    async def _collect_output_images(
        self,
        client: Any,
        session: aiohttp.ClientSession,
        data: dict[str, Any],
        request_config: ApiRequestConfig | None,
    ) -> tuple[list[str], list[str]]:
        image_urls: list[str] = []
        image_paths: list[str] = []
        raw_images = data.get("output_images")
        candidates = raw_images if isinstance(raw_images, list) else []
        for item in candidates:
            url = item.strip() if isinstance(item, str) else ""
            if not url:
                continue
            if client._request_has_proxy(request_config):
                image_path = None
                try:
                    _, image_path = await client._download_image(
                        url,
                        session,
                        use_cache=False,
                        proxy=client._request_http_proxy(request_config),
                    )
                except Exception as e:  # noqa: BLE001 — 下载失败回退直链
                    logger.warning("[modelscope] 图片下载失败，回退直链: %s", e)
                if image_path:
                    image_urls.append(image_path)
                    image_paths.append(image_path)
                    continue
            image_urls.append(url)
            logger.debug("[modelscope] 图片 URL: %s...", url[:80])

        if not image_urls:
            raise APIError(
                "ModelScope 任务成功但未返回图片",
                None,
                "no_image",
                retryable=False,
            )
        return image_urls, image_paths

    def _error_from_body(self, response_data: Any, http_status: int | None) -> APIError:
        """非 200 错误体解析；retryable=None 交框架通用判断（429/5xx 重试）。"""
        message = self._extract_error_message(response_data) or f"HTTP {http_status}"
        return APIError(
            f"ModelScope 请求失败: {message}",
            http_status,
            "api_error",
            retryable=None,
        )

    @staticmethod
    def _extract_error_message(data: Any) -> str:
        """兼容顶层 message / error、errors 为 dict 或 list 的错误结构。"""
        if not isinstance(data, dict):
            return ""
        for key in ("message", "error"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        errors = data.get("errors")
        parts: list[str] = []
        items = errors if isinstance(errors, list) else [errors]
        for item in items:
            if isinstance(item, dict):
                text = item.get("message") or item.get("code")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            elif isinstance(item, str) and item.strip():
                parts.append(item.strip())
        return "; ".join(parts)

    @staticmethod
    def _normalize_api_base(value: Any) -> str:
        base = str(value or "").strip().rstrip("/")
        if base.lower().endswith("/v1"):
            # 官方文档示例 base 带 /v1 后缀，剥掉避免拼出 /v1/v1/...
            base = base[:-3].rstrip("/")
        return base or _DEFAULT_API_BASE
