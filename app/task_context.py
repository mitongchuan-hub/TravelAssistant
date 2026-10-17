"""Validated workflow-state v2 for bounded cross-turn conversation tasks.

This module accepts untrusted persisted/model-shaped dictionaries only through
``normalize_workflow``.  Mutation helpers always normalize first and return a
new dictionary; callers never install a model-provided workflow wholesale.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from typing import Any, Callable
from uuid import uuid4

from .agent_protocol import FrozenDict, PlanBasis, ProtocolError, thaw


WORKFLOW_SCHEMA_VERSION = 2
MAX_TASKS = 20
MAX_PROGRESS = 24
MAX_UNRESOLVED = 24
MAX_EVIDENCE = 24
MAX_SOURCES = 32
MAX_AUTHORIZATIONS = 32

TASK_STATES = frozenset({"active", "waiting_user", "suspended", "completed", "cancelled", "failed"})
OPEN_TASK_STATES = frozenset({"active", "waiting_user", "suspended"})
TERMINAL_TASK_STATES = frozenset({"completed", "cancelled", "failed"})
TASK_EFFECT_KINDS = frozenset({"read", "requirements", "itinerary", "confirmation", "mixed"})
TRANSITIONS = {
    "active": frozenset({"waiting_user", "suspended", "completed", "cancelled", "failed"}),
    "waiting_user": frozenset({"active", "suspended", "completed", "cancelled", "failed"}),
    "suspended": frozenset({"active", "cancelled", "failed"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
    "failed": frozenset(),
}


class WorkflowError(ValueError):
    """Workflow state is malformed, stale, over capacity, or unauthorized."""


def empty_workflow(*, sequence: int = 0, epoch: int = 0) -> dict[str, Any]:
    return {
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "revision": 0,
        "sequence": _counter(sequence, "sequence"),
        "epoch": _counter(epoch, "epoch"),
        "focus_task_id": None,
        "active_question": None,
        "tasks": [],
    }


def normalize_workflow(
    raw: Any,
    *,
    trip: Any = None,
    trip_id: str | None = None,
    messages: Iterable[Any] | Mapping[str, Any] | None = None,
    plan_basis: PlanBasis | Mapping[str, Any] | None = None,
    plan: Any = None,
    requirements_revision: int | None = None,
) -> dict[str, Any]:
    """Return strict canonical v2 state, conservatively migrating legacy state.

    ``messages`` supplies the actual message IDs visible to this trip.  Unknown
    source/authorization/question message references are removed; a task that
    thereby loses all sources is suspended and cannot authorize a write.
    ``plan_basis`` (or ``plan``) suspends open tasks whose plan dependency is no
    longer current.  The function never trusts IDs merely because they occur in
    ``raw``.
    """
    if trip is not None:
        actual_trip_id = getattr(trip, "id", None) if not isinstance(trip, Mapping) else trip.get("id")
        if trip_id is not None and actual_trip_id != trip_id:
            raise WorkflowError("workflow trip does not match trip_id")
        if trip_id is None:
            trip_id = actual_trip_id
    if trip_id is not None:
        _identifier(trip_id, "trip_id")
    # Trip ownership is enforced by Store; workflow intentionally carries no trip field.
    current_basis = _coerce_plan_basis(plan_basis, plan)
    if requirements_revision is None and trip is not None:
        requirements = trip.get("requirements") if isinstance(trip, Mapping) else getattr(trip, "requirements", None)
        requirements_revision = (
            requirements.get("revision") if isinstance(requirements, Mapping)
            else getattr(requirements, "revision", None)
        )
    if requirements_revision is not None:
        requirements_revision = _counter(requirements_revision, "requirements_revision")
    message_ids = _message_ids(messages)
    if raw in (None, {}):
        return empty_workflow()
    if isinstance(raw, FrozenDict):
        raw = thaw(raw)
    if not isinstance(raw, Mapping):
        raise WorkflowError("workflow state must be an object")
    schema = raw.get("schema_version")
    if schema != WORKFLOW_SCHEMA_VERSION:
        return _migrate_legacy(raw, message_ids=message_ids, current_basis=current_basis)
    allowed = {
        "schema_version", "revision", "sequence", "epoch",
        "focus_task_id", "active_question", "tasks",
    }
    if set(raw) - allowed:
        raise WorkflowError("workflow state contains unknown fields")
    revision = _counter(raw.get("revision", 0), "revision")
    sequence = _counter(raw.get("sequence", 0), "sequence")
    epoch = _counter(raw.get("epoch", 0), "epoch")
    task_values = raw.get("tasks", [])
    if not isinstance(task_values, (list, tuple)) or len(task_values) > MAX_TASKS:
        raise WorkflowError(f"workflow can contain at most {MAX_TASKS} tasks")
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in task_values:
        task = _normalize_task(
            value,
            message_ids=message_ids,
            current_basis=current_basis,
            current_requirements_revision=requirements_revision,
        )
        if task["id"] in seen:
            raise WorkflowError("workflow contains duplicate task IDs")
        seen.add(task["id"])
        tasks.append(task)
    by_id = {task["id"]: task for task in tasks}
    focus = raw.get("focus_task_id")
    if focus is not None and (not isinstance(focus, str) or focus not in by_id or by_id[focus]["status"] in TERMINAL_TASK_STATES):
        focus = None
    if focus is None:
        focus = next((task["id"] for task in reversed(tasks) if task["status"] in OPEN_TASK_STATES), None)
    question = _normalize_question(raw.get("active_question"), by_id=by_id, message_ids=message_ids)
    if question is not None:
        task = by_id[question["task_id"]]
        if task["status"] != "waiting_user":
            question = None
    # A task cannot remain waiting_user without the one visible question.
    active_task_id = question["task_id"] if question else None
    for task in tasks:
        if task["status"] == "waiting_user" and task["id"] != active_task_id:
            task["status"] = "suspended"
            task["status_reason"] = "no active question"
    return {
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "revision": revision,
        "sequence": sequence,
        "epoch": epoch,
        "focus_task_id": focus,
        "active_question": question,
        "tasks": tasks,
    }


def _normalize_task(
    raw: Any,
    *,
    message_ids: set[str] | None,
    current_basis: PlanBasis | None,
    current_requirements_revision: int | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WorkflowError("workflow task must be an object")
    allowed = {
        "id", "goal", "effect_kind", "status", "status_reason", "source_message_ids",
        "authorization_refs", "basis", "progress", "unresolved_items", "evidence",
        "created_sequence", "updated_sequence",
    }
    if set(raw) - allowed:
        raise WorkflowError("workflow task contains unknown fields")
    task_id = _identifier(raw.get("id"), "task id")
    goal = _text(raw.get("goal"), "task goal", maximum=2000)
    effect_kind = raw.get("effect_kind", "read")
    if effect_kind not in TASK_EFFECT_KINDS:
        raise WorkflowError("workflow task has an invalid effect_kind")
    status = raw.get("status")
    if status not in TASK_STATES:
        raise WorkflowError("workflow task has an invalid status")
    status_reason = _optional_text(raw.get("status_reason", ""), "status_reason", maximum=1000)
    sources = _ids(raw.get("source_message_ids", []), "source_message_ids", MAX_SOURCES)
    authorizations = _ids(raw.get("authorization_refs", []), "authorization_refs", MAX_AUTHORIZATIONS)
    if message_ids is not None:
        sources = [item for item in sources if item in message_ids]
        authorizations = [item for item in authorizations if item in message_ids]
    basis = _normalize_basis(raw.get("basis", {}))
    progress = _text_list(raw.get("progress", []), "progress", MAX_PROGRESS, 1000)
    unresolved = _text_list(raw.get("unresolved_items", []), "unresolved_items", MAX_UNRESOLVED, 1000)
    evidence = _evidence(raw.get("evidence", []))
    created_sequence = _counter(raw.get("created_sequence", 0), "created_sequence")
    updated_sequence = _counter(raw.get("updated_sequence", created_sequence), "updated_sequence")
    if updated_sequence < created_sequence:
        raise WorkflowError("updated_sequence cannot precede created_sequence")
    if status in OPEN_TASK_STATES and not sources:
        status = "suspended"
        status_reason = "source authorization needs reconfirmation"
    if status in OPEN_TASK_STATES and (current_basis is not None or current_requirements_revision is not None) and not basis_matches(
        basis, current_basis, current_requirements_revision
    ):
        status = "suspended"
        status_reason = "task basis changed"
    return {
        "id": task_id,
        "goal": goal,
        "effect_kind": effect_kind,
        "status": status,
        "status_reason": status_reason,
        "source_message_ids": sources,
        "authorization_refs": authorizations,
        "basis": basis,
        "progress": progress,
        "unresolved_items": unresolved,
        "evidence": evidence,
        "created_sequence": created_sequence,
        "updated_sequence": updated_sequence,
    }


def _normalize_basis(raw: Any) -> dict[str, Any]:
    if raw in (None, {}):
        return {"requirements_revision": None, "plan_basis": None}
    if not isinstance(raw, Mapping) or set(raw) - {"requirements_revision", "plan_basis"}:
        raise WorkflowError("task basis is invalid")
    requirements_revision = raw.get("requirements_revision")
    if requirements_revision is not None:
        requirements_revision = _counter(requirements_revision, "basis requirements_revision")
    raw_plan = raw.get("plan_basis")
    parsed_plan = None
    if raw_plan is not None:
        try:
            parsed_plan = PlanBasis.from_value(raw_plan).to_dict()
        except ProtocolError as exc:
            raise WorkflowError("task plan basis is invalid") from exc
    return {"requirements_revision": requirements_revision, "plan_basis": parsed_plan}


def basis_matches(
    basis: Mapping[str, Any] | None,
    current_plan_basis: PlanBasis | Mapping[str, Any] | None,
    requirements_revision: int | None = None,
) -> bool:
    """Return whether a task's explicit dependencies still match current state."""
    try:
        normalized = _normalize_basis(basis or {})
        current = None if current_plan_basis is None else PlanBasis.from_value(current_plan_basis)
    except (WorkflowError, ProtocolError):
        return False
    expected_revision = normalized["requirements_revision"]
    if expected_revision is not None and requirements_revision is not None and expected_revision != requirements_revision:
        return False
    expected_plan = normalized["plan_basis"]
    if expected_plan is not None:
        return current is not None and expected_plan == current.to_dict()
    return True


def advance_request(raw: Any) -> dict[str, Any]:
    """Advance the request sequence without implicitly resuming any task."""
    state = normalize_workflow(raw)
    state["sequence"] += 1
    state["revision"] += 1
    return state


def bump_epoch(raw: Any) -> dict[str, Any]:
    """Invalidate every execution captured under an earlier request epoch."""
    state = normalize_workflow(raw)
    state["epoch"] += 1
    state["sequence"] += 1
    state["revision"] += 1
    return state


def clear_workflow(raw: Any, *, reason: str = "chat cleared") -> dict[str, Any]:
    """Cancel unfinished goals and clear the active question, preserving history."""
    state = normalize_workflow(raw)
    for task in state["tasks"]:
        if task["status"] in OPEN_TASK_STATES:
            task["status"] = "cancelled"
            task["status_reason"] = reason[:1000]
            task["updated_sequence"] = state["sequence"] + 1
    state["active_question"] = None
    state["focus_task_id"] = None
    state["epoch"] += 1
    state["sequence"] += 1
    state["revision"] += 1
    return state


def rollback_workflow(raw: Any, *, reason: str = "task execution rolled back") -> dict[str, Any]:
    """Suspend unfinished v2 goals for legacy rollback; never create auto-generation state."""
    state = normalize_workflow(raw)
    for task in state["tasks"]:
        if task["status"] in OPEN_TASK_STATES:
            task["status"] = "suspended"
            task["status_reason"] = reason[:1000]
            task["updated_sequence"] = state["sequence"] + 1
    state["active_question"] = None
    state["focus_task_id"] = next(
        (task["id"] for task in reversed(state["tasks"]) if task["status"] == "suspended"), None
    )
    state["epoch"] += 1
    state["sequence"] += 1
    state["revision"] += 1
    return state


def add_task(
    raw: Any,
    *,
    goal: str,
    source_message_ids: Iterable[str],
    effect_kind: str = "read",
    authorization_refs: Iterable[str] = (),
    basis: Mapping[str, Any] | None = None,
    task_id: str | None = None,
    status: str = "active",
    id_factory: Callable[[str], str] | None = None,
) -> tuple[dict[str, Any], str]:
    """Append one bounded, server-identified task and focus it."""
    state = normalize_workflow(raw)
    if len(state["tasks"]) >= MAX_TASKS:
        raise WorkflowError("too many workflow tasks; close an older task first")
    if status not in {"active", "suspended"}:
        raise WorkflowError("a new task must start active or suspended")
    factory = id_factory or (lambda prefix: f"{prefix}-{uuid4().hex}")
    identifier = task_id or factory("conversation-task")
    if any(task["id"] == identifier for task in state["tasks"]):
        raise WorkflowError("duplicate workflow task ID")
    sequence = state["sequence"]
    task = _normalize_task({
        "id": identifier,
        "goal": goal,
        "effect_kind": effect_kind,
        "status": status,
        "status_reason": "",
        "source_message_ids": list(source_message_ids),
        "authorization_refs": list(authorization_refs),
        "basis": basis or {},
        "progress": [],
        "unresolved_items": [],
        "evidence": [],
        "created_sequence": sequence,
        "updated_sequence": sequence,
    }, message_ids=None, current_basis=None)
    if not task["source_message_ids"]:
        raise WorkflowError("a task needs a real source message")
    state["tasks"].append(task)
    state["focus_task_id"] = identifier
    state["revision"] += 1
    return state, identifier


def transition_task(
    raw: Any,
    task_id: str,
    status: str,
    *,
    reason: str = "",
    progress: Iterable[str] | None = None,
    unresolved_items: Iterable[str] | None = None,
    evidence: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply a legal lifecycle transition; terminal tasks cannot be revived."""
    state = normalize_workflow(raw)
    task = _task(state, task_id)
    if status not in TASK_STATES:
        raise WorkflowError("invalid task status")
    previous = task["status"]
    if status != previous and status not in TRANSITIONS[previous]:
        raise WorkflowError(f"illegal task transition: {previous} -> {status}")
    if progress is not None:
        task["progress"] = _text_list(list(progress), "progress", MAX_PROGRESS, 1000)
    if unresolved_items is not None:
        task["unresolved_items"] = _text_list(list(unresolved_items), "unresolved_items", MAX_UNRESOLVED, 1000)
    if evidence is not None:
        task["evidence"] = _evidence(list(evidence))
    task["status"] = status
    task["status_reason"] = _optional_text(reason, "status_reason", maximum=1000)
    task["updated_sequence"] = state["sequence"]
    if status != "waiting_user" and state["active_question"] and state["active_question"]["task_id"] == task_id:
        state["active_question"] = None
    if status in TERMINAL_TASK_STATES and state["focus_task_id"] == task_id:
        state["focus_task_id"] = next(
            (item["id"] for item in reversed(state["tasks"]) if item["status"] in OPEN_TASK_STATES), None
        )
    elif status in OPEN_TASK_STATES:
        state["focus_task_id"] = task_id
    state["revision"] += 1
    return state


def set_active_question(
    raw: Any,
    *,
    task_id: str,
    text: str,
    answer_target: Mapping[str, str],
    assistant_message_id: str,
    source_message_ids: Iterable[str],
    question_id: str | None = None,
    id_factory: Callable[[str], str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Install the sole externally active question and suspend any prior waiter."""
    state = normalize_workflow(raw)
    target_task = _task(state, task_id)
    if target_task["status"] in TERMINAL_TASK_STATES:
        raise WorkflowError("cannot question for a terminal task")
    previous = state["active_question"]
    if previous and previous["task_id"] != task_id:
        old = _task(state, previous["task_id"])
        if old["status"] == "waiting_user":
            old["status"] = "suspended"
            old["status_reason"] = "another task has the active question"
            old["updated_sequence"] = state["sequence"]
    for task in state["tasks"]:
        if task["id"] != task_id and task["status"] == "waiting_user":
            task["status"] = "suspended"
            task["status_reason"] = "another task has the active question"
            task["updated_sequence"] = state["sequence"]
    factory = id_factory or (lambda prefix: f"{prefix}-{uuid4().hex}")
    question = _normalize_question({
        "id": question_id or factory("question"),
        "task_id": task_id,
        "assistant_message_id": assistant_message_id,
        "text": text,
        "answer_target": dict(answer_target),
        "source_message_ids": list(source_message_ids),
    }, by_id={item["id"]: item for item in state["tasks"]}, message_ids=None)
    assert question is not None
    target_task["status"] = "waiting_user"
    target_task["status_reason"] = ""
    target_task["updated_sequence"] = state["sequence"]
    state["active_question"] = question
    state["focus_task_id"] = task_id
    state["revision"] += 1
    return state, deepcopy(question)


def consume_active_question(raw: Any, *, question_id: str, answer_message_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Consume exactly the current question; a short answer cannot satisfy two tasks."""
    state = normalize_workflow(raw)
    question = state["active_question"]
    if question is None or question["id"] != question_id:
        raise WorkflowError("active question changed")
    _identifier(answer_message_id, "answer message id")
    task = _task(state, question["task_id"])
    task["status"] = "active"
    task["status_reason"] = ""
    task["source_message_ids"] = _ids(
        task["source_message_ids"] + [answer_message_id], "source_message_ids", MAX_SOURCES
    )
    task["updated_sequence"] = state["sequence"]
    state["active_question"] = None
    state["focus_task_id"] = task["id"]
    state["revision"] += 1
    return state, deepcopy(question)


def _normalize_question(
    raw: Any,
    *,
    by_id: Mapping[str, dict[str, Any]],
    message_ids: set[str] | None,
) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise WorkflowError("active_question must be an object")
    expected = {"id", "task_id", "assistant_message_id", "text", "answer_target", "source_message_ids"}
    if set(raw) != expected:
        raise WorkflowError("active_question fields are incomplete")
    question_id = _identifier(raw.get("id"), "question id")
    task_id = _identifier(raw.get("task_id"), "question task id")
    if task_id not in by_id or by_id[task_id]["status"] in TERMINAL_TASK_STATES:
        return None
    assistant_message_id = _identifier(raw.get("assistant_message_id"), "assistant message id")
    source_ids = _ids(raw.get("source_message_ids", []), "question source_message_ids", MAX_SOURCES)
    if message_ids is not None:
        if assistant_message_id not in message_ids:
            return None
        source_ids = [item for item in source_ids if item in message_ids]
    if not source_ids:
        return None
    target = raw.get("answer_target")
    if not isinstance(target, Mapping) or set(target) != {"namespace", "field"}:
        raise WorkflowError("answer_target needs namespace and field")
    answer_target = {
        "namespace": _text(target.get("namespace"), "answer_target namespace", maximum=200),
        "field": _text(target.get("field"), "answer_target field", maximum=200),
    }
    return {
        "id": question_id,
        "task_id": task_id,
        "assistant_message_id": assistant_message_id,
        "text": _text(raw.get("text"), "question text", maximum=2000),
        "answer_target": answer_target,
        "source_message_ids": source_ids,
    }


def _migrate_legacy(
    raw: Mapping[str, Any],
    *,
    message_ids: set[str] | None,
    current_basis: PlanBasis | None,
) -> dict[str, Any]:
    """Migrate old awaiting-generation state without inventing authorization."""
    state = empty_workflow()
    if not raw:
        return state
    if raw.get("awaiting_generation"):
        task_id = "migrated-awaiting-generation"
        sources: list[str] = []
        for key in ("source_message_id", "message_id"):
            value = raw.get(key)
            if isinstance(value, str) and value and (message_ids is None or value in message_ids):
                sources.append(value)
        question = raw.get("question")
        task = {
            "id": task_id,
            "goal": "确认是否继续旧的行程生成目标",
            "effect_kind": "itinerary",
            "status": "suspended",
            "status_reason": "legacy authorization needs reconfirmation",
            "source_message_ids": sources,
            "authorization_refs": [],
            "basis": {
                "requirements_revision": None,
                "plan_basis": current_basis.to_dict() if current_basis else None,
            },
            "progress": [],
            "unresolved_items": [question.strip()] if isinstance(question, str) and question.strip() else ["请确认是否继续原任务"],
            "evidence": [],
            "created_sequence": 0,
            "updated_sequence": 0,
        }
        state["tasks"].append(task)
        state["focus_task_id"] = task_id
        state["revision"] = 1
    return state


def _coerce_plan_basis(raw: Any, plan: Any) -> PlanBasis | None:
    if raw is not None and plan is not None:
        raise WorkflowError("provide plan_basis or plan, not both")
    if raw is not None:
        try:
            return PlanBasis.from_value(raw)
        except ProtocolError as exc:
            raise WorkflowError("current plan_basis is invalid") from exc
    if plan is not None:
        from .agent_protocol import plan_basis as make_plan_basis
        return make_plan_basis(plan)
    return None


def _message_ids(messages: Iterable[Any] | Mapping[str, Any] | None) -> set[str] | None:
    if messages is None:
        return None
    values = messages.keys() if isinstance(messages, Mapping) else messages
    result: set[str] = set()
    for item in values:
        value = item if isinstance(item, str) else item.get("id") if isinstance(item, Mapping) else getattr(item, "id", None)
        if isinstance(value, str) and value:
            result.add(value)
    return result


def _task(state: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    _identifier(task_id, "task id")
    for task in state["tasks"]:
        if task["id"] == task_id:
            return task
    raise WorkflowError("unknown workflow task")


def _counter(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise WorkflowError(f"{label} must be a non-negative integer")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
        raise WorkflowError(f"{label} is invalid")
    return value.strip()


def _text(value: Any, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise WorkflowError(f"{label} is invalid")
    return value.strip()


def _optional_text(value: Any, label: str, *, maximum: int) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or len(value.strip()) > maximum:
        raise WorkflowError(f"{label} is invalid")
    return value.strip()


def _ids(value: Any, label: str, maximum: int) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise WorkflowError(f"{label} contains too many values")
    result = []
    for item in value:
        clean = _identifier(item, label)
        if clean not in result:
            result.append(clean)
    return result


def _text_list(value: Any, label: str, maximum: int, item_maximum: int) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise WorkflowError(f"{label} contains too many values")
    result = []
    for item in value:
        clean = _text(item, label, maximum=item_maximum)
        if clean not in result:
            result.append(clean)
    return result


def _evidence(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_EVIDENCE:
        raise WorkflowError("task evidence contains too many values")
    result = []
    for item in value:
        if not isinstance(item, Mapping):
            raise WorkflowError("task evidence must be objects")
        evidence_id = _identifier(item.get("id"), "evidence id")
        summary = _text(item.get("summary"), "evidence summary", maximum=1000)
        if evidence_id not in {existing["id"] for existing in result}:
            result.append({"id": evidence_id, "summary": summary})
    return result


__all__ = [
    "MAX_TASKS", "OPEN_TASK_STATES", "TASK_EFFECT_KINDS", "TASK_STATES", "TERMINAL_TASK_STATES",
    "TRANSITIONS", "WORKFLOW_SCHEMA_VERSION", "WorkflowError", "add_task",
    "advance_request", "basis_matches", "bump_epoch", "clear_workflow",
    "consume_active_question", "empty_workflow", "normalize_workflow",
    "rollback_workflow", "set_active_question", "transition_task",
]
