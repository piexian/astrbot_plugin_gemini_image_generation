"""Recovery helpers for the standalone scheduler."""

from __future__ import annotations

from typing import Any

from .models import TMP_REFERENCES_METADATA_KEY, Job
from .states import JobState


async def recoverable_jobs(store: Any) -> list[Job]:
    """Return persisted snapshots after JobStore owner recovery.

    JobStore itself owns stale-owner decisions. The scheduler deliberately does
    not replay interrupted/orphaned jobs implicitly; a later policy can choose
    which snapshots to resubmit without inventing a runtime owner.
    """
    jobs = await store.list_jobs(
        states={
            JobState.ACCEPTED,
            JobState.QUEUED,
            JobState.RUNNING,
            JobState.RESULT_READY,
            JobState.DELIVERY_PENDING,
            JobState.INTERRUPTED,
            JobState.ORPHANED,
        }
    )
    return [
        job for job in jobs if not job.metadata.get(TMP_REFERENCES_METADATA_KEY, False)
    ]
