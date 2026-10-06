from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from tl.core.errors import (
    InvalidRequestError,
    LeaseError,
    StateTransitionError,
)
from tl.core.leases import Lease, LeaseKind, LeaseSet
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


@pytest.mark.asyncio
async def test_lease_set_releases_all_once_even_when_one_callback_fails() -> None:
    released: list[str] = []

    async def release_ok() -> None:
        released.append("ok")

    def release_bad() -> None:
        released.append("bad")
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

    assert released == ["bad", "ok"]
    assert all(lease.released for lease in leases.leases)


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
