"""ProviderRouter：候选选择、fallback、错误分类与 deadline 截断的统一执行层。

按 scheduler-v2.md 的 Provider contract，对每个候选 provider 按固定顺序执行
supports -> normalize -> build_request(attempt) -> send -> parse_response；
Provider 保持纯适配器。本模块尚未接入旧入口，也不改变 dispatch.py 的最小
调度路径；JobScheduler 切换到 Router 属于后续阶段。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from .errors import (
    InvalidRequestError,
    NoSupportedProviderError,
    ProviderRouteError,
    redact_text,
)
from .requests import GenerationRequest, ProviderAttempt
from .results import GenerationResult
from .retry import is_retryable, run_with_retry

logger = logging.getLogger(__name__)

MAX_OUTCOME_MESSAGE_CHARS = 512

# 候选执行状态：unsupported=不支持该请求，failed=执行失败，succeeded=成功。
OUTCOME_UNSUPPORTED = "unsupported"
OUTCOME_FAILED = "failed"
OUTCOME_SUCCEEDED = "succeeded"


async def _call(value: Any, *args: Any, **kwargs: Any) -> Any:
    result = value(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def _remaining_seconds(deadline: datetime | None) -> float | None:
    if deadline is None:
        return None
    remaining = (
        deadline.astimezone(timezone.utc) - datetime.now(timezone.utc)
    ).total_seconds()
    return max(remaining, 0.0)


def is_route_retryable(error: BaseException) -> bool:
    """Router 统一错误分类：瞬时错误重试同一候选，其余转下一候选。"""

    if isinstance(error, asyncio.TimeoutError):
        return True
    return is_retryable(error)


def _error_retryable(error: BaseException | None) -> bool | None:
    if error is None:
        return None
    value = getattr(error, "retryable", None)
    if isinstance(value, bool):
        return value
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return 500 <= status <= 599
    return None


@dataclass(frozen=True, slots=True)
class ProviderCandidate:
    """一个候选适配器；candidate_id 用于显式指定候选。"""

    provider: Any
    candidate_id: str | None = None

    @property
    def name(self) -> str:
        return str(getattr(self.provider, "name", "") or "")

    @property
    def display_id(self) -> str:
        return self.candidate_id or self.name or type(self.provider).__name__


@dataclass(frozen=True, slots=True)
class CandidateOutcome:
    """单个候选的执行结果，用于错误聚合与后续 attempt 持久化。"""

    candidate_id: str
    provider: str
    attempts: int
    status: str
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "provider": self.provider,
            "attempts": self.attempts,
            "status": self.status,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


class ProviderRouter:
    """对候选 provider 按统一 contract 顺序执行，并决定 fallback 与 retry。"""

    def __init__(
        self,
        candidates: Iterable[ProviderCandidate | Any],
        *,
        max_attempts: int = 3,
        backoff: float = 0.05,
    ) -> None:
        normalized: list[ProviderCandidate] = [
            item if isinstance(item, ProviderCandidate) else ProviderCandidate(item)
            for item in candidates
        ]
        if not normalized:
            raise InvalidRequestError("ProviderRouter 至少需要一个候选 provider")
        self._candidates = tuple(normalized)
        self.max_attempts = max(int(max_attempts), 1)
        self.backoff = max(float(backoff), 0.0)

    async def select(self, request: GenerationRequest) -> list[ProviderCandidate]:
        """candidate_id 精确唯一；provider 名称优先排序但不排除 fallback。"""

        candidates = list(self._candidates)
        if request.candidate_id:
            candidates = [
                item for item in candidates if item.candidate_id == request.candidate_id
            ]
        elif request.provider:
            preferred = [item for item in candidates if item.name == request.provider]
            candidates = preferred + [
                item for item in candidates if item not in preferred
            ]
        selected: list[ProviderCandidate] = []
        for candidate in candidates:
            provider = candidate.provider
            if not hasattr(provider, "supports"):
                selected.append(candidate)
                continue
            try:
                supported = await _call(provider.supports, request)
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                logger.warning(
                    "provider 候选 %s supports 检查失败: %s",
                    candidate.display_id,
                    redact_text(str(error)),
                )
                continue
            if supported:
                selected.append(candidate)
        return selected

    async def execute(
        self,
        request: GenerationRequest,
        *,
        deadline_at: datetime | None = None,
    ) -> GenerationResult:
        """执行候选链并返回首个成功结果；全部失败抛 ProviderRouteError。"""

        if not isinstance(request, GenerationRequest):
            raise InvalidRequestError("request 必须是 GenerationRequest")
        deadline = deadline_at if deadline_at is not None else request.deadline_at
        if deadline is not None:
            if not isinstance(deadline, datetime):
                raise InvalidRequestError("deadline_at 必须是 datetime")
            if deadline.tzinfo is None or deadline.utcoffset() is None:
                raise InvalidRequestError("deadline_at 不允许使用 naive datetime")
            deadline = deadline.astimezone(timezone.utc)

        selected = await self.select(request)
        if not selected:
            raise NoSupportedProviderError(
                "没有候选 provider 支持当前请求",
                details={
                    "requested_provider": request.provider,
                    "requested_candidate_id": request.candidate_id,
                    "candidates": [item.display_id for item in self._candidates],
                },
            )

        outcomes: list[CandidateOutcome] = []
        attempts_total = 0
        last_error: BaseException | None = None
        for candidate in selected:
            outcome, result, error = await self._run_candidate(
                candidate, request, deadline
            )
            outcomes.append(outcome)
            attempts_total += outcome.attempts
            if result is not None:
                return self._annotate(result, candidate, attempts_total)
            last_error = error
            remaining = _remaining_seconds(deadline)
            if remaining is not None and remaining <= 0:
                raise ProviderRouteError(
                    "provider 调用超过总 deadline",
                    code="deadline_exceeded",
                    retryable=True,
                    details={"candidates": [item.to_dict() for item in outcomes]},
                ) from last_error
        raise ProviderRouteError(
            "所有候选 provider 均失败",
            retryable=_error_retryable(last_error),
            details={"candidates": [item.to_dict() for item in outcomes]},
        ) from last_error

    async def _run_candidate(
        self,
        candidate: ProviderCandidate,
        request: GenerationRequest,
        deadline: datetime | None,
    ) -> tuple[CandidateOutcome, GenerationResult | None, BaseException | None]:
        provider = candidate.provider
        normalized = request
        if hasattr(provider, "normalize"):
            try:
                normalized = await _call(provider.normalize, request)
                if not isinstance(normalized, GenerationRequest):
                    raise InvalidRequestError("provider normalize 返回格式无效")
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                return self._outcome(candidate, 0, OUTCOME_FAILED, error), None, error

        attempts = 0

        async def attempt(number: int) -> GenerationResult:
            nonlocal attempts
            attempts = number
            attempt_input = ProviderAttempt(
                number=number,
                deadline_at=deadline,
                provider=request.provider,
                model=normalized.model,
                candidate_id=(
                    candidate.candidate_id
                    if candidate.candidate_id is not None
                    else request.candidate_id
                ),
            )
            built = await _call(provider.build_request, normalized, attempt_input)
            if (
                not isinstance(built, tuple)
                or len(built) != 3
                or not callable(built[2])
            ):
                raise InvalidRequestError(
                    "provider build_request 须返回 (url, headers, body_factory)"
                )
            url, headers, body_factory = built
            response = await _call(
                provider.send,
                normalized,
                attempt_input,
                url=url,
                headers=headers,
                body_factory=body_factory,
            )
            result = await _call(
                provider.parse_response,
                response,
                request=normalized,
                attempt=attempt_input,
            )
            if not isinstance(result, GenerationResult):
                raise InvalidRequestError("provider parse_response 须返回结果对象")
            return result

        try:
            result = await run_with_retry(
                attempt,
                deadline=deadline,
                max_attempts=self.max_attempts,
                backoff=self.backoff,
                retryable=is_route_retryable,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            return (
                self._outcome(candidate, attempts, OUTCOME_FAILED, error),
                None,
                error,
            )
        return self._outcome(candidate, attempts, OUTCOME_SUCCEEDED), result, None

    def _outcome(
        self,
        candidate: ProviderCandidate,
        attempts: int,
        status: str,
        error: BaseException | None = None,
    ) -> CandidateOutcome:
        if error is None:
            return CandidateOutcome(
                candidate.display_id, candidate.name, attempts, status
            )
        code = getattr(error, "code", None)
        return CandidateOutcome(
            candidate.display_id,
            candidate.name,
            attempts,
            status,
            error_code=str(code) if code else type(error).__name__,
            error_message=redact_text(str(error))[:MAX_OUTCOME_MESSAGE_CHARS],
        )

    def _annotate(
        self,
        result: GenerationResult,
        candidate: ProviderCandidate,
        attempts_total: int,
    ) -> GenerationResult:
        return replace(
            result,
            provider=result.provider or candidate.name or None,
            candidate_id=result.candidate_id or candidate.candidate_id,
            attempt_count=max(result.attempt_count, attempts_total),
        )
