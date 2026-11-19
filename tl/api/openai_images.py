"""OpenAI Images API 供应商实现。

支持：
- /v1/images/generations  文生图（JSON 请求）
- /v1/images/edits        图像编辑（multipart/form-data 请求）

兼容 OpenAI 官方、NewAPI等 OpenAI Images API 兼容端点。
"""

from __future__ import annotations

import codecs
import json
from typing import Any

import aiohttp
from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from ..generation_scheduler import emit_generation_preview
from ..openai_image_size import (
    derive_custom_size_from_preset_params,
    derive_custom_size_matching_aspect,
    normalize_size_mode,
    resolve_openai_custom_size,
    validate_custom_size,
)
from ..tl_utils import save_base64_image
from .base import ProviderRequest
from .openai_image_options import gpt_image_output_options
from .reference_pipeline import load_reference_bytes

# ---------- 合法尺寸映射 ----------

_SIZE_MAP_GPT_IMAGE: dict[str, str] = {
    "1K": "1024x1024",
    "2K": "1536x1024",
    "4K": "auto",
}


def _is_gpt_image_model(model: str) -> bool:
    """判断是否为 GPT image 系列模型（gpt-image-1-mini、gpt-image-1.5、gpt-image-2 等）"""
    m = (model or "").lower()
    return m.startswith("gpt-image") or m == "chatgpt-image-latest"


def _get_size_mapping(model: str) -> dict[str, str]:
    """返回合法尺寸映射表；未知模型名统一沿用 GPT Image 预设。"""
    return _SIZE_MAP_GPT_IMAGE


def _resolve_size_value(
    model: str,
    resolution: str | None,
    settings: dict[str, Any],
    *,
    ref_image_dims: tuple[int, int] | None = None,
    suppress_resolution: bool = False,
    aspect_ratio: str | None = None,
) -> str | None:
    """根据配置和请求参数决定最终传给 OpenAI Images API 的 size。

    当 ``ref_image_dims`` 提供且 resolution 为空（preserve_reference_image_size 触发）：
    - preset 模式：返回 None（不传 size，由 API 默认按原图）
    - custom 模式：根据参考图实际比例推导一个合法 custom size，避免回落到固定 custom_size
    """
    if suppress_resolution:
        return None
    if settings.get("size_mode") == "auto":
        return "auto"
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
                validated = validate_custom_size(
                    raw_custom, field_name="openai_images.custom_size"
                )
                w_str, h_str = validated.split("x")
                target_pixels = int(w_str) * int(h_str)
            except ValueError as e:
                # custom_size 配置不合法时明确报错，不静默回退到默认像素数
                raise APIError(
                    str(e),
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
        if model.lower().startswith("gpt-image-2") and resolution in {"1K", "2K", "4K"}:
            try:
                return derive_custom_size_from_preset_params(
                    resolution, aspect_ratio or settings.get("aspect_ratio") or "1:1"
                )
            except ValueError as exc:
                raise APIError(
                    str(exc), error_type="invalid_size", retryable=False
                ) from exc
        size_map = _get_size_mapping(model)
        return size_map.get(resolution, resolution)
    return None


async def read_images_response(*, response: aiohttp.ClientResponse) -> dict[str, Any]:
    """Read native Images events; only completed events contain final images."""
    if "text/event-stream" not in response.headers.get("Content-Type", "").lower():
        result = json.loads(await response.text())
        if not isinstance(result, dict):
            raise ValueError("Images API response must be an object")
        return result
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending = ""
    lines: list[str] = []

    async def consume():
        if not lines:
            return None
        raw = "\n".join(lines)
        lines.clear()
        if raw.strip() == "[DONE]":
            return None
        event = json.loads(raw)
        kind = event.get("type", "")
        if kind in {"image_generation.partial_image", "image_edit.partial_image"}:
            await emit_generation_preview(
                event.get("b64_json"), event.get("output_format") or "png"
            )
        elif kind in {"image_generation.completed", "image_edit.completed"}:
            return (
                {**event, "data": [{"b64_json": event.get("b64_json")}]}
                if event.get("b64_json")
                else {"error": {"message": "图片完成事件缺少图片数据"}}
            )
        elif kind == "error":
            return {"error": event.get("error") or event}
        return None

    async for chunk in response.content.iter_any():
        pending += decoder.decode(chunk)
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                result = await consume()
                if result is not None:
                    return result
            elif line.startswith("data:"):
                lines.append(line[5:].lstrip(" "))
    pending += decoder.decode(b"", final=True)
    if pending.startswith("data:"):
        lines.append(pending[5:].lstrip(" ").rstrip("\r"))
    result = await consume()
    if result is not None:
        return result
    raise APIError(
        "图片流中断，结果未知，已停止自动重试。",
        error_type="outcome_unknown",
        retryable=False,
    )


def _gpt_request_options(
    config: ApiRequestConfig, settings: dict[str, Any]
) -> dict[str, Any]:
    model = config.model or "gpt-image-2.5-flare"
    options = gpt_image_output_options(
        model,
        {
            **settings,
            "quality": config.quality or settings.get("quality"),
            # config 的 output_format 字段可能不存在，缺省回落候选配置
            "output_format": getattr(config, "output_format", None)
            or settings.get("output_format"),
        },
    )
    complexity = settings.get("complexity")
    if complexity not in (None, ""):
        if complexity not in ("low", "medium", "high"):
            raise APIError(
                f"complexity 仅支持 low/medium/high，当前值 {complexity!r}。",
                error_type="invalid_parameter",
                retryable=False,
            )
        options["complexity"] = complexity
    count = settings.get("n", config.effective_image_count)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10:
        raise APIError(
            "GPT Image 的 n 必须为 1–10。",
            error_type="invalid_parameter",
            retryable=False,
        )
    options["n"] = count
    output_format = options.get("output_format")
    if output_format in {"gif", "svg"}:
        if settings.get("stream", False):
            raise APIError(
                f"{output_format.upper()} 输出不支持流式生成。",
                error_type="invalid_parameter",
                retryable=False,
            )
        partial = settings.get("partial_images", 0)
        if isinstance(partial, int) and not isinstance(partial, bool) and partial > 0:
            raise APIError(
                f"{output_format.upper()} 输出不支持 partial_images 生成预览。",
                error_type="invalid_parameter",
                retryable=False,
            )
    if output_format == "svg" and count != 1:
        raise APIError(
            "SVG 输出仅支持单张图片（n=1）。",
            error_type="invalid_parameter",
            retryable=False,
        )
    if output_format == "gif":
        frame_count = settings.get("frame_count")
        if frame_count not in (None, 0, "") or isinstance(frame_count, bool):
            if (
                isinstance(frame_count, bool)
                or not isinstance(frame_count, int)
                or not 2 <= frame_count <= 16
            ):
                raise APIError(
                    "frame_count 必须是 2–16 的整数。",
                    error_type="invalid_parameter",
                    retryable=False,
                )
            options["frame_count"] = frame_count
        frame_duration_ms = settings.get("frame_duration_ms")
        if frame_duration_ms not in (None, 0, "") or isinstance(
            frame_duration_ms, bool
        ):
            if (
                isinstance(frame_duration_ms, bool)
                or not isinstance(frame_duration_ms, int)
                or frame_duration_ms < 20
            ):
                raise APIError(
                    "frame_duration_ms 必须是不小于 20 的整数。",
                    error_type="invalid_parameter",
                    retryable=False,
                )
            options["frame_duration_ms"] = frame_duration_ms
    elif output_format == "svg":
        svg_model = str(settings.get("svg_model") or "").strip()
        if svg_model:
            options["svg"] = {"model": svg_model}
    if settings.get("stream", False):
        if count != 1:
            raise APIError(
                "流式图片请求每次生成一张，多图请使用插件批量调度。",
                error_type="invalid_parameter",
                retryable=False,
            )
        partial = settings.get("partial_images", 0)
        if (
            isinstance(partial, bool)
            or not isinstance(partial, int)
            or not 0 <= partial <= 3
        ):
            raise APIError(
                "partial_images 必须为 0–3。",
                error_type="invalid_parameter",
                retryable=False,
            )
        options.update(stream=True, partial_images=partial)
    return options


class OpenAIImagesProvider:
    """OpenAI /v1/images/generations + /v1/images/edits 端点实现"""

    name = "openai_images"

    async def build_request(
        self, *, client: Any, config: ApiRequestConfig
    ) -> ProviderRequest:  # noqa: ANN401
        settings: dict[str, Any] = (
            getattr(config, "provider_settings", None)
            or getattr(client, "openai_images_settings", None)
            or {}
        )
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
        request_config: ApiRequestConfig | None = None,
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
        resp_output_format = (
            response_data.get("output_format")
            or (request_config.output_format if request_config else None)
            or (
                (request_config.provider_settings or {}).get("output_format")
                if request_config
                else ""
            )
            or ""
        )
        save_ext = (
            resp_output_format
            if resp_output_format in {"png", "jpeg", "webp", "gif", "svg"}
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
            if image_item.get("url"):
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
                            image_urls.append(image_path)
                            image_paths.append(image_path)
                        continue
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
                    else:
                        raise APIError(
                            "图片已生成但保存失败，已停止自动重试。",
                            http_status,
                            "outcome_unknown",
                            retryable=False,
                        )

            # 记录修订后的提示词
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
            http_status,
            "no_image",
            error_obj.get("code") if isinstance(error_obj, dict) else None,
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
        model = config.model or "gpt-image-2.5-flare"
        payload: dict[str, Any] = {
            "model": model,
            "prompt": config.prompt,
        }

        # ---- size ----
        size_value = _resolve_size_value(
            model,
            config.resolution,
            settings,
            suppress_resolution=config.suppress_resolution,
            aspect_ratio=config.aspect_ratio,
        )
        if size_value:
            payload["size"] = size_value

        # ---- GPT Image 协议参数（非 GPT 名统一按 GPT 行为发送，由端点自行报错）----
        payload.update(_gpt_request_options(config, settings))

        logger.debug(
            f"[openai_images] generations payload: model={model} size={payload.get('size')} "
            f"quality={payload.get('quality')} prompt_len={len(config.prompt)}"
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
        model = config.model or "gpt-image-2.5-flare"
        form = aiohttp.FormData()

        # ---- image (required): 第一张参考图 ----
        limit = min(max(int(settings.get("max_reference_images", 6)), 1), 16)
        ref_images = (config.reference_images or [])[:limit]
        if not ref_images:
            raise APIError(
                "/v1/images/edits 需要至少一张参考图",
                None,
                "missing_image",
                retryable=False,
            )

        image_data = await load_reference_bytes(
            client, config, ref_images[0], log_prefix="[openai_images]"
        )
        if image_data is None:
            raise APIError(
                "无法解码参考图为二进制数据",
                None,
                "invalid_image",
                retryable=False,
            )
        form.add_field(
            "image[]",
            image_data,
            filename="image." + self._image_format(image_data),
            content_type="image/" + self._image_format(image_data),
        )

        # ---- 多图支持 ----
        if len(ref_images) > 1:
            for idx, extra_ref in enumerate(ref_images[1:], start=2):
                extra_data = await load_reference_bytes(
                    client, config, extra_ref, log_prefix="[openai_images]"
                )
                if extra_data:
                    form.add_field(
                        "image[]",
                        extra_data,
                        filename=f"image_{idx}." + self._image_format(extra_data),
                        content_type="image/" + self._image_format(extra_data),
                    )

        # ---- prompt ----
        form.add_field("prompt", config.prompt)

        # ---- model ----
        form.add_field("model", model)

        # ---- size ----
        ref_dims: tuple[int, int] | None = None
        if not config.suppress_resolution and not config.resolution:
            ref_dims = self._probe_image_dims(image_data)
        size_value = _resolve_size_value(
            model,
            config.resolution,
            settings,
            ref_image_dims=ref_dims,
            suppress_resolution=config.suppress_resolution,
            aspect_ratio=config.aspect_ratio,
        )
        if size_value:
            form.add_field("size", size_value)

        for key, value in _gpt_request_options(config, settings).items():
            if isinstance(value, dict):
                # svg 等对象参数序列化为 JSON 字符串进表单
                form.add_field(key, json.dumps(value, ensure_ascii=False))
            else:
                form.add_field(
                    key, str(value).lower() if isinstance(value, bool) else str(value)
                )

        # ---- input_fidelity（仅编辑接口，按模型门控）----
        input_fidelity = str(settings.get("input_fidelity") or "").strip()
        if input_fidelity:
            model_lower = model.lower()
            if model_lower.startswith("gpt-image-1.5"):
                allowed_fidelity = ("low", "high")
            elif model_lower.startswith("gpt-image-1-mini"):
                allowed_fidelity = ("low",)
            else:
                raise APIError(
                    f"{model} 不支持 input_fidelity 参数，请清空该候选的编辑保真度配置。",
                    error_type="invalid_parameter",
                    retryable=False,
                )
            if input_fidelity not in allowed_fidelity:
                raise APIError(
                    f"{model} 的 input_fidelity 仅支持 {'/'.join(allowed_fidelity)}。",
                    error_type="invalid_parameter",
                    retryable=False,
                )
            form.add_field("input_fidelity", input_fidelity)

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
    def _image_format(data: bytes) -> str:
        if data.startswith(b"\xff\xd8\xff"):
            return "jpeg"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "webp"
        return "png"

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
