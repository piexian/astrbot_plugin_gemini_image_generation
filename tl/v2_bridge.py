"""命令入口与 v2 调度器之间的纯函数桥接层（不依赖 AstrBot）。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from .core.requests import GenerationRequest
from .core.results import GenerationResult


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
    """把命令入口参数装配为 v2 GenerationRequest；metadata 透传 legacy 专属开关。"""

    return GenerationRequest(
        prompt=prompt,
        source="command",
        provider=requested_provider,
        model=requested_model,
        resolution=resolution,
        aspect_ratio=aspect_ratio,
        reference_images=tuple(reference_images),
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=deadline_seconds),
        requester=dict(requester or {}),
        metadata=dict(metadata or {}),
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
    """把结果候选标注回写到入口 config，不覆盖已有非空值。"""

    if getattr(config, "successful_provider", None) is None and result.provider:
        config.successful_provider = result.provider
    if getattr(config, "successful_model", None) is None and result.model:
        config.successful_model = result.model
    if getattr(config, "successful_candidate_id", None) is None and result.candidate_id:
        config.successful_candidate_id = result.candidate_id


def v2_failure_error(result: Any) -> RuntimeError:
    """从 JobResult 终态构造带 error code/message 的异常，走入口原错误分支。"""

    code = result.error_code or "unknown_error"
    message = result.error_message or "未提供错误详情"
    return RuntimeError(f"v2 调度生成失败 [{code}]: {message}")
