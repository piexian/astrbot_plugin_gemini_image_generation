"""Gemini Interactions API（Nano Banana 系列）官方接口供应商。

与 `google.py`（legacy generateContent）互不影响，供官方 API 用户选择新端点。
"""

from __future__ import annotations

import base64
import time
from typing import Any

import aiohttp
from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from ..tl_utils import get_temp_dir, save_base64_image
from .base import ProviderRequest
from .provider_limits import MAX_REFERENCE_IMAGES_GEMINI_INTERACTIONS
from .reference_intake import announce_reference_intake

_DEFAULT_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
_DEFAULT_MODEL = "gemini-3.1-flash-image"
_THINKING_LEVELS = {"minimal", "high"}
# 极端比例仅 3.1 Flash Image 支持（官方比例表）
_EXTREME_RATIOS = {"1:4", "1:8", "4:1", "8:1"}


class GeminiInteractionsProvider:
    name = "gemini_interactions"

    async def build_request(
        self, *, client: Any, config: ApiRequestConfig
    ) -> ProviderRequest:  # noqa: ANN401
        if not config.api_key:
            raise APIError(
                "gemini_interactions 缺少 API Key，请在 provider_overrides."
                "gemini_interactions.api_keys 中配置",
                None,
                "missing_api_key",
                retryable=False,
            )
        prompt = (config.prompt or "").strip()
        if not prompt:
            raise APIError(
                "gemini_interactions 需要非空 prompt",
                None,
                "empty_prompt",
                retryable=False,
            )

        url = self._resolve_url(client, config)
        model = self._resolve_model(config)
        resolution, aspect_ratio = self._normalize_params(model, config)

        payload: dict[str, Any] = {
            "model": model,
            "input": await self._build_input(client=client, config=config),
            # 单轮生图无需服务端会话存储，关闭以避免请求内容被留存
            "store": False,
            "response_format": self._build_response_format(
                config, resolution, aspect_ratio
            ),
        }

        tools = self._build_tools(model, config)
        if tools:
            payload["tools"] = tools

        generation_config = self._build_generation_config(model, config)
        if generation_config:
            payload["generation_config"] = generation_config

        if config.safety_settings:
            logger.warning(
                "[gemini_interactions] Interactions API 暂不支持自定义 "
                "safety settings，已忽略"
            )

        headers = {
            "x-goog-api-key": config.api_key or "",
            "Content-Type": "application/json",
        }
        logger.debug(
            "[gemini_interactions] build_request: url=%s model=%s res=%s "
            "ratio=%s refs=%d",
            url,
            model,
            resolution,
            aspect_ratio,
            len(config.reference_images or []),
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
        request_config: ApiRequestConfig | None = None,
        is_retry: bool = False,
    ) -> tuple[list[str], list[str], str | None, str | None]:  # noqa: ANN401
        self._raise_for_error(response_data, http_status)

        image_urls: list[str] = []
        image_paths: list[str] = []
        text_chunks: list[str] = []
        last_thought_image: tuple[str, str, bool] | None = None

        for step in response_data.get("steps") or []:
            if not isinstance(step, dict):
                continue
            step_type = step.get("type")
            blocks = (
                step.get("content")
                if step_type == "model_output"
                else step.get("summary")
                if step_type == "thought"
                else None
            )
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                # 仅 model_output 文本对用户可见，thought 思维链文本一律丢弃
                if (
                    step_type == "model_output"
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                ):
                    text_chunks.append(block["text"])
                    continue
                if block.get("type") != "image":
                    continue
                mime_type = block.get("mime_type") or "image/png"
                data = block.get("data")
                uri = block.get("uri")
                if step_type == "thought":
                    # 中间帧不交付，只记录最后一帧（即最终渲染结果）
                    if isinstance(data, str) and data:
                        last_thought_image = (mime_type, data, False)
                    elif isinstance(uri, str) and uri:
                        last_thought_image = (mime_type, uri, True)
                    continue
                if isinstance(data, str) and data:
                    await self._append_saved_image(
                        mime_type, data, image_urls, image_paths
                    )
                elif isinstance(uri, str) and uri:
                    await self._append_remote_image(
                        client,
                        uri,
                        session,
                        request_config,
                        image_urls,
                        image_paths,
                    )

        # thought 兜底：仅取最后一帧，避免把中间渲染发给用户
        if not image_urls and last_thought_image:
            logger.debug(
                "[gemini_interactions] 无 model_output 图像，取最后一帧 thought 图"
            )
            mime_type, payload, is_uri = last_thought_image
            if is_uri:
                await self._append_remote_image(
                    client,
                    payload,
                    session,
                    request_config,
                    image_urls,
                    image_paths,
                )
            else:
                await self._append_saved_image(
                    mime_type, payload, image_urls, image_paths
                )

        text_content = (
            " ".join(chunk for chunk in text_chunks if chunk).strip()
            if text_chunks
            else None
        )

        if not (image_urls or image_paths) and text_content:
            extracted_urls, extracted_paths = await self._extract_from_text(
                client, text_content, session, request_config
            )
            image_urls.extend(extracted_urls)
            image_paths.extend(extracted_paths)

        if image_urls or image_paths:
            return image_urls, image_paths, text_content or None, None

        # 原始响应可能含思维链内容，只写日志，不进用户可见错误消息
        logger.debug(
            "[gemini_interactions] 未生成图像的响应: %s", str(response_data)[:1000]
        )
        if text_content:
            logger.warning(
                "[gemini_interactions] API 只返回文本，未生成图像，将触发重试"
            )
            raise APIError(
                "图像生成失败：API只返回了文本响应，正在重试...",
                500,
                "no_image_retry",
            )

        raise APIError(
            "图像生成失败：响应格式异常，未找到有效的图像数据",
            http_status,
            "invalid_response",
        )

    def _resolve_url(self, client: Any, config: ApiRequestConfig) -> str:
        api_base = (config.api_base or "").rstrip("/")
        if not api_base:
            return f"{_DEFAULT_API_BASE}/interactions"
        if api_base.endswith(("/v1beta", "/v1")):
            return f"{api_base}/interactions"
        logger.debug("[gemini_interactions] 为自定义 API Base 自动补 /v1beta 前缀")
        return f"{api_base}/v1beta/interactions"

    def _resolve_model(self, config: ApiRequestConfig) -> str:
        settings = config.provider_settings or {}
        model = str(settings.get("model") or config.model or _DEFAULT_MODEL).strip()
        return model or _DEFAULT_MODEL

    def _normalize_params(
        self, model: str, config: ApiRequestConfig
    ) -> tuple[str | None, str | None]:
        """按模型分层钳制分辨率与比例：lite 仅 1K，极端比例仅 3.1 Flash。"""
        model_lc = model.lower()
        resolution = (config.resolution or "").strip().upper() or None
        aspect_ratio = (config.aspect_ratio or "").strip() or None

        if (
            resolution
            and resolution not in ("1K", "1024X1024")
            and "flash-lite-image" in model_lc
        ):
            logger.warning(
                "[gemini_interactions] %s 仅支持 1K 分辨率，%s 已降级为 1K",
                model,
                resolution,
            )
            resolution = "1K"

        if aspect_ratio in _EXTREME_RATIOS and "3.1-flash-image" not in model_lc:
            logger.warning(
                "[gemini_interactions] 极端比例 %s 仅 3.1 Flash Image 支持，"
                "%s 已忽略该比例",
                aspect_ratio,
                model,
            )
            aspect_ratio = None

        return resolution, aspect_ratio

    async def _build_input(
        self, *, client: Any, config: ApiRequestConfig
    ) -> list[dict[str, Any]]:  # noqa: ANN401
        blocks: list[dict[str, Any]] = [{"type": "text", "text": config.prompt}]
        refs = config.reference_images or []
        added = 0
        fail_reasons: list[str] = []
        _, processed = announce_reference_intake(
            refs,
            MAX_REFERENCE_IMAGES_GEMINI_INTERACTIONS,
            log_prefix="[gemini_interactions] ",
        )

        for idx, image_input in enumerate(
            refs[:MAX_REFERENCE_IMAGES_GEMINI_INTERACTIONS]
        ):
            image_str = str(image_input).strip()
            try:
                mime_type, data, is_url = await client._process_reference_image(
                    image_input, idx, config.image_input_mode
                )
            except Exception as e:  # noqa: BLE001
                fail_reasons.append(f"图片{idx + 1}: {e}")
                logger.warning(
                    "[gemini_interactions] 参考图 idx=%s 处理失败: %s", idx, e
                )
                continue

            if not data:
                if is_url:
                    # 无 data 时按 Interactions 内容块 schema 用 uri 引用
                    blocks.append({"type": "image", "uri": image_str})
                    added += 1
                    continue
                data = image_str
                mime_type = client._ensure_mime_type(mime_type)

            validated_data, is_valid = client._validate_b64_with_fallback(
                data, context="gemini_interactions-inline"
            )
            if not is_valid and is_url:
                blocks.append({"type": "image", "uri": image_str})
                added += 1
                continue
            if not is_valid:
                fail_reasons.append(f"图片{idx + 1}: base64校验失败")
                continue

            blocks.append(
                {
                    "type": "image",
                    "mime_type": client._ensure_mime_type(mime_type),
                    "data": validated_data,
                }
            )
            added += 1

        if processed > 0:
            logger.info(
                "📎 [gemini_interactions] 参考图处理完成 %d/%d 张加入请求",
                added,
                processed,
            )
        if refs and added == 0:
            raise APIError(
                "参考图全部无效或下载失败，请重新发送图片后重试。"
                + (f" 详情: {'; '.join(fail_reasons[:3])}" if fail_reasons else ""),
                None,
                "invalid_reference_image",
            )
        return blocks

    def _build_response_format(
        self, config: ApiRequestConfig, resolution: str | None, aspect_ratio: str | None
    ) -> dict[str, Any] | list[dict[str, Any]]:
        image_format: dict[str, Any] = {"type": "image"}
        if resolution:
            image_format["image_size"] = resolution
        if aspect_ratio:
            image_format["aspect_ratio"] = aspect_ratio
        if config.response_modalities == "TEXT_IMAGE":
            # 官方约定：数组形式表示同时返回文本与图像
            return [{"type": "text"}, image_format]
        return image_format

    def _build_tools(
        self, model: str, config: ApiRequestConfig
    ) -> list[dict[str, Any]]:
        if not config.enable_grounding:
            return []
        model_lc = model.lower()
        if "flash-lite-image" in model_lc:
            logger.warning(
                "[gemini_interactions] %s 不支持 Google 搜索接地，已忽略", model
            )
            return []
        settings = config.provider_settings or {}
        tool: dict[str, Any] = {"type": "google_search"}
        if settings.get("image_search"):
            if "3.1-flash-image" in model_lc:
                tool["search_types"] = ["web_search", "image_search"]
            else:
                logger.warning(
                    "[gemini_interactions] image_search 仅 gemini-3.1-flash-image "
                    "支持，%s 已忽略",
                    model,
                )
        return [tool]

    def _build_generation_config(
        self, model: str, config: ApiRequestConfig
    ) -> dict[str, Any]:
        settings = config.provider_settings or {}
        generation_config: dict[str, Any] = {}
        thinking_level = str(settings.get("thinking_level") or "").strip().lower()
        if thinking_level:
            if "3.1-flash-image" not in model.lower():
                logger.warning(
                    "[gemini_interactions] thinking_level 仅 gemini-3.1-flash-image "
                    "支持，%s 已忽略",
                    model,
                )
            elif thinking_level in _THINKING_LEVELS:
                generation_config["thinking_level"] = thinking_level
            else:
                logger.warning(
                    "[gemini_interactions] thinking_level 仅支持 minimal/high，"
                    "已忽略 %s",
                    thinking_level,
                )
        if config.temperature is not None:
            generation_config["temperature"] = config.temperature
        if config.seed is not None:
            generation_config["seed"] = config.seed
        return generation_config

    def _raise_for_error(
        self, response_data: dict[str, Any], http_status: int | None
    ) -> None:
        error = response_data.get("error")
        if isinstance(error, dict) and error.get("message"):
            status = error.get("code") or http_status
            raise APIError(
                f"Gemini Interactions API 错误: {error.get('message')}",
                status if isinstance(status, int) else http_status,
                "api_error",
                retryable=bool(
                    isinstance(status, int) and (status >= 500 or status == 429)
                ),
            )
        status_text = response_data.get("status")
        if status_text in ("failed", "cancelled", "budget_exceeded"):
            faults = response_data.get("faults") or []
            fault_message = ""
            if faults and isinstance(faults, list) and isinstance(faults[0], dict):
                fault_message = str(faults[0].get("message") or "")
            raise APIError(
                f"Gemini Interactions 交互状态为 {status_text}"
                + (f": {fault_message}" if fault_message else ""),
                http_status,
                status_text,
                retryable=False,
            )

    async def _append_saved_image(
        self,
        mime_type: str,
        base64_data: str,
        image_urls: list[str],
        image_paths: list[str],
    ) -> None:
        image_format = mime_type.split("/", 1)[1] if "/" in mime_type else "png"
        saved_path = await save_base64_image(base64_data, image_format)
        if saved_path:
            image_paths.append(saved_path)
            image_urls.append(saved_path)
            return
        try:
            # save_base64_image 依赖 PIL，格式异常时用宽松解码兜底落盘
            temp_dir = get_temp_dir()
            tmp_path = temp_dir / f"gi_inline_{int(time.time() * 1000)}.{image_format}"
            cleaned = base64_data.strip().replace("\n", "")
            if ";base64," in cleaned:
                cleaned = cleaned.partition(";base64,")[2]
            tmp_path.write_bytes(base64.b64decode(cleaned, validate=False))
            image_paths.append(str(tmp_path))
            image_urls.append(str(tmp_path))
        except Exception as e:  # noqa: BLE001
            logger.warning("[gemini_interactions] inline 图像解码失败，跳过: %s", e)

    async def _append_remote_image(
        self,
        client: Any,
        uri: str,
        session: aiohttp.ClientSession,
        request_config: ApiRequestConfig | None,
        image_urls: list[str],
        image_paths: list[str],
    ) -> None:
        """远程图像先落盘；失败时仅 image_urls 保留直链，不污染本地路径列表。"""
        path: str | None = None
        try:
            _, path = await client._download_image(
                uri,
                session,
                use_cache=False,
                proxy=client._request_http_proxy(request_config),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[gemini_interactions] 远程图像下载失败，回退直链: %s", e)
        if path:
            image_urls.append(path)
            image_paths.append(path)
        else:
            image_urls.append(uri)

    async def _extract_from_text(
        self,
        client: Any,
        text: str,
        session: aiohttp.ClientSession,
        request_config: ApiRequestConfig | None,
    ) -> tuple[list[str], list[str]]:
        """文本中兜底提取图像（第三方网关可能返回 Markdown 图链）。"""
        urls = client._find_image_urls_in_text(text)
        extracted_urls: list[str] = []
        extracted_paths: list[str] = []
        for url in urls:
            await self._append_remote_image(
                client, url, session, request_config, extracted_urls, extracted_paths
            )
        return extracted_urls, extracted_paths
