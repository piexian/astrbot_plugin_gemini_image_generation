"""Job 状态和状态转换规则。

状态名是持久化和入口兼容层之间的公共契约，因此这里使用普通
``str`` 枚举，序列化后仍然是稳定的小写字符串。
"""

from __future__ import annotations

from enum import Enum


class JobState(str, Enum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    RUNNING = "running"
    RESULT_READY = "result_ready"
    DELIVERY_PENDING = "delivery_pending"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    ORPHANED = "orphaned"


TERMINAL_STATES: frozenset[JobState] = frozenset(
    {
        JobState.SUCCEEDED,
        JobState.PARTIAL,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.INTERRUPTED,
        JobState.ORPHANED,
    }
)

# 旧 tracker 的 ``partial_success`` 在兼容 facade 中映射到 v2 的 ``partial``。
LEGACY_STATE_ALIASES: dict[str, JobState] = {
    "partial_success": JobState.PARTIAL,
}

# ``accepted`` is persisted before a runtime task is attached. ``orphaned``
# describes a persisted job for which that attachment failed or was lost after
# a restart. Terminal states deliberately have no outgoing transitions.
ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.ACCEPTED: frozenset(
        {JobState.QUEUED, JobState.RUNNING, JobState.CANCELLED, JobState.ORPHANED}
    ),
    JobState.QUEUED: frozenset(
        {JobState.RUNNING, JobState.CANCELLED, JobState.INTERRUPTED, JobState.ORPHANED}
    ),
    JobState.RUNNING: frozenset(
        {
            JobState.RESULT_READY,
            JobState.DELIVERY_PENDING,
            JobState.SUCCEEDED,
            JobState.PARTIAL,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.INTERRUPTED,
            JobState.ORPHANED,
        }
    ),
    JobState.RESULT_READY: frozenset(
        {
            JobState.DELIVERY_PENDING,
            JobState.SUCCEEDED,
            JobState.PARTIAL,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.INTERRUPTED,
        }
    ),
    JobState.DELIVERY_PENDING: frozenset(
        {JobState.SUCCEEDED, JobState.PARTIAL, JobState.FAILED, JobState.CANCELLED}
    ),
    JobState.SUCCEEDED: frozenset(),
    JobState.PARTIAL: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
    JobState.INTERRUPTED: frozenset(),
    JobState.ORPHANED: frozenset(),
}


def coerce_state(value: JobState | str) -> JobState:
    """将持久化字符串转换为状态枚举，并拒绝未知值。"""

    if isinstance(value, JobState):
        return value
    alias = LEGACY_STATE_ALIASES.get(str(value))
    if alias is not None:
        return alias
    try:
        return JobState(str(value))
    except ValueError as exc:
        raise ValueError(f"未知 Job 状态: {value!r}") from exc


def can_transition(current: JobState | str, target: JobState | str) -> bool:
    """返回一次状态转换是否符合领域规则。"""

    return coerce_state(target) in ALLOWED_TRANSITIONS[coerce_state(current)]
