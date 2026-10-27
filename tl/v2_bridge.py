"""命令入口与 v2 调度器之间的纯函数桥接层（不依赖 AstrBot）。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from .core.requests import GenerationRequest
from .core.results import GenerationResult
from .generation_errors import api_failure_message


def build_request(
    *,
    source: str,
    prompt: str,
    resolution: str | None,
    aspect_ratio: str | None,
    provider: str | None,
    model: str | None,
    reference_images: Sequence[str],
    deadline_seconds: float,
    requester: Mapping[str, str] | None = None,
    metadata: Mapping[str, Any] | None = None,
    negative_prompt: str | None = None,
    quality: str | None = None,
    watermark: bool | None = None,
    image_count: int = 1,
) -> GenerationRequest:
    """把入口参数装配为 v2 GenerationRequest；metadata 透传 legacy 专属开关。"""

    return GenerationRequest(
        prompt=prompt,
        source=source,
        provider=provider,
        model=model,
        resolution=resolution,
        aspect_ratio=aspect_ratio,
        negative_prompt=negative_prompt,
        quality=quality,
        watermark=watermark,
        image_count=image_count,
        reference_images=tuple(reference_images),
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=deadline_seconds),
        requester=dict(requester or {}),
        metadata=dict(metadata or {}),
    )


def command_request(
    *,
    prompt: str,
    resolution: str | None,
    aspect_ratio: str | None,
    requested_provider: str | None,
    requested_model: str | None,
    reference_images: Sequence[str],
    deadline_seconds: float,
    metadata: Mapping[str, Any] | None = None,
    requester: Mapping[str, str] | None = None,
) -> GenerationRequest:
    """命令入口参数装配（委托 build_request，source 固定 command）。"""

    return build_request(
        source="command",
        prompt=prompt,
        resolution=resolution,
        aspect_ratio=aspect_ratio,
        provider=requested_provider,
        model=requested_model,
        reference_images=reference_images,
        deadline_seconds=deadline_seconds,
        metadata=metadata,
        requester=requester,
    )


def split_artifacts(artifacts: Sequence[str]) -> tuple[list[str], list[str]]:
    """按 URL / 本地路径拆分 artifacts，对齐 legacy 返回的二元组语义。"""

    urls: list[str] = []
    paths: list[str] = []
    for item in artifacts:
        if item.startswith(("http://", "https://")):
            urls.append(item)
        else:
            paths.append(item)
    return urls, paths


def apply_result_to_config(config: Any, result: GenerationResult) -> None:
    """把候选标注与 legacy 统计字段回写到入口 config，不覆盖已有非空值。"""

    if getattr(config, "successful_provider", None) is None and result.provider:
        config.successful_provider = result.provider
    if getattr(config, "successful_model", None) is None and result.model:
        config.successful_model = result.model
    if getattr(config, "successful_candidate_id", None) is None and result.candidate_id:
        config.successful_candidate_id = result.candidate_id
    # 统计字段经 result.stats 回写：缺键不动；retry_count 以 0、其余以空为未设置。
    stats = result.stats or {}
    retry_count = stats.get("retry_count")
    if retry_count and not getattr(config, "retry_count", 0):
        config.retry_count = retry_count
    retry_note = stats.get("retry_note")
    if retry_note and not getattr(config, "retry_note", None):
        config.retry_note = retry_note
    token_usage = stats.get("token_usage")
    if token_usage and not getattr(config, "token_usage", None):
        config.token_usage = dict(token_usage)
    model_alias = stats.get("successful_model_alias")
    if model_alias and not getattr(config, "successful_model_alias", None):
        config.successful_model_alias = model_alias


def v2_failure_error(result: Any) -> RuntimeError:
    """从 JobResult 终态构造带 error code/message 的异常，走入口原错误分支。"""

    code = result.error_code or "unknown_error"
    message = result.error_message or "未提供错误详情"
    return RuntimeError(f"v2 调度生成失败 [{code}]: {message}")


def v2_failure_text(code: str, message: str, *, total_timeout: int) -> str:
    """按错误码生成与旧路径 APIError 分支同措辞的用户提示。"""

    code = str(code or "")
    message = str(message or "未提供错误详情")

    if code == "provider_empty_result":
        return (
            "❌ 图像生成失败：供应商未返回图片或文本内容\n"
            "🧐 可能原因：上游模型本次未产出任何图像。\n"
            "✅ 建议：稍后重试，或调整提示词/减少参考图后再试。"
        )
    # 错误码映射回旧分支的输入形态；未知码走"未知错误"文案。
    error_type: str | None = None
    status_code: int | None = None
    if code == "provider_timeout":
        error_type = "timeout"
    elif code == "provider_network":
        error_type = "network"
    elif code == "provider_workspace_unavailable":
        error_type = "workspace_unavailable"
    elif code.startswith("provider_http_"):
        suffix = code[len("provider_http_") :]
        status_code = int(suffix) if suffix.isdigit() else None
    return api_failure_message(
        message=message,
        status_code=status_code,
        error_type=error_type,
        total_timeout=total_timeout,
    )


def v2_failure_message(result: Any, *, total_timeout: int) -> str:
    """按 Job 终态错误码生成与旧路径 APIError 分支同措辞的用户提示。"""

    return v2_failure_text(
        str(getattr(result, "error_code", None) or ""),
        str(getattr(result, "error_message", None) or "未提供错误详情"),
        total_timeout=total_timeout,
    )
