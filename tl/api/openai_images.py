"""OpenAI Images API 供应商实现。

支持：
- /v1/images/generations  文生图（JSON 请求）
- /v1/images/edits        图像编辑（multipart/form-data 请求）

兼容 OpenAI 官方、NewAPI等 OpenAI Images API 兼容端点。
"""

from __future__ import annotations

import base64
import re
from typing import Any

import aiohttp

from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from ..openai_image_size import (
    derive_custom_size_matching_aspect,
    normalize_custom_size_input,
    normalize_size_mode,
    resolve_openai_custom_size,
)
from ..tl_utils import save_base64_image
from .base import ProviderRequest

# ---------- 按模型族分的合法尺寸映射 ----------

_SIZE_MAP_DALLE2: dict[str, str] = {
    "1K": "1024x1024",
    "2K": "1024x1024",  # dall-e-2 最大 1024x1024
    "4K": "1024x1024",
}

_SIZE_MAP_DALLE3: dict[str, str] = {
    "1K": "1024x1024",
    "2K": "1792x1024",
    "4K": "1024x1792",
}

_SIZE_MAP_GPT_IMAGE: dict[str, str] = {
    "1K": "1024x1024",
    "2K": "1536x1024",
    "4K": "auto",
}


def _is_gpt_image_model(model: str) -> bool:
    """判断是否为 GPT image 系列模型（gpt-image-1 / gpt-image-1-mini / gpt-image-1.5 等）"""
    m = (model or "").lower()
    return m.startswith("gpt-image") or m == "chatgpt-image-latest"


def _get_size_mapping(model: str) -> dict[str, str]:
    """根据模型名称返回对应的尺寸映射表"""
    m = (model or "").lower()
    if _is_gpt_image_model(m):
        return _SIZE_MAP_GPT_IMAGE
    if "dall-e-2" in m or "dalle2" in m:
        return _SIZE_MAP_DALLE2
    # dall-e-3 及其他未知模型使用 dall-e-3 映射
    return _SIZE_MAP_DALLE3


def _resolve_size_value(
    model: str,
    resolution: str | None,
    settings: dict[str, Any],
    *,
    ref_image_dims: tuple[int, int] | None = None,
) -> str | None:
    """根据配置和请求参数决定最终传给 OpenAI Images API 的 size。

    当 ``ref_image_dims`` 提供且 resolution 为空（preserve_reference_image_size 触发）：
    - preset 模式：返回 None（不传 size，由 API 默认按原图）
    - custom 模式：根据参考图实际比例推导一个合法 custom size，避免回落到固定 custom_size
    """
    try:
        size_mode = normalize_size_mode(settings.get("size_mode"))
    except ValueError as e:
        raise APIError(str(e), None, "invalid_size_mode", retryable=False) from e

    # 保留参考图尺寸场景：resolution 被显式置空
    if not resolution and ref_image_dims is not None:
        if size_mode != "custom":
            return None
        ref_w, ref_h = ref_image_dims
        # 以用户配置的 custom_size 像素总量作为目标；未配置留为 None 交由推导函数使用默认值
        target_pixels: int | None = None
        raw_custom = settings.get("custom_size")
        if raw_custom not in (None, ""):
            try:
                normalized = normalize_custom_size_input(raw_custom)
                match = re.fullmatch(r"(\d+)x(\d+)", normalized)
                if match:
                    target_pixels = int(match.group(1)) * int(match.group(2))
            except (TypeError, ValueError) as e:
                # custom_size 配置不合法时明确报错，不静默回退到默认像素数
                raise APIError(
                    f"invalid custom_size: {raw_custom}",
                    None,
                    "invalid_size",
                    retryable=False,
                ) from e
        try:
            return derive_custom_size_matching_aspect(
                ref_w, ref_h, target_pixels=target_pixels
            )
        except ValueError as e:
            raise APIError(str(e), None, "invalid_size", retryable=False) from e

    if size_mode == "custom":
        try:
            return resolve_openai_custom_size(
                resolution,
                None,
                None,
                settings,
                size_field_name="size",
            )
        except ValueError as e:
            raise APIError(str(e), None, "invalid_size", retryable=False) from e

    if resolution:
        size_map = _get_size_mapping(model)
        return size_map.get(resolution, resolution)
    return None


class OpenAIImagesProvider:
    """OpenAI /v1/images/generations + /v1/images/edits 端点实现"""

    name = "openai_images"

    async def build_request(
        self, *, client: Any, config: ApiRequestConfig
    ) -> ProviderRequest:  # noqa: ANN401
        settings: dict[str, Any] = getattr(client, "openai_images_settings", None) or {}
        api_base = (config.api_base or "").rstrip("/")
        default_base = "https://api.openai.com"

        base = api_base if api_base else default_base
        logger.debug(f"[openai_images] API Base: {base}")

        # 根据是否有参考图决定走 generations 还是 edits
        generations_only = bool(settings.get("generations_only", False))
        has_ref_images = bool(config.reference_images) and not generations_only

        if generations_only and config.reference_images:
            logger.info(
                "[openai_images] generations_only 已开启，忽略参考图，仅使用文生图"
            )

        if has_ref_images:
            # /v1/images/edits (multipart/form-data)
            endpoint = "images/edits"
            if base.endswith("/v1"):
                url = f"{base}/{endpoint}"
            else:
                url = f"{base}/v1/{endpoint}"

            payload = await self._prepare_edits_payload(
                client=client,
                config=config,
                settings=settings,
            )
            headers = {
                "Authorization": f"Bearer {config.api_key}",
                # Content-Type 由 aiohttp 自动设置 multipart boundary
            }
        else:
            # /v1/images/generations (JSON)
            endpoint = "images/generations"
            if base.endswith("/v1"):
                url = f"{base}/{endpoint}"
            else:
                url = f"{base}/v1/{endpoint}"

            payload = await self._prepare_generations_payload(
                client=client,
                config=config,
                settings=settings,
            )
            headers = {
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            }

        logger.debug(f"[openai_images] URL: {url} (edits={has_ref_images})")
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
        """解析 OpenAI Images API 响应（generations / edits 共用）"""
        image_urls: list[str] = []
        image_paths: list[str] = []
        text_content = None
        thought_signature = None

        # ---------- 解析 usage ----------
        usage = response_data.get("usage")
        if isinstance(usage, dict):
            in_tok = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            total_tok = usage.get("total_tokens", in_tok + out_tok)
            logger.info(
                f"[openai_images] Token 用量: input={in_tok} output={out_tok} total={total_tok}"
            )

        # ---------- 推断输出格式 ----------
        resp_output_format = response_data.get("output_format") or ""
        save_ext = (
            resp_output_format
            if resp_output_format in {"png", "jpeg", "webp"}
            else "png"
        )

        # ---------- 解析图片数据 ----------
        if not response_data.get("data"):
            # 优先提取 API 返回的错误信息
            error_obj = response_data.get("error")
            if error_obj:
                error_msg = (
                    error_obj.get("message", "未知错误")
                    if isinstance(error_obj, dict)
                    else str(error_obj)
                )
                logger.warning(f"[openai_images] API 返回错误: {error_msg}")
                raise APIError(
                    f"图像生成失败: {error_msg}",
                    http_status,
                    "api_error",
                    error_obj.get("code") if isinstance(error_obj, dict) else None,
                    retryable=False,
                )
            logger.warning(f"[openai_images] 响应无 data 字段: {response_data}")
            raise APIError(
                "API 响应格式不正确，缺少 data 字段",
                None,
                "invalid_response",
                retryable=True,
            )

        for image_item in response_data["data"]:
            if not isinstance(image_item, dict):
                continue

            # 优先处理 URL
            if "url" in image_item:
                image_url = image_item["url"]
                if isinstance(image_url, str) and image_url:
                    image_urls.append(image_url)
                    logger.debug(f"[openai_images] 图片 URL: {image_url[:80]}...")

            # 处理 base64 图片
            elif "b64_json" in image_item:
                b64_data = image_item["b64_json"]
                if isinstance(b64_data, str) and b64_data:
                    image_path = await save_base64_image(b64_data, save_ext)
                    if image_path:
                        image_urls.append(image_path)
                        image_paths.append(image_path)
                        logger.debug(
                            f"[openai_images] base64 图片 ({save_ext}): {len(b64_data)} 字节"
                        )

            # 记录修订后的提示词（dall-e-3 only）
            revised = image_item.get("revised_prompt")
            if revised:
                text_content = f"修订提示词: {revised}"
                logger.debug(f"[openai_images] 修订提示词: {revised[:100]}...")

        if image_urls or image_paths:
            logger.debug(f"[openai_images] 共 {len(image_urls)} 张图片")
            return image_urls, image_paths, text_content, thought_signature

        # 没有图片 → 提取错误信息
        error_obj = response_data.get("error") or {}
        error_msg = (
            error_obj.get("message", "未知错误")
            if isinstance(error_obj, dict)
            else str(error_obj)
        )
        logger.warning(f"[openai_images] 未返回图片: {error_msg}")
        raise APIError(
            f"图像生成失败: {error_msg}",
            error_obj.get("code") if isinstance(error_obj, dict) else None,
            "no_image",
            retryable=False,
        )

    async def _prepare_generations_payload(
        self,
        *,
        client: Any,
        config: ApiRequestConfig,
        settings: dict[str, Any],
    ) -> dict[str, Any]:  # noqa: ANN401
        """构建 /v1/images/generations 请求体"""
        model = config.model or "gpt-image-1"
        payload: dict[str, Any] = {
            "model": model,
            "prompt": config.prompt,
        }

        # ---- size ----
        size_value = _resolve_size_value(
            model,
            config.resolution,
            settings,
        )
        if size_value:
            payload["size"] = size_value

        # ---- quality ----
        quality = str(settings.get("quality") or "").strip()
        if quality:
            payload["quality"] = quality

        # ---- response_format ----
        response_format = str(settings.get("response_format") or "").strip()
        if response_format:
            payload["response_format"] = response_format

        # ---- style (dall-e-3 only) ----
        style = str(settings.get("style") or "").strip()
        if style:
            payload["style"] = style

        # ---- GPT image 模型专属参数 ----
        is_gpt = _is_gpt_image_model(model)

        background = str(settings.get("background") or "").strip()
        if background and is_gpt:
            payload["background"] = background

        output_format = str(settings.get("output_format") or "").strip()
        if output_format and is_gpt:
            payload["output_format"] = output_format

        try:
            output_compression = int(settings.get("output_compression", 0))
        except (TypeError, ValueError):
            output_compression = 0
        if output_compression > 0 and is_gpt and output_format in {"jpeg", "webp"}:
            payload["output_compression"] = min(output_compression, 100)

        moderation = str(settings.get("moderation") or "").strip()
        if moderation and is_gpt:
            payload["moderation"] = moderation

        # ---- seed ----
        if config.seed is not None:
            payload["seed"] = config.seed

        logger.debug(
            f"[openai_images] generations payload: model={model} size={payload.get('size')} "
            f"quality={payload.get('quality')} style={payload.get('style')} "
            f"response_format={payload.get('response_format')} prompt_len={len(config.prompt)}"
        )
        return payload

    async def _prepare_edits_payload(
        self,
        *,
        client: Any,
        config: ApiRequestConfig,
        settings: dict[str, Any],
    ) -> dict[str, Any]:  # noqa: ANN401
        """构建 /v1/images/edits 请求体（multipart/form-data）

        返回 payload dict 中包含 ``_multipart`` 标记和 ``_form_data`` 对象，
        由 tl_api._perform_request 识别并切换为 FormData 发送。
        """
        model = config.model or "gpt-image-1"
        form = aiohttp.FormData()

        # ---- image (required): 第一张参考图 ----
        ref_images = config.reference_images or []
        if not ref_images:
            raise APIError(
                "/v1/images/edits 需要至少一张参考图",
                None,
                "missing_image",
                retryable=False,
            )

        image_data = self._decode_image_input(ref_images[0])
        if image_data is None:
            raise APIError(
                "无法解码参考图为二进制数据",
                None,
                "invalid_image",
                retryable=False,
            )
        form.add_field(
            "image",
            image_data,
            filename="image.png",
            content_type="image/png",
        )

        # ---- 多图支持 (GPT image 模型支持多张 image) ----
        if _is_gpt_image_model(model) and len(ref_images) > 1:
            for idx, extra_ref in enumerate(ref_images[1:], start=2):
                extra_data = self._decode_image_input(extra_ref)
                if extra_data:
                    form.add_field(
                        "image",
                        extra_data,
                        filename=f"image_{idx}.png",
                        content_type="image/png",
                    )

        # ---- prompt ----
        form.add_field("prompt", config.prompt)

        # ---- model ----
        form.add_field("model", model)

        # ---- size ----
        ref_dims: tuple[int, int] | None = None
        if not config.resolution:
            ref_dims = self._probe_image_dims(image_data)
        size_value = _resolve_size_value(
            model,
            config.resolution,
            settings,
            ref_image_dims=ref_dims,
        )
        if size_value:
            form.add_field("size", size_value)

        response_format = str(settings.get("response_format") or "b64_json").strip()
        form.add_field("response_format", response_format)

        quality = str(settings.get("quality") or "").strip()
        if quality:
            form.add_field("quality", quality)

        logger.debug(
            f"[openai_images] edits payload: model={model} ref_images={len(ref_images)} "
            f"prompt_len={len(config.prompt)}"
        )

        # 返回带标记的 payload，tl_api 会检测 _multipart 并使用 _form_data 发送
        return {
            "_multipart": True,
            "_form_data": form,
            "model": model,
            "prompt": config.prompt,
        }

    @staticmethod
    def _decode_image_input(image_input: str) -> bytes | None:
        """将 base64 字符串或 data URI 解码为二进制数据"""
        s = (image_input or "").strip()
        if not s:
            return None

        # 处理 data URI: data:image/png;base64,xxxx
        if s.startswith("data:"):
            parts = s.split(",", 1)
            if len(parts) == 2:
                s = parts[1]

        try:
            return base64.b64decode(s, validate=True)
        except Exception:
            logger.debug(f"[openai_images] 无法 base64 解码图片输入 (len={len(s)})")
            return None

    @staticmethod
    def _probe_image_dims(image_bytes: bytes | None) -> tuple[int, int] | None:
        """读取图片二进制的真实宽高（用于参考图比例感知）"""
        if not image_bytes:
            return None
        try:
            from io import BytesIO

            from PIL import Image as _PILImage

            with _PILImage.open(BytesIO(image_bytes)) as img:
                return int(img.width), int(img.height)
        except Exception as e:
            logger.debug(f"[openai_images] 读取参考图尺寸失败: {e}")
            return None
