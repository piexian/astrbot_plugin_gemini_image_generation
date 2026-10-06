"""核心生图领域模型。

第一阶段只提供稳定的领域类型和协议；现有入口仍使用旧的调度、追踪和
供应商适配器。后续阶段可以在不改变入口兼容接口的前提下接入这些类型。
"""

from .errors import (
    CoreError,
    InvalidRequestError,
    JobCancelledError,
    JobNotFoundError,
    QueueFullError,
    ServiceClosedError,
    StateTransitionError,
)
from .leases import Lease, LeaseKind, LeaseSet
from .models import Artifact, Attempt, Job, JobEvent, LeaseRecord
from .requests import BodyFactory, GenerationRequest, ProviderAttempt
from .results import GenerationResult
from .states import (
    LEGACY_STATE_ALIASES,
    TERMINAL_STATES,
    JobState,
    can_transition,
    coerce_state,
)

__all__ = [
    "Artifact",
    "Attempt",
    "BodyFactory",
    "CoreError",
    "GenerationRequest",
    "GenerationResult",
    "InvalidRequestError",
    "Job",
    "JobCancelledError",
    "JobEvent",
    "JobNotFoundError",
    "JobState",
    "LeaseRecord",
    "Lease",
    "LeaseKind",
    "LeaseSet",
    "LEGACY_STATE_ALIASES",
    "ProviderAttempt",
    "QueueFullError",
    "ServiceClosedError",
    "StateTransitionError",
    "TERMINAL_STATES",
    "can_transition",
    "coerce_state",
]
