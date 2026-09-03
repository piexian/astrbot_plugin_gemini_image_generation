"""SiliconFlow（硅基流动）图像供应商 — 同步单端点实现。

链路（官方文档，2026-09 核对）：
- ``POST {base}/v1/images/generations``，文生图与图像编辑同一端点，同步返回；
- 响应 ``{"images": [{"url": ...}], "timings": {...}, "seed": ...}``，无任务轮询；
- 图片 URL 仅 1 小时有效：解析阶段无条件立即下载落盘，失败回退直链并告警。

模型族差异（模型名小写匹配；edit 系列是 qwen-image 的超集子串，顺序敏感）：
- Kwai-Kolors/Kolors：image_size 预设表 + batch_size(1-4) + guidance_scale(0-20)；
- Qwen/Qwen-Image：image_size 预设表（档位不影响预设）；
- Qwen/Qwen-Image-Edit(-2509)：不传 image_size；参考图 image/image2/image3，
  2509 最多 3 张、经典 Edit 仅 1 张（超出截断并告警）。
"""

from __future__ import annotations

from typing import Any, Final

import aiohttp
from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from .base import ProviderRequest
from .param_utils import coerce_int
from .provider_limits import MAX_REFERENCE_IMAGES_SILICONFLOW
from .reference_values import resolve_reference_api_values

_DEFAULT_API_BASE: Final[str] = "https://api.siliconflow.cn"
_GENERATIONS_PATH: Final[str] = "/v1/images/generations"

# 官方未公布 prompt 长度上限，不做本地 fail-fast。

_TIER_PIXELS: Final[dict[str, int]] = {"1K": 1024 * 1024, "2K": 2048 * 2048}
_DIM_STEP: Final[int] = 8
# 未命中官方预设时按档位预算本地计算，长边按模型族钳制
_FAMILY_BOUNDS: Final[dict[str, tuple[int, int]]] = {
    "kolors": (512, 1440),
    "qwen-image": (512, 1664),
    "unknown": (512, 1440),
}

# 官方推荐预设（WxH）。Kolors 3:4 有 1K/2K 双档预设，其余比例无预设。
_KOLORS_PRESETS: Final[dict[str, tuple[int, int]]] = {
    "1:1": (1024, 1024),
    "3:4": (768, 1024),
    "1:2": (720, 1440),
    "9:16": (720, 1280),
}
_KOLORS_3_4_2K: Final[tuple[int, int]] = (960, 1280)
_QWEN_IMAGE_PRESETS: Final[dict[str, tuple[int, int]]] = {
    "1:1": (1328, 1328),
    "16:9": (1664, 928),
    "9:16": (928, 1664),
    "4:3": (1472, 1140),
    "3:4": (1140, 1472),
    "3:2": (1584, 1056),
    "2:3": (1056, 1584),
}

_EDIT_FAMILIES: Final[frozenset[str]] = frozenset(
    {"qwen-image-edit", "qwen-image-edit-2509"}
)
_BATCH_LIMIT_KOLORS: Final[int] = 4
_STEPS_BOUNDS: Final[tuple[int, int]] = (1, 100)
_GUIDANCE_BOUNDS: Final[tuple[float, float]] = (0.0, 20.0)
_SEED_MAX: Final[int] = 9999999999
# 官方过载错误码，显式可重试
_OVERLOAD_CODE: Final[int] = 50505


def _model_family(model: str) -> str:
    """模型名小写匹配族；edit-2509 先于 edit 先于 qwen-image（前缀包含关系）。"""
    name = (model or "").strip().lower()
    if "qwen-image-edit-2509" in name:
        return "qwen-image-edit-2509"
    if "qwen-image-edit" in name:
        return "qwen-image-edit"
    if "qwen-image" in name:
        return "qwen-image"
    if "kolors" in name:
        return "kolors"
    return "unknown"


def _parse_ratio(value: Any) -> tuple[str, float] | None:
    """解析为 ('W:H' 标准标签, 宽高比浮点)；非法返回 None。"""
    if not value:
        return None
    text = (
        str(value).strip().lower().replace("×", ":").replace("x", ":").replace("*", ":")
    )
    parts = text.split(":", 1)
    if len(parts) != 2:
        return None
    try:
        width, height = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return f"{width:g}:{height:g}", width / height


def _round_to_step(value: float, step: int, lo: int, hi: int) -> int:
    clamped = max(lo, min(int(round(value / step)) * step, hi))
    return max(lo, clamped)


def _resolve_image_size(*, resolution: Any, aspect_ratio: Any, family: str) -> str:
    """映射 image_size：命中官方预设直接用，未命中按档位预算本地计算。"""
    tier = str(resolution or "1K").strip().upper()
    parsed = _parse_ratio(aspect_ratio)
    label = parsed[0] if parsed else None

    preset: tuple[int, int] | None = None
    if family == "kolors":
        if label == "3:4":
            preset = _KOLORS_3_4_2K if tier == "2K" else _KOLORS_PRESETS["3:4"]
        else:
            preset = _KOLORS_PRESETS.get(label or "")
    elif family == "qwen-image":
        preset = _QWEN_IMAGE_PRESETS.get(label or "")
        if preset and tier == "2K":
            # 档位不影响 Qwen-Image 预设，仅记录便于排查
            logger.debug("[siliconflow] Qwen-Image 预设尺寸与 resolution=%s 无关", tier)
    if preset:
        return f"{preset[0]}x{preset[1]}"

    # 未知族固定按 1K 预算保守计算；非法档位回退 1K
    budget = _TIER_PIXELS["1K"] if family == "unknown" else _TIER_PIXELS.get(tier)
    if budget is None:
        logger.warning("[siliconflow] resolution=%s 非法，已回退为 1K", resolution)
        budget = _TIER_PIXELS["1K"]
    lo, hi = _FAMILY_BOUNDS.get(family, _FAMILY_BOUNDS["unknown"])
    ratio = parsed[1] if parsed else 1.0
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
        logger.warning("[siliconflow] %s=%r 非法，已忽略", label, value)
        return None
    if number == 0:
        return None
    return max(lo, min(number, hi))


def _optional_float(
    value: Any, bounds: tuple[float, float], label: str
) -> float | None:
    """0/空/非法值不传；有效值钳位到 bounds。"""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        logger.warning("[siliconflow] %s=%r 非法，已忽略", label, value)
        return None
    if number == 0:
        return None
    lo, hi = bounds
    return max(lo, min(number, hi))


class SiliconFlowProvider:
    """SiliconFlow /v1/images/generations 同步端点实现。"""

    name = "siliconflow"

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
            or getattr(client, "siliconflow_settings", None)
            or {}
        )
        prompt = (config.prompt or "").strip()
        if not prompt:
            raise APIError(
                "SiliconFlow 需要非空 prompt", None, "empty_prompt", retryable=False
            )

        model = str(settings.get("model") or config.model or "").strip()
        if not model:
            raise APIError(
                "SiliconFlow 缺少模型名", None, "invalid_model", retryable=False
            )

        family = _model_family(model)
        payload: dict[str, Any] = {"model": model, "prompt": prompt}

        negative = str(settings.get("negative_prompt") or "").strip()
        if negative:
            payload["negative_prompt"] = negative

        # Edit 系列官方不支持 image_size；其余族命中预设或按档位本地计算
        if family not in _EDIT_FAMILIES and not getattr(
            config, "suppress_resolution", False
        ):
            payload["image_size"] = _resolve_image_size(
                resolution=config.resolution,
                aspect_ratio=config.aspect_ratio,
                family=family,
            )

        if family == "kolors":
            payload["batch_size"] = coerce_int(
                settings.get("batch_size"),
                lo=1,
                hi=_BATCH_LIMIT_KOLORS,
                default=1,
                warn_prefix="[siliconflow] batch_size",
            )
            guidance = _optional_float(
                settings.get("guidance_scale"), _GUIDANCE_BOUNDS, "guidance_scale"
            )
            if guidance is not None:
                payload["guidance_scale"] = guidance

        steps = _optional_int(
            settings.get("num_inference_steps"), *_STEPS_BOUNDS, "num_inference_steps"
        )
        if steps:
            payload["num_inference_steps"] = steps
        seed = _optional_int(settings.get("seed"), 0, _SEED_MAX, "seed")
        if seed:
            payload["seed"] = seed

        refs = config.reference_images or []
        if refs:
            if family not in _EDIT_FAMILIES:
                raise APIError(
                    f"SiliconFlow 模型 {model} 不支持参考图，"
                    "请改用编辑模型（如 Qwen/Qwen-Image-Edit-2509）或去掉参考图",
                    None,
                    "invalid_reference_image",
                    retryable=False,
                )
            max_count = (
                _optional_int(
                    settings.get("max_reference_images"),
                    1,
                    MAX_REFERENCE_IMAGES_SILICONFLOW,
                    "max_reference_images",
                )
                or MAX_REFERENCE_IMAGES_SILICONFLOW
            )
            if family == "qwen-image-edit":
                # 经典 Edit 官方仅支持单张参考图，与 2509 的 3 张上限分层
                max_count = 1
            values = await resolve_reference_api_values(
                client,
                config,
                refs,
                max_count=max_count,
                log_prefix="[siliconflow] ",
                error_label="siliconflow",
            )
            payload["image"] = values[0]
            if len(values) > 1:
                payload["image2"] = values[1]
            if len(values) > 2:
                payload["image3"] = values[2]

        base = self._normalize_api_base(config.api_base or settings.get("api_base"))
        url = f"{base}{_GENERATIONS_PATH}"
        logger.debug(
            "[siliconflow] build_request: url=%s model=%s family=%s "
            "image_size=%s refs=%s prompt_len=%s",
            url,
            model,
            family,
            payload.get("image_size"),
            len(refs),
            len(prompt),
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
        is_retry: bool = False,
    ) -> tuple[list[str], list[str], str | None, str | None]:  # noqa: ANN401
        # parse_errors_with_provider=True：非 200 一律由本方法解析错误体
        if http_status is not None and http_status != 200:
            raise self._error_from_body(response_data, http_status)

        images = (
            response_data.get("images") if isinstance(response_data, dict) else None
        )
        if not isinstance(images, list) or not images:
            raise APIError(
                "SiliconFlow 未返回图片",
                http_status,
                "no_image",
                retryable=False,
            )

        image_urls: list[str] = []
        image_paths: list[str] = []
        for item in images:
            url_value = item.get("url") if isinstance(item, dict) else None
            if not isinstance(url_value, str) or not url_value:
                continue
            # URL 仅 1 小时有效：无条件立即下载落盘；显式传候选级代理（优先于全局）
            image_path = None
            try:
                _, image_path = await client._download_image(
                    url_value,
                    session,
                    use_cache=False,
                    proxy=client._request_http_proxy(request_config),
                )
            except Exception as e:  # noqa: BLE001 — 下载失败回退直链
                logger.warning("[siliconflow] 图片下载失败，回退直链: %s", e)
            if image_path:
                image_urls.append(image_path)
                image_paths.append(image_path)
            else:
                logger.warning("[siliconflow] 图片 URL 约 1 小时后失效，直链仅为兜底")
                image_urls.append(url_value)

        if not image_urls:
            raise APIError(
                "SiliconFlow 未返回图片",
                http_status,
                "no_image",
                retryable=False,
            )
        return image_urls, image_paths, None, None

    def _error_from_body(self, response_data: Any, http_status: int | None) -> APIError:
        """非 200 错误体解析；retryable=None 交框架通用判断（429/5xx 重试、多 Key 轮换）。"""
        message, code = self._parse_error_body(response_data)
        if code == _OVERLOAD_CODE:
            return APIError(
                f"SiliconFlow 服务过载: {message or f'HTTP {http_status}'}",
                http_status,
                "api_error",
                retryable=True,
            )
        return APIError(
            f"SiliconFlow 请求失败: {message or f'HTTP {http_status}'}",
            http_status,
            "api_error",
            retryable=None,
        )

    @staticmethod
    def _parse_error_body(data: Any) -> tuple[str, int | None]:
        """官方错误体混杂：dict 取 code/message，JSON 字符串直接用（如 "Invalid token"）。"""
        if isinstance(data, dict):
            raw_code = data.get("code")
            code: int | None = None
            if raw_code is not None:
                try:
                    code = int(raw_code)
                except (TypeError, ValueError):
                    code = None
            message = str(data.get("message") or "").strip()
            return message, code
        if isinstance(data, str) and data.strip():
            return data.strip(), None
        return "", None

    @staticmethod
    def _normalize_api_base(value: Any) -> str:
        base = str(value or "").strip().rstrip("/")
        if base.lower().endswith("/v1"):
            # 防止用户照抄带 /v1 的文档示例拼出 /v1/v1
            base = base[:-3].rstrip("/")
        return base or _DEFAULT_API_BASE
