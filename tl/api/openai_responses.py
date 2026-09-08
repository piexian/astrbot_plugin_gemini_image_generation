"""Responses API hosted image-generation tool, including gateway model names."""

from __future__ import annotations

import codecs
import json
from typing import Any

import aiohttp

from ..api_types import APIError, ApiRequestConfig
from ..tl_utils import save_base64_image
from .base import ProviderRequest
from .openai_images import _resolve_size_value
from .reference_values import resolve_reference_api_values

DEFAULT_BASE_MODEL = "gpt-5.6-luna"
DEFAULT_IMAGE_MODEL = "gpt-image-2.5-flare"


def _unknown_result() -> APIError:
    return APIError(
        "生图连接中断，任务结果未知，已停止自动重试以避免重复生成。",
        error_type="outcome_unknown",
        retryable=False,
    )


async def read_responses_response(
    *, response: aiohttp.ClientResponse
) -> dict[str, Any]:
    """Keep completed images; partial previews never count as final output."""
    content_type = response.headers.get("Content-Type", "").lower()
    if "text/event-stream" not in content_type:
        try:
            result = await response.json(content_type=None)
        except ValueError as exc:
            raise _unknown_result() from exc
        if not isinstance(result, dict):
            raise _unknown_result()
        return result

    decoder = codecs.getincrementaldecoder("utf-8")()
    pending = ""
    data_lines: list[str] = []
    images: dict[str, dict[str, Any]] = {}

    def event_result() -> dict[str, Any] | None:
        if not data_lines:
            return None
        raw = "\n".join(data_lines)
        data_lines.clear()
        if raw.strip() == "[DONE]":
            return None
        try:
            event = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise _unknown_result() from exc
        if not isinstance(event, dict):
            raise _unknown_result()
        kind = event.get("type")
        if kind in {"error", "response.failed"}:
            result = event.get("response") or event
            return {"status": "failed", "error": result.get("error") or result}
        if kind == "response.incomplete":
            raise _unknown_result()
        if kind == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "image_generation_call" and item.get("result"):
                images[str(item.get("id") or event.get("output_index", 0))] = item
        if kind == "response.completed":
            result = dict(event.get("response") or {})
            output = list(result.get("output") or [])
            output = [
                images.get(str(item.get("id")), item)
                if isinstance(item, dict) and not item.get("result")
                else item
                for item in output
            ]
            ids = {item.get("id") for item in output if isinstance(item, dict)}
            output.extend(item for item in images.values() if item.get("id") not in ids)
            result["output"] = output
            result.setdefault("status", "completed")
            return result
        return None

    async for chunk in response.content.iter_any():
        pending += decoder.decode(chunk)
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                result = event_result()
                if result is not None:
                    return result
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
            # event/id/retry metadata and heartbeat comments are not JSON data.
    pending += decoder.decode(b"", final=True)
    if pending.startswith("data:"):
        data_lines.append(pending[5:].lstrip(" ").rstrip("\r"))
    result = event_result()
    if result is not None:
        return result
    raise _unknown_result()


class OpenAIResponsesProvider:
    name = "openai_responses"

    async def build_request(
        self, *, client: Any, config: ApiRequestConfig
    ) -> ProviderRequest:
        settings = config.provider_settings or {}
        model = str(
            config.model or settings.get("model") or DEFAULT_IMAGE_MODEL
        ).strip()
        base_model = str(settings.get("base_model") or DEFAULT_BASE_MODEL).strip()
        base = str(config.api_base or "https://api.openai.com").rstrip("/")
        if base.endswith("/responses"):
            url = base
        elif base.endswith("/v1"):
            url = base + "/responses"
        else:
            url = base + "/v1/responses"
        tool: dict[str, Any] = {"type": "image_generation", "model": model}
        size = _resolve_size_value(
            "gpt-image-2",
            config.resolution,
            settings,
            suppress_resolution=config.suppress_resolution,
        )
        if size:
            tool["size"] = size
        for key in ("quality", "output_format"):
            value = (config.quality if key == "quality" else None) or settings.get(key)
            if value:
                tool[key] = value
        tool.setdefault("output_format", "png")
        refs = await resolve_reference_api_values(
            client,
            config,
            config.reference_images,
            max_count=max(int(settings.get("max_reference_images", 6)), 0),
            log_prefix="[openai_responses]",
            error_label=self.name,
        )
        if config.reference_images and not refs:
            raise APIError(
                "参考图处理失败。",
                error_type="invalid_reference_image",
                retryable=False,
            )
        content = [{"type": "input_text", "text": config.prompt}]
        content.extend({"type": "input_image", "image_url": ref} for ref in refs)
        return ProviderRequest(
            url=url,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream, application/json",
            },
            payload={
                "model": base_model,
                "input": [{"role": "user", "content": content}],
                "tools": [tool],
                "tool_choice": {"type": "image_generation"},
                "stream": True,
                "store": False,
            },
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
    ) -> tuple[list[str], list[str], str | None, str | None]:
        result = response_data.get("response") or response_data
        error = result.get("error")
        if error or result.get("status") == "failed":
            message = error.get("message") if isinstance(error, dict) else error
            raise APIError(
                str(message or "Responses 生图失败"),
                http_status,
                "api_error",
                retryable=False,
            )
        if result.get("status") != "completed":
            raise _unknown_result()
        paths: list[str] = []
        texts: list[str] = []
        seen: set[str] = set()
        for index, item in enumerate(result.get("output") or []):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                texts.extend(
                    block["text"]
                    for block in item.get("content") or []
                    if isinstance(block, dict) and isinstance(block.get("text"), str)
                )
            if item.get("type") != "image_generation_call" or not item.get("result"):
                continue
            identity = str(item.get("id") or index)
            if identity in seen:
                continue
            seen.add(identity)
            settings = request_config.provider_settings if request_config else {}
            output_format = (
                item.get("output_format")
                or (settings or {}).get("output_format")
                or "png"
            )
            if output_format not in {"png", "jpeg", "webp"}:
                output_format = "png"
            if not isinstance(item["result"], str):
                raise _unknown_result()
            path = await save_base64_image(item["result"], output_format)
            if not path:
                raise APIError(
                    "生成的图片保存失败。",
                    error_type="outcome_unknown",
                    retryable=False,
                )
            paths.append(path)
        if not paths:
            raise APIError(
                "Responses 已完成，但没有返回图片。",
                error_type="no_image",
                retryable=False,
            )
        return paths, paths, "\n".join(texts) or None, None
