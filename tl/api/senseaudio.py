"""SenseAudio 图片同步生成与异步任务协议（2026-09 核对官方文档）。"""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

import aiohttp
from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from .base import ProviderRequest
from .param_utils import coerce_float, ensure_prompt_length
from .provider_limits import MAX_REFERENCE_IMAGES_SENSEAUDIO
from .reference_values import resolve_reference_api_values

DEFAULT_MODEL = "senseaudio-image-2.0-260319"
DEFAULT_API_BASE = "https://api.senseaudio.cn"
# 固定白名单来自 image/sync 与 image/async；不能套用原厂同名模型的尺寸。
MODEL_SIZES: dict[str, tuple[str, ...]] = {
    DEFAULT_MODEL: tuple(
        "1024x1024 1536x864 864x1536 2016x864 864x2016 2048x1024 "
        "1024x2048 2048x1152 1152x2048 2688x1152 1152x2688 2688x1344 "
        "1344x2688 3840x1648 1648x3840 3840x1920 1920x3840 3840x2160 "
        "2160x3840".split()
    ),
    "senseaudio-image-1.0-260319": tuple(
        "1664x928 928x1664 1584x1056 1056x1584 1472x1140 1140x1472 1328x1328".split()
    ),
    "doubao-seedream-5-0-260128": tuple(
        "2304x1728 1728x2304 2496x1664 1664x2496 2048x2048 3136x1344 "
        "2848x1600 1600x2848 3456x2592 2592x3456 2496x3744 3744x2496 "
        "4096x2304 2304x4096 3072x3072 4704x2016".split()
    ),
    "sensenova-u1-fast": tuple(
        "1664x2496 2496x1664 1760x2368 2368x1760 1824x2272 2272x1824 "
        "2048x2048 2752x1536 1536x2752 3072x1376 1344x3136".split()
    ),
}
MODEL_RESOLUTIONS = {
    DEFAULT_MODEL: ("1K", "2K", "4K"),
    "senseaudio-image-1.0-260319": ("1K",),
    "doubao-seedream-5-0-260128": ("2K", "4K"),
    "sensenova-u1-fast": ("2K",),
}
MODEL_RATIOS = {
    DEFAULT_MODEL: ("1:1", "16:9", "9:16", "21:9", "2:1", "1:2"),
    "senseaudio-image-1.0-260319": ("1:1", "16:9", "9:16", "3:2", "2:3", "4:3", "3:4"),
    "doubao-seedream-5-0-260128": (
        "1:1",
        "4:3",
        "3:4",
        "3:2",
        "2:3",
        "21:9",
        "16:9",
        "9:16",
    ),
    "sensenova-u1-fast": (
        "1:1",
        "2:3",
        "3:2",
        "3:4",
        "4:3",
        "4:5",
        "5:4",
        "16:9",
        "9:16",
        "21:9",
    ),
}


def _resolve_size(model: str, resolution: str | None, aspect_ratio: str | None) -> str:
    ratio = 1.0
    if aspect_ratio:
        try:
            width, height = map(float, aspect_ratio.split(":"))
            if width > 0 and height > 0 and math.isfinite(width / height):
                ratio = width / height
        except (ValueError, ZeroDivisionError):
            pass
    target = {"1K": 1024, "2K": 2048, "4K": 4096}.get(resolution or "1K", 1024)

    def distance(size: str) -> tuple[float, float]:
        w, h = map(int, size.split("x"))
        # 同比例的不同尺寸有像素取整误差，先归入同一比例，再比较长边档位。
        return round(abs(math.log((w / h) / ratio)), 2), abs(max(w, h) - target)

    return min(MODEL_SIZES[model], key=distance)


def _api_base(value: str | None) -> str:
    base = (value or DEFAULT_API_BASE).strip().rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


class SenseAudioProvider:
    name = "senseaudio"

    async def build_request(
        self, *, client: Any, config: ApiRequestConfig, is_retry: bool = False
    ) -> ProviderRequest:
        settings = config.provider_settings or {}
        model = str(config.model or settings.get("model") or DEFAULT_MODEL).strip()
        if model not in MODEL_SIZES:
            raise APIError(
                "SenseAudio 模型不在支持列表中", None, "invalid_model", retryable=False
            )
        if not config.api_key:
            raise APIError(
                "SenseAudio 缺少 API Key", None, "missing_api_key", retryable=False
            )
        prompt = (config.prompt or "").strip()
        if not prompt:
            raise APIError(
                "SenseAudio 需要非空 prompt", None, "empty_prompt", retryable=False
            )
        ensure_prompt_length(
            prompt,
            max_chars=6000 if model == DEFAULT_MODEL else 2000,
            provider="SenseAudio",
        )
        mode = settings.get("request_mode", "sync")
        if mode not in ("sync", "async"):
            raise APIError(
                "SenseAudio request_mode 必须为 sync 或 async",
                None,
                "invalid_request",
                retryable=False,
            )
        payload: dict[str, Any] = {"model": model, "prompt": prompt}
        if config.reference_images:
            refs = await resolve_reference_api_values(
                client,
                config,
                config.reference_images,
                max_count=MAX_REFERENCE_IMAGES_SENSEAUDIO,
                log_prefix="[senseaudio] ",
                error_label="senseaudio",
            )
            if not refs:
                raise APIError(
                    "SenseAudio 未取得有效参考图",
                    None,
                    "invalid_reference_image",
                    retryable=False,
                )
            payload["reference"] = refs[0]
        # 文生图 size 必填；只有参考图请求可让服务端自动适配尺寸。
        if not (payload.get("reference") and config.suppress_resolution):
            size = str(settings.get("size") or "").strip()
            if size and size not in MODEL_SIZES[model]:
                raise APIError(
                    "SenseAudio size 不在当前模型的尺寸白名单中",
                    None,
                    "invalid_size",
                    retryable=False,
                )
            payload["size"] = size or _resolve_size(
                model, config.resolution, config.aspect_ratio
            )
        seed = config.seed if config.seed is not None else settings.get("seed")
        if seed is not None and seed != "":
            try:
                payload["seed"] = int(seed)
            except (ValueError, TypeError) as exc:
                raise APIError(
                    "SenseAudio seed 必须为整数或留空",
                    None,
                    "invalid_request",
                    retryable=False,
                ) from exc
        base = _api_base(config.api_base or settings.get("api_base"))
        return ProviderRequest(
            url=f"{base}/v1/image/{mode}",
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
    ) -> tuple[list[str], list[str], str | None, str | None]:
        if http_status is not None and http_status != 200:
            raise self._error(response_data, http_status)
        if not isinstance(response_data, dict):
            raise APIError(
                "SenseAudio 响应不是对象",
                http_status,
                "invalid_response",
                retryable=False,
            )
        settings = getattr(request_config, "provider_settings", None) or {}
        if settings.get("request_mode", "sync") == "async":
            task_id = response_data.get("task_id")
            if not isinstance(task_id, str) or not task_id.strip():
                raise APIError(
                    "SenseAudio 响应缺少 task_id",
                    http_status,
                    "invalid_response",
                    retryable=False,
                )
            response_data = await self._poll(
                client=client,
                session=session,
                task_id=task_id,
                base=_api_base(
                    api_base
                    or getattr(request_config, "api_base", None)
                    or settings.get("api_base")
                ),
                request_config=request_config,
                settings=settings,
            )
        url = response_data.get("url")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise APIError(
                "SenseAudio 响应缺少有效图片 URL",
                http_status,
                "no_image",
                retryable=False,
            )
        if client._request_has_proxy(request_config):
            _, path = await client._download_image(
                url,
                session,
                use_cache=False,
                proxy=client._request_http_proxy(request_config),
            )
            if not path:
                # 共享下载器失败时返回空路径；不能触发上游重新生成。
                raise APIError(
                    "SenseAudio 图片下载失败", None, "download_error", retryable=False
                )
            return [path], [path], None, None
        return [url], [], None, None

    async def _poll(
        self,
        *,
        client: Any,
        session: aiohttp.ClientSession,
        task_id: str,
        base: str,
        request_config: ApiRequestConfig | None,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        interval = coerce_float(settings.get("poll_interval"), lo=0.1, hi=30, default=3)
        timeout = coerce_float(settings.get("poll_timeout"), lo=1, hi=3600, default=100)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        if request_config and request_config.request_deadline is not None:
            deadline = min(deadline, request_config.request_deadline)
        headers = {
            "Authorization": f"Bearer {getattr(request_config, 'api_key', None)}"
        }
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise APIError(
                    "SenseAudio 轮询超时；任务可能仍在生成，不再重新提交",
                    None,
                    "timeout",
                    retryable=False,
                )
            try:
                async with session.get(
                    f"{base}/v1/image/pending",
                    params={"task_id": task_id},
                    headers=headers,
                    proxy=client._request_http_proxy(request_config),
                    timeout=aiohttp.ClientTimeout(total=min(15, remaining)),
                ) as response:
                    status = response.status
                    body = await response.text()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                # 已取得任务 ID，瞬时查询失败只能重查同一任务。
                logger.warning("[senseaudio] 任务查询连接失败，继续轮询")
            else:
                if status == 429 or status >= 500:
                    logger.warning("[senseaudio] 任务查询 HTTP %s，继续轮询", status)
                else:
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError as exc:
                        raise APIError(
                            "SenseAudio 查询响应不是 JSON",
                            status,
                            "invalid_response",
                            retryable=False,
                        ) from exc
                    if status != 200:
                        raise self._error(data, status, retryable=False)
                    if not isinstance(data, dict):
                        raise APIError(
                            "SenseAudio 查询响应不是对象",
                            status,
                            "invalid_response",
                            retryable=False,
                        )
                    state = data.get("status")
                    if state == "completed":
                        return data
                    if state == "failed":
                        raise self._error(data, status, retryable=False)
                    if state != "pending":
                        raise APIError(
                            "SenseAudio 返回未知任务状态",
                            status,
                            "invalid_response",
                            retryable=False,
                        )
            await asyncio.sleep(min(interval, max(0, deadline - loop.time())))

    @staticmethod
    def _error(
        data: Any, status: int | None, retryable: bool | None = None
    ) -> APIError:
        message = (
            (data.get("error_message") or data.get("message"))
            if isinstance(data, dict)
            else None
        )
        code = (
            (data.get("ref_code") or data.get("code"))
            if isinstance(data, dict)
            else None
        )
        return APIError(
            f"SenseAudio 请求失败: {message or f'HTTP {status}'}",
            status,
            "api_error",
            str(code) if code is not None else None,
            retryable=retryable,
        )
