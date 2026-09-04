"""WebUI 工作台生成编排、gallery 与上传文件管理。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import math
import mimetypes
import os
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import cv2
import numpy as np
from astrbot.api import logger
from PIL import Image, UnidentifiedImageError

from .api_types import ApiRequestConfig
from .generation_tracker import TERMINAL_STATUSES, GenerationTracker, _timestamp
from .provider_capabilities import (
    candidate_capability,
    candidate_reference_limit,
    select_candidates,
)

_SAFE_FILE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_REMOTE_IMAGE_MAX_BYTES = 20 * 1024 * 1024
_UPLOAD_QUOTA_BYTES = 200 * 1024 * 1024
_UPLOAD_EXPIRE_HOURS = 24
_CHUNK_SIZE = 1024 * 1024
_THUMBNAIL_MAX_EDGE = 512
_THUMBNAIL_JPEG_QUALITY = 80
_THUMBNAIL_B64_MAX_BYTES = 2 * 1024 * 1024
_ORIGINAL_B64_MAX_BYTES = 8 * 1024 * 1024
_DISPLAY_NAMES = {
    "google": "Google Gemini",
    "gemini_interactions": "Gemini Interactions",
    "openai": "OpenAI Compatible",
    "agnes_ai": "Agnes AI",
    "xai": "xAI",
    "minimax": "MiniMax",
    "stepfun": "StepFun",
    "openai_images": "OpenAI Images",
    "doubao": "Doubao Seedream",
    "sensenova": "SenseNova",
    "dashscope": "DashScope",
    "modelscope": "ModelScope",
    "siliconflow": "SiliconFlow",
}


class StudioServiceError(Exception):
    """可安全映射为 HTTP 错误响应的工作台异常。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        data: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.data = data


def _positive_int(value: Any, default: int = 1) -> int:
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return default


def _string(value: Any, *, name: str, limit: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise StudioServiceError(f"{name} 必须是字符串")
    value = value.strip()
    if required and not value:
        raise StudioServiceError(f"{name} 不能为空")
    if len(value) > limit:
        raise StudioServiceError(f"{name} 不能超过 {limit} 个字符")
    return value


def _contains_non_finite(value: Any, depth: int = 0) -> bool:
    if depth > 32:
        return True
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_contains_non_finite(item, depth + 1) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_non_finite(item, depth + 1) for item in value)
    return False


class WebStudioService:
    """执行工作台请求，并保证生成结果只引用稳定的 gallery 文件。"""

    def __init__(
        self,
        api_client: Any,
        tracker: GenerationTracker,
        config: Any,
        data_dir: str | Path,
    ) -> None:
        self.api_client = api_client
        self.tracker = tracker
        self.config = config
        self.data_dir = Path(data_dir)
        self.gallery_dir = self.data_dir / "gallery"
        self.upload_dir = self.data_dir / "webui_uploads"
        self._api_semaphore = asyncio.Semaphore(
            max(int(getattr(config, "webui_max_concurrent_jobs", 2)), 1)
        )
        self._runtime_tasks: dict[str, asyncio.Task[Any]] = {}
        self._gallery_lock = asyncio.Lock()
        self._upload_lock = asyncio.Lock()
        self._admitted_jobs = 0
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def update_api_client(self, api_client: Any) -> None:
        self.api_client = api_client

    def _admit(self) -> None:
        if self._closed:
            raise StudioServiceError("工作台服务已关闭", status_code=503)
        maximum = max(
            int(getattr(self.config, "webui_max_concurrent_jobs", 2)),
            1,
        )
        if self._admitted_jobs >= maximum:
            logger.info(
                "[WebUI] 工作台任务被拒绝: 原因=并发已满, "
                f"当前任务数={self._admitted_jobs}, 并发上限={maximum}, 状态码=429"
            )
            raise StudioServiceError(
                "工作台生成任务已达到并发上限，请稍后重试",
                status_code=429,
            )
        self._admitted_jobs += 1

    def _attach(self, job_id: str, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._runtime_tasks[job_id] = task

        def done(done_task: asyncio.Task[Any]) -> None:
            self._runtime_tasks.pop(job_id, None)
            self._admitted_jobs = max(self._admitted_jobs - 1, 0)
            if done_task.cancelled():
                return
            exception = done_task.exception()
            if exception is not None:
                logger.error(
                    f"[WebUI] 任务 {job_id} 异常退出: {exception}",
                    exc_info=(
                        type(exception),
                        exception,
                        exception.__traceback__,
                    ),
                )

        task.add_done_callback(done)

    async def generate(
        self,
        payload: dict[str, Any],
        requester: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized, warning = self.validate_payload(payload)
        if self.api_client is None:
            raise StudioServiceError("API 客户端尚未初始化", status_code=503)
        self._admit()
        requester = requester or {}
        try:
            if normalized.get("batch") is not None:
                response = await self._start_batch(normalized, requester)
            else:
                response = await self._start_single(normalized, requester)
        except Exception:
            self._admitted_jobs = max(self._admitted_jobs - 1, 0)
            raise
        if warning:
            response["warning"] = warning
        batch_items = normalized.get("batch") or []
        target_count = (
            sum(item["image_count"] for item in batch_items)
            if batch_items
            else normalized["image_count"]
        )
        logger.info(
            f"[WebUI] 工作台任务已受理: job_id={response['job_id']}, "
            f"来源=webui, 目标张数={target_count}, 批量条目数={len(batch_items)}"
        )
        return response

    async def _start_single(
        self,
        payload: dict[str, Any],
        requester: dict[str, Any],
    ) -> dict[str, Any]:
        record = await self.tracker.begin(
            source="webui",
            prompt=payload["prompt"],
            params=self._history_params(payload),
            requester=requester,
            requested_images=payload["image_count"],
        )
        self._attach(record["job_id"], self._run_single(record["job_id"], payload))
        return {"job_id": record["job_id"]}

    async def _start_batch(
        self,
        payload: dict[str, Any],
        requester: dict[str, Any],
    ) -> dict[str, Any]:
        items = payload["batch"]
        requested_images = sum(item["image_count"] for item in items)
        parent = await self.tracker.begin(
            source="webui",
            prompt="",
            params=self._history_params(payload),
            requester=requester,
            requested_images=requested_images,
        )
        child_jobs: list[tuple[str, dict[str, Any]]] = []
        for item in items:
            child_payload = {**payload, **item, "batch": None}
            child = await self.tracker.begin(
                source="webui",
                prompt=item["prompt"],
                params=self._history_params(child_payload),
                requester=requester,
                parent_job_id=parent["job_id"],
                item_name=item["name"],
                requested_images=item["image_count"],
            )
            child_jobs.append((child["job_id"], child_payload))
        self._attach(
            parent["job_id"],
            self._run_batch(parent["job_id"], child_jobs),
        )
        return {
            "job_id": parent["job_id"],
            "item_job_ids": [job_id for job_id, _ in child_jobs],
        }

    @staticmethod
    def _history_params(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "resolution": payload.get("resolution"),
            "aspect_ratio": payload.get("aspect_ratio"),
            "provider": payload.get("provider"),
            "model": payload.get("model"),
            "candidate_id": payload.get("candidate_id"),
            "image_count": payload.get("image_count", 1),
            "quality": payload.get("quality"),
        }

    def validate_payload(
        self,
        payload: Any,
    ) -> tuple[dict[str, Any], str | None]:
        if not isinstance(payload, dict) or _contains_non_finite(payload):
            raise StudioServiceError("请求体必须是有效的 JSON 对象")

        source = dict(payload)
        warning: str | None = None
        if source.get("reuse_job_id") not in (None, ""):
            source, warning = self._apply_reuse(source)

        prompt_present = "prompt" in source and source.get("prompt") is not None
        batch_present = "batch" in source and source.get("batch") is not None
        if prompt_present and batch_present:
            raise StudioServiceError("prompt 与 batch 不能同时提供")
        if not prompt_present and not batch_present:
            raise StudioServiceError("必须提供 prompt 或 batch")

        normalized: dict[str, Any] = {
            "prompt": "",
            "batch": None,
            "resolution": self._optional_text(source.get("resolution"), "resolution"),
            "aspect_ratio": self._optional_text(
                source.get("aspect_ratio"), "aspect_ratio"
            ),
            "quality": self._optional_text(source.get("quality"), "quality"),
            "seed": self._optional_integer(source.get("seed"), "seed"),
            "provider": self._optional_text(source.get("provider"), "provider"),
            "model": self._optional_text(source.get("model"), "model", limit=256),
            "candidate_id": self._optional_text(
                source.get("candidate_id"), "candidate_id"
            ),
            "negative_prompt": self._optional_text(
                source.get("negative_prompt"), "negative_prompt", limit=500
            ),
            "image_count": self._image_count(source.get("image_count", 1)),
            "reference_names": self._name_list(
                source.get("reference_names"), "reference_names"
            ),
            "upload_names": self._name_list(source.get("upload_names"), "upload_names"),
        }
        route_candidates = select_candidates(
            getattr(self.config, "provider_candidates", []) or [],
            provider=normalized["provider"],
            model=normalized["model"],
            candidate_id=normalized["candidate_id"],
        )
        maximum_refs = max(
            (candidate_reference_limit(candidate) for candidate in route_candidates),
            default=0,
        )
        if (
            len(normalized["reference_names"]) + len(normalized["upload_names"])
            > maximum_refs
        ):
            raise StudioServiceError(f"参考图片总数不能超过 {maximum_refs}")
        normalized["reference_images"] = self._resolve_reference_paths(normalized)

        if batch_present:
            normalized["batch"] = self._validate_batch(
                source.get("batch"), normalized["image_count"]
            )
        else:
            normalized["prompt"] = _string(
                source.get("prompt"), name="prompt", limit=2000, required=True
            )

        self._validate_capabilities(normalized)
        return normalized, warning

    def _apply_reuse(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None]:
        if payload.get("batch") is not None:
            raise StudioServiceError("batch 请求不能使用 reuse_job_id")
        reuse_job_id = _string(
            payload.get("reuse_job_id"),
            name="reuse_job_id",
            limit=128,
            required=True,
        )
        record = self.tracker.get(reuse_job_id)
        if record is None or record.get("status") not in TERMINAL_STATUSES:
            raise StudioServiceError("reuse_job_id 不存在或尚未结束")
        defaults = {"prompt": record.get("prompt") or ""}
        defaults.update(record.get("params") or {})
        merged = defaults
        for key, value in payload.items():
            if key != "reuse_job_id":
                merged[key] = value

        reused_route = not any(
            key in payload for key in ("provider", "model", "candidate_id")
        )
        if not reused_route and "candidate_id" not in payload:
            merged["candidate_id"] = None
        if reused_route and (
            merged.get("provider") or merged.get("model") or merged.get("candidate_id")
        ):
            candidates = select_candidates(
                getattr(self.config, "provider_candidates", []) or [],
                provider=merged.get("provider"),
                model=merged.get("model"),
                candidate_id=merged.get("candidate_id"),
            )
            if not candidates:
                merged["provider"] = None
                merged["model"] = None
                merged["candidate_id"] = None
                return merged, "原记录的供应商或模型已不可用，已回退默认配置"
        return merged, None

    @staticmethod
    def _optional_text(value: Any, name: str, *, limit: int = 128) -> str | None:
        if value in (None, ""):
            return None
        return _string(value, name=name, limit=limit, required=True)

    @staticmethod
    def _image_count(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise StudioServiceError("image_count 必须是 1 到 10 的整数")
        try:
            count = int(value)
        except (TypeError, ValueError) as exc:
            raise StudioServiceError("image_count 必须是 1 到 10 的整数") from exc
        if count < 1 or count > 10 or str(value).strip() != str(count):
            raise StudioServiceError("image_count 必须是 1 到 10 的整数")
        return count

    @staticmethod
    def _optional_integer(value: Any, name: str) -> int | None:
        if value in (None, ""):
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise StudioServiceError(f"{name} 必须是整数")
        return value

    @staticmethod
    def _name_list(value: Any, name: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise StudioServiceError(f"{name} 必须是数组")
        names = []
        for item in value:
            if not isinstance(item, str) or not _SAFE_FILE_NAME.fullmatch(item):
                raise StudioServiceError(f"{name} 包含非法文件名")
            if Path(item).name != item:
                raise StudioServiceError(f"{name} 包含非法文件名")
            names.append(item)
        return names

    def _resolve_reference_paths(self, payload: dict[str, Any]) -> list[str]:
        paths = []
        for directory, key, source_name in (
            (self.gallery_dir, "reference_names", "画廊"),
            (self.upload_dir, "upload_names", "上传暂存"),
        ):
            logger.debug(
                f"[WebUI] 参考图解析: 来源={source_name}, 数量={len(payload[key])}"
            )
            root = directory.resolve()
            for name in payload[key]:
                path = (directory / name).resolve()
                if path.parent != root or not path.is_file():
                    raise StudioServiceError(f"参考图片不存在: {name}")
                paths.append(str(path))
        return paths

    def _validate_batch(self, value: Any, default_count: int) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not value:
            raise StudioServiceError("batch 必须是非空数组")
        maximum = max(int(getattr(self.config, "batch_max_tasks", 20)), 1)
        if len(value) > maximum:
            raise StudioServiceError(f"batch 任务数不能超过 {maximum}")
        items = []
        for index, item in enumerate(value, start=1):
            if not isinstance(item, dict):
                raise StudioServiceError(f"batch 第 {index} 项必须是对象")
            items.append(
                {
                    "name": _string(
                        item.get("name"),
                        name=f"batch 第 {index} 项 name",
                        limit=64,
                        required=True,
                    ),
                    "prompt": _string(
                        item.get("prompt"),
                        name=f"batch 第 {index} 项 prompt",
                        limit=2000,
                        required=True,
                    ),
                    "image_count": self._image_count(
                        item.get("image_count", default_count)
                    ),
                }
            )
        budget = max(
            int(getattr(self.config, "webui_batch_total_budget", 40)),
            1,
        )
        if sum(item["image_count"] for item in items) > budget:
            raise StudioServiceError(f"批量图片总预算不能超过 {budget}")
        return items

    def _validate_capabilities(self, payload: dict[str, Any]) -> None:
        configured = list(getattr(self.config, "provider_candidates", []) or [])
        if not configured:
            raise StudioServiceError("没有可用的供应商配置", status_code=503)
        required = set()
        if payload.get("negative_prompt"):
            required.add("negative_prompt")
        if payload.get("quality"):
            required.add("quality")
        if payload.get("seed") is not None:
            required.add("seed")
        candidates = select_candidates(
            configured,
            provider=payload.get("provider"),
            model=payload.get("model"),
            candidate_id=payload.get("candidate_id"),
            has_reference_images=bool(payload.get("reference_images")),
            required_parameters=required,
        )
        if not candidates:
            raise StudioServiceError("没有匹配本次请求能力的供应商或模型")
        reference_count = len(payload.get("reference_images") or [])
        candidates = [
            candidate
            for candidate in candidates
            if candidate_reference_limit(candidate) >= reference_count
        ]
        if not candidates:
            raise StudioServiceError("参考图片数量超过目标候选上限")

        for field in ("resolution", "aspect_ratio", "quality"):
            value = payload.get(field)
            if value is None:
                continue
            accepted = []
            legal: set[str] = set()
            for candidate in candidates:
                descriptor = (
                    candidate_capability(candidate).get("parameters", {}).get(field, {})
                )
                allowed = descriptor.get("enum")
                if allowed:
                    legal.update(str(item) for item in allowed)
                    if value in allowed:
                        accepted.append(candidate)
                elif descriptor:
                    accepted.append(candidate)
            if not accepted:
                raise StudioServiceError(
                    f"{field} 不受目标供应商支持",
                    data={"field": field, "allowed": sorted(legal)},
                )
            candidates = accepted

        seed = payload.get("seed")
        if seed is None:
            payload["candidate_id"] = str(candidates[0].id)
            return
        accepted = []
        minimums: list[int] = []
        maximums: list[int] = []
        for candidate in candidates:
            descriptor = (
                candidate_capability(candidate).get("parameters", {}).get("seed", {})
            )
            if descriptor.get("type") != "integer":
                continue
            minimum = descriptor.get("minimum")
            maximum = descriptor.get("maximum")
            if isinstance(minimum, int):
                minimums.append(minimum)
                if seed < minimum:
                    continue
            if isinstance(maximum, int):
                maximums.append(maximum)
                if seed > maximum:
                    continue
            accepted.append(candidate)
        if not accepted:
            data: dict[str, Any] = {"field": "seed"}
            if minimums:
                data["minimum"] = min(minimums)
            if maximums:
                data["maximum"] = max(maximums)
            raise StudioServiceError("seed 不受目标供应商支持", data=data)
        payload["candidate_id"] = str(accepted[0].id)

    async def _run_single(self, job_id: str, payload: dict[str, Any]) -> None:
        try:
            await self._execute_generation(job_id, payload)
        except asyncio.CancelledError:
            await self.tracker.update(
                job_id, status="interrupted", error="工作台服务关闭，任务已中断"
            )
            raise

    async def _run_batch(
        self,
        parent_job_id: str,
        child_jobs: list[tuple[str, dict[str, Any]]],
    ) -> None:
        semaphore = asyncio.Semaphore(
            max(int(getattr(self.config, "batch_concurrency", 3)), 1)
        )

        async def run_child(job_id: str, payload: dict[str, Any]) -> None:
            async with semaphore:
                await self._execute_generation(job_id, payload)

        try:
            await asyncio.gather(
                *(run_child(job_id, payload) for job_id, payload in child_jobs)
            )
            records = [self.tracker.get(job_id) for job_id, _ in child_jobs]
            generated = sum(
                int(record.get("generated_images") or 0) for record in records if record
            )
            statuses = {record.get("status") for record in records if record}
            if statuses == {"succeeded"}:
                status = "succeeded"
            elif generated:
                status = "partial_success"
            else:
                status = "failed"
            if status == "failed":
                await self.tracker.fail(parent_job_id, error="所有批量任务均生成失败")
            else:
                await self.tracker.complete(
                    parent_job_id,
                    image_files=[],
                    text_content="批量生成完成",
                    stats={},
                    status=status,
                )
                await self.tracker.update(
                    parent_job_id,
                    generated_images=generated,
                )
            self._log_job_outcome(parent_job_id)
        except asyncio.CancelledError:
            await self.tracker.update(
                parent_job_id,
                status="interrupted",
                error="工作台服务关闭，任务已中断",
            )
            for child_job_id, _ in child_jobs:
                record = self.tracker.get(child_job_id)
                if record and record.get("status") == "running":
                    await self.tracker.update(
                        child_job_id,
                        status="interrupted",
                        error="工作台服务关闭，任务已中断",
                    )
                    self._log_job_outcome(child_job_id)
            self._log_job_outcome(parent_job_id)
            raise
        except Exception as exc:
            await self.tracker.fail(parent_job_id, error=exc)
            self._log_job_outcome(parent_job_id)

    async def _execute_generation(
        self,
        job_id: str,
        payload: dict[str, Any],
    ) -> None:
        target = payload["image_count"]
        collected: list[str] = []
        text_parts: list[str] = []
        last_stats: dict[str, Any] = {}
        source_candidates: dict[str, str | None] = {}
        error: str | None = None
        try:
            while len(collected) < target:
                remaining = target - len(collected)
                request_config = ApiRequestConfig(
                    model="",
                    prompt=payload["prompt"],
                    resolution=payload.get("resolution"),
                    aspect_ratio=payload.get("aspect_ratio"),
                    reference_images=payload.get("reference_images") or None,
                    enable_smart_retry=bool(
                        getattr(self.config, "enable_smart_retry", True)
                    ),
                    image_input_mode="force_base64",
                    requested_provider=payload.get("provider"),
                    requested_model=payload.get("model"),
                    requested_candidate_id=payload.get("candidate_id"),
                    negative_prompt=payload.get("negative_prompt"),
                    quality=payload.get("quality"),
                    seed=payload.get("seed"),
                    image_count=remaining,
                )
                async with self._api_semaphore:
                    result = await self.api_client.generate_image(
                        config=request_config,
                        max_retries=_positive_int(
                            getattr(self.config, "max_attempts_per_key", 3), 3
                        ),
                        per_retry_timeout=_positive_int(
                            getattr(self.config, "total_timeout", 120), 120
                        ),
                        max_total_time=_positive_int(
                            getattr(self.config, "total_timeout", 120), 120
                        ),
                    )
                image_urls, image_paths, text_content, _signature = result
                returned = [
                    str(item)
                    for item in (image_urls or []) + (image_paths or [])
                    if item
                ]
                new_images = [item for item in returned if item not in collected]
                if not new_images:
                    error = "供应商未返回新的图片"
                    break
                collected.extend(new_images[:remaining])
                source_candidates.update(
                    dict.fromkeys(
                        new_images[:remaining], request_config.successful_candidate_id
                    )
                )
                if text_content:
                    text_parts.append(str(text_content))
                last_stats = {
                    "provider": request_config.successful_provider,
                    "model": request_config.successful_model,
                    "alias": request_config.successful_model_alias,
                    "retry_count": request_config.retry_count,
                }
                await self.tracker.update(
                    job_id,
                    generated_images=len(collected),
                    stats=last_stats,
                )
        except asyncio.CancelledError:
            await self.tracker.update(
                job_id,
                status="interrupted",
                error="工作台服务关闭，任务已中断",
            )
            self._log_job_outcome(job_id)
            raise
        except Exception as exc:
            error = str(exc) or type(exc).__name__

        try:
            archived = await self.archive_sources(
                collected, candidate_ids=source_candidates, job_id=job_id
            )
        except StudioServiceError as exc:
            archived = []
            error = exc.message
        if not archived:
            await self.tracker.fail(job_id, error=error or "生成结果归档失败")
            self._log_job_outcome(job_id)
            return
        status = "succeeded" if len(archived) >= target else "partial_success"
        await self.tracker.complete(
            job_id,
            image_files=archived[:target],
            text_content="\n".join(text_parts) or None,
            stats=last_stats,
            status=status,
        )
        if error:
            await self.tracker.update(job_id, error=error)
        self._log_job_outcome(job_id)

    def _log_job_outcome(self, job_id: str) -> None:
        record = self.tracker.get(job_id)
        if record is None:
            return
        status = str(record.get("status") or "未知")
        stats = record.get("stats") or {}
        params = record.get("params") or {}
        provider = stats.get("provider") or params.get("provider") or "未记录"
        model = stats.get("model") or params.get("model") or "未记录"
        outcome = "失败" if status in {"failed", "interrupted"} else "完成"
        logger.info(
            f"[WebUI] 工作台任务{outcome}: job_id={job_id}, 状态={status}, "
            f"耗时={record.get('duration_ms') or 0}ms, "
            f"产出张数={record.get('generated_images') or 0}, "
            f"供应商={provider}, 模型={model}"
        )

    async def archive_images(
        self,
        image_urls: list[str] | None,
        image_paths: list[str] | None,
        *,
        candidate_id: str | None = None,
        job_id: str | None = None,
    ) -> list[str]:
        sources = [
            str(item) for item in (image_urls or []) + (image_paths or []) if item
        ]
        return await self.archive_sources(
            sources,
            candidate_ids=dict.fromkeys(sources, candidate_id),
            job_id=job_id,
        )

    # 启动时纳入管理的历史生成图前缀（其余前缀仍按缓存处理）
    _LEGACY_IMAGE_PREFIXES = ("gemini_image_", "gemini_advanced_image_")

    def import_legacy_images(self) -> int:
        """把 images/ 发送目录中的存量生成图迁入 gallery 并补录历史记录。

        同步方法，仅在插件初始化期调用；已归档内容按 尺寸+SHA256 去重，
        无法解码的文件按旧清理语义直接删除。
        """
        images_dir = self.data_dir / "images"
        if not images_dir.is_dir():
            return 0
        try:
            candidates = sorted(
                (
                    path
                    for path in images_dir.iterdir()
                    if path.is_file()
                    and path.name.startswith(self._LEGACY_IMAGE_PREFIXES)
                ),
                key=lambda path: path.stat().st_mtime,
            )
        except OSError:
            return 0
        if not candidates:
            return 0

        # gallery 现有文件按尺寸分组，尺寸命中再比哈希
        hashes_by_size: dict[int, set[str]] = {}
        if self.gallery_dir.is_dir():
            for existing in self.gallery_dir.iterdir():
                try:
                    if not existing.is_file():
                        continue
                    size = existing.stat().st_size
                except OSError:
                    continue
                hashes_by_size.setdefault(size, set()).add(
                    hashlib.sha256(existing.read_bytes()).hexdigest()
                )

        self.gallery_dir.mkdir(parents=True, exist_ok=True)
        records: list[dict[str, Any]] = []
        for path in candidates:
            try:
                data = path.read_bytes()
                file_mtime = path.stat().st_mtime
            except OSError:
                continue
            try:
                extension = self._validate_image_bytes(data)
            except StudioServiceError:
                path.unlink(missing_ok=True)
                continue
            digest = hashlib.sha256(data).hexdigest()
            if digest in hashes_by_size.get(len(data), set()):
                # 上一会话已归档过同内容图片，仅移除发送目录冗余副本
                path.unlink(missing_ok=True)
                continue
            name = self._gallery_name(extension)
            try:
                os.replace(path, self.gallery_dir / name)
            except OSError as exc:
                logger.warning(f"[WebUI] 历史图片迁移失败 {path.name}: {exc}")
                continue
            hashes_by_size.setdefault(len(data), set()).add(digest)
            created_at = _timestamp(datetime.fromtimestamp(file_mtime, tz=UTC))
            records.append(
                {
                    "job_id": f"legacy-{uuid.uuid4().hex[:12]}",
                    "parent_job_id": None,
                    "item_name": None,
                    "source": "legacy",
                    "status": "succeeded",
                    "prompt": "",
                    "params": {},
                    "requester": {"user_id": "", "user_name": "", "group_id": ""},
                    "created_at": created_at,
                    "finished_at": created_at,
                    "duration_ms": 0,
                    "requested_images": 1,
                    "generated_images": 1,
                    "images": [name],
                    "text_content": "",
                    "error": None,
                    "stats": {},
                }
            )
        imported = self.tracker.import_legacy(records)
        if imported:
            logger.info(f"[WebUI] 已把 {imported} 张历史生成图纳入画廊管理")
        return imported

    async def archive_sources(
        self,
        sources: list[str],
        *,
        candidate_ids: dict[str, str | None] | None = None,
        job_id: str | None = None,
    ) -> list[str]:
        async with self._gallery_lock:
            return await self._archive_sources(sources, candidate_ids or {}, job_id)

    async def _archive_sources(
        self,
        sources: list[str],
        candidate_ids: dict[str, str | None],
        job_id: str | None,
    ) -> list[str]:
        self.gallery_dir.mkdir(parents=True, exist_ok=True)
        archived: list[str] = []
        seen: set[str] = set()
        for source in sources:
            if source in seen:
                continue
            seen.add(source)
            try:
                if source.startswith(("http://", "https://")):
                    data = await self._download_remote_image(
                        source, candidate_id=candidate_ids.get(source)
                    )
                else:
                    path = Path(source[8:] if source.startswith("file:///") else source)
                    data = await asyncio.to_thread(path.read_bytes)
                extension = await asyncio.to_thread(self._validate_image_bytes, data)
                name = self._gallery_name(extension)
                await asyncio.to_thread(
                    self._write_atomic, self.gallery_dir / name, data
                )
                archived.append(name)
            except Exception as exc:
                logger.warning(f"[WebUI] gallery 归档失败: {exc}")
        if not await self._enforce_gallery_quota_locked(set(archived)):
            await asyncio.to_thread(self.tracker._delete_gallery_files, set(archived))
            raise StudioServiceError("画廊空间不足以保存本次结果", status_code=507)
        # 归档与 complete 之间仍可能让出执行权，先让运行中记录持有文件。
        if job_id:
            await self.tracker.update(job_id, images=archived)
        logger.info(
            f"[WebUI] gallery 归档完成: 张数={len(archived)}, 文件名列表={archived}"
        )
        return archived

    async def _download_remote_image(
        self, url: str, *, candidate_id: str | None = None
    ) -> bytes:
        timeout = aiohttp.ClientTimeout(total=10)
        data = bytearray()
        candidate = next(
            (
                candidate
                for candidate in getattr(self.config, "provider_candidates", []) or []
                if candidate.id == candidate_id
            ),
            None,
        )
        settings = getattr(candidate, "settings", None) or {}
        proxy = (
            getattr(candidate, "proxy", None)
            or settings.get("proxy")
            or getattr(self.api_client, "_default_proxy", None)
            or getattr(self.api_client, "proxy", None)
            or getattr(self.config, "proxy", None)
        )
        connector = None
        http_proxy = proxy
        if proxy and proxy.lower().startswith("socks"):
            from aiohttp_socks import ProxyConnector

            connector = ProxyConnector.from_url(proxy)
            http_proxy = None
        async with aiohttp.ClientSession(
            timeout=timeout, connector=connector
        ) as session:
            async with session.get(url, proxy=http_proxy) as response:
                response.raise_for_status()
                content_length = response.content_length
                if content_length and content_length > _REMOTE_IMAGE_MAX_BYTES:
                    raise StudioServiceError("远程图片超过 20MB")
                async for chunk in response.content.iter_chunked(_CHUNK_SIZE):
                    data.extend(chunk)
                    if len(data) > _REMOTE_IMAGE_MAX_BYTES:
                        raise StudioServiceError("远程图片超过 20MB")
        return bytes(data)

    @staticmethod
    def _validate_image_bytes(data: bytes) -> str:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            extension = "png"
        elif data.startswith(b"\xff\xd8\xff"):
            extension = "jpg"
        elif data.startswith((b"GIF87a", b"GIF89a")):
            extension = "gif"
        elif data.startswith(b"BM"):
            extension = "bmp"
        elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            extension = "webp"
        else:
            raise StudioServiceError("不支持的图片格式")
        try:
            with Image.open(io.BytesIO(data)) as header:
                width, height = header.size
                if width > 8000 or height > 8000:
                    raise StudioServiceError("图片像素尺寸不能超过 8000×8000")
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            Image.DecompressionBombError,
        ) as exc:
            raise StudioServiceError("图片内容无法解码或像素尺寸超限") from exc
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if image is None or image.size == 0:
            raise StudioServiceError("图片内容无法解码")
        height, width = image.shape[:2]
        if width > 8000 or height > 8000:
            raise StudioServiceError("图片像素尺寸不能超过 8000×8000")
        return extension

    @staticmethod
    def _gallery_name(extension: str) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"gemini_studio_{stamp}_{uuid.uuid4().hex[:12]}.{extension}"

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_bytes(data)
        os.replace(temp, path)

    async def enforce_gallery_quota(self) -> None:
        async with self._gallery_lock:
            await self._enforce_gallery_quota_locked(set())

    async def _enforce_gallery_quota_locked(self, protected: set[str]) -> bool:
        try:
            maximum_mb = float(getattr(self.config, "webui_gallery_max_size_mb", 512))
        except (TypeError, ValueError):
            maximum_mb = 512
        if maximum_mb <= 0 or not self.gallery_dir.is_dir():
            return True
        maximum = int(maximum_mb * 1024 * 1024)
        records = self.tracker.records_snapshot()
        return await asyncio.to_thread(
            self._enforce_gallery_quota_sync, maximum, records, protected
        )

    def _enforce_gallery_quota_sync(
        self,
        maximum: int,
        records: list[dict[str, Any]],
        protected: set[str] | None = None,
    ) -> bool:
        with self.tracker.gallery_lock:
            return self._enforce_gallery_quota_files(
                maximum, records, set(protected or ())
            )

    def _enforce_gallery_quota_files(
        self, maximum: int, records: list[dict[str, Any]], protected: set[str]
    ) -> bool:
        files = {
            path.name: path
            for path in self.gallery_dir.iterdir()
            if path.is_file() and not path.is_symlink()
        }
        cache_dir = self.gallery_dir / ".thumbs"
        thumbnails = {}
        if cache_dir.is_dir() and not cache_dir.is_symlink():
            thumbnails = {
                path.name: path for path in cache_dir.iterdir() if path.is_file()
            }
        sizes: dict[Path, int] = {}
        mtimes: dict[Path, float] = {}
        for path in [*files.values(), *thumbnails.values()]:
            try:
                stat = path.lstat()
                sizes[path] = stat.st_size
                mtimes[path] = stat.st_mtime
            except OSError:
                continue
        total = sum(sizes.values())
        original_total = total

        def remove(path: Path) -> None:
            nonlocal total
            try:
                path.unlink(missing_ok=True)
            except OSError:
                return
            total -= sizes.pop(path, 0)

        # 缓存可重建，先清孤儿和超额缩略图，避免只浏览画廊就挤掉原图。
        for name, path in sorted(
            thumbnails.items(), key=lambda item: mtimes.get(item[1], 0)
        ):
            if name.removesuffix(".jpg") not in files or total > maximum:
                remove(path)

        groups: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            group_id = str(record.get("parent_job_id") or record.get("job_id") or "")
            groups.setdefault(group_id, []).append(record)
        ordered = sorted(
            groups.values(),
            key=lambda group: str(group[0].get("created_at") or ""),
        )
        removed: set[str] = set()
        removed_groups = 0
        for group in ordered:
            names = {
                str(name) for record in group for name in record.get("images") or []
            }
            if any(record.get("status") == "running" for record in group):
                protected.update(names)
        for group in ordered:
            if total <= maximum:
                break
            names = {
                str(name) for record in group for name in record.get("images") or []
            }
            if names & protected:
                continue
            removed_before_group = len(removed)
            for record in group:
                for name in record.get("images") or []:
                    path = files.get(str(name))
                    if path is None:
                        continue
                    remove(path)
                    thumbnail = thumbnails.get(f"{name}.jpg")
                    if thumbnail is not None:
                        remove(thumbnail)
                    removed.add(str(name))
            if len(removed) > removed_before_group:
                removed_groups += 1

        if total > maximum:
            remaining = sorted(
                (
                    path
                    for name, path in files.items()
                    if name not in removed | protected
                ),
                key=lambda path: mtimes.get(path, 0),
            )
            for path in remaining:
                if total <= maximum:
                    break
                remove(path)
                thumbnail = thumbnails.get(f"{path.name}.jpg")
                if thumbnail is not None:
                    remove(thumbnail)
        released = min(max(original_total - total, 0), original_total)
        if released:
            logger.info(
                f"[WebUI] gallery 容量淘汰完成: 释放量={released} 字节, "
                f"删除组数={removed_groups}"
            )
        return total <= maximum

    async def save_uploads(self, files: list[Any]) -> list[str]:
        try:
            async with self._upload_lock:
                return await self._save_uploads(files)
        finally:
            await self._close_uploads(files)

    async def _save_uploads(self, files: list[Any]) -> list[str]:
        if not files:
            logger.info("[WebUI] 上传被拒绝: 原因=未找到上传文件")
            raise StudioServiceError("未找到上传文件")
        if len(files) > 4:
            logger.info("[WebUI] 上传被拒绝: 原因=单次最多上传 4 个文件")
            raise StudioServiceError("单次最多上传 4 个文件")
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._cleanup_uploads_sync)
        saved: list[str] = []
        total_size = 0
        try:
            for upload in files:
                server_stem = f"upload-{uuid.uuid4().hex}"
                try:
                    data = await self._read_upload(upload)
                    extension = await asyncio.to_thread(
                        self._validate_image_bytes, data
                    )
                except StudioServiceError as exc:
                    logger.debug(
                        f"[WebUI] 上传文件校验未通过: 文件名={server_stem}, "
                        f"原因={exc.message}"
                    )
                    raise
                name = f"{server_stem}.{extension}"
                logger.debug(
                    f"[WebUI] 上传文件校验通过: 文件名={name}, "
                    f"大小={len(data)} 字节, 格式={extension}"
                )
                await asyncio.to_thread(
                    self._write_atomic, self.upload_dir / name, data
                )
                saved.append(name)
                total_size += len(data)
            await asyncio.to_thread(self._enforce_upload_quota_sync, set(saved))
            logger.info(
                f"[WebUI] 上传已受理: 文件数={len(saved)}, 总大小={total_size} 字节"
            )
            return saved
        except StudioServiceError as exc:
            for name in saved:
                (self.upload_dir / name).unlink(missing_ok=True)
            logger.info(f"[WebUI] 上传被拒绝: 原因={exc.message}")
            raise
        except BaseException:
            for name in saved:
                (self.upload_dir / name).unlink(missing_ok=True)
            raise

    async def _read_upload(self, upload: Any) -> bytes:
        maximum = (
            max(
                int(getattr(self.config, "webui_upload_max_mb", 20)),
                1,
            )
            * 1024
            * 1024
        )
        content_length = getattr(upload, "content_length", None)
        if content_length is not None:
            try:
                if int(content_length) > maximum:
                    raise StudioServiceError("上传文件超过大小限制")
            except (TypeError, ValueError):
                pass
        data = bytearray()
        while len(data) <= maximum:
            chunk = await upload.read(min(_CHUNK_SIZE, maximum + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > maximum:
            raise StudioServiceError("上传文件超过大小限制")
        return bytes(data)

    @staticmethod
    async def _close_uploads(files: list[Any]) -> None:
        for upload in files:
            try:
                await upload.close()
            except Exception:
                pass

    def _cleanup_uploads_sync(self) -> None:
        if not self.upload_dir.is_dir():
            return
        cutoff = time.time() - timedelta(hours=_UPLOAD_EXPIRE_HOURS).total_seconds()
        for path in self.upload_dir.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def _enforce_upload_quota_sync(self, protected: set[str]) -> None:
        files = sorted(
            (path for path in self.upload_dir.iterdir() if path.is_file()),
            key=lambda path: path.stat().st_mtime,
        )
        total = sum(path.stat().st_size for path in files)
        for path in files:
            if total <= _UPLOAD_QUOTA_BYTES:
                break
            if path.name in protected:
                continue
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            total -= size
        if total > _UPLOAD_QUOTA_BYTES:
            raise StudioServiceError("上传暂存目录已达到容量上限", status_code=507)

    def gallery_file(self, name: str) -> Path:
        if not isinstance(name, str) or not _SAFE_FILE_NAME.fullmatch(name):
            raise StudioServiceError("非法图片文件名")
        root = self.gallery_dir.resolve()
        path = (self.gallery_dir / name).resolve()
        if path.parent != root:
            raise StudioServiceError("非法图片文件名")
        if not path.is_file():
            raise StudioServiceError("图片不存在或已清理", status_code=404)
        return path

    async def gallery_image_base64(
        self,
        name: str,
        *,
        thumbnail: bool = False,
    ) -> dict[str, str]:
        async with self._gallery_lock:
            payload = await asyncio.to_thread(
                self._gallery_image_base64_sync, name, thumbnail
            )
            if thumbnail:
                await self._enforce_gallery_quota_locked({name})
            return payload

    def _gallery_image_base64_sync(
        self,
        name: str,
        thumbnail: bool,
    ) -> dict[str, str]:
        with self.tracker.gallery_lock:
            return self._gallery_image_base64_locked(name, thumbnail)

    def _gallery_image_base64_locked(
        self, name: str, thumbnail: bool
    ) -> dict[str, str]:
        path = self.gallery_file(name)
        if thumbnail:
            raw = self._thumbnail_bytes(path)
            mime_type = "image/jpeg"
            limit = _THUMBNAIL_B64_MAX_BYTES
            error_message = "缩略图响应超过 2MB 限制"
        else:
            limit = _ORIGINAL_B64_MAX_BYTES
            error_message = "原图响应超过 8MB 限制，请使用下载功能"
            try:
                with path.open("rb") as stream:
                    raw = stream.read(limit // 4 * 3 + 1)
            except OSError as exc:
                raise StudioServiceError(
                    "图片读取失败",
                    status_code=500,
                ) from exc
            mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

        if ((len(raw) + 2) // 3) * 4 > limit:
            raise StudioServiceError(error_message, status_code=413)
        encoded = base64.b64encode(raw).decode("ascii")
        if len(encoded) > limit:
            raise StudioServiceError(error_message, status_code=413)
        return {"mime": mime_type, "b64": encoded}

    def _thumbnail_bytes(self, source: Path) -> bytes:
        try:
            source_stat = source.stat()
        except OSError as exc:
            raise StudioServiceError("图片不存在或已清理", status_code=404) from exc

        cache_dir = self.gallery_dir / ".thumbs"
        cache_path = cache_dir / f"{source.name}.jpg"
        if cache_dir.is_symlink() or cache_path.is_symlink():
            raise StudioServiceError("非法缩略图缓存路径", status_code=400)
        try:
            cache_stat = cache_path.stat()
            if cache_stat.st_mtime_ns == source_stat.st_mtime_ns:
                cached = cache_path.read_bytes()
                if cached:
                    return cached
        except OSError:
            pass

        try:
            source_bytes = source.read_bytes()
        except OSError as exc:
            raise StudioServiceError("图片读取失败", status_code=500) from exc
        image = cv2.imdecode(
            np.frombuffer(source_bytes, dtype=np.uint8),
            cv2.IMREAD_UNCHANGED,
        )
        if image is None or image.size == 0:
            raise StudioServiceError("图片内容无法生成缩略图", status_code=422)
        if image.ndim == 3 and image.shape[2] == 4:
            image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
        height, width = image.shape[:2]
        longest = max(height, width)
        if longest > _THUMBNAIL_MAX_EDGE:
            scale = _THUMBNAIL_MAX_EDGE / longest
            image = cv2.resize(
                image,
                (max(round(width * scale), 1), max(round(height * scale), 1)),
                interpolation=cv2.INTER_AREA,
            )
        try:
            encoded_ok, encoded = cv2.imencode(
                ".jpg",
                image,
                [cv2.IMWRITE_JPEG_QUALITY, _THUMBNAIL_JPEG_QUALITY],
            )
        except cv2.error as exc:
            raise StudioServiceError(
                "图片内容无法生成缩略图",
                status_code=422,
            ) from exc
        if not encoded_ok:
            raise StudioServiceError("图片内容无法生成缩略图", status_code=422)
        data = encoded.tobytes()

        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            self._write_atomic(cache_path, data)
            os.utime(
                cache_path,
                ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
            )
        except OSError as exc:
            logger.warning(f"[WebUI] 缩略图缓存写入失败 {source.name}: {exc}")
        return data

    def capabilities(self) -> dict[str, Any]:
        # 扁平候选列表：一条 provider_candidates 配置 = 一个可选项，
        # 前端一个下拉直接选「供应商+模型」，不做供应商级联去重。
        models = []
        for index, candidate in enumerate(
            getattr(self.config, "provider_candidates", []) or []
        ):
            name = str(getattr(candidate, "api_type", "") or "")
            if not name:
                continue
            capability = candidate_capability(candidate)
            descriptors = capability.get("parameters") or {}
            parameters = {}
            for key, descriptor in descriptors.items():
                if key not in {
                    "resolution",
                    "aspect_ratio",
                    "image_count",
                    "negative_prompt",
                    "watermark",
                    "quality",
                    "seed",
                }:
                    continue
                parameters[key] = {
                    allowed_key: descriptor[allowed_key]
                    for allowed_key in ("type", "enum", "minimum", "maximum")
                    if allowed_key in descriptor
                }
            models.append(
                {
                    "id": str(getattr(candidate, "id", "") or f"candidate-{index}"),
                    "candidate_id": str(
                        getattr(candidate, "id", "") or f"candidate-{index}"
                    ),
                    "max_reference_images": candidate_reference_limit(candidate),
                    "provider": name,
                    "provider_display": _DISPLAY_NAMES.get(name, name),
                    "model": str(getattr(candidate, "model", "") or ""),
                    "model_alias": str(getattr(candidate, "model_alias", "") or "")
                    or None,
                    "resolutions": list(
                        (descriptors.get("resolution") or {}).get("enum") or []
                    ),
                    "aspect_ratios": list(
                        (descriptors.get("aspect_ratio") or {}).get("enum") or []
                    ),
                    "parameters": parameters,
                }
            )
        logger.debug(f"[WebUI] capabilities 计算完成: 模型数={len(models)}")
        return {
            "models": models,
            "limits": {
                "batch_total_budget": max(
                    int(getattr(self.config, "webui_batch_total_budget", 40)), 1
                ),
                "upload_max_mb": max(
                    int(getattr(self.config, "webui_upload_max_mb", 20)), 1
                ),
                "batch_max_tasks": max(
                    int(getattr(self.config, "batch_max_tasks", 20)), 1
                ),
            },
        }

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = list(self._runtime_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._runtime_tasks.clear()
        self._admitted_jobs = 0
