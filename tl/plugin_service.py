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
from .file_uri import file_uri_to_path
from .generation_scheduler import generation_progress, generation_reservation
from .generation_tracker import tracking_context
from .limit_config import validate_umo
from .provider_capabilities import (
    candidate_capability,
    candidate_reference_limit,
    routing_mode,
    select_candidates,
)


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

    def __init__(self, plugin):
        self.plugin = plugin
        self.instance_id = uuid.uuid4().hex
        self._state = "initializing"
        self._callbacks = {}
        self._callback_tasks = {}
        self._events = {}
        self._tasks = {}
        self._started = {}
        self._submissions = set()

    def initialized(self):
        self._state = "ready"

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
        self._admit()
        ticket = self.plugin.generation_scheduler.reserve()
        token = None
        record = None
        try:
            if use_rate_limit:
                decision = await self.plugin.rate_limiter.acquire(
                    umo, plugin_id=plugin_id
                )
                if not decision.allowed:
                    raise PluginServiceError(
                        "rate_limited", decision.message or "请求超过限流额度"
                    )
                token = decision.token
            # Recheck lifecycle after asynchronous quota/storage operations.
            if self._state != "ready" or self.plugin.provider_runtime.closed:
                raise PluginServiceError("service_closed", "服务正在关闭")
            manager = self.plugin.background_task_manager
            record = await manager.create(
                session_id=umo or "",
                kind="plugin",
                plugin_id=plugin_id,
                routing_mode=routing_mode(provider, model),
                message="任务已接收",
            )
            task_id = record["task_id"]
            await manager.update(
                task_id,
                status="queued",
                caller={"plugin_id": plugin_id, "plugin_name": plugin_name},
                requester=requester,
                requested_images=image_count,
                generated_images=0,
                image_urls=[],
                image_paths=[],
                callback_status="pending" if on_complete else "none",
            )
            if self._state != "ready" or self.plugin.provider_runtime.closed:
                raise PluginServiceError("service_closed", "服务正在关闭")
            if on_complete:
                self._callbacks[task_id] = on_complete
            request = dict(
                parameters,
                prompt=prompt,
                image_count=image_count,
                watermark=watermark,
                reference_images=refs,
                requester=requester,
                caller={"plugin_id": plugin_id, "plugin_name": plugin_name},
            )
            task = manager.attach(task_id, self._run(task_id, request, ticket))
            self._started[task_id] = asyncio.Event()
            self._tasks[task_id] = task

            def finished(done):
                self._tasks.pop(task_id, None)
                self._started.pop(task_id, None)

            task.add_done_callback(finished)
            return {
                "task_id": task_id,
                "status": "queued",
                "instance_id": self.instance_id,
            }
        except BaseException:
            ticket.release()
            await self.plugin.rate_limiter.refund(token)
            if record:
                await self.plugin.background_task_manager.update(
                    record["task_id"], status="interrupted", message="任务提交未完成"
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
                ref = str(path)
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
        return record

    async def wait_task(
        self, task_id: str, *, plugin_id: str, timeout: float | None = None
    ):
        if timeout is None:
            timeout = self.plugin.cfg.total_timeout
        record = await self.get_task(task_id, plugin_id=plugin_id)
        if record["status"] not in TERMINAL_STATUSES:
            event = self._events.setdefault(task_id, asyncio.Event())
            await asyncio.wait_for(event.wait(), timeout)
        return await self.get_task(task_id, plugin_id=plugin_id)

    async def cancel_task(self, task_id: str, *, plugin_id: str):
        record = await self.get_task(task_id, plugin_id=plugin_id)
        if record["status"] not in TERMINAL_STATUSES:
            task = self._tasks.get(task_id)
            if task:
                await self._started[task_id].wait()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
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

    async def _run(self, task_id, request, ticket):
        self._started[task_id].set()
        manager = self.plugin.background_task_manager
        tracker = self.plugin.generation_tracker
        job_id = None
        images, texts, stats = [], [], {}
        error = None
        status = "failed"
        try:
            if tracker.enabled:
                history = await tracker.begin(
                    source="plugin",
                    prompt=request["prompt"],
                    params=request,
                    requester=request["requester"],
                    caller=request["caller"],
                    requested_images=request["image_count"],
                    reference_names=[
                        Path(ref).name
                        for ref in request["reference_images"]
                        if Path(ref).parent == tracker.gallery_dir
                    ],
                )
                job_id = history["job_id"]
            await manager.update(task_id, job_id=job_id)

            async def progress(value):
                await manager.update(task_id, status=value)
                if job_id:
                    await tracker.update(job_id, status=value)

            with (
                generation_reservation(ticket),
                generation_progress(progress),
                tracking_context("plugin", managed_externally=True),
            ):
                while len(images) < request["image_count"]:
                    (
                        success,
                        result,
                    ) = await self.plugin.image_generator.generate_image_core(
                        event=None,
                        prompt=request["prompt"],
                        reference_images=request["reference_images"],
                        avatar_reference=[],
                        override_resolution=request["resolution"],
                        override_aspect_ratio=request["aspect_ratio"],
                        requested_provider=request["provider"],
                        requested_model=request["model"],
                        image_count=request["image_count"] - len(images),
                        is_tool_call=False,
                        negative_prompt=request["negative_prompt"],
                        watermark=request["watermark"],
                        quality=request["quality"],
                    )
                    stats = self.plugin.image_generator.get_request_stats()
                    if not success:
                        error = str(result)
                        break
                    urls, paths, text, _signature = result
                    fresh = [
                        value
                        for value in dict.fromkeys([*urls, *paths])
                        if value and value not in images
                    ]
                    if not fresh:
                        error = "供应商未返回新的图片"
                        break
                    images.extend(fresh[: request["image_count"] - len(images)])
                    if text:
                        texts.append(text)
            status = (
                "succeeded"
                if len(images) == request["image_count"]
                else ("partial_success" if images else "failed")
            )
        except asyncio.CancelledError:
            status, error = "interrupted", "任务已取消或服务已关闭"
        except Exception:
            logger.error(f"[插件接入] 任务 {task_id} 执行失败", exc_info=True)
            status = "partial_success" if images else "failed"
            error = "图像生成失败，请查看插件日志"
        finally:
            ticket.release()

        finish = asyncio.create_task(
            self._finish(task_id, job_id, images, texts, stats, status, error)
        )
        try:
            await asyncio.shield(finish)
        except asyncio.CancelledError:
            callback_task = self._callback_tasks.get(task_id)
            if callback_task:
                callback_task.cancel()
            await finish

    async def _finish(self, task_id, job_id, images, texts, stats, status, error):
        manager = self.plugin.background_task_manager
        tracker = self.plugin.generation_tracker
        urls = [value for value in images if value.startswith(("http://", "https://"))]
        paths = [value for value in images if value not in urls]
        archived = []
        if images and tracker.enabled and status != "interrupted":
            try:
                archived = await self.plugin.web_studio_service.archive_images(
                    urls,
                    paths,
                    job_id=job_id,
                    candidate_id=stats.get("successful_candidate_id"),
                )
            except Exception:
                logger.warning(f"[插件接入] 任务 {task_id} 图片归档失败", exc_info=True)
        # Prefer the archived local artifacts only when the entire result was preserved.
        if len(archived) == len(images) and archived:
            paths = [str(tracker.gallery_dir / name) for name in archived]
            urls = []
        try:
            # Publish the terminal task before optional history writes. Failed disk
            # writes must not hide usable images from callers in this process.
            result = await manager.update(
                task_id,
                best_effort=True,
                status=status,
                message=error or "生成完成",
                error=error,
                generated_images=len(images),
                image_urls=urls,
                image_paths=paths,
                text_content="\n".join(texts),
                stats=stats,
            )
            await self._update_history(
                job_id,
                status=status,
                generated_images=len(images),
                images=archived,
                source_urls=[
                    v for v in images if v.startswith(("http://", "https://"))
                ],
                text_content="\n".join(texts),
                stats=stats,
                error=error,
                callback_status=result.get("callback_status", "none"),
            )
        finally:
            event = self._events.pop(task_id, None)
            if event:
                event.set()
        callback = self._callbacks.pop(task_id, None)
        if callback and self._state == "ready":
            await manager.update(task_id, best_effort=True, callback_status="running")
            await self._update_history(job_id, callback_status="running")
            callback_task = asyncio.create_task(callback(copy.deepcopy(result)))
            self._callback_tasks[task_id] = callback_task
            callback_status = "succeeded"
            try:
                await asyncio.wait_for(callback_task, 30)
            except asyncio.CancelledError:
                callback_status = "interrupted"
            except Exception:
                callback_status = "failed"
                logger.warning(f"[插件接入] 任务 {task_id} 完成回调失败", exc_info=True)
            finally:
                self._callback_tasks.pop(task_id, None)
            await manager.update(
                task_id, best_effort=True, callback_status=callback_status
            )
            await self._update_history(job_id, callback_status=callback_status)
        elif callback:
            await manager.update(
                task_id, best_effort=True, callback_status="interrupted"
            )
            await self._update_history(job_id, callback_status="interrupted")

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
        await asyncio.gather(*(event.wait() for event in list(self._started.values())))
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._callbacks.clear()
        for event in self._events.values():
            event.set()
        self._events.clear()
        self._state = "closed"
