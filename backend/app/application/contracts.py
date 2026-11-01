"""Detached request snapshots and direct execution results for task persistence."""
from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass
from typing import Any

from app.domain.message import validate_message_text
from app.domain.itinerary.identity import PlanBasis


def serialize(value):
    """Convert snapshots to detached JSON-compatible data for logs and storage."""
    if is_dataclass(value):
        return {field.name: serialize(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {key: serialize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [serialize(item) for item in value]
    return deepcopy(value)


@dataclass(frozen=True)
class RequestContext:
    """Server task identity and state captured before model execution.

    ``request_id`` is the client idempotency key; ``execution_task_id`` is the
    server job identity. ``message_id`` is the triggering user message and is
    distinct from the task's assistant placeholder ``thinking_id``.
    ``conversation_task_id`` is legacy workflow linkage, not another request ID.
    """
    trip_id: str
    owner_id: str
    execution_task_id: str
    conversation_task_id: str | None
    request_id: str
    # Compatibility name: this is the original user message ID, not the task ID.
    message_id: str
    body: str
    entry: str
    strategy: str
    requirements_revision: int
    plan_basis: PlanBasis
    workflow_revision: int
    sequence: int
    epoch: int

    def __post_init__(self):
        validate_message_text(self.body, allow_empty=True)
        object.__setattr__(self, 'plan_basis', PlanBasis.from_value(self.plan_basis))


@dataclass
class ExecutionResult:
    """One reply and at most one validated change package; no candidate registry."""
    answer: str = ''
    changes: dict | None = None
    failed: bool = False
    reason: str = ''

    def __post_init__(self):
        if not isinstance(self.answer, str) or len(self.answer) > 50000:
            raise ValueError('执行回复格式不正确')
        if type(self.failed) is not bool or not isinstance(self.reason, str) or len(self.reason) > 4000:
            raise ValueError('执行状态格式不正确')
        if self.changes is not None and (not isinstance(self.changes, dict) or self.failed):
            raise ValueError('失败结果不能包含业务变更，变更必须为对象')
        self.changes = deepcopy(self.changes)

    @classmethod
    def failure(cls, reason):
        return cls(failed=True, reason=reason)


@dataclass
class CommitReceipt:
    """Persisted task outcome. Legacy receipt fields remain readable on replay."""
    trip_id: str
    execution_task_id: str
    request_id: str
    committed: bool
    accepted_artifact_ids: tuple = ()
    rejected_artifacts: tuple = ()
    after_requirements_revision: int = 0
    after_plan_basis: PlanBasis = PlanBasis('', '', '', '', None, False)
    after_workflow_revision: int = 0
    after_sequence: int = 0
    after_epoch: int = 0
    message_id: str | None = None
    message_status: str = ''
    task_status: str = ''
    conversation_task_states: dict | None = None
    reason: str = ''

    def __post_init__(self):
        self.after_plan_basis = PlanBasis.from_value(self.after_plan_basis)
        self.accepted_artifact_ids = tuple(self.accepted_artifact_ids)
        self.rejected_artifacts = tuple(self.rejected_artifacts)
        self.conversation_task_states = deepcopy(self.conversation_task_states or {})
