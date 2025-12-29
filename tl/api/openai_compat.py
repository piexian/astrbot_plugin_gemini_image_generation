"""OpenAI 兼容接口供应商实现。

用于各类“OpenAI API 兼容”的服务（例如反代、第三方兼容网关等）。
"""

from __future__ import annotations

import base64
import binascii
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from ..tl_utils import save_base64_image
from .base import ProviderRequest


class OpenAICompatProvider:
    name = "openai_compat"

    async def build_request(
        self, *, client: Any, config: ApiRequestConfig
    ) -> ProviderRequest:  # noqa: ANN401
        api_base = (config.api_base or "").rstrip("/")
        default_base = getattr(client, "OPENAI_API_BASE", "https://api.openai.com/v1")

        if api_base:
            base = api_base
            logger.debug(f"使用自定义 API Base: {base}")
        else:
            base = default_base
            logger.debug(f"使用默认 API Base ({config.api_type}): {base}")

        # OpenAI 兼容格式：自动补齐 /v1
        if not config.api_base or base == default_base:
            url = f"{base}/chat/completions"
        elif not any(base.endswith(suffix) for suffix in ["/v1", "/v1beta"]):
            url = f"{base}/v1/chat/completions"
            logger.debug("为OpenAI兼容API自动添加v1前缀")
        else:
            url = f"{base}/chat/completions"
            logger.debug("使用已包含版本前缀的OpenAI兼容API地址")

        payload = await self._prepare_payload(client=client, config=config)
        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/astrbot",
            "X-Title": "AstrBot Gemini Image Advanced",
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
        return await self._parse_openai_response(
            client=client,
            response_data=response_data,
            session=session,
            api_base=api_base,
        )

    async def _handle_special_candidate_url(
        self,
        *,
        client: Any,
        session: aiohttp.ClientSession,
        candidate_url: str,
        image_urls: list[str],
        image_paths: list[str],
        api_base: str | None,
        state: dict[str, Any],
    ) -> bool:  # noqa: ANN401
        """子类钩子：处理特殊图片 URL（如相对路径/临时缓存），返回是否已处理。"""
        return False

    def _find_additional_image_urls_in_text(self, text: str) -> list[str]:
        """子类钩子：从文本中额外提取图片链接（默认不提取）。"""
        return []

    async def _prepare_payload(
        self, *, client: Any, config: ApiRequestConfig
    ) -> dict[str, Any]:  # noqa: ANN401
        message_content: list[dict[str, Any]] = [
            {"type": "text", "text": f"Generate an image: {config.prompt}"}
        ]

        force_b64 = (
            str(getattr(config, "image_input_mode", "auto")).lower() == "force_base64"
        )

        supported_exts = {
            "jpg",
            "jpeg",
            "png",
            "webp",
            "gif",
            "bmp",
            "tif",
            "tiff",
            "heic",
            "avif",
        }

        if config.reference_images:
            processed_cache: dict[str, dict[str, Any]] = {}
            total_start = time.perf_counter()
            total_ref_count = len(config.reference_images)
            processed_ref_count = min(total_ref_count, 6)
            if total_ref_count > processed_ref_count:
                logger.info(
                    f"📎 开始处理 {processed_ref_count} 张参考图片 (共配置 {total_ref_count} 张，最多处理 6 张)..."
                )
            else:
                logger.info(f"📎 开始处理 {processed_ref_count} 张参考图片...")

            for idx, image_input in enumerate(config.reference_images[:6]):
                per_start = time.perf_counter()
                image_str = str(image_input).strip()
                if not image_str:
                    logger.warning(f"跳过空白参考图像: idx={idx}")
                    continue

                if "&amp;" in image_str:
                    image_str = image_str.replace("&amp;", "&")

                if image_str in processed_cache:
                    logger.debug(f"参考图像命中缓存: idx={idx}")
                    message_content.append(processed_cache[image_str])
                    continue

                parsed = urllib.parse.urlparse(image_str)
                image_payload: dict[str, Any] | None = None

                try:
                    if (
                        parsed.scheme in ("http", "https")
                        and parsed.netloc
                        and not force_b64
                    ):
                        ext = Path(parsed.path).suffix.lower().lstrip(".")
                        if ext and ext not in supported_exts:
                            logger.debug(
                                "参考图像URL扩展名不在常见列表: idx=%s ext=%s url=%s",
                                idx,
                                ext,
                                image_str[:80],
                            )

                        image_payload = {
                            "type": "image_url",
                            "image_url": {"url": image_str},
                        }
                        logger.info(f"📎 图片 {idx + 1}/{processed_ref_count} 已加入发送请求 (URL)")
                        logger.debug(
                            "OpenAI兼容API使用URL参考图: idx=%s ext=%s url=%s",
                            idx,
                            ext or "unknown",
                            image_str[:120],
                        )

                    elif (
                        image_str.startswith("data:image/") and ";base64," in image_str
                    ):
                        header, _, data_part = image_str.partition(";base64,")
                        mime_type = header.replace("data:", "").lower()
                        try:
                            base64.b64decode(data_part, validate=True)
                        except (binascii.Error, ValueError) as e:
                            logger.warning(
                                "跳过无效的 data URL 参考图: idx=%s 错误=%s", idx, e
                            )
                            mime_type = None

                        if mime_type:
                            ext = mime_type.split("/")[-1]
                            if ext and ext not in supported_exts:
                                logger.debug(
                                    "data URL 图片格式不常见: idx=%s mime=%s",
                                    idx,
                                    mime_type,
                                )
                            image_payload = {
                                "type": "image_url",
                                "image_url": {"url": image_str},
                            }
                            logger.info(f"📎 图片 {idx + 1}/{processed_ref_count} 已加入发送请求 (data URL)")
                            logger.debug(
                                "OpenAI兼容API使用data URL参考图: idx=%s mime=%s",
                                idx,
                                mime_type,
                            )

                    else:
                        mime_type, data = await client._normalize_image_input(
                            image_input, image_input_mode=config.image_input_mode
                        )
                        if not data:
                            if force_b64:
                                raise APIError(
                                    f"参考图转 base64 失败（force_base64），idx={idx}, type={type(image_input)}",
                                    None,
                                    "invalid_reference_image",
                                )
                            logger.warning(f"📎 图片 {idx + 1}/{processed_ref_count} 未能加入发送请求 - 无法转换")
                            logger.debug(
                                "跳过无法识别/读取的参考图像: idx=%s type=%s",
                                idx,
                                type(image_input),
                            )
                            continue

                        if not mime_type or not mime_type.startswith("image/"):
                            logger.debug(
                                "未检测到明确的图片 MIME，默认使用 image/png: idx=%s",
                                idx,
                            )
                            mime_type = "image/png"

                        ext = mime_type.split("/")[-1]
                        if ext and ext not in supported_exts:
                            logger.debug(
                                "规范化后图片格式不常见: idx=%s mime=%s",
                                idx,
                                mime_type,
                            )

                        if force_b64:
                            cleaned = data.strip().replace("\n", "")
                            try:
                                base64.b64decode(cleaned, validate=True)
                                b64_kb = len(cleaned) * 3 // 4 // 1024
                                logger.info(f"📎 图片 {idx + 1}/{processed_ref_count} 已加入发送请求 (base64, {b64_kb}KB)")
                            except Exception:
                                raise APIError(
                                    f"参考图 base64 校验失败（force_base64），来源: idx={idx}",
                                    None,
                                    "invalid_reference_image",
                                )
                            payload_url = f"data:{mime_type};base64,{cleaned}"
                        else:
                            payload_url = f"data:{mime_type};base64,{data}"

                        image_payload = {
                            "type": "image_url",
                            "image_url": {"url": payload_url},
                        }

                    if image_payload:
                        message_content.append(image_payload)
                        processed_cache[image_str] = image_payload
                        elapsed_ms = (time.perf_counter() - per_start) * 1000
                        logger.debug(
                            "参考图像处理完成: idx=%s 耗时=%.2fms 来源=%s",
                            idx,
                            elapsed_ms,
                            parsed.scheme or "normalized",
                        )

                except Exception as e:
                    logger.warning(f"📎 图片 {idx + 1}/{processed_ref_count} 未能加入发送请求 - {str(e)[:30]}")
                    logger.debug("处理参考图像时出现异常: idx=%s err=%s", idx, e)
                    continue

            total_elapsed_ms = (time.perf_counter() - total_start) * 1000
            success_count = len(processed_cache)
            if success_count > 0:
                logger.info(f"📎 参考图片处理完成：{success_count}/{processed_ref_count} 张已成功加入发送请求")
            else:
                # 参考图全部处理失败，抛出错误
                raise APIError(
                    "参考图全部处理失败，可能是网络问题或格式不支持。建议：1) 检查图片链接是否可访问；2) 尝试重新发送图片；3) 使用 Google API 格式可能有更好的错误提示。",
                    None,
                    "invalid_reference_image",
                )

        payload: dict[str, Any] = {
            "model": config.model,
            "messages": [{"role": "user", "content": message_content}],
            "max_tokens": config.max_tokens,
            "temperature": config.temperature
            if config.temperature is not None
            else 0.7,
            "modalities": ["image", "text"],
            "stream": False,
        }

        _res_key = (config.resolution_param_name or "").strip()
        resolution_key = _res_key if _res_key else "image_size"
        _aspect_key = (config.aspect_ratio_param_name or "").strip()
        aspect_ratio_key = _aspect_key if _aspect_key else "aspect_ratio"

        model_name = (config.model or "").lower()
        is_gemini_image_model = (
            "gemini-3-pro-image" in model_name
            or "gemini-3-pro-preview" in model_name
            or config.force_resolution
        )

        image_config: dict[str, Any] = {}

        if config.aspect_ratio:
            image_config[aspect_ratio_key] = config.aspect_ratio

        if is_gemini_image_model and config.resolution:
            image_config[resolution_key] = config.resolution

        if image_config:
            payload["image_config"] = image_config

        if is_gemini_image_model and config.enable_grounding:
            payload["tools"] = [{"google_search": {}}]

        return payload

    async def _parse_openai_response(
        self,
        *,
        client: Any,
        response_data: dict[str, Any],
        session: aiohttp.ClientSession,
        api_base: str | None = None,
    ) -> tuple[list[str], list[str], str | None, str | None]:  # noqa: ANN401
        image_urls: list[str] = []
        image_paths: list[str] = []
        text_content = None
        thought_signature = None
        fail_reasons: list[str] = []
        fallback_texts = client._collect_fallback_texts(response_data)
        special_state: dict[str, Any] = {}

        message: dict[str, Any] | None = None
        if "choices" in response_data and response_data["choices"]:
            choice = response_data["choices"][0]
            message = choice.get("message", {})
        else:
            message = client._coerce_basic_openai_message(response_data)

        if message:
            if "choices" not in response_data:
                logger.debug(
                    "[openai] 使用非标准字段构造 message，keys=%s",
                    list(response_data.keys())[:5],
                )
            content = message.get("content", "")

            text_chunks: list[str] = []
            image_candidates: list[str] = []
            extracted_urls: list[str] = []

            logger.debug(
                "[openai] 解析响应 choices，content_type=%s images_field=%s",
                type(content),
                bool(message.get("images")),
            )

            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue

                    part_type = part.get("type")
                    if part_type == "text" and "text" in part:
                        text_val = str(part.get("text", ""))
                        text_chunks.append(text_val)
                        extracted_urls.extend(client._find_image_urls_in_text(text_val))
                    elif part_type == "image_url":
                        image_obj = part.get("image_url") or {}
                        if isinstance(image_obj, dict):
                            url_val = image_obj.get("url")
                            if url_val:
                                image_candidates.append(url_val)
            elif isinstance(content, str):
                text_chunks.append(content)
                extracted_urls.extend(client._find_image_urls_in_text(content))

            if message.get("images"):
                for image_item in message["images"]:
                    if not isinstance(image_item, dict):
                        continue

                    image_obj = image_item.get("image_url")
                    if isinstance(image_obj, dict):
                        url_val = image_obj.get("url")
                        if isinstance(url_val, str) and url_val:
                            image_candidates.append(url_val)
                    elif isinstance(image_obj, str) and image_obj:
                        image_candidates.append(image_obj)
                    elif isinstance(image_item.get("url"), str):
                        image_candidates.append(image_item["url"])

            if extracted_urls:
                image_candidates.extend(extracted_urls)

            if text_chunks:
                text_content = " ".join([t for t in text_chunks if t]).strip() or None

            for candidate_url in image_candidates:
                logger.debug("[openai] 处理候选URL: %s", str(candidate_url)[:120])
                if isinstance(candidate_url, str) and candidate_url.startswith(
                    "data:image/"
                ):
                    image_url, image_path = await client._parse_data_uri(candidate_url)
                elif isinstance(candidate_url, str):
                    cleaned_candidate = (
                        candidate_url.strip()
                        .replace("&amp;", "&")
                        .rstrip(").,;")
                        .strip("'\"")
                    )
                    if not cleaned_candidate:
                        continue
                    if await self._handle_special_candidate_url(
                        client=client,
                        session=session,
                        candidate_url=cleaned_candidate,
                        image_urls=image_urls,
                        image_paths=image_paths,
                        api_base=api_base,
                        state=special_state,
                    ):
                        continue
                    if cleaned_candidate.startswith(
                        "http://"
                    ) or cleaned_candidate.startswith("https://"):
                        image_urls.append(cleaned_candidate)
                        logger.debug(
                            f"🖼️ OpenAI 返回可直接访问的图像链接: {cleaned_candidate}"
                        )
                        continue
                    image_url, image_path = await client._download_image(
                        cleaned_candidate, session, use_cache=False
                    )
                else:
                    logger.warning(f"跳过非字符串类型的图像URL: {type(candidate_url)}")
                    continue

                if image_url or image_path:
                    if image_url:
                        image_urls.append(image_url)
                    if image_path:
                        image_paths.append(image_path)

            extracted_urls2: list[str] = []
            extracted_paths2: list[str] = []

            if isinstance(content, str):
                extracted_urls2, extracted_paths2 = await client._extract_from_content(
                    content
                )
            elif text_content:
                extracted_urls2, extracted_paths2 = await client._extract_from_content(
                    text_content
                )

            if extracted_urls2 or extracted_paths2:
                for url in extracted_urls2:
                    cleaned_url = (
                        str(url)
                        .strip()
                        .replace("&amp;", "&")
                        .rstrip(").,;")
                        .strip("'\"")
                    )
                    if not cleaned_url:
                        continue
                    if await self._handle_special_candidate_url(
                        client=client,
                        session=session,
                        candidate_url=cleaned_url,
                        image_urls=image_urls,
                        image_paths=image_paths,
                        api_base=api_base,
                        state=special_state,
                    ):
                        continue
                    if cleaned_url not in image_urls:
                        image_urls.append(cleaned_url)
                for p in extracted_paths2:
                    if p and p not in image_paths:
                        image_paths.append(p)

            if text_content:
                http_urls = client._find_image_urls_in_text(text_content)
                extra_urls = self._find_additional_image_urls_in_text(text_content)
                for url in [*http_urls, *extra_urls]:
                    cleaned_url = (
                        str(url)
                        .strip()
                        .replace("&amp;", "&")
                        .rstrip(").,;")
                        .strip("'\"")
                    )
                    if not cleaned_url:
                        continue
                    if await self._handle_special_candidate_url(
                        client=client,
                        session=session,
                        candidate_url=cleaned_url,
                        image_urls=image_urls,
                        image_paths=image_paths,
                        api_base=api_base,
                        state=special_state,
                    ):
                        continue
                    if cleaned_url not in image_urls:
                        image_urls.append(cleaned_url)

                loose_matches = re.finditer(
                    r"data:image/([a-zA-Z0-9.+-]+);base64,([-A-Za-z0-9+/=_\\s]+)",
                    text_content,
                    flags=re.IGNORECASE,
                )
                for m in loose_matches:
                    fmt = m.group(1)
                    b64_raw = m.group(2)
                    b64_clean = re.sub(r"\\s+", "", b64_raw)
                    image_path = await save_base64_image(b64_clean, fmt.lower())
                    if image_path:
                        image_urls.append(image_path)
                        image_paths.append(image_path)
                        logger.debug(
                            "[openai] 松散提取 data URI 成功: fmt=%s len=%s",
                            fmt,
                            len(b64_clean),
                        )

        else:
            logger.debug("[openai] 响应缺少可用的 message 字段，尝试 data/b64 解析")

        if not (image_urls or image_paths) and fallback_texts:
            fallback_added = await client._append_images_from_texts(
                fallback_texts, image_urls, image_paths
            )
            if fallback_added and not text_content:
                text_content = (
                    " ".join(t.strip() for t in fallback_texts if t and t.strip())
                    or text_content
                )

        if not image_urls and not image_paths and response_data.get("data"):
            for image_item in response_data["data"]:
                if "url" in image_item:
                    image_url, image_path = await client._download_image(
                        image_item["url"], session, use_cache=False
                    )
                    if image_url:
                        image_urls.append(image_url)
                    if image_path:
                        image_paths.append(image_path)
                elif "b64_json" in image_item:
                    image_path = await save_base64_image(image_item["b64_json"], "png")
                    if image_path:
                        image_urls.append(image_path)
                        image_paths.append(image_path)

        if image_urls or image_paths:
            logger.debug(
                f"🖼️ OpenAI 收集到 {len(image_paths) or len(image_urls)} 张图片"
            )
            return image_urls, image_paths, text_content, thought_signature

        if text_content:
            detail = (
                f" | 参考图处理提示: {'; '.join(fail_reasons[:3])}"
                if fail_reasons
                else ""
            )
            logger.debug(
                "[openai] 仅返回文本，长度=%s 预览=%s",
                len(text_content),
                text_content[:200],
            )
            logger.warning(f"OpenAI只返回了文本响应，未生成图像，将触发重试{detail}")
            logger.debug(f"OpenAI响应内容: {str(response_data)[:1000]}")
            raise APIError(
                f"图像生成失败：API只返回了文本响应，正在重试... | 响应预览: {str(response_data)[:300]}",
                500,
                "no_image_retry",
            )

        logger.warning(
            f"OpenAI 响应格式不支持或未找到图像数据，响应: {str(response_data)[:500]}"
        )
        return image_urls, image_paths, text_content, thought_signature
