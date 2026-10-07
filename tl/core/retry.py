"""Deadline-bounded retry primitives for the minimal scheduler."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TypeVar

T = TypeVar("T")


def _remaining(deadline: datetime | None, deadline_mono: float | None) -> float | None:
    if deadline_mono is not None:
        return max(deadline_mono - time.monotonic(), 0.0)
    if deadline is None:
        return None
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise ValueError("deadline 必须是带时区的 datetime")
    return max(
        (
            deadline.astimezone(timezone.utc) - datetime.now(timezone.utc)
        ).total_seconds(),
        0.0,
    )


def is_retryable(error: BaseException) -> bool:
    status = getattr(error, "status_code", None)
    return bool(getattr(error, "retryable", False)) or (
        isinstance(status, int) and 500 <= status <= 599
    )


async def run_with_retry(
    operation: Callable[[int], Awaitable[T] | T],
    *,
    deadline: datetime | None,
    max_attempts: int = 3,
    backoff: float = 0.05,
) -> T:
    """Run attempt factories without retrying past the total deadline."""
    max_attempts = max(int(max_attempts), 1)
    remaining = _remaining(deadline, None)
    deadline_mono = time.monotonic() + remaining if remaining is not None else None
    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        remaining = _remaining(deadline, deadline_mono)
        if remaining is not None and remaining <= 0:
            raise asyncio.TimeoutError("retry deadline exceeded") from last_error
        try:
            result = operation(attempt)
            if inspect.isawaitable(result):
                if remaining is None:
                    return await result
                return await asyncio.wait_for(result, timeout=remaining)
            return result
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            last_error = error
            if attempt >= max_attempts or not is_retryable(error):
                raise
            remaining = _remaining(deadline, deadline_mono)
            if remaining is not None and remaining <= 0:
                raise asyncio.TimeoutError("retry deadline exceeded") from error
            delay = (
                min(backoff * (2 ** (attempt - 1)), remaining)
                if remaining is not None
                else backoff * (2 ** (attempt - 1))
            )
            if delay > 0:
                if deadline_mono is None:
                    await asyncio.sleep(delay)
                else:
                    await asyncio.wait_for(
                        asyncio.sleep(delay),
                        timeout=_remaining(deadline, deadline_mono),
                    )
    raise RuntimeError("retry loop exhausted") from last_error
