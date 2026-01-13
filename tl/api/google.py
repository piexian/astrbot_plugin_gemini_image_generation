"""Google/Gemini 官方接口供应商实现。"""

from __future__ import annotations

import asyncio
import base64
import tempfile
import time
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from ..tl_utils import save_base64_image
from .base import ProviderRequest


class GoogleProvider:
    name = "google"

    async def build_request(
        self, *, client: Any, config: ApiRequestConfig
    ) -> ProviderRequest:  # noqa: ANN401
        api_base = (config.api_base or "").rstrip("/")
        default_base = getattr(
            client,
            "GOOGLE_API_BASE",
            "https://generativelanguage.googleapis.com/v1beta",
        )

        if api_base:
            base = api_base
            logger.debug(f"使用自定义 API Base: {base}")
        else:
            base = default_base
            logger.debug(f"使用默认 API Base (google): {base}")

        # Google API 需要版本前缀
        if not config.api_base or base == default_base:
            url = f"{base}/models/{config.model}:generateContent"
        elif not any(base.endswith(suffix) for suffix in ["/v1beta", "/v1"]):
            url = f"{base}/v1beta/models/{config.model}:generateContent"
            logger.debug("为Google API自动添加v1beta前缀")
        else:
            url = f"{base}/models/{config.model}:generateContent"
            logger.debug("使用已包含版本前缀的Google API地址")

        payload = await self._prepare_payload(client=client, config=config)
        headers = {
            "x-goog-api-key": config.api_key or "",
            "Content-Type": "application/json",
        }
        logger.debug(f"智能构建API URL: {url}")
        return ProviderRequest(url=url, headers=headers, payload=payload)

    async def parse_response(
        self,
        *,
        client: Any,
        response_data: dict[str, Any],
        session: aiohttp.ClientSession,
        api_base: str | None = None,
    ) -> tuple[list[str], list[str], str | None, str | None]:  # noqa: ANN401
        # 解析逻辑在本文件内实现，但会复用 client 上的通用能力。
        return await self._parse_gresponse(
            client=client, response_data=response_data, session=session
        )

    async def _prepare_payload(
        self, *, client: Any, config: ApiRequestConfig
    ) -> dict[str, Any]:  # noqa: ANN401
        logger.debug(
            "[google] 构建 payload: model=%s refs=%s force_b64=%s aspect=%s res=%s",
            config.model,
            len(config.reference_images or []),
            config.image_input_mode,
            config.aspect_ratio,
            config.resolution,
        )
        parts: list[dict[str, Any]] = [{"text": config.prompt}]

        added_refs = 0
        fail_reasons: list[str] = []
        total_ref_count = len(config.reference_images or [])
        # 实际处理的参考图数量受 [:14] 限制
        processed_ref_count = min(total_ref_count, 14)
        total_start = time.perf_counter()
        if total_ref_count > 0:
            if total_ref_count > processed_ref_count:
                logger.info(
                    f"📎 开始处理 {processed_ref_count} 张参考图片 (共配置 {total_ref_count} 张，最多处理 14 张)..."
                )
            else:
                logger.info(f"📎 开始处理 {processed_ref_count} 张参考图片...")

        if config.reference_images:
            for idx, image_input in enumerate(config.reference_images[:14]):
                image_str = str(image_input).strip()
                logger.debug(
                    "[google] 处理参考图 idx=%s type=%s preview=%s",
                    idx,
                    type(image_input),
                    image_str[:120],
                )

                mime_type, data, is_url = await client._process_reference_image(
                    image_input, idx, config.image_input_mode
                )

                if not data:
                    if is_url:
                        parts.append({"fileData": {"fileUri": image_str}})
                        added_refs += 1
                        logger.info(
                            "[google] URL 下载失败，改用 fileData 传输 idx=%s url=%s",
                            idx,
                            image_str[:80],
                        )
                        continue

                    data = image_str
                    mime_type = client._ensure_mime_type(mime_type)
                    logger.debug(
                        "[google] 转换失败，直接透传原始数据 idx=%s preview=%s",
                        idx,
                        image_str[:80],
                    )

                validated_data, is_valid = client._validate_b64_with_fallback(
                    data, context="google-inline"
                )

                if not is_valid and is_url:
                    parts.append({"fileData": {"fileUri": image_str}})
                    added_refs += 1
                    logger.info(
                        "[google] base64 校验失败，改用 fileData 传输 idx=%s url=%s",
                        idx,
                        image_str[:80],
                    )
                    continue

                if not is_valid:
                    fail_reasons.append(f"图片{idx + 1}: base64校验失败")
                    logger.debug(
                        "[google] 参考图 idx=%s base64 校验失败且非URL，跳过",
                        idx,
                    )
                    continue

                mime_type = client._ensure_mime_type(mime_type)
                size_kb = len(validated_data) // 1024 if validated_data else 0
                logger.info(
                    f"📎 图片 {idx + 1}/{processed_ref_count} 已加入发送请求 ({mime_type}, {size_kb}KB)"
                )
                logger.debug(
                    "[google] 成功处理参考图 idx=%s mime=%s size=%s",
                    idx,
                    mime_type,
                    len(validated_data) if validated_data else 0,
                )

                parts.append(
                    {"inlineData": {"mimeType": mime_type, "data": validated_data}}
                )
                added_refs += 1

        # 输出最终统计
        if processed_ref_count > 0:
            total_elapsed_ms = (time.perf_counter() - total_start) * 1000
            if added_refs > 0:
                logger.info(
                    f"📎 参考图片处理完成：{added_refs}/{processed_ref_count} 张已成功加入发送请求，耗时 {total_elapsed_ms:.0f}ms"
                )
            else:
                logger.info(
                    f"📎 参考图片处理完成：0/{processed_ref_count} 张成功，全部未能加入发送请求，耗时 {total_elapsed_ms:.0f}ms"
                )

        if config.reference_images and added_refs == 0:
            raise APIError(
                "参考图全部无效或下载失败，请重新发送图片后重试。"
                + (f" 详情: {'; '.join(fail_reasons[:3])}" if fail_reasons else ""),
                None,
                "invalid_reference_image",
            )

        contents = [{"role": "user", "parts": parts}]

        generation_config: dict[str, Any] = {"responseModalities": ["TEXT", "IMAGE"]}

        modalities_map = {
            "TEXT": ["TEXT"],
            "IMAGE": ["IMAGE"],
            "TEXT_IMAGE": ["TEXT", "IMAGE"],
        }

        modalities = modalities_map.get(config.response_modalities, ["TEXT", "IMAGE"])

        if "IMAGE" not in modalities:
            logger.warning("配置中缺少 IMAGE modality，自动添加以支持图像生成")
            modalities.append("IMAGE")

        if "TEXT" not in modalities:
            logger.debug("添加 TEXT modality 以提供更好的兼容性")
            modalities.append("TEXT")

        generation_config["responseModalities"] = modalities
        logger.debug(f"响应模态: {modalities}")

        image_config: dict[str, Any] = {}

        _res_key = (config.resolution_param_name or "").strip()
        resolution_key = _res_key if _res_key else "image_size"
        _aspect_key = (config.aspect_ratio_param_name or "").strip()
        aspect_ratio_key = _aspect_key if _aspect_key else "aspect_ratio"

        if config.resolution:
            resolution = config.resolution.upper()

            if resolution in ["1K", "1024X1024"]:
                image_config[resolution_key] = "1K"
                logger.debug(f"设置图像尺寸: 1K (参数名: {resolution_key})")
            elif resolution in ["2K", "2048X2048"]:
                image_config[resolution_key] = "2K"
                logger.debug(f"设置图像尺寸: 2K (参数名: {resolution_key})")
            elif resolution in ["4K", "4096X4096"]:
                image_config[resolution_key] = "4K"
                logger.debug(f"设置图像尺寸: 4K (参数名: {resolution_key})")
            else:
                image_config[resolution_key] = config.resolution
                logger.debug(
                    f"设置图像尺寸: {config.resolution} (参数名: {resolution_key})"
                )

        if config.aspect_ratio:
            ratio = config.aspect_ratio.strip()
            image_config[aspect_ratio_key] = ratio
            logger.debug(f"设置长宽比: {ratio} (参数名: {aspect_ratio_key})")

        if image_config:
            generation_config["image_config"] = image_config

        if config.temperature is not None:
            generation_config["temperature"] = config.temperature
        if config.seed is not None:
            generation_config["seed"] = config.seed
        if config.safety_settings:
            generation_config["safetySettings"] = config.safety_settings

        tools: list[dict[str, Any]] = []
        if config.enable_grounding:
            tools.append({"google_search": {}})

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config,
        }

        if tools:
            payload["tools"] = tools

        if "image_config" in generation_config:
            logger.debug(
                f"实际发送的 image_config: {generation_config['image_config']}"
            )

        return payload

    async def _parse_gresponse(
        self,
        *,
        client: Any,
        response_data: dict[str, Any],
        session: aiohttp.ClientSession,
    ) -> tuple[list[str], list[str], str | None, str | None]:  # noqa: ANN401
        parse_start = asyncio.get_running_loop().time()
        logger.debug("🔍 开始解析API响应数据...")

        image_urls: list[str] = []
        image_paths: list[str] = []
        text_chunks: list[str] = []
        thought_signature = None
        fallback_texts = client._collect_fallback_texts(response_data)

        if "candidates" not in response_data or not response_data["candidates"]:
            logger.warning(
                "Google 响应缺少 candidates 字段，尝试从 fallback 文本提取图像"
            )
            appended = False
            if fallback_texts:
                appended = await client._append_images_from_texts(
                    fallback_texts, image_urls, image_paths
                )
            if appended and (image_urls or image_paths):
                text_content = (
                    " ".join(t.strip() for t in fallback_texts if t and t.strip())
                    or None
                )
                return image_urls, image_paths, text_content, thought_signature

            if "promptFeedback" in response_data:
                feedback = response_data["promptFeedback"]
                logger.warning(f"请求被阻止: {feedback}")
            else:
                logger.error("响应中没有 candidates，fallback 提取也失败")
                logger.debug(f"完整响应: {str(response_data)[:1000]}")
                logger.debug(f"fallback_texts: {fallback_texts}")
            return [], [], None, None

        candidates = response_data["candidates"]
        logger.debug(f"📝 找到 {len(candidates)} 个候选结果")

        for idx, candidate in enumerate(candidates):
            finish_reason = candidate.get("finishReason")
            if finish_reason in ["SAFETY", "RECITATION"]:
                logger.warning(f"候选 {idx} 生成被阻止: {finish_reason}")
                continue

            content = candidate.get("content", {})
            parts = content.get("parts") or []
            logger.debug(f"📋 候选 {idx} 包含 {len(parts)} 个部分")

            for i, part in enumerate(parts):
                try:
                    logger.debug(f"检查候选 {idx} 的第 {i} 个part: {list(part.keys())}")

                    if "thoughtSignature" in part and not thought_signature:
                        thought_signature = part["thoughtSignature"]
                        logger.debug(f"🧠 找到思维签名: {thought_signature[:50]}...")

                    if "text" in part and isinstance(part.get("text"), str):
                        text_chunks.append(part.get("text", ""))

                    inline_data = part.get("inlineData") or part.get("inline_data")
                    if inline_data and not part.get("thought", False):
                        mime_type = (
                            inline_data.get("mimeType")
                            or inline_data.get("mime_type")
                            or "image/png"
                        )
                        base64_data = inline_data.get("data", "")

                        logger.debug(
                            f"🎯 找到图像数据 (候选{idx} 第{i + 1}部分): {mime_type}, 大小: {len(base64_data)} 字符"
                        )

                        if base64_data:
                            image_format = (
                                mime_type.split("/")[1] if "/" in mime_type else "png"
                            )

                            logger.debug("💾 开始保存图像文件...")
                            save_start = asyncio.get_running_loop().time()

                            saved_path = await save_base64_image(
                                base64_data, image_format
                            )

                            save_end = asyncio.get_running_loop().time()
                            logger.debug(
                                f"✅ 图像保存完成，耗时: {save_end - save_start:.2f}秒"
                            )

                            if saved_path:
                                image_paths.append(saved_path)
                                image_urls.append(saved_path)
                            else:
                                try:
                                    with tempfile.NamedTemporaryFile(
                                        prefix="gem_inline_",
                                        suffix=f".{image_format}",
                                        delete=False,
                                    ) as tmp_file:
                                        tmp_path = Path(tmp_file.name)
                                        cleaned = base64_data.strip().replace("\n", "")
                                        if ";base64," in cleaned:
                                            _, _, cleaned = cleaned.partition(
                                                ";base64,"
                                            )
                                        raw = base64.b64decode(cleaned, validate=False)
                                        tmp_file.write(raw)
                                    image_paths.append(str(tmp_path))
                                    image_urls.append(str(tmp_path))
                                    logger.debug(
                                        "⚠️ save_base64_image 失败，已使用宽松解码写入临时文件: %s",
                                        tmp_path,
                                    )
                                except Exception as e:
                                    logger.warning(
                                        "候选 %s 第 %s 部分 inlineData 解码失败，跳过：%s",
                                        idx,
                                        i + 1,
                                        e,
                                    )
                        else:
                            logger.warning(
                                f"候选 {idx} 的第 {i} 个part有inlineData但data为空"
                            )
                    elif "thought" in part and part.get("thought", False):
                        logger.debug(f"候选 {idx} 的第 {i} 个part是思考内容")
                    else:
                        logger.debug(
                            f"候选 {idx} 的第 {i} 个part不是图像也不是思考: {list(part.keys())}"
                        )
                except Exception as e:
                    logger.error(
                        f"处理候选 {idx} 的第 {i} 个part时出错: {e}", exc_info=True
                    )

        logger.debug(f"🖼️ 共找到 {len(image_paths)} 张图片")

        if text_chunks:
            extracted_urls: list[str] = []
            extracted_paths: list[str] = []
            for chunk in text_chunks:
                extracted_urls.extend(client._find_image_urls_in_text(chunk))
                urls2, paths2 = await client._extract_from_content(chunk)
                extracted_urls.extend(urls2)
                extracted_paths.extend(paths2)

            if extracted_urls or extracted_paths:
                image_urls.extend(extracted_urls)
                image_paths.extend(extracted_paths)

        text_content = (
            " ".join(chunk for chunk in text_chunks if chunk).strip()
            if text_chunks
            else None
        )
        if text_content:
            logger.debug(f"🎯 找到文本内容: {text_content[:100]}...")

        if not (image_paths or image_urls) and fallback_texts:
            appended = await client._append_images_from_texts(
                fallback_texts, image_urls, image_paths
            )
            if appended and not text_content:
                text_content = (
                    " ".join(t.strip() for t in fallback_texts if t and t.strip())
                    or text_content
                )

        if image_paths or image_urls:
            parse_end = asyncio.get_running_loop().time()
            logger.debug(f"🎉 API响应解析完成，总耗时: {parse_end - parse_start:.2f}秒")
            return image_urls, image_paths, text_content, thought_signature

        if text_content:
            logger.warning("API只返回了文本响应，未生成图像，将触发重试")
            logger.debug(f"Google响应内容: {str(response_data)[:1000]}")
            raise APIError(
                f"图像生成失败：API只返回了文本响应，正在重试... | 响应预览: {str(response_data)[:300]}",
                500,
                "no_image_retry",
            )

        logger.warning(f"未在响应中找到图像数据，响应内容: {str(response_data)[:500]}")
        raise APIError(
            f"图像生成失败：响应格式异常，未找到有效的图像数据 | 响应: {str(response_data)[:300]}",
            None,
            "invalid_response",
        )
