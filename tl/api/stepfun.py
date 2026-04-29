"""阶跃星辰图片生成供应商实现。"""

from __future__ import annotations

import base64
from typing import Any

import aiohttp
from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from ..tl_utils import save_base64_image
from .base import ProviderRequest

# generations 接口不同模型支持的 size 预设（WxH 格式）。
# step-image-edit-2 的 edits 接口：size 不生效，输出始终与输入同尺寸。
# step-1x-edit 的 edits 接口：仅支持 512x512 / 768x768 / 1024x1024。
_STEP_IMAGE_EDIT2_GEN_SIZES: list[tuple[int, int]] = [
    (1024, 1024),  # 1:1
    (768, 1360),  # ~9:16  竖
    (896, 1184),  # ~3:4   竖
    (1360, 768),  # ~16:9  横
    (1184, 896),  # ~4:3   横
]
_STEP_1X_MEDIUM_GEN_SIZES: list[tuple[int, int]] = [
    (256, 256),
    (512, 512),
    (768, 768),
    (1024, 1024),
    (1280, 800),  # 16:10
    (800, 1280),  # 10:16
]
_STEP1X_EDIT_SIZES: set[str] = {"512x512", "768x768", "1024x1024"}


def _gen_size_presets_for(model: str) -> list[tuple[int, int]]:
    name = (model or "").strip().lower()
    if name == "step-1x-medium":
        return _STEP_1X_MEDIUM_GEN_SIZES
    # 默认按 step-image-edit-2 的预设集处理
    return _STEP_IMAGE_EDIT2_GEN_SIZES


def _parse_aspect_ratio(value: str | None) -> float | None:
    """解析 'W:H' 格式的长宽比为 W/H 浮点值"""
    if not value:
        return None
    text = str(value).strip().lower().replace("x", ":").replace("×", ":")
    if ":" not in text:
        return None
    try:
        w_str, h_str = text.split(":", 1)
        w = float(w_str.strip())
        h = float(h_str.strip())
        if w <= 0 or h <= 0:
            return None
        return w / h
    except (TypeError, ValueError):
        return None


def _normalize_size_str(value: Any) -> str | None:
    """将 'WxH' / 'W×H' 归一化为 'WxH' 小写格式。"""
    if value is None:
        return None
    text = str(value).strip().lower().replace("×", "x")
    if not text or "x" not in text:
        return None
    parts = text.split("x", 1)
    try:
        w = int(parts[0].strip())
        h = int(parts[1].strip())
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return f"{w}x{h}"


def _resolve_step_size(
    resolution: str | None,
    aspect_ratio: str | None,
    *,
    explicit_size: Any = None,
    model: str | None = None,
) -> str | None:
    """将 resolution + aspect_ratio 映射到当前模型支持的 generations 预设尺寸。

    优先级：
    1. ``explicit_size`` 合法且在该模型预设集合内 → 透传；
    2. resolution 为 1K 或留空 + aspect_ratio → 选最接近预设；
    3. 其他情况 → None（不传 size，服务端默认 1024x1024）。
    """
    presets = _gen_size_presets_for(model or "")
    preset_set = {f"{w}x{h}" for w, h in presets}

    norm_explicit = _normalize_size_str(explicit_size)
    if norm_explicit and norm_explicit in preset_set:
        return norm_explicit

    res_norm = (resolution or "").strip().upper()
    if res_norm and res_norm not in {"1K", ""}:
        return None

    target_ratio = _parse_aspect_ratio(aspect_ratio)
    if target_ratio is None:
        # 未指定长宽比 → 默认正方形
        return "1024x1024"

    # 选择与目标长宽比最接近的预设
    best = min(
        presets,
        key=lambda wh: abs((wh[0] / wh[1]) - target_ratio),
    )
    return f"{best[0]}x{best[1]}"


class StepfunProvider:
    """StepFun /v1/images/generations + /v1/images/edits 端点实现"""

    name = "stepfun"

    # ------------------------------------------------------------------
    # build_request
    # ------------------------------------------------------------------

    async def build_request(
        self, *, client: Any, config: ApiRequestConfig
    ) -> ProviderRequest:  # noqa: ANN401
        settings: dict[str, Any] = getattr(client, "stepfun_settings", None) or {}

        api_base = (
            (config.api_base or "").strip()
            or str(settings.get("api_base") or "").strip()
            or "https://api.stepfun.com"
        )
        base = api_base.rstrip("/")

        model = (
            str(settings.get("model") or "").strip()
            or (config.model or "").strip()
            or "step-image-edit-2"
        )

        has_ref = bool(config.reference_images)

        if has_ref:
            endpoint = "images/edits"
        else:
            endpoint = "images/generations"

        if base.endswith("/v1"):
            url = f"{base}/{endpoint}"
        else:
            url = f"{base}/v1/{endpoint}"

        if has_ref:
            payload = self._prepare_edits_payload(
                config=config, settings=settings, model=model
            )
            headers = {
                "Authorization": f"Bearer {config.api_key}",
                # multipart Content-Type 由 aiohttp 自动设置 boundary
            }
        else:
            payload = self._prepare_generations_payload(
                config=config, settings=settings, model=model
            )
            headers = {
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            }

        logger.debug(f"[stepfun] URL: {url} (edits={has_ref}) model={model}")
        return ProviderRequest(url=url, headers=headers, payload=payload)

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
    ) -> tuple[list[str], list[str], str | None, str | None]:  # noqa: ANN401
        """解析 StepFun Images API 响应"""
        image_urls: list[str] = []
        image_paths: list[str] = []

        # 优先处理顶层错误
        error_obj = response_data.get("error")
        if error_obj:
            error_msg = (
                error_obj.get("message", "未知错误")
                if isinstance(error_obj, dict)
                else str(error_obj)
            )
            error_code = error_obj.get("code") if isinstance(error_obj, dict) else None
            logger.warning(f"[stepfun] API 返回错误: {error_msg}")
            raise APIError(
                f"StepFun 错误: {error_msg}",
                http_status,
                "api_error",
                error_code,
                retryable=False,
            )

        data_list = response_data.get("data")
        if not isinstance(data_list, list) or not data_list:
            logger.warning(f"[stepfun] 响应缺少 data: {response_data}")
            raise APIError(
                "StepFun 未返回图片",
                http_status,
                "no_image",
                retryable=False,
            )

        for image_item in data_list:
            if not isinstance(image_item, dict):
                continue

            url_value = image_item.get("url")
            if isinstance(url_value, str) and url_value:
                image_urls.append(url_value)
                logger.debug(f"[stepfun] 图片 URL: {url_value[:80]}...")
                continue

            b64_value = image_item.get("b64_json")
            if isinstance(b64_value, str) and b64_value:
                image_path = await save_base64_image(b64_value, "png")
                if image_path:
                    image_urls.append(image_path)
                    image_paths.append(image_path)
                    logger.debug(f"[stepfun] base64 图片: {len(b64_value)} 字节")

        if not image_urls and not image_paths:
            logger.warning(f"[stepfun] data 中未提取到图片: {response_data}")
            raise APIError(
                "StepFun 未返回图片",
                http_status,
                "no_image",
                retryable=False,
            )

        logger.debug(f"[stepfun] 共 {len(image_urls)} 张图片")
        return image_urls, image_paths, None, None

    # ------------------------------------------------------------------
    # _prepare_generations_payload (文生图 JSON)
    # ------------------------------------------------------------------

    def _prepare_generations_payload(
        self,
        *,
        config: ApiRequestConfig,
        settings: dict[str, Any],
        model: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "prompt": config.prompt,
        }

        # ---- size（仅文生图模式）----
        # 优先使用 LLM/工具显式传入的 resolution（可能是 WxH字符串）作为 explicit size
        size_value = _resolve_step_size(
            config.resolution,
            config.aspect_ratio,
            explicit_size=config.resolution,
            model=model,
        )
        if size_value:
            payload["size"] = size_value

        self._apply_common_params(payload, config=config, settings=settings)
        return payload

    # ------------------------------------------------------------------
    # _prepare_edits_payload (图像编辑 multipart/form-data)
    # ------------------------------------------------------------------

    def _prepare_edits_payload(
        self,
        *,
        config: ApiRequestConfig,
        settings: dict[str, Any],
        model: str,
    ) -> dict[str, Any]:
        ref_images = config.reference_images or []
        if not ref_images:
            raise APIError(
                "/v1/images/edits 需要至少一张参考图",
                None,
                "missing_image",
                retryable=False,
            )

        # 官方文档：edits 接口当前仅支持传入一个图片
        if len(ref_images) > 1:
            logger.debug(
                f"[stepfun] edits 仅支持单张参考图，已提供 {len(ref_images)} 张，取首张"
            )

        image_data = self._decode_image_input(ref_images[0])
        if image_data is None:
            raise APIError(
                "无法解码参考图为二进制数据",
                None,
                "invalid_image",
                retryable=False,
            )

        form = aiohttp.FormData()
        form.add_field("model", model)
        form.add_field("prompt", config.prompt)
        form.add_field(
            "image",
            image_data,
            filename="image.png",
            content_type="image/png",
        )

        # ---- size：仅对 step-1x-edit 生效（官方 512/768/1024 三档）;
        # step-image-edit-2 的 edits 接口 size 不生效，输出始终与输入同尺寸。
        if model.strip().lower() == "step-1x-edit":
            explicit = _normalize_size_str(config.resolution)
            if explicit and explicit in _STEP1X_EDIT_SIZES:
                form.add_field("size", explicit)

        # ---- 可选参数（仅非零/非空才传） ----
        response_format = str(settings.get("response_format") or "url").strip() or "url"
        form.add_field("response_format", response_format)

        try:
            steps = int(settings.get("steps", 0) or 0)
        except (TypeError, ValueError):
            steps = 0
        if steps > 0:
            form.add_field("steps", str(steps))

        try:
            cfg_scale = float(settings.get("cfg_scale", 0) or 0)
        except (TypeError, ValueError):
            cfg_scale = 0.0
        if cfg_scale > 0:
            form.add_field("cfg_scale", str(cfg_scale))

        seed_setting = settings.get("seed", 0)
        try:
            seed_value = int(seed_setting or 0)
        except (TypeError, ValueError):
            seed_value = 0
        if seed_value > 0:
            form.add_field("seed", str(seed_value))
        elif config.seed is not None and int(config.seed) > 0:
            form.add_field("seed", str(int(config.seed)))

        negative_prompt = str(settings.get("negative_prompt") or "").strip()
        if negative_prompt:
            form.add_field("negative_prompt", negative_prompt)

        if bool(settings.get("text_mode", False)):
            form.add_field("text_mode", "true")

        logger.debug(
            f"[stepfun] edits payload: model={model} ref_images={len(ref_images)} "
            f"prompt_len={len(config.prompt)} response_format={response_format}"
        )

        return {
            "_multipart": True,
            "_form_data": form,
            "model": model,
            "prompt": config.prompt,
        }

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _apply_common_params(
        self,
        payload: dict[str, Any],
        *,
        config: ApiRequestConfig,
        settings: dict[str, Any],
    ) -> None:
        """文生图 JSON 路径下的可选参数应用"""
        response_format = str(settings.get("response_format") or "url").strip() or "url"
        payload["response_format"] = response_format

        try:
            steps = int(settings.get("steps", 0) or 0)
        except (TypeError, ValueError):
            steps = 0
        if steps > 0:
            payload["steps"] = steps

        try:
            cfg_scale = float(settings.get("cfg_scale", 0) or 0)
        except (TypeError, ValueError):
            cfg_scale = 0.0
        if cfg_scale > 0:
            payload["cfg_scale"] = cfg_scale

        seed_setting = settings.get("seed", 0)
        try:
            seed_value = int(seed_setting or 0)
        except (TypeError, ValueError):
            seed_value = 0
        if seed_value > 0:
            payload["seed"] = seed_value
        elif config.seed is not None and int(config.seed) > 0:
            payload["seed"] = int(config.seed)

        negative_prompt = str(settings.get("negative_prompt") or "").strip()
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt

        if bool(settings.get("text_mode", False)):
            payload["text_mode"] = True

    @staticmethod
    def _decode_image_input(image_input: str) -> bytes | None:
        """将 base64 字符串或 data URI 解码为二进制数据"""
        s = (image_input or "").strip()
        if not s:
            return None

        if s.startswith("data:"):
            parts = s.split(",", 1)
            if len(parts) == 2:
                s = parts[1]

        try:
            return base64.b64decode(s, validate=True)
        except Exception:
            logger.debug(f"[stepfun] 无法 base64 解码图片输入 (len={len(s)})")
            return None
