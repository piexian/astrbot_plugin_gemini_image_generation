"""核心图像生成逻辑模块"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from astrbot.api import logger

from .core.errors import (
    CoreError,
    QueueFullError,
    QuotaExceededError,
    ServiceClosedError,
    redact_text,
)
from .core.requests import GenerationRequest
from .core.states import JobState
from .generation_errors import api_failure_message
from .generation_tracker import (
    current_tracking_context,
    requester_from_event,
)
from .image_outputs import normalize_image_outputs
from .provider_capabilities import routing_mode
from .session_image_dir import bind_session_image_dir, reset_session_image_dir
from .v2_bridge import (
    build_request as v2_build_request,
)
from .v2_bridge import (
    split_artifacts as v2_split_artifacts,
)
from .v2_bridge import (
    v2_failure_message,
    v2_failure_text,
)

DEFAULT_MAX_REFERENCE_IMAGES = 6

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.star import Context

    from .tl_api import APIClient

_API_CLIENT_UNAVAILABLE_MESSAGE = (
    "❌ 无法生成图像：API 客户端尚未初始化。\n"
    "🧐 可能原因：服务启动过快，提供商尚未加载或 API 配置/密钥缺失。\n"
    "✅ 建议：先在配置文件中填写有效的 API 密钥并重启服务。"
)
_V2_SERVICE_UNAVAILABLE_MESSAGE = "❌ 生成服务尚未初始化"


def _avatar_prompt_note(avatar_count: int) -> str:
    return f"""

[System Note]
The last {avatar_count} image(s) provided are User Avatars (marked as optional reference). You may use them for character consistency if needed, but they are NOT mandatory if they conflict with the requested style."""


@dataclass(frozen=True, slots=True)
class V2Submission:
    """v2 第一阶段（提交）的结果。

    成功时带 ``handle``；被拒或出错时 ``handle`` 为 None，``message`` 是可直接
    展示的提示。``silent`` 为 True 表示静默拒绝（群名单不通过），不应回复。
    """

    handle: Any = None
    message: str | None = None
    silent: bool = False


class ImageGenerator:
    """核心图像生成处理器"""

    def __init__(
        self,
        context: Context,
        api_client: APIClient | None = None,
        enable_smart_retry: bool = True,
        total_timeout: int = 120,
        max_attempts_per_key: int = 3,
        max_reference_images: int = DEFAULT_MAX_REFERENCE_IMAGES,
        filter_valid_fn=None,
        tracker=None,
        archive_images_fn=None,
    ):
        """
        Args:
            context: AstrBot Context 实例
            api_client: API 客户端实例
            enable_smart_retry: 是否启用智能重试
            total_timeout: 总超时时间
            max_attempts_per_key: 每个密钥最大尝试次数
            max_reference_images: 最大参考图片数量
            filter_valid_fn: 过滤有效参考图片的函数
        """
        self.context = context
        self.api_client = api_client
        self.enable_smart_retry = enable_smart_retry
        self.total_timeout = total_timeout
        self.max_attempts_per_key = max_attempts_per_key
        self.max_reference_images = self._coerce_max_reference_images(
            max_reference_images
        )
        self._filter_valid_fn = filter_valid_fn
        self.tracker = tracker
        self._archive_images_fn = archive_images_fn
        initial_request_stats: dict[str, object] = {
            "retry_count": 0,
            "token_usage": None,
            "retry_note": None,
        }
        self._request_stats_var: ContextVar[dict[str, object]] = ContextVar(
            "image_generation_request_stats",
            default=initial_request_stats,
        )

    def update_config(self, **kwargs):
        """更新配置"""
        for key, value in kwargs.items():
            if hasattr(self, key) and value is not None:
                if key == "max_reference_images":
                    value = self._coerce_max_reference_images(value)
                setattr(self, key, value)

    @staticmethod
    def _coerce_max_reference_images(value) -> int:
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return DEFAULT_MAX_REFERENCE_IMAGES

    def _filter_valid_reference_images(
        self, images: list[str] | None, source: str
    ) -> list[str]:
        """过滤有效参考图片"""
        if self._filter_valid_fn:
            return self._filter_valid_fn(images, source)
        return images or []

    def get_request_stats(self) -> dict[str, object]:
        """Return stats for the current async generation context."""
        return dict(self._request_stats_var.get())

    @property
    def last_request_stats(self) -> dict[str, object]:
        """Compatibility view of stats for the current async context."""
        return self.get_request_stats()

    @last_request_stats.setter
    def last_request_stats(self, stats: dict[str, object]) -> None:
        self._request_stats_var.set(dict(stats))

    def _set_request_stats(self, stats: dict[str, object]) -> None:
        self.last_request_stats = stats

    async def generate_image_core(
        self,
        event: AstrMessageEvent,
        v2_service: Any = None,
        v2_admission: Mapping[str, Any] | None = None,
        v2_quota: Mapping[str, Any] | Literal[False] | None = None,
        **kwargs,
    ) -> tuple[bool, tuple[list[str], list[str], str | None, str | None] | str]:
        """执行 v2 两阶段生成：``submit_v2`` 提交并绑定会话图片目录，``wait_v2`` 转换结果。

        ``v2_admission`` 与 ``v2_quota`` 分别是提交时的准入上下文和请求
        ``metadata["quota"]``；未装配 v2 服务时直接返回失败提示。
        """
        if v2_service is None:
            return False, _V2_SERVICE_UNAVAILABLE_MESSAGE
        submission = await self.submit_v2(
            event=event,
            v2_service=v2_service,
            admission=v2_admission,
            quota=v2_quota,
            **kwargs,
        )
        if submission.handle is None:
            return False, submission.message or ""
        return await self.wait_v2(
            v2_service=v2_service,
            handle=submission.handle,
            requested_provider=kwargs.get("requested_provider"),
            requested_model=kwargs.get("requested_model"),
            image_count=kwargs.get("image_count", 1),
        )

    def _reset_request_stats(self) -> None:
        self._set_request_stats(
            {
                "retry_count": 0,
                "token_usage": None,
                "retry_note": None,
                "successful_candidate_id": None,
                "successful_provider": None,
                "successful_model": None,
                "successful_model_alias": None,
                "effective_image_count": 1,
                "routing_mode": "full_polling",
            }
        )

    def _prepare_reference_inputs(
        self, reference_images: list[str] | None, avatar_reference: list[str] | None
    ) -> tuple[list[str], int]:
        """旧路径与 v2 共用的参考图过滤、数量截断；返回 (合并列表, 头像数)。"""
        valid_msg_images = self._filter_valid_reference_images(
            reference_images, source="消息图片"
        )
        valid_avatar_images = self._filter_valid_reference_images(
            avatar_reference, source="头像"
        )
        max_images = self.max_reference_images
        if len(valid_msg_images) > max_images:
            valid_msg_images = valid_msg_images[:max_images]
        remaining_slots = max(max_images - len(valid_msg_images), 0)
        if len(valid_avatar_images) > remaining_slots:
            valid_avatar_images = valid_avatar_images[:remaining_slots]
        return valid_msg_images + valid_avatar_images, len(valid_avatar_images)

    async def prepare_v2_request(
        self,
        *,
        event: Any,
        v2_service: Any,
        prompt: str,
        reference_images: list[str] | None,
        avatar_reference: list[str] | None,
        override_resolution: str | None = None,
        override_aspect_ratio: str | None = None,
        is_tool_call: bool = False,
        requested_provider: str | None = None,
        requested_model: str | None = None,
        negative_prompt: str | None = None,
        watermark: bool | None = None,
        quality: str | None = None,
        image_count: int = 1,
        suppress_resolution: bool = False,
        deadline_seconds: float | None,
        source: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        quota: Mapping[str, Any] | Literal[False] | None = None,
        trusted_local: bool = False,
        template_id: str | None = None,
    ) -> GenerationRequest:
        """准备单图与批量共用的 v2 请求，不提交 Job、不写生成历史。"""

        all_reference_images, avatar_count = self._prepare_reference_inputs(
            reference_images, avatar_reference
        )
        if avatar_count:
            prompt += _avatar_prompt_note(avatar_count)
        requester = requester_from_event(event)
        umo = getattr(event, "unified_msg_origin", None)
        if umo:
            requester["umo"] = str(umo)
        context = current_tracking_context()
        resolved_source = source or (
            context.source if context else ("llm_tool" if is_tool_call else "command")
        )
        request_metadata: dict[str, Any] = {
            "suppress_resolution": suppress_resolution,
            "enable_smart_retry": self.enable_smart_retry,
            "max_attempts_per_key": self.max_attempts_per_key,
            "total_timeout": self.total_timeout,
            # 与旧核心路径一致：没有任何图片的结果按失败处理，哪怕带了文本。
            "require_images": True,
        }
        request_metadata.update(dict(metadata or {}))
        if quota is not None:
            request_metadata["quota"] = quota if quota is False else dict(quota)
        prepare_options = {"trusted_local": True} if trusted_local else {}
        if template_id:
            request_metadata["template_id"] = template_id
        refs = await v2_service.prepare_references(
            all_reference_images, requester=requester, **prepare_options
        )
        logger.info(f"v2 调度提交: 参考图片: {len(refs)} 张, 来源={resolved_source}")
        return v2_build_request(
            source=resolved_source,
            prompt=prompt,
            resolution=override_resolution,
            aspect_ratio=override_aspect_ratio,
            provider=requested_provider,
            model=requested_model,
            reference_images=refs,
            deadline_seconds=deadline_seconds,
            requester=requester,
            metadata=request_metadata,
            negative_prompt=negative_prompt,
            quality=quality,
            watermark=watermark,
            image_count=max(int(image_count or 1), 1),
        )

    async def submit_v2(
        self,
        *,
        event: Any,
        v2_service: Any,
        prompt: str,
        reference_images: list[str] | None,
        avatar_reference: list[str] | None,
        override_resolution: str | None = None,
        override_aspect_ratio: str | None = None,
        is_tool_call: bool = False,
        requested_provider: str | None = None,
        requested_model: str | None = None,
        negative_prompt: str | None = None,
        watermark: bool | None = None,
        quality: str | None = None,
        image_count: int = 1,
        suppress_resolution: bool = False,
        admission: Mapping[str, Any] | None = None,
        quota: Mapping[str, Any] | Literal[False] | None = None,
        trusted_local: bool = False,
        template_id: str | None = None,
    ) -> V2Submission:
        """v2 第一阶段：准备输入并提交 Job，额度与队列准入由调度器在 submit 内完成。

        ``admission`` 是不持久化的准入上下文（如命令事件）；``quota`` 写入请求
        ``metadata["quota"]``，False 表示本次不计额度，None 按默认扣会话额度 1。
        ``trusted_local`` 表示调用方已校验过本地路径参考图（LLM 工具的
        tool_path_guard），它们会随内联图一起换成临时句柄。请求带
        ``require_images``：供应商没有返回任何图片时按失败处理（只有文本也算），
        与旧核心路径一致。
        被拒或出错时不外抛，返回可直接展示的提示（静默拒绝带 ``silent``）；
        生成历史由 TrackerProjection 负责，本方法不碰 tracker。整个阶段在会话
        图片目录绑定期间执行，runtime task 继承该 contextvar。
        """
        self._reset_request_stats()
        if not self.api_client:
            return V2Submission(message=_API_CLIENT_UNAVAILABLE_MESSAGE)
        token = await bind_session_image_dir(event, self.context)
        try:
            try:
                request = await self.prepare_v2_request(
                    event=event,
                    v2_service=v2_service,
                    prompt=prompt,
                    reference_images=reference_images,
                    avatar_reference=avatar_reference,
                    override_resolution=override_resolution,
                    override_aspect_ratio=override_aspect_ratio,
                    is_tool_call=is_tool_call,
                    requested_provider=requested_provider,
                    requested_model=requested_model,
                    negative_prompt=negative_prompt,
                    watermark=watermark,
                    quality=quality,
                    image_count=image_count,
                    suppress_resolution=suppress_resolution,
                    deadline_seconds=self.total_timeout,
                    quota=quota,
                    trusted_local=trusted_local,
                    template_id=template_id,
                )
            except asyncio.CancelledError:
                raise
            except CoreError as error:
                # 参考图解析失败/过大/服务关闭等：按错误码给旧路径同措辞提示。
                error_msg = v2_failure_text(
                    str(getattr(error, "code", "") or ""),
                    getattr(error, "message", None) or str(error),
                    total_timeout=self.total_timeout,
                )
                logger.error(error_msg)
                return V2Submission(message=error_msg)
            except Exception as e:
                logger.error(
                    f"生成图像时发生未预期的错误: {redact_text(e)[:240]}", exc_info=True
                )
                return V2Submission(message=f"❌ 生成图像时发生错误: {str(e)}")
            try:
                handle = await v2_service.submit(request, admission=admission)
            except asyncio.CancelledError:
                raise
            except QuotaExceededError as error:
                # 与旧路径一致：额度提示原样回复，群名单拒绝时不回复。
                return V2Submission(
                    message=error.message, silent=bool(error.details.get("silent"))
                )
            except (QueueFullError, ServiceClosedError) as error:
                # 与旧路径队列满/关闭同类错误同措辞（走 APIError 分支拼装）。
                error_msg = api_failure_message(
                    message=error.message,
                    status_code=None,
                    error_type=error.code,
                    total_timeout=self.total_timeout,
                )
                logger.error(error_msg)
                return V2Submission(message=error_msg)
            except Exception as e:
                logger.error(
                    f"生成图像时发生未预期的错误: {redact_text(e)[:240]}", exc_info=True
                )
                return V2Submission(message=f"❌ 生成图像时发生错误: {str(e)}")
            logger.info(
                f"[v2] 提交成功: job_id={getattr(handle, 'job_id', None)}, "
                f"umo={getattr(event, 'unified_msg_origin', None)}, "
                f"provider={getattr(request, 'provider', None) or requested_provider}, "
                f"model={getattr(request, 'model', None) or requested_model}, "
                f"candidate_id={getattr(request, 'candidate_id', None)}, "
                f"refs={len(reference_images or []) + len(avatar_reference or [])}"
            )
            return V2Submission(handle=handle)
        finally:
            reset_session_image_dir(token)

    async def wait_v2(
        self,
        *,
        v2_service: Any,
        handle: Any,
        requested_provider: str | None = None,
        requested_model: str | None = None,
        image_count: int = 1,
    ) -> tuple[bool, tuple[list[str], list[str], str | None, str | None] | str]:
        """v2 第二阶段：等待 Job 终态并转换为旧调用方兼容结果。"""
        deadline = self.total_timeout
        wait_started = asyncio.get_running_loop().time()
        job_id = getattr(handle, "job_id", None)
        # 等待上限由调度器给出（deadline 之外还容纳观察者钩子时间）；等待期间
        # 调用方被取消或超时都先取消 Job，与旧路径"取消入口即取消生成"一致。
        try:
            job_result = await v2_service.wait(
                handle.job_id, timeout=v2_service.result_wait_timeout(deadline)
            )
        except asyncio.CancelledError:
            await asyncio.shield(v2_service.cancel(handle.job_id))
            raise
        except asyncio.TimeoutError:
            await asyncio.shield(v2_service.cancel(handle.job_id))
            error_msg = api_failure_message(
                message="生成等待超时",
                status_code=None,
                error_type="timeout",
                total_timeout=self.total_timeout,
            )
            logger.warning(
                f"[v2] 等待超时并取消: job_id={job_id}, umo={getattr(handle, 'umo', None)}, timeout={deadline}"
            )
            logger.error(error_msg)
            return False, error_msg
        except Exception as e:
            # wait 自身异常：Job 可能仍在运行，先取消再按旧路径措辞返回。
            logger.error(
                f"生成图像时发生未预期的错误: {redact_text(e)[:240]}", exc_info=True
            )
            await asyncio.shield(v2_service.cancel(handle.job_id))
            return False, f"❌ 生成图像时发生错误: {str(e)}"
        if (
            job_result.state not in (JobState.SUCCEEDED, JobState.PARTIAL)
            or job_result.result is None
        ):
            error_msg = v2_failure_message(job_result, total_timeout=self.total_timeout)
            logger.error(error_msg)
            return False, error_msg
        result = job_result.result
        image_urls, image_paths = v2_split_artifacts(result.artifacts)
        image_paths, image_urls = await normalize_image_outputs(image_urls, image_paths)
        stats = result.stats or {}
        self._set_request_stats(
            {
                "retry_count": stats.get("retry_count", 0),
                "token_usage": stats.get("token_usage"),
                "retry_note": stats.get("retry_note"),
                "successful_candidate_id": result.candidate_id,
                "successful_provider": result.provider,
                "successful_model": result.model,
                "successful_model_alias": stats.get("successful_model_alias"),
                # v2 结果不含供应商实际生效张数，保留请求张数。
                "effective_image_count": max(int(image_count or 1), 1),
                "routing_mode": routing_mode(requested_provider, requested_model),
            }
        )
        logger.info(
            f"[v2] 等待完成: job_id={job_id}, umo={getattr(handle, 'umo', None)}, "
            f"images={len(image_paths) + len(image_urls)}, provider={result.provider}, "
            f"model={result.model}, elapsed={asyncio.get_running_loop().time() - wait_started:.2f}s"
        )
        logger.info(
            f"🖼️ v2 返回图片数量: {len(image_paths)}, URL 数量: {len(image_urls)}"
        )
        return True, (image_urls, image_paths, result.text, None)
