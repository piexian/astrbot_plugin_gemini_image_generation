from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import pytest

from tl.rate_limiter import RateLimiter


@dataclass
class _Config:
    group_limit_mode: str = "blacklist"
    group_limit_list: list[str] = field(default_factory=list)
    rate_limit_rules: list[dict[str, Any]] = field(default_factory=list)
    default_rate_limit: dict[str, Any] = field(
        default_factory=lambda: {
            "enabled": True,
            "period_seconds": 60,
            "max_requests": 10,
        }
    )


@dataclass
class _Event:
    group_id: str


async def _cancel_pending_save(limiter: RateLimiter) -> None:
    task = limiter._pending_save_task
    if task and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_rate_limiter_debounces_successive_kv_writes() -> None:
    writes: list[dict[str, list[float]]] = []

    async def put_kv(key: str, value: dict[str, list[float]]) -> None:
        writes.append({group: list(bucket) for group, bucket in value.items()})

    limiter = RateLimiter(_Config(), put_kv=put_kv)
    limiter.SAVE_DEBOUNCE_SECONDS = 60.0

    try:
        allowed, _ = await limiter.check_and_consume(_Event("10001"))
        assert allowed is True
        assert len(writes) == 1

        allowed, _ = await limiter.check_and_consume(_Event("10001"))
        assert allowed is True
        assert len(writes) == 1
        assert limiter._pending_save_task is not None
    finally:
        await _cancel_pending_save(limiter)


@pytest.mark.asyncio
async def test_rate_limiter_skips_kv_write_for_unchanged_limited_bucket() -> None:
    writes: list[dict[str, list[float]]] = []

    async def put_kv(key: str, value: dict[str, list[float]]) -> None:
        writes.append({group: list(bucket) for group, bucket in value.items()})

    limiter = RateLimiter(
        _Config(
            default_rate_limit={
                "enabled": True,
                "period_seconds": 60,
                "max_requests": 1,
            }
        ),
        put_kv=put_kv,
    )
    limiter._loaded = True
    limiter._rate_limit_buckets["10001"] = [time.time()]

    allowed, message = await limiter.check_and_consume(_Event("10001"))

    assert allowed is False
    assert message
    assert writes == []
