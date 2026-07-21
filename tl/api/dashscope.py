"""DashScope（阿里云百炼）图像生成供应商实现。

接入 multimodal-generation 同步接口，支持 wan2.7 / qwen-image-2.0 系列。
端点、参数门控与尺寸规则详见 docs/config.md 的 dashscope_settings 章节。
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from .base import ProviderRequest
from .data_uri import format_data_uri, looks_like_base64, strip_data_uri_prefix
from .provider_limits import MAX_REFERENCE_IMAGES_DASHSCOPE
from .reference_intake import announce_reference_intake

# 官方默认 API Base
_DEFAULT_API_BASE: str = "https://dashscope.aliyuncs.com"

# Token Plan 套餐端点
_TOKEN_PLAN_API_BASE: str = "https://token-plan.cn-beijing.maas.aliyuncs.com"

# endpoint_mode → 默认 API Base
_ENDPOINT_BASES: dict[str, str] = {
    "dashscope": _DEFAULT_API_BASE,
    "token_plan": _TOKEN_PLAN_API_BASE,
}

# 同步调用端点路径
_SYNC_ENDPOINT_PATH: str = "/api/v1/services/aigc/multimodal-generation/generation"

# 默认模型
_DEFAULT_MODEL: str = "wan2.7-image-pro"

# wan2.7 专用简写尺寸
_SHORTHAND_SIZES: frozenset[str] = frozenset({"1K", "2K", "4K"})

# WxH / W×H / W*H 尺寸格式
_SIZE_RE = re.compile(r"^(\d{2,5})\s*[x×*]\s*(\d{2,5})$")

# 官方推荐分辨率表
_SIZE_TABLE: dict[tuple[str, str], str] = {
    ("4K", "1:1"): "4096*4096",
    ("4K", "16:9"): "4096*2304",
    ("4K", "9:16"): "2304*4096",
    ("4K", "4:3"): "4096*3072",
    ("4K", "3:4"): "3072*4096",
    ("2K", "1:1"): "2048*2048",
    ("2K", "16:9"): "2688*1536",
    ("2K", "9:16"): "1536*2688",
    ("2K", "4:3"): "2368*1728",
    ("2K", "3:4"): "1728*2368",
    ("1K", "1:1"): "1280*1280",
    ("1K", "16:9"): "1696*960",
    ("1K", "9:16"): "960*1696",
    ("1K", "4:3"): "1472*1104",
    ("1K", "3:4"): "1104*1472",
}

# 各档位总像素预算
_TIER_PIXELS: dict[str, int] = {
    "1K": 1280 * 1280,
    "2K": 2048 * 2048,
    "4K": 4096 * 4096,
}

# 可重试错误码
_RETRYABLE_CODES: frozenset[str] = frozenset(
    {
        "Throttling",
        "Throttling.RateQuota",
        "Throttling.AllocationQuota",
        "InternalError",
        "InternalError.Algo",
        "ResponseTimeout",
        "ServiceUnavailable",
    }
)

# 不可重试错误码（认证/配额类不在此列：标记三态 None，交框架在多 Key 时轮换重试）
_NON_RETRYABLE_CODES: frozenset[str] = frozenset(
    {
        "DataInspectionFailed",
        "InvalidParameter",
        "ModelNotFound",
        "BadRequest.EmptyBody",
    }
)


def _normalize_api_base(value: Any, default: str = _DEFAULT_API_BASE) -> str:
    """归一化 api_base：剥离端点后缀，返回不带尾斜杠的 base。"""
    base = str(value or "").strip().rstrip("/")
    if not base:
        return default
    for suffix in (_SYNC_ENDPOINT_PATH, "/api/v1"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base.rstrip("/") or default


def _resolve_api_base(settings: dict[str, Any], config: ApiRequestConfig) -> str:
    """决定最终 API Base：显式 api_base 优先，否则按 endpoint_mode 选默认端点。"""
    mode = str(settings.get("endpoint_mode") or "dashscope").strip().lower()
    default_base = _ENDPOINT_BASES.get(mode)
    if default_base is None:
        logger.warning(
            "[dashscope] endpoint_mode=%s 未知，已回退为 dashscope 官方端点",
            settings.get("endpoint_mode"),
        )
        default_base = _DEFAULT_API_BASE
    explicit = str(settings.get("api_base") or config.api_base or "").strip()
    if explicit:
        return _normalize_api_base(explicit, default_base)
    return default_base


def _normalize_ratio(value: Any) -> str:
    """将 'W:H' / 'W×H' / 'WxH' 归一化为 'W:H'，失败回退 1:1。"""
    text = str(value or "").strip().lower().replace("×", ":").replace("x", ":")
    parts = text.split(":", 1)
    if len(parts) == 2:
        try:
            w = int(parts[0].strip())
            h = int(parts[1].strip())
            if w > 0 and h > 0:
                return f"{w}:{h}"
        except (TypeError, ValueError):
            pass
    if text:
        logger.warning("[dashscope] aspect_ratio=%s 无法解析，已回退为 1:1", value)
    return "1:1"


def _to_wire_size(value: str) -> str | None:
    """将用户输入尺寸转为 wire 格式：1K/2K/4K 简写大写原样，WxH → W*H。"""
    text = (value or "").strip()
    if text.upper() in _SHORTHAND_SIZES:
        return text.upper()
    match = _SIZE_RE.match(text)
    if match:
        return f"{int(match.group(1))}*{int(match.group(2))}"
    logger.warning(
        "[dashscope] custom_size=%s 格式无法识别，已回退为 preset 模式", value
    )
    return None


def _compute_size(tier: str, rw: int, rh: int) -> str:
    """按档位像素预算推算表外比例的尺寸（16 对齐，钳位 [512, 4096]）。"""
    budget = _TIER_PIXELS.get(tier, _TIER_PIXELS["2K"])
    h = round(math.sqrt(budget / (rw / rh)))
    w = round(h * rw / rh)
    w = max(512, min(4096, round(w / 16) * 16))
    h = max(512, min(4096, round(h / 16) * 16))
    logger.info(
        "[dashscope] 比例 %s:%s 不在推荐表，按 %s 档推算为 %s*%s", rw, rh, tier, w, h
    )
    return f"{w}*{h}"


def _resolve_size(settings: dict[str, Any], config: ApiRequestConfig) -> str | None:
    """决定最终 size；返回 None 表示不发送该参数。"""
    if getattr(config, "suppress_resolution", False):
        return None

    size_mode = str(settings.get("size_mode") or "preset").strip().lower()
    if size_mode == "custom":
        custom = str(settings.get("custom_size") or "").strip()
        if custom:
            wire = _to_wire_size(custom)
            if wire:
                return wire

    tier = str(config.resolution or "2K").strip().upper()
    if tier not in _TIER_PIXELS:
        logger.warning("[dashscope] resolution=%s 非法，已回退为 2K", config.resolution)
        tier = "2K"

    ratio = _normalize_ratio(config.aspect_ratio or "1:1")
    preset = _SIZE_TABLE.get((tier, ratio))
    if preset:
        return preset

    rw, rh = (int(part) for part in ratio.split(":", 1))
    return _compute_size(tier, rw, rh)


def _coerce_n(value: Any, n_max: int) -> int:
    """解析 n；失败回退 1，越界钳位到 [1, n_max]。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 1
    if n < 1 or n > n_max:
        logger.warning("[dashscope] n=%s 越界，已钳位到 [1, %d]", value, n_max)
    return max(1, min(n, n_max))


def _build_api_error(code: str, message: str, http_status: int | None) -> APIError:
    """根据 DashScope 错误码构建带重试语义的 APIError。

    未知错误码 retryable=None，交框架通用逻辑判断（多 Key 轮换、5xx/429 重试等）。
    """
    retryable: bool | None = None
    if code in _RETRYABLE_CODES:
        retryable = True
    elif code in _NON_RETRYABLE_CODES:
        retryable = False
    return APIError(
        f"DashScope 图像生成失败: {message}",
        http_status,
        "api_error",
        code,
        retryable=retryable,
    )


class DashScopeProvider:
    """DashScope ``multimodal-generation/generation`` 同步端点实现。"""

    name = "dashscope"

    async def build_request(
        self, *, client: Any, config: ApiRequestConfig
    ) -> ProviderRequest:  # noqa: ANN401
        settings: dict[str, Any] = (
            getattr(config, "provider_settings", None)
            or getattr(client, "dashscope_settings", None)
            or {}
        )

        api_base = _resolve_api_base(settings, config)
        url = f"{api_base}{_SYNC_ENDPOINT_PATH}"

        if not config.api_key:
            raise APIError(
                "DashScope 缺少 API Key，请在 provider_overrides.dashscope.api_keys 中配置",
                None,
                "missing_api_key",
                retryable=False,
            )

        prompt = (config.prompt or "").strip()
        if not prompt:
            raise APIError(
                "DashScope 需要非空 prompt", None, "empty_prompt", retryable=False
            )

        model = (
            str(settings.get("model") or config.model or _DEFAULT_MODEL).strip()
            or _DEFAULT_MODEL
        )

        content: list[dict[str, Any]] = [{"text": prompt}]
        image_values = await self._prepare_image_values(client=client, config=config)
        for value in image_values:
            content.append({"image": value})

        is_wan27 = model.startswith("wan2.7")
        enable_seq = is_wan27 and bool(settings.get("enable_sequential", False))

        params: dict[str, Any] = {}

        size = _resolve_size(settings, config)
        if size:
            params["size"] = size

        # n 上限按模型分级
        if enable_seq:
            n_max = 12
        elif is_wan27:
            n_max = 4
        elif model.startswith("qwen-image-2.0"):
            n_max = 6
        else:
            n_max = 1
        params["n"] = _coerce_n(settings.get("n", 1), n_max)

        params["watermark"] = bool(settings.get("watermark", False))

        neg = str(settings.get("negative_prompt") or "").strip()
        if neg:
            if is_wan27:
                logger.info("[dashscope] %s 不支持 negative_prompt，已忽略", model)
            else:
                params["negative_prompt"] = neg

        if is_wan27:
            if enable_seq:
                # 与 thinking_mode 互斥
                params["enable_sequential"] = True
            else:
                params["thinking_mode"] = bool(settings.get("thinking_mode", True))
        else:
            if settings.get("enable_sequential"):
                logger.info("[dashscope] enable_sequential 仅 wan2.7 支持，已忽略")
            # 服务端默认 true，需显式关闭
            params["prompt_extend"] = bool(settings.get("prompt_extend", False))

        payload: dict[str, Any] = {
            "model": model,
            "input": {"messages": [{"role": "user", "content": content}]},
            "parameters": params,
        }

        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }

        logger.debug(
            "[dashscope] build_request: url=%s model=%s size=%s n=%s refs=%d",
            url,
            model,
            params.get("size"),
            params["n"],
            len(image_values),
        )
        return ProviderRequest(url=url, headers=headers, payload=payload)

    async def _prepare_image_values(
        self, *, client: Any, config: ApiRequestConfig
    ) -> list[str]:  # noqa: ANN401
        """将参考图转换为 URL / data URI 列表（不支持 file://，本地路径转 base64）。"""
        refs = config.reference_images or []
        if not refs:
            return []

        announce_reference_intake(
            refs, MAX_REFERENCE_IMAGES_DASHSCOPE, log_prefix="[dashscope] "
        )
        force_b64 = (
            getattr(config, "image_input_mode", "force_base64") == "force_base64"
        )

        values: list[str] = []
        for image_str in refs[:MAX_REFERENCE_IMAGES_DASHSCOPE]:
            value = await self._process_single_image(
                client=client,
                config=config,
                image_str=str(image_str or ""),
                force_b64=force_b64,
            )
            if value:
                values.append(value)
        return values

    async def _process_single_image(
        self,
        *,
        client: Any,
        config: ApiRequestConfig,
        image_str: str,
        force_b64: bool,
    ) -> str | None:  # noqa: ANN401
        if not image_str:
            return None

        # URL 输入且不强制 base64 → 原样透传
        if image_str.startswith(("http://", "https://")) and not force_b64:
            return image_str

        # 已是标准 data URI → 原样
        if image_str.startswith("data:image/") and ";base64," in image_str:
            return image_str

        # 裸 base64 → 补 data URI 前缀
        if looks_like_base64(image_str) and not image_str.startswith("data:"):
            return format_data_uri(strip_data_uri_prefix(image_str))

        # 其余（本地路径 / 强制 base64 的 URL）走客户端归一化；
        # 共享归一化器不认裸路径，先转 file:// URI
        normalize_input = image_str
        if "://" not in image_str and Path(image_str).is_file():
            normalize_input = Path(image_str).resolve().as_uri()
        try:
            mime_type, b64_data = await client._normalize_reference_image_input(
                normalize_input,
                image_input_mode=getattr(config, "image_input_mode", "force_base64"),
            )
        except Exception as e:
            logger.debug("[dashscope] normalize_reference_image_input failed: %s", e)
            mime_type, b64_data = None, None

        if not b64_data:
            if force_b64:
                raise APIError(
                    "参考图转换失败（dashscope），请检查图片来源后重试。",
                    None,
                    "invalid_reference_image",
                    retryable=False,
                )
            if image_str.startswith(("http://", "https://")):
                return image_str
            return None

        return format_data_uri(strip_data_uri_prefix(b64_data), mime_type)

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
        if not isinstance(response_data, dict):
            raise APIError(
                "DashScope API 返回了非预期格式的响应，请稍后重试。",
                http_status,
                "invalid_response",
                retryable=True,
            )

        # 错误优先：DashScope 错误体顶层 code 非空
        code = str(response_data.get("code") or "").strip()
        if code:
            message = str(response_data.get("message") or "未知错误")
            raise _build_api_error(code, message, http_status)

        remote_urls: list[str] = []
        text_parts: list[str] = []
        output = response_data.get("output")
        choices = output.get("choices") if isinstance(output, dict) else None
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message_obj = choice.get("message")
                if not isinstance(message_obj, dict):
                    continue
                content = message_obj.get("content")
                if not isinstance(content, list):
                    continue
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    image = item.get("image")
                    if isinstance(image, str) and image:
                        remote_urls.append(image)
                        continue
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        text_parts.append(text)

        text_content = "\n".join(text_parts) or None

        usage = response_data.get("usage")
        if isinstance(usage, dict):
            logger.debug(
                "[dashscope] usage: image_count=%s input_tokens=%s output_tokens=%s",
                usage.get("image_count"),
                usage.get("input_tokens"),
                usage.get("output_tokens"),
            )

        image_urls: list[str] = []
        image_paths: list[str] = []
        # URL 24 小时过期，一律尝试下载落盘；失败兜底直链
        for remote_url in dict.fromkeys(remote_urls):
            image_path = None
            try:
                _, image_path = await client._download_image(
                    remote_url,
                    session,
                    use_cache=False,
                    proxy=client._request_http_proxy(request_config),
                )
            except Exception as e:
                logger.warning("[dashscope] 图片下载失败，回退直链: %s", e)
            if image_path:
                image_urls.append(image_path)
                image_paths.append(image_path)
            else:
                image_urls.append(remote_url)

        if not image_urls:
            raise APIError(
                "DashScope 未返回图片数据",
                http_status,
                "no_image",
                retryable=False,
            )

        logger.debug("[dashscope] 共 %d 张图片", len(image_urls))
        return image_urls, image_paths, text_content, None
