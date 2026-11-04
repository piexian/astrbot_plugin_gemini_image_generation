"""Versioned, same-process image service for other AstrBot plugins."""

from __future__ import annotations

import asyncio
import copy
import inspect
import uuid
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from astrbot.api import logger

from .background_tasks import TERMINAL_STATUSES
from .core.errors import (
    CoreError,
    QueueFullError,
    QuotaExceededError,
    ServiceClosedError,
)
from .file_uri import file_uri_to_path
from .generation_tracker import without_session_origin
from .limit_config import validate_umo
from .provider_capabilities import (
    candidate_capability,
    candidate_reference_limit,
    routing_mode,
    select_candidates,
)
from .v2_bridge import build_request as v2_build_request
from .v2_sdk_delivery import SdkDelivery


class PluginServiceError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _submission(function):
    @wraps(function)
    async def call(self, *args, **kwargs):
        completed = asyncio.Event()
        self._submissions.add(completed)
        try:
            return await function(self, *args, **kwargs)
        finally:
            self._submissions.discard(completed)
            completed.set()

    return call


def _text(value, name, maximum=128, required=False):
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise PluginServiceError("invalid_request", f"{name} 必须是字符串")
    value = value.strip()
    if (
        (required and not value)
        or len(value) > maximum
        or any(ord(c) < 32 for c in value)
    ):
        raise PluginServiceError("invalid_request", f"{name} 为空、过长或含控制字符")
    return value


class ImageGenerationService:
    api_version = 1

    # SDK v1 固定能力标识：声明接口实现支持的能力，不代表账号权限或当前可执行。
    features = ("image.generate", "image.tasks", "image.callbacks")

    def __init__(self, plugin):
        self.plugin = plugin
        self.instance_id = uuid.uuid4().hex
        self._state = "initializing"
        self._callbacks = {}
        self._callback_tasks = {}
        self._events = {}
        self._v2_jobs = {}
        self._submissions = set()

    def initialized(self):
        self._state = "ready"

    def for_api_version(self, api_version: int = 1) -> ImageGenerationService:
        """按 SDK 约定协商版本并返回当前服务实例；只接受真正的 int 1。"""
        if type(api_version) is not int or api_version != 1:
            raise PluginServiceError("unsupported_version", "仅支持插件生图接口 v1")
        return self

    def get_status(self) -> dict[str, Any]:
        state, reason = self._state, None
        if state in {"closing", "closed"}:
            reason = "service_closed"
        runtime = self.plugin.provider_runtime
        scheduler = self.plugin.generation_scheduler
        if state == "ready":
            if runtime.closed:
                state, reason = "closing", "service_closing"
            elif runtime.failed:
                state, reason = "unavailable", "configuration_failed"
            elif runtime.updating:
                state, reason = "unavailable", "configuration_updating"
            elif not self.plugin.api_client or not self.plugin.cfg.provider_candidates:
                state, reason = "unavailable", "not_configured"
        accepting = state == "ready" and scheduler.accepting
        if state == "ready" and not accepting:
            reason = "service_closing" if scheduler.closed else "queue_full"
        return {
            "api_version": self.api_version,
            "instance_id": self.instance_id,
            "state": state,
            "ready": state == "ready",
            "accepting_tasks": accepting,
            "reason": reason,
            "active_requests": scheduler.active,
            "queued_requests": len(scheduler.waiters),
        }

    async def wait_ready(self, timeout: float | None = None):
        async with asyncio.timeout(timeout):
            while True:
                status = self.get_status()
                if status["ready"]:
                    return status
                if status["state"] in {"closing", "closed"}:
                    raise PluginServiceError(
                        "service_closed", "服务实例已关闭，请重新获取"
                    )
                # Config transactions can change readiness without replacing this facade.
                await asyncio.sleep(0.1)

    def capabilities(self):
        candidates = []
        for candidate in self.plugin.cfg.provider_candidates:
            capability = copy.deepcopy(candidate_capability(candidate))
            capability.pop("request_setting_map", None)
            candidates.append(
                {
                    "id": candidate.id,
                    "provider": candidate.api_type,
                    "model": candidate.model,
                    "alias": candidate.model_alias,
                    "reference_limit": candidate_reference_limit(candidate),
                    **capability,
                }
            )
        return {
            "api_version": self.api_version,
            "features": list(self.features),
            "max_images_per_task": self.plugin.cfg.batch_max_images_per_task,
            "candidates": candidates,
        }

    def _admit(self):
        status = self.get_status()
        if not status["accepting_tasks"]:
            raise PluginServiceError(
                status["reason"] or "not_ready", "生图服务暂不可接收任务"
            )

    @_submission
    async def submit(
        self,
        *,
        plugin_id: str,
        prompt: str,
        plugin_name: str = "",
        umo: str | None = None,
        requester: dict | None = None,
        use_rate_limit: bool = False,
        reference_images: list[str] | None = None,
        provider: str | None = None,
        model: str | None = None,
        image_count: int = 1,
        resolution: str | None = None,
        aspect_ratio: str | None = None,
        negative_prompt: str | None = None,
        watermark: bool | None = None,
        quality: str | None = None,
        on_complete=None,
    ) -> dict[str, Any]:
        self._admit()
        plugin_id = _text(plugin_id, "plugin_id", required=True)
        plugin_name = _text(plugin_name, "plugin_name", 200)
        if not isinstance(prompt, str) or not prompt.strip():
            raise PluginServiceError("invalid_request", "prompt 必须是非空字符串")
        if (
            type(image_count) is not int
            or not 1 <= image_count <= self.plugin.cfg.batch_max_images_per_task
        ):
            raise PluginServiceError("invalid_request", "image_count 超出允许范围")
        if type(use_rate_limit) is not bool or (
            watermark is not None and type(watermark) is not bool
        ):
            raise PluginServiceError("invalid_request", "限流和水印参数必须是布尔值")
        if umo is not None:
            try:
                umo = validate_umo(umo)
            except ValueError as exc:
                raise PluginServiceError("invalid_request", str(exc)) from exc
        if requester is not None and not isinstance(requester, dict):
            raise PluginServiceError("invalid_request", "requester 必须是对象")
        requester = {
            key: _text(value, key, 200)
            for key, value in (requester or {}).items()
            if key in {"user_id", "user_name", "group_id", "chat_type"}
        }
        requester["umo"] = umo or ""
        if on_complete is not None and not (
            inspect.iscoroutinefunction(on_complete)
            or inspect.iscoroutinefunction(getattr(on_complete, "__call__", None))
        ):
            raise PluginServiceError("invalid_request", "on_complete 必须是异步函数")
        parameters = {
            "provider": provider,
            "model": model,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "negative_prompt": negative_prompt,
            "quality": quality,
        }
        for name, value in parameters.items():
            if value is not None and not isinstance(value, str):
                raise PluginServiceError("invalid_request", f"{name} 必须是字符串")
        refs = await self._references(reference_images)
        required = {
            k
            for k, v in {
                "negative_prompt": negative_prompt,
                "watermark": watermark,
                "quality": quality,
            }.items()
            if v is not None
        }
        values = {"quality": quality} if quality is not None else {}
        if not select_candidates(
            self.plugin.cfg.provider_candidates,
            provider=provider,
            model=model,
            has_reference_images=bool(refs),
            required_parameters=required,
            request_values=values,
        ):
            raise PluginServiceError(
                "invalid_request", "没有符合供应商、模型和参数要求的候选"
            )
        ensure_v2 = getattr(self.plugin, "_ensure_v2_service", None)
        v2_service = ensure_v2() if callable(ensure_v2) else None
        if v2_service is None:
            # 过渡兼容：宿主尚未提供装配入口时回退读取按会话过滤的 v2 入口。
            get_v2_service = getattr(self.plugin, "_v2_service_for", None)
            v2_service = get_v2_service(umo) if callable(get_v2_service) else None
        if v2_service is None:
            raise PluginServiceError("not_configured", "生图服务暂不可接收任务")
        return await self._submit_v2(
            v2_service,
            plugin_id=plugin_id,
            plugin_name=plugin_name,
            umo=umo,
            requester=requester,
            use_rate_limit=use_rate_limit,
            refs=refs,
            parameters=parameters,
            prompt=prompt,
            image_count=image_count,
            watermark=watermark,
            on_complete=on_complete,
        )

    async def _submit_v2(
        self,
        v2_service: Any,
        *,
        plugin_id: str,
        plugin_name: str,
        umo: str | None,
        requester: dict[str, Any],
        use_rate_limit: bool,
        refs: list[str],
        parameters: dict[str, Any],
        prompt: str,
        image_count: int,
        watermark: bool | None,
        on_complete: Any,
    ) -> dict[str, Any]:
        delivery = SdkDelivery(self)
        total_timeout = max(int(getattr(self.plugin.cfg, "total_timeout", 120)), 1)
        quota = False
        if use_rate_limit:
            quota = (
                {"cost": 1, "scope": "session"}
                if umo
                else {"cost": 1, "scope": "plugin", "plugin_id": plugin_id}
            )
        try:
            references = await v2_service.prepare_references(
                refs, requester=requester, trusted_local=True
            )
            request = v2_build_request(
                source="plugin",
                prompt=prompt,
                resolution=parameters["resolution"],
                aspect_ratio=parameters["aspect_ratio"],
                provider=parameters["provider"],
                model=parameters["model"],
                reference_images=references,
                deadline_seconds=total_timeout * image_count,
                requester=requester,
                negative_prompt=parameters["negative_prompt"],
                quality=parameters["quality"],
                watermark=watermark,
                image_count=image_count,
                metadata={
                    "caller": {"plugin_id": plugin_id, "plugin_name": plugin_name},
                    "fill_image_count": True,
                    "require_images": True,
                    "enable_smart_retry": bool(
                        getattr(self.plugin.cfg, "enable_smart_retry", True)
                    ),
                    "max_attempts_per_key": max(
                        int(getattr(self.plugin.cfg, "max_attempts_per_key", 3)), 1
                    ),
                    "total_timeout": total_timeout,
                    "quota": quota,
                },
            )
            handle = await v2_service.submit(request, delivery=delivery)
        except CoreError as error:
            if isinstance(error, QuotaExceededError):
                raise PluginServiceError(
                    "rate_limited", error.message or "请求超过限流额度"
                ) from error
            if isinstance(error, QueueFullError):
                raise PluginServiceError("queue_full", error.message) from error
            if isinstance(error, ServiceClosedError):
                raise PluginServiceError("service_closed", error.message) from error
            raise PluginServiceError("invalid_request", error.message) from error

        record = None
        task_id = None
        try:
            if self._state != "ready" or self.plugin.provider_runtime.closed:
                raise PluginServiceError("service_closed", "服务正在关闭")
            manager = self.plugin.background_task_manager
            record = await manager.create(
                session_id=umo or "",
                kind="plugin",
                plugin_id=plugin_id,
                routing_mode=routing_mode(parameters["provider"], parameters["model"]),
                message="任务已接收",
            )
            task_id = record["task_id"]
            await manager.update(
                task_id,
                status="queued",
                job_id=handle.job_id,
                caller={"plugin_id": plugin_id, "plugin_name": plugin_name},
                requester=requester,
                requested_images=image_count,
                generated_images=0,
                image_urls=[],
                image_paths=[],
                callback_status="pending" if on_complete else "none",
            )
            if on_complete:
                self._callbacks[task_id] = on_complete
            self._v2_jobs[task_id] = (v2_service, handle.job_id)
            logger.info(
                f"[插件接入] v2 提交完成: plugin_id={plugin_id}, job_id={handle.job_id}, "
                f"task_id={task_id}, umo={umo}"
            )
            delivery.bind_record(task_id)
            return {
                "task_id": task_id,
                "status": "queued",
                "instance_id": self.instance_id,
            }
        except BaseException:
            delivery.bind_record(None)
            try:
                await asyncio.shield(v2_service.cancel(handle.job_id))
            except Exception:
                logger.warning("[插件接入] 回滚 v2 任务失败", exc_info=True)
            if task_id:
                self._v2_jobs.pop(task_id, None)
                self._callbacks.pop(task_id, None)
            if record:
                await manager.update(
                    record["task_id"],
                    best_effort=True,
                    status="interrupted",
                    message="任务提交未完成",
                )
            raise

    async def _references(self, refs):
        if refs is None:
            return []
        if not isinstance(refs, list) or any(
            not isinstance(ref, str) or not ref.strip() for ref in refs
        ):
            raise PluginServiceError(
                "invalid_request", "reference_images 必须是图片 URL 或本地路径列表"
            )
        result = []
        for ref in refs:
            ref = ref.strip()
            if ref.startswith(("http://", "https://")):
                if not urlsplit(ref).hostname:
                    raise PluginServiceError("invalid_request", "无效参考图 URL")
            else:
                path = Path(file_uri_to_path(ref) or ref)
                if not path.is_absolute() or not await asyncio.to_thread(path.is_file):
                    raise PluginServiceError(
                        "invalid_request", "参考图必须是存在的本地绝对路径或 HTTP URL"
                    )
                ref = path.as_uri()
            result.append(ref)
        return list(dict.fromkeys(result))

    async def get_task(self, task_id: str, *, plugin_id: str):
        if self._state in {"closing", "closed"}:
            raise PluginServiceError("service_closed", "服务实例已关闭，请重新获取")
        if _text(task_id, "task_id", required=True) != task_id:
            raise PluginServiceError(
                "invalid_request", "请使用提交时返回的完整 task_id"
            )
        plugin_id = _text(plugin_id, "plugin_id", required=True)
        record = await self.plugin.background_task_manager.get_for_plugin(
            task_id, plugin_id
        )
        if record is None:
            raise PluginServiceError("task_not_found", "任务不存在或已过期")
        return without_session_origin(record)

    async def wait_task(
        self, task_id: str, *, plugin_id: str, timeout: float | None = None
    ):
        record = await self.get_task(task_id, plugin_id=plugin_id)
        if timeout is None:
            try:
                requested_images = max(int(record.get("requested_images") or 1), 1)
            except (TypeError, ValueError):
                requested_images = 1
            timeout = self.plugin.cfg.total_timeout * requested_images
        if record["status"] not in TERMINAL_STATUSES:
            event = self._events.setdefault(task_id, asyncio.Event())
            await asyncio.wait_for(event.wait(), timeout)
        return await self.get_task(task_id, plugin_id=plugin_id)

    async def cancel_task(self, task_id: str, *, plugin_id: str):
        record = await self.get_task(task_id, plugin_id=plugin_id)
        if record["status"] not in TERMINAL_STATUSES:
            v2_job = self._v2_jobs.get(task_id)
            if v2_job:
                service, _ = v2_job
                await service.cancel(record.get("job_id") or v2_job[1])
        return await self.get_task(task_id, plugin_id=plugin_id)

    async def remove_callback(self, task_id: str, *, plugin_id: str):
        await self.get_task(task_id, plugin_id=plugin_id)
        removed = self._callbacks.pop(task_id, None) is not None
        if removed:
            record = await self.plugin.background_task_manager.update(
                task_id, callback_status="removed"
            )
            if record.get("job_id"):
                await self.plugin.generation_tracker.update(
                    record["job_id"], callback_status="removed"
                )
        return removed

    async def _update_history(self, job_id, **changes):
        if job_id:
            try:
                await self.plugin.generation_tracker.update(job_id, **changes)
            except Exception:
                logger.error(
                    f"[插件接入] 历史记录 {job_id} 更新失败，任务结果仍可查询",
                    exc_info=True,
                )

    async def close(self):
        self._state = "closing"
        await asyncio.gather(*(event.wait() for event in list(self._submissions)))
        pending_v2 = list(self._v2_jobs.values())
        if pending_v2:
            results = await asyncio.gather(
                *(service.cancel(job_id) for service, job_id in pending_v2),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, Exception):
                    logger.warning(f"[插件接入] 关闭 v2 任务失败: {result}")
        self._v2_jobs.clear()
        callbacks = [task for task in self._callback_tasks.values() if not task.done()]
        for task in callbacks:
            task.cancel()
        if callbacks:
            await asyncio.gather(*callbacks, return_exceptions=True)
        self._callback_tasks.clear()
        self._callbacks.clear()
        for event in self._events.values():
            event.set()
        self._events.clear()
        self._state = "closed"
