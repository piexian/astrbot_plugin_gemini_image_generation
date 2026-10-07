"""Provider dispatch contract used by the standalone scheduler tests."""

from __future__ import annotations

import inspect
from typing import Any

from .models import Job
from .requests import ProviderAttempt
from .results import GenerationResult
from .retry import run_with_retry


class ProviderDispatchError(RuntimeError):
    """Adapter error with optional HTTP status/retryability metadata."""

    def __init__(
        self, message: str, *, status_code: int | None = None, retryable: bool = False
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


async def _call(value: Any, *args: Any, **kwargs: Any) -> Any:
    result = value(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


async def dispatch_provider(
    provider: Any,
    job: Job,
    *,
    max_attempts: int = 3,
    backoff: float = 0.05,
) -> GenerationResult:
    """Build a fresh request body on every attempt and parse the response."""

    request = job.request
    if hasattr(provider, "supports") and not await _call(provider.supports, request):
        raise ProviderDispatchError("provider 不支持当前请求", retryable=False)
    if hasattr(provider, "normalize"):
        request = await _call(provider.normalize, request)

    async def attempt(number: int) -> GenerationResult:
        attempt_input = ProviderAttempt(
            number=number,
            deadline_at=job.deadline_at,
            provider=request.provider,
            model=request.model,
            candidate_id=request.candidate_id,
        )
        if hasattr(provider, "build_request"):
            built = await _call(provider.build_request, request, attempt_input)
            if not isinstance(built, tuple) or len(built) != 3:
                raise ProviderDispatchError("provider build_request 返回格式无效")
            url, headers, body_factory = built
            response = await _call(
                provider.send,
                request,
                attempt_input,
                url=url,
                headers=headers,
                body_factory=body_factory,
            )
            return await _call(
                provider.parse_response,
                response,
                request=request,
                attempt=attempt_input,
            )
        if hasattr(provider, "generate"):
            return await _call(provider.generate, request, attempt_input)
        raise ProviderDispatchError("provider 缺少统一 contract")

    return await run_with_retry(
        attempt,
        deadline=job.deadline_at,
        max_attempts=max_attempts,
        backoff=backoff,
    )
