from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from tl.core.errors import (
    InvalidRequestError,
    LeaseError,
    StateTransitionError,
)
from tl.core.leases import Lease, LeaseKind, LeaseSet, LeaseStatus
from tl.core.models import Job
from tl.core.requests import GenerationRequest, ProviderAttempt
from tl.core.results import GenerationResult
from tl.core.states import JobState, can_transition, coerce_state


def test_generation_request_normalizes_legacy_reference_lists() -> None:
    request = GenerationRequest.from_values(
        prompt="draw",
        source="command",
        reference_images=["data:image/png;base64,abc"],
    )

    assert request.reference_images == ("data:image/png;base64,abc",)
    assert request.image_count == 1


@pytest.mark.parametrize(
    "values",
    [
        {"prompt": "", "source": "command"},
        {"prompt": "draw", "source": "", "image_count": 1},
        {"prompt": "draw", "source": "command", "image_count": 0},
        {"prompt": "draw", "source": "command", "watermark": "yes"},
        {"prompt": "draw", "source": "command", "reference_images": [""]},
    ],
)
def test_generation_request_rejects_invalid_values(values) -> None:
    with pytest.raises(InvalidRequestError):
        GenerationRequest.from_values(**values)


def test_job_lifecycle_serializes_and_restores_without_losing_state() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    request = GenerationRequest(
        prompt="draw",
        source="webui",
        provider="google",
        requester={"user_id": "u1"},
        deadline_at=now + timedelta(minutes=1),
    )
    job = Job.create(request, job_id="job-1", now=now)
    job.transition(JobState.QUEUED, now=now)
    job.transition(JobState.RUNNING, now=now)
    job.set_result(
        GenerationResult(
            artifacts=("gallery/a.png",),
            provider="google",
            model="gemini",
        ),
        now=now,
    )
    job.transition(JobState.RESULT_READY, now=now)

    restored = Job.from_dict(job.as_dict())

    assert restored.job_id == "job-1"
    assert restored.state is JobState.RESULT_READY
    assert restored.request.provider == "google"
    assert restored.result is not None
    assert restored.result.artifacts == ("gallery/a.png",)
    assert restored.deadline_at == now + timedelta(minutes=1)
    assert restored.revision == 0


def test_job_revision_must_be_non_negative_and_is_serialized() -> None:
    request = GenerationRequest(prompt="draw", source="test")
    job = Job.create(request, job_id="job-1")

    assert job.revision == 0
    restored = Job.from_dict({**job.as_dict(), "revision": 3})
    assert restored.revision == 3
    with pytest.raises(ValueError, match="revision"):
        Job(job_id="invalid", request=request, revision=-1)


def test_state_machine_rejects_terminal_reuse() -> None:
    assert can_transition(JobState.RUNNING, JobState.FAILED)
    assert not can_transition(JobState.SUCCEEDED, JobState.RUNNING)

    request = GenerationRequest(prompt="draw", source="sdk")
    job = Job.create(request, job_id="job-1")
    job.transition(JobState.QUEUED)
    with pytest.raises(StateTransitionError):
        job.transition(JobState.SUCCEEDED)

    assert coerce_state("partial_success") is JobState.PARTIAL


def test_provider_attempt_uses_remaining_deadline() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    attempt = ProviderAttempt(number=1, deadline_at=now + timedelta(seconds=2.5))

    assert attempt.remaining_seconds(now) == pytest.approx(2.5)
    assert attempt.remaining_seconds(now + timedelta(seconds=3)) == 0


def test_naive_deadline_is_rejected() -> None:
    naive = datetime(2026, 1, 1)

    with pytest.raises(InvalidRequestError, match="naive"):
        GenerationRequest(prompt="draw", source="test", deadline_at=naive)
    with pytest.raises(InvalidRequestError, match="naive"):
        ProviderAttempt(number=1, deadline_at=naive)
    with pytest.raises(ValueError, match="naive"):
        Job.from_dict(
            {
                "job_id": "naive-job",
                "state": "accepted",
                "deadline_at": "2026-01-01T00:00:00",
                "request": {"prompt": "draw", "source": "test"},
            }
        )


def test_deadline_roundtrip() -> None:
    deadline = datetime(2026, 1, 1, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    request = GenerationRequest(prompt="draw", source="test", deadline_at=deadline)
    job = Job.create(request, job_id="deadline-job")
    restored = Job.from_dict(job.as_dict())

    assert job.deadline_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert job.request.deadline_at == job.deadline_at
    assert restored.deadline_at == job.deadline_at
    assert restored.request.deadline_at == restored.deadline_at


def test_remaining_deadline_is_consistent() -> None:
    deadline = datetime(2026, 1, 1, 0, 0, 10, tzinfo=timezone.utc)
    attempt = ProviderAttempt(number=1, deadline_at=deadline)
    now_utc = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now_offset = datetime(2026, 1, 1, 8, 0, tzinfo=timezone(timedelta(hours=8)))

    assert attempt.remaining_seconds(now_utc) == pytest.approx(10)
    assert attempt.remaining_seconds(now_offset) == pytest.approx(10)
    with pytest.raises(InvalidRequestError, match="naive"):
        attempt.remaining_seconds(datetime(2026, 1, 1))


@pytest.mark.asyncio
async def test_lease_set_releases_all_once_even_when_one_callback_fails() -> None:
    released: list[str] = []
    failures = 1

    async def release_ok() -> None:
        released.append("ok")

    def release_bad() -> None:
        nonlocal failures
        released.append("bad")
        if failures:
            failures -= 1
            raise RuntimeError("cleanup failed")

    leases = LeaseSet(
        [
            Lease("a", LeaseKind.ARTIFACT, "job-1", release_ok),
            Lease("b", LeaseKind.QUOTA, "job-1", release_bad),
        ]
    )
    with pytest.raises(LeaseError):
        await leases.release_all()
    await leases.release_all()

    assert released == ["bad", "ok", "bad"]
    assert all(lease.released for lease in leases.leases)


@pytest.mark.asyncio
async def test_failed_lease_release_can_retry() -> None:
    attempts = 0

    async def callback() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary failure")

    lease = Lease("retry", LeaseKind.QUOTA, "job-1", callback)
    with pytest.raises(LeaseError):
        await lease.release()
    assert lease.status == LeaseStatus.RELEASE_FAILED
    assert lease.released is False

    await lease.release()
    assert lease.status == LeaseStatus.RELEASED
    assert lease.release_attempts == 2


@pytest.mark.asyncio
async def test_cancelled_lease_release_propagates() -> None:
    async def callback() -> None:
        raise asyncio.CancelledError("release cancelled")

    lease = Lease("cancel", LeaseKind.REFERENCE, "job-1", callback)
    with pytest.raises(asyncio.CancelledError, match="release cancelled"):
        await lease.release()
    assert lease.status == LeaseStatus.RELEASE_FAILED
    assert lease.released is False


@pytest.mark.asyncio
async def test_release_all_continues_after_failure() -> None:
    called: list[str] = []
    failures = 1

    async def fail_once() -> None:
        nonlocal failures
        called.append("failed")
        if failures:
            failures -= 1
            raise RuntimeError("temporary failure")

    async def succeed() -> None:
        called.append("succeeded")

    failed = Lease("failed", LeaseKind.ARTIFACT, "job-1", fail_once)
    succeeded = Lease("succeeded", LeaseKind.DELIVERY, "job-1", succeed)
    leases = LeaseSet([failed, succeeded])
    with pytest.raises(LeaseError):
        await leases.release_all()
    assert called == ["succeeded", "failed"]
    assert failed.status == LeaseStatus.RELEASE_FAILED
    assert succeeded.status == LeaseStatus.RELEASED

    await leases.release_all()
    assert failed.status == LeaseStatus.RELEASED


@pytest.mark.asyncio
async def test_failed_lease_is_visible() -> None:
    async def callback() -> None:
        raise ValueError("quota still held")

    lease = Lease("visible", LeaseKind.QUOTA, "job-1", callback)
    with pytest.raises(LeaseError):
        await lease.release()

    assert lease.status == LeaseStatus.RELEASE_FAILED
    assert lease.release_error is not None
    assert "quota still held" in lease.release_error


@pytest.mark.asyncio
async def test_lease_set_context_releases_on_cancelled_scope() -> None:
    released: list[str] = []
    lease = Lease("r", LeaseKind.REFERENCE, "job-1", lambda: released.append("r"))

    with pytest.raises(asyncio.CancelledError):
        async with LeaseSet([lease]):
            raise asyncio.CancelledError

    assert released == ["r"]


def test_core_error_dict_does_not_include_exception_repr() -> None:
    error = InvalidRequestError("bad request", details={"field": "prompt"})

    assert error.as_dict() == {
        "code": "invalid_request",
        "message": "bad request",
        "retryable": False,
        "details": {"field": "prompt"},
    }


def test_public_serialization_masks_provider_secrets() -> None:
    request = GenerationRequest(
        prompt="draw",
        source="sdk",
        metadata={"api_key": "secret", "nested": {"token": "value"}},
    )
    job = Job.create(request, job_id="job-1")

    public = job.as_dict()

    assert public["request"]["metadata"] == {
        "api_key": "***",
        "nested": {"token": "***"},
    }


def test_serialization_layers_are_explicit() -> None:
    request = GenerationRequest(
        prompt="secret prompt",
        source="command",
        reference_images=("https://example.test/a.png?token=secret",),
        requester={"user_id": "u1", "api_key": "must-drop"},
    )
    job = Job.create(request, job_id="job-serialization")

    storage = job.to_storage_dict()
    public = job.to_public_dict()
    log = job.to_log_dict()

    assert storage["request"]["reference_images"][0]["reference_id"].startswith("ref:")
    assert "token=secret" not in repr(storage)
    assert "api_key" not in storage["request"]["requester"]
    assert public == storage
    assert "prompt" not in log
