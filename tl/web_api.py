"""AstrBot 插件页 Web API 的薄适配层。"""

from __future__ import annotations

import asyncio
import json
import math
import mimetypes
import re
from collections.abc import Callable
from typing import Any

from astrbot.api import logger

from .generation_tracker import GenerationTracker
from .web_studio_service import StudioServiceError, WebStudioService

try:
    from astrbot.api.web import (
        error_response,
        file_response,
        json_response,
        request,
        stream_response,
    )

    WEB_API_AVAILABLE = True
except (ImportError, AttributeError):
    error_response = None
    file_response = None
    json_response = None
    request = None
    stream_response = None
    WEB_API_AVAILABLE = False

PLUGIN_NAME = "astrbot_plugin_gemini_image_generation"
WEBUI_PREFIX = f"/{PLUGIN_NAME}/webui"
_SAFE_FILE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def _valid_json_value(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_valid_json_value(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_valid_json_value(item) for item in value)
    return True


class WebStudioAPI:
    """将 tracker/service 映射为 AstrBot 标准信封和流式响应。"""

    ROUTES = (
        ("/jobs", "jobs", ["GET"], "WebUI 生成任务快照"),
        ("/jobs/stream", "jobs_stream", ["GET"], "WebUI 生成任务事件流"),
        ("/history", "history", ["GET"], "WebUI 生成历史"),
        ("/history/<job_id>", "history_detail", ["GET"], "WebUI 历史详情"),
        ("/history/delete", "delete_history", ["POST"], "删除生成历史"),
        ("/image/<name>", "image", ["GET"], "读取 gallery 图片"),
        ("/image_b64/<name>", "image_b64", ["GET"], "读取 gallery 图片字节"),
        ("/capabilities", "capabilities", ["GET"], "WebUI 供应商能力"),
        ("/generate", "generate", ["POST"], "WebUI 发起生成"),
        ("/upload", "upload", ["POST"], "WebUI 上传参考图"),
    )

    def __init__(
        self,
        tracker: GenerationTracker,
        service: WebStudioService,
        *,
        is_closed: Callable[[], bool] | None = None,
    ) -> None:
        self.tracker = tracker
        self.service = service
        self._web_closed = False
        self._is_closed = is_closed or (lambda: self._web_closed)
        self._registered_entries: list[tuple[Any, ...]] = []

    def register(self, context: Any) -> list[tuple[Any, ...]]:
        if not WEB_API_AVAILABLE:
            return []
        entries = []
        registered = getattr(context, "registered_web_apis", [])
        for suffix, method_name, methods, description in self.ROUTES:
            route = f"{WEBUI_PREFIX}{suffix}"
            handler = getattr(self, method_name)
            context.register_web_api(route, handler, methods, description)
            entry = next(
                (
                    candidate
                    for candidate in registered
                    if candidate[0] == route
                    and candidate[1] == handler
                    and candidate[2] == methods
                ),
                None,
            )
            if entry is not None:
                entries.append(entry)
        self._registered_entries = entries
        return list(entries)

    def unregister(
        self,
        context: Any,
        entries: list[tuple[Any, ...]] | None = None,
    ) -> None:
        self._web_closed = True
        registered = getattr(context, "registered_web_apis", None)
        if not isinstance(registered, list):
            return
        targets = entries if entries is not None else self._registered_entries
        target_ids = {id(entry) for entry in targets}
        registered[:] = [entry for entry in registered if id(entry) not in target_ids]
        self._registered_entries = []

    def _closed_response(self):
        if self._is_closed() or self._web_closed:
            return error_response("插件正在卸载或已关闭", status_code=503)
        return None

    @staticmethod
    def _ok(data: Any, *, status_code: int = 200):
        return json_response({"status": "ok", "data": data}, status_code=status_code)

    @staticmethod
    def _service_error(exc: StudioServiceError):
        return error_response(
            exc.message,
            status_code=exc.status_code,
            data=exc.data,
        )

    async def jobs(self):
        if closed := self._closed_response():
            return closed
        return self._ok(self.tracker.active_and_recent())

    async def jobs_stream(self):
        if closed := self._closed_response():
            return closed
        queue = self.tracker.subscribe()
        snapshot = self.tracker.active_and_recent()

        async def events():
            try:
                logger.debug(f"[WebUI] SSE 快照帧发送: 记录数={len(snapshot)}")
                yield self._sse({"type": "snapshot", "data": snapshot})
                while True:
                    if self._is_closed() or self._web_closed:
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        if self._is_closed() or self._web_closed:
                            break
                        yield ": heartbeat\n\n"
                        continue
                    if self._is_closed() or self._web_closed:
                        break
                    yield self._sse(event)
            finally:
                self.tracker.unsubscribe(queue)

        return stream_response(
            events(),
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @staticmethod
    def _sse(payload: dict[str, Any]) -> str:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return f"data: {body}\n\n"

    async def history(self):
        if closed := self._closed_response():
            return closed
        try:
            page = self._query_int("page", 1, minimum=1, maximum=100)
            size = self._query_int("size", 20, minimum=1, maximum=100)
            keyword = self._query_text("keyword", 100)
            source = self._query_text("source", 32)
            group_id = self._query_text("group_id", 128)
            user_id = self._query_text("user_id", 128)
        except StudioServiceError as exc:
            return self._service_error(exc)
        return self._ok(
            self.tracker.query_history(
                page=page,
                size=size,
                keyword=keyword,
                source=source,
                group_id=group_id,
                user_id=user_id,
            )
        )

    @staticmethod
    def _query_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
        raw = request.query.get(name, str(default))
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise StudioServiceError(f"{name} 必须是整数") from exc
        if value < minimum or value > maximum:
            raise StudioServiceError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
        return value

    @staticmethod
    def _query_text(name: str, limit: int) -> str:
        value = str(request.query.get(name, "") or "")
        if len(value) > limit:
            raise StudioServiceError(f"{name} 不能超过 {limit} 个字符")
        return value

    async def history_detail(self, job_id: str):
        if closed := self._closed_response():
            return closed
        record = self.tracker.get(str(job_id or ""))
        if record is None:
            return error_response("生成记录不存在", status_code=404)
        return self._ok(record)

    async def delete_history(self):
        if closed := self._closed_response():
            return closed
        payload = await self._json_body()
        if payload is None:
            return error_response("请求体必须是有效的 JSON 对象", status_code=400)
        job_ids = payload.get("job_ids")
        if not isinstance(job_ids, list) or not job_ids or len(job_ids) > 20:
            return error_response("job_ids 必须是包含 1 到 20 项的数组")
        if any(not isinstance(item, str) or not item.strip() for item in job_ids):
            return error_response("job_ids 只能包含非空字符串")
        result = await self.tracker.delete(job_ids)
        return self._ok(result)

    async def image(self, name: str):
        if closed := self._closed_response():
            return closed
        if not isinstance(name, str) or not _SAFE_FILE_NAME.fullmatch(name):
            return error_response("非法图片文件名", status_code=400)
        try:
            path = self.service.gallery_file(name)
        except StudioServiceError as exc:
            return self._service_error(exc)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        download = str(request.query.get("download", "") or "").lower() in {
            "1",
            "true",
            "yes",
        }
        return file_response(
            path,
            filename=path.name if download else None,
            content_type=content_type,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    async def image_b64(self, name: str):
        if closed := self._closed_response():
            return closed
        if not isinstance(name, str) or not _SAFE_FILE_NAME.fullmatch(name):
            return error_response("非法图片文件名", status_code=400)
        thumbnail = str(request.query.get("thumb", "") or "").lower() in {
            "1",
            "true",
            "yes",
        }
        try:
            payload = await self.service.gallery_image_base64(
                name,
                thumbnail=thumbnail,
            )
        except StudioServiceError as exc:
            return self._service_error(exc)
        return self._ok(payload)

    async def capabilities(self):
        if closed := self._closed_response():
            return closed
        return self._ok(self.service.capabilities())

    async def generate(self):
        if closed := self._closed_response():
            return closed
        payload = await self._json_body()
        if payload is None:
            return error_response("请求体必须是有效的 JSON 对象", status_code=400)
        try:
            requester = {
                "user_id": "",
                "user_name": str(getattr(request, "username", "") or "")[:200],
                "group_id": "",
            }
            result = await self.service.generate(payload, requester=requester)
        except StudioServiceError as exc:
            return self._service_error(exc)
        except Exception as exc:
            logger.error(f"[WebUI] 发起生成失败: {exc}", exc_info=True)
            return error_response("发起生成失败", status_code=500)
        return self._ok(result, status_code=202)

    async def upload(self):
        if closed := self._closed_response():
            return closed
        try:
            uploaded = await request.files()
            files = []
            for key in uploaded.keys():
                files.extend(uploaded.getlist(key))
            names = await self.service.save_uploads(files)
        except StudioServiceError as exc:
            return self._service_error(exc)
        except Exception as exc:
            logger.error(f"[WebUI] 上传参考图失败: {exc}", exc_info=True)
            return error_response("上传参考图失败", status_code=400)
        return self._ok({"names": names})

    @staticmethod
    async def _json_body() -> dict[str, Any] | None:
        sentinel = object()
        payload = await request.json(default=sentinel)
        if payload is sentinel or not isinstance(payload, dict):
            return None
        if not _valid_json_value(payload):
            return None
        return payload
