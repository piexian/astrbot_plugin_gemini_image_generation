from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

from tl.limit_config import apply_limits, normalize_limits
from tl.rate_limiter import RateLimiter

A = "bot_a:GroupMessage:10001"
B = "bot_b:GroupMessage:10001"
PRIVATE = "bot_a:FriendMessage:10001"


def config(*, global_max=None, session_max=1, rules=None):
    value = SimpleNamespace(group_limit_mode="none", group_limit_list=set())
    limits = normalize_limits(
        {
            "global_rate_limit": {
                "enabled": global_max is not None,
                "max_requests": global_max or 5,
            },
            "default_rate_limit": {
                "enabled": session_max is not None,
                "max_requests": session_max or 5,
            },
            "rate_limit_rules": rules or [],
        }
    )
    apply_limits(value, limits)
    return value


def event(umo=A, group_id="10001"):
    return SimpleNamespace(unified_msg_origin=umo, group_id=group_id)


def count(limiter, key):
    return sum(entry["count"] for entry in limiter._rate_limit_buckets.get(key, []))


@pytest.mark.asyncio
async def test_umo_isolates_platform_private_and_unique_group_sessions():
    limiter = RateLimiter(config())
    for umo in (A, B, PRIVATE, "bot_a:GroupMessage:user_10001"):
        assert (await limiter.check_and_consume(event(umo)))[0]
        assert not (await limiter.check_and_consume(event(umo)))[0]
    assert set(limiter._rate_limit_buckets) == {
        A,
        B,
        PRIVATE,
        "bot_a:GroupMessage:user_10001",
    }


@pytest.mark.asyncio
async def test_global_and_session_are_atomic_and_studio_only_uses_global():
    limiter = RateLimiter(config(global_max=3, session_max=1))
    assert (await limiter.acquire(A)).allowed
    assert not (await limiter.acquire(A)).allowed
    assert count(limiter, "global") == 1
    assert (await limiter.acquire(None, cost=2)).allowed
    decision = await limiter.acquire(B)
    assert not decision.allowed and decision.scope == "global"
    assert count(limiter, B) == 0


@pytest.mark.asyncio
async def test_first_enabled_rule_wins_and_default_is_per_session():
    limiter = RateLimiter(
        config(
            session_max=1,
            rules=[
                {"enabled": False, "umos": [A], "max_requests": 1},
                {"umos": [A], "max_requests": 2},
                {"umos": [], "max_requests": 3},
            ],
        )
    )
    assert (await limiter.acquire(A, cost=2)).allowed
    assert not (await limiter.acquire(A)).allowed
    assert (await limiter.acquire(B, cost=3)).allowed
    assert (await limiter.acquire(PRIVATE, cost=3)).allowed


@pytest.mark.asyncio
async def test_missing_or_partial_identity_never_falls_back():
    limiter = RateLimiter(config())
    for umo in ("", None, "10001", "bot_a:GroupMessage:"):
        assert not (await limiter.check_and_consume(event(umo)))[0]
    assert not limiter._rate_limit_buckets


@pytest.mark.asyncio
async def test_group_access_control_is_unchanged():
    cfg = config(global_max=10)
    cfg.group_limit_mode = "whitelist"
    cfg.group_limit_list = {"10002"}
    limiter = RateLimiter(cfg)
    assert await limiter.check_and_consume(event()) == (False, None)
    assert (await limiter.check_and_consume(event(PRIVATE, "")))[0]
    assert count(limiter, "global") == 1


@pytest.mark.asyncio
async def test_weighted_batch_expiry_ceil_and_refund(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("tl.rate_limiter.time.time", lambda: now[0])
    limiter = RateLimiter(config(global_max=5, session_max=5))
    reservation = await limiter.acquire(A, cost=3)
    now[0] += 0.4
    assert (await limiter.acquire(A, cost=2)).allowed
    blocked = await limiter.acquire(A, cost=3)
    assert not blocked.allowed and blocked.retry_after == 60
    await limiter.refund(reservation.token)
    await limiter.refund(reservation.token)
    assert count(limiter, A) == count(limiter, "global") == 2
    assert (await limiter.acquire(A, cost=3)).allowed
    now[0] = 1060.4
    assert (await limiter.acquire(A, cost=5)).allowed
    assert not (await limiter.acquire(A, cost=6)).allowed


@pytest.mark.asyncio
async def test_first_load_is_serialized_and_restores_state():
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def get_kv(key, default):
        calls.append(key)
        if key == RateLimiter.KV_KEY:
            started.set()
            await release.wait()
        return None

    limiter = RateLimiter(config(), get_kv=get_kv)
    first = asyncio.create_task(limiter.acquire(A))
    await started.wait()
    second = asyncio.create_task(limiter.acquire(A))
    release.set()
    results = await asyncio.gather(first, second)
    assert sum(result.allowed for result in results) == 1
    assert calls.count(RateLimiter.KV_KEY) == 1
    assert count(limiter, A) == 1


@pytest.mark.asyncio
async def test_kv_load_failure_and_corruption_fail_closed_then_retry():
    broken = [True]

    async def get_kv(key, default):
        if broken[0]:
            raise OSError("unavailable")
        return None

    limiter = RateLimiter(config(), get_kv=get_kv)
    assert (await limiter.acquire(A)).scope == "storage"
    assert not limiter._loaded
    broken[0] = False
    assert (await limiter.acquire(A)).allowed

    async def corrupt(key, default):
        return {"version": 2, "buckets": {A: [{"at": float("nan")}]}}

    limiter = RateLimiter(config(), get_kv=corrupt)
    assert (await limiter.acquire(A)).scope == "storage"


@pytest.mark.asyncio
async def test_legacy_rules_and_counter_cooldown_do_not_silently_open(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("tl.rate_limiter.time.time", lambda: now[0])
    cfg = config(rules=[{"group_ids": ["10001"]}])

    async def get_kv(key, default):
        return {"10001": [990.0]} if key == RateLimiter.LEGACY_KV_KEY else None

    limiter = RateLimiter(cfg, get_kv=get_kv)
    assert (await limiter.acquire(A)).scope == "migration"
    cfg.rate_limit_rules[0]["group_ids"] = []
    cfg.rate_limit_rules[0]["umos"] = [A]
    assert (await limiter.acquire(A)).retry_after == 50
    assert (await limiter.acquire(None)).allowed
    now[0] = 1050.0
    assert (await limiter.acquire(A)).allowed


@pytest.mark.asyncio
async def test_rate_limiter_debounces_successive_kv_writes():
    writes = []

    async def put_kv(key, value):
        writes.append(copy.deepcopy(value))

    limiter = RateLimiter(config(session_max=10), put_kv=put_kv)
    limiter.SAVE_DEBOUNCE_SECONDS = 60
    try:
        assert (await limiter.acquire(A)).allowed
        assert (await limiter.acquire(A)).allowed
        assert len(writes) == 1
        assert limiter._pending_save_task is not None
    finally:
        await limiter.close()
    assert sum(e["count"] for e in writes[-1]["buckets"][A]) == 2


@pytest.mark.asyncio
async def test_rate_limiter_delayed_save_flushes_latest_bucket(monkeypatch):
    writes = []

    async def put_kv(key, value):
        writes.append(copy.deepcopy(value))

    async def immediate_sleep(delay):
        return None

    limiter = RateLimiter(config(session_max=10), put_kv=put_kv)
    limiter.SAVE_DEBOUNCE_SECONDS = 60
    monkeypatch.setattr("tl.rate_limiter.asyncio.sleep", immediate_sleep)
    await limiter.acquire(A)
    await limiter.acquire(A)
    task = limiter._pending_save_task
    assert task is not None
    await task
    assert len(writes) == 2
    assert sum(e["count"] for e in writes[-1]["buckets"][A]) == 2
    await limiter.close()


@pytest.mark.asyncio
async def test_rate_limiter_reset_clears_buckets_and_forces_kv_write():
    saved = {}

    async def put_kv(key, value):
        saved[key] = copy.deepcopy(value)

    limiter = RateLimiter(config(session_max=10), put_kv=put_kv)
    await limiter.acquire(A)
    await limiter.acquire(A)
    await limiter.reset()
    assert saved[RateLimiter.KV_KEY]["buckets"] == {}
    assert limiter._pending_save_task is None
    await limiter.close()


@pytest.mark.asyncio
async def test_rate_limiter_skips_kv_write_for_unchanged_limited_bucket():
    writes = []

    async def put_kv(key, value):
        writes.append(copy.deepcopy(value))

    limiter = RateLimiter(config(), put_kv=put_kv)
    await limiter.acquire(A)
    assert not (await limiter.acquire(A)).allowed
    assert len(writes) == 1
    await limiter.close()


@pytest.mark.asyncio
async def test_restart_and_close_keep_buckets_and_reject_late_requests():
    saved = {}

    async def put_kv(key, value):
        saved[key] = copy.deepcopy(value)

    async def get_kv(key, default):
        return copy.deepcopy(saved.get(key, default))

    limiter = RateLimiter(config(global_max=1), put_kv=put_kv)
    await limiter.acquire(A)
    await limiter.close()
    assert (await limiter.acquire(B)).scope == "closed"
    restarted = RateLimiter(config(global_max=1), get_kv=get_kv, put_kv=put_kv)
    assert (await restarted.acquire(B)).scope == "global"
    await restarted.close()


@pytest.mark.asyncio
async def test_cancellation_during_save_refunds_all_buckets():
    started, release = asyncio.Event(), asyncio.Event()

    async def put_kv(key, value):
        started.set()
        await release.wait()

    limiter = RateLimiter(config(global_max=1), put_kv=put_kv)
    task = asyncio.create_task(limiter.acquire(A))
    await started.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not limiter._rate_limit_buckets
    await limiter.close()
