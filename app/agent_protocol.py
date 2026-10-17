"""Immutable contracts for task-execution agents and the Store commit boundary.

The model may choose references from these contracts, but it never creates their
identities or provenance.  :mod:`app.turn_workspace` is the only normal producer
of ``TerminalProposal`` objects.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from enum import Enum
from hashlib import sha256
import hmac
import json
import secrets
from typing import Any, Callable


ARTIFACT_KINDS = frozenset({"requirements", "itinerary", "confirmation", "task", "evidence"})
EFFECT_KINDS = frozenset({"independent", "conditional", "hypothetical"})
CONDITION_STATES = frozenset({"satisfied", "not_satisfied", "unknown"})
ENTRY_KINDS = frozenset({"main", "planning", "revision", "confirm"})
STRATEGIES = frozenset({"legacy", "task_execution"})


class ProtocolError(ValueError):
    """A proposed protocol value is malformed or outside its authority."""


class FrozenDict(Mapping[str, Any]):
    """A small deeply immutable, JSON-shaped mapping.

    ``dataclass(frozen=True)`` alone is insufficient when a field contains a
    mutable dict.  Protocol objects use ``FrozenDict`` at every mapping boundary.
    """

    __slots__ = ("__data", "__hash")

    def __init__(self, value: Mapping[str, Any] | None = None) -> None:
        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise ProtocolError("expected a mapping")
        object.__setattr__(self, "_FrozenDict__data", {str(key): freeze(item) for key, item in value.items()})
        object.__setattr__(self, "_FrozenDict__hash", None)

    def __getitem__(self, key: str) -> Any:
        return self.__data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.__data)

    def __len__(self) -> int:
        return len(self.__data)

    def __repr__(self) -> str:
        return f"FrozenDict({self.__data!r})"

    def __hash__(self) -> int:
        cached = self.__hash
        if cached is None:
            cached = hash(tuple(sorted((key, _hashable(value)) for key, value in self.__data.items())))
            object.__setattr__(self, "_FrozenDict__hash", cached)
        return cached

    def __deepcopy__(self, memo: dict[int, Any]) -> FrozenDict:
        return self

    def to_dict(self) -> dict[str, Any]:
        return {key: thaw(value) for key, value in self.__data.items()}


def freeze(value: Any) -> Any:
    """Deep-freeze JSON-shaped values used at trust boundaries."""
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, Mapping):
        return FrozenDict(value)
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((freeze(item) for item in value), key=repr))
    return value


def thaw(value: Any) -> Any:
    """Return a detached mutable/JSON-friendly representation."""
    if isinstance(value, FrozenDict):
        return value.to_dict()
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: thaw(getattr(value, field.name)) for field in fields(value) if not field.name.startswith("_")}
    return value


def _hashable(value: Any) -> Any:
    if isinstance(value, FrozenDict):
        return hash(value)
    if isinstance(value, tuple):
        return tuple(_hashable(item) for item in value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return tuple((field.name, _hashable(getattr(value, field.name))) for field in fields(value))
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


def _identifier(value: Any, label: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ProtocolError(f"{label} must be a non-empty server identifier")
    return value.strip()


def _revision(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ProtocolError(f"{label} must be a non-negative integer")
    return value


def _strings(value: Any, label: str, *, maximum: int = 64) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise ProtocolError(f"{label} must contain at most {maximum} identifiers")
    result: list[str] = []
    for item in value:
        clean = _identifier(item, label)
        assert clean is not None
        if clean not in result:
            result.append(clean)
    return tuple(result)


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _object_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): thaw(item) for key, item in value.items()}
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return {str(key): thaw(item) for key, item in vars(value).items() if not key.startswith("_")}
    raise ProtocolError("plan must be a dataclass, mapping, or object")


@dataclass(frozen=True)
class PlanBasis:
    """Identity of committed plan business content, excluding async display data."""

    plan_id: str
    content_hash: str
    business_hash: str
    status: str
    requirements_revision: int | None
    has_content: bool

    def __post_init__(self) -> None:
        if not isinstance(self.plan_id, str) or len(self.plan_id) > 200:
            raise ProtocolError("plan_id is invalid")
        for name in ("content_hash", "business_hash"):
            value = getattr(self, name)
            if value and (not isinstance(value, str) or len(value) != 64):
                raise ProtocolError(f"{name} must be a sha256 digest")
        if not isinstance(self.status, str) or len(self.status) > 100:
            raise ProtocolError("plan status is invalid")
        if self.requirements_revision is not None:
            _revision(self.requirements_revision, "plan requirements_revision")
        if type(self.has_content) is not bool:
            raise ProtocolError("has_content must be boolean")

    @classmethod
    def from_value(cls, value: PlanBasis | Mapping[str, Any]) -> PlanBasis:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ProtocolError("plan_basis must be PlanBasis or a mapping")
        expected = {field.name for field in fields(cls)}
        if set(value) != expected:
            raise ProtocolError("plan_basis fields are incomplete")
        return cls(**{name: value[name] for name in expected})

    def to_dict(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


_DISPLAY_FIELDS = frozenset({"display", "display_source_hash", "display_status"})


def plan_basis(plan: Any) -> PlanBasis:
    """Build a stable basis for committed plan content.

    Presentation/display fields are deliberately excluded so an asynchronous
    display job cannot create a false conflict for otherwise unchanged prose.
    """
    raw = _object_mapping(plan)
    if not raw:
        return PlanBasis("", "", "", "", None, False)
    business = {key: value for key, value in raw.items() if key not in _DISPLAY_FIELDS}
    text = business.get("text")
    days = business.get("days")
    content = text if isinstance(text, str) and text.strip() else days if isinstance(days, list) else []
    has_content = bool((isinstance(text, str) and text.strip()) or days)
    requirements_revision = business.get("requirements_revision")
    if requirements_revision is not None and (type(requirements_revision) is not int or requirements_revision < 0):
        requirements_revision = None
    return PlanBasis(
        plan_id=str(business.get("id") or "")[:200],
        content_hash=_stable_hash(content) if has_content else "",
        business_hash=_stable_hash(business),
        status=str(business.get("status") or "")[:100],
        requirements_revision=requirements_revision,
        has_content=has_content,
    )


@dataclass(frozen=True)
class RequestContext:
    """Immutable authority captured by the server for one execution request."""

    trip_id: str
    owner_id: str
    execution_task_id: str
    conversation_task_id: str | None
    request_id: str
    message_id: str
    body: str
    entry: str
    strategy: str
    requirements_revision: int
    plan_basis: PlanBasis
    workflow_revision: int
    sequence: int
    epoch: int

    def __post_init__(self) -> None:
        for name in ("trip_id", "owner_id", "execution_task_id", "request_id", "message_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        object.__setattr__(self, "conversation_task_id", _identifier(
            self.conversation_task_id, "conversation_task_id", optional=True
        ))
        if not isinstance(self.body, str) or len(self.body) > 10000:
            raise ProtocolError("body must be text no longer than 10000 characters")
        if self.entry not in ENTRY_KINDS:
            raise ProtocolError(f"entry must be one of {sorted(ENTRY_KINDS)}")
        if self.strategy not in STRATEGIES:
            raise ProtocolError(f"strategy must be one of {sorted(STRATEGIES)}")
        for name in ("requirements_revision", "workflow_revision", "sequence", "epoch"):
            _revision(getattr(self, name), name)
        object.__setattr__(self, "plan_basis", PlanBasis.from_value(self.plan_basis))

    def same_authority(self, other: RequestContext) -> bool:
        return (
            isinstance(other, RequestContext)
            and self.trip_id == other.trip_id
            and self.owner_id == other.owner_id
            and self.execution_task_id == other.execution_task_id
            and self.request_id == other.request_id
            and self.message_id == other.message_id
            and self.sequence == other.sequence
            and self.epoch == other.epoch
        )


class CapabilityStatus(str, Enum):
    OK = "ok"
    NEEDS_INPUT = "needs_input"
    NEEDS_REQUIREMENTS = "needs_requirements"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True)
class TerminalQuestion:
    id: str
    conversation_task_id: str
    text: str
    answer_target: FrozenDict
    source_message_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _identifier(self.id, "question id"))
        object.__setattr__(self, "conversation_task_id", _identifier(
            self.conversation_task_id, "question conversation_task_id"
        ))
        if not isinstance(self.text, str) or not self.text.strip() or len(self.text.strip()) > 2000:
            raise ProtocolError("question text must contain 1 to 2000 characters")
        object.__setattr__(self, "text", self.text.strip())
        target = self.answer_target if isinstance(self.answer_target, FrozenDict) else FrozenDict(self.answer_target)
        if set(target) != {"namespace", "field"} or any(
            not isinstance(target[key], str) or not target[key].strip() or len(target[key]) > 200
            for key in target
        ):
            raise ProtocolError("answer_target must contain namespace and field")
        object.__setattr__(self, "answer_target", target)
        object.__setattr__(self, "source_message_ids", _strings(
            self.source_message_ids, "question source_message_ids", maximum=16
        ))


@dataclass(frozen=True)
class Artifact:
    """A server-identified candidate; payload never controls provenance."""

    id: str
    kind: str
    payload: FrozenDict
    source_capability: str
    trip_id: str
    owner_id: str
    execution_task_id: str
    conversation_task_id: str | None
    request_id: str
    message_id: str
    authorization_refs: tuple[str, ...]
    requirements_revision: int
    plan_basis: PlanBasis
    workflow_revision: int
    sequence: int
    epoch: int
    depends_on: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    effect: str = "independent"
    condition: str = "satisfied"

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _identifier(self.id, "artifact id"))
        if self.kind not in ARTIFACT_KINDS:
            raise ProtocolError(f"unknown artifact kind: {self.kind}")
        payload = self.payload if isinstance(self.payload, FrozenDict) else FrozenDict(self.payload)
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "source_capability", _identifier(self.source_capability, "source_capability"))
        for name in ("trip_id", "owner_id", "execution_task_id", "request_id", "message_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        object.__setattr__(self, "conversation_task_id", _identifier(
            self.conversation_task_id, "conversation_task_id", optional=True
        ))
        object.__setattr__(self, "authorization_refs", _strings(
            self.authorization_refs, "authorization_refs", maximum=32
        ))
        object.__setattr__(self, "depends_on", _strings(self.depends_on, "depends_on", maximum=32))
        object.__setattr__(self, "evidence_refs", _strings(
            self.evidence_refs, "evidence_refs", maximum=64
        ))
        for name in ("requirements_revision", "workflow_revision", "sequence", "epoch"):
            _revision(getattr(self, name), name)
        object.__setattr__(self, "plan_basis", PlanBasis.from_value(self.plan_basis))
        if self.effect not in EFFECT_KINDS:
            raise ProtocolError(f"unknown effect kind: {self.effect}")
        if self.condition not in CONDITION_STATES:
            raise ProtocolError(f"unknown condition state: {self.condition}")
        if self.effect == "independent" and self.condition != "satisfied":
            raise ProtocolError("independent artifacts cannot have an unresolved condition")
        _validate_payload(self.kind, payload)

    @property
    def artifact_id(self) -> str:
        return self.id

    @property
    def is_evidence(self) -> bool:
        return self.kind == "evidence"

    def belongs_to(self, context: RequestContext) -> bool:
        return (
            self.trip_id == context.trip_id
            and self.owner_id == context.owner_id
            and self.execution_task_id == context.execution_task_id
            and self.request_id == context.request_id
            and self.message_id == context.message_id
            and self.sequence == context.sequence
            and self.epoch == context.epoch
        )


def _validate_payload(kind: str, payload: FrozenDict) -> None:
    if kind == "requirements":
        required = {"normalized_changes", "raw_patch"}
        if not required.issubset(payload) or not all(isinstance(payload[key], FrozenDict) for key in required):
            raise ProtocolError("requirements payload needs normalized_changes and raw_patch mappings")
    elif kind == "itinerary":
        if not isinstance(payload.get("text"), str) or not payload["text"].strip():
            raise ProtocolError("itinerary payload needs non-empty text")
        if "evidence" not in payload or not isinstance(payload["evidence"], tuple):
            raise ProtocolError("itinerary payload needs an evidence list")
        if type(payload.get("preserve_plan_basis")) is not bool:
            raise ProtocolError("itinerary payload needs preserve_plan_basis boolean")
    elif kind == "confirmation":
        raw_basis = payload.get("plan_basis")
        if not isinstance(raw_basis, FrozenDict):
            raise ProtocolError("confirmation payload needs committed plan_basis")
        PlanBasis.from_value(raw_basis)
    elif kind == "task":
        if not isinstance(payload.get("operation"), str) or not payload["operation"].strip():
            raise ProtocolError("task payload needs an operation")
    elif kind == "evidence":
        if not payload:
            raise ProtocolError("evidence payload cannot be empty")


@dataclass(frozen=True)
class CapabilityResult:
    status: CapabilityStatus
    source_capability: str
    summary: str = ""
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    completed_items: tuple[str, ...] = ()
    unresolved_items: tuple[str, ...] = ()
    input_request: TerminalQuestion | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        try:
            status = self.status if isinstance(self.status, CapabilityStatus) else CapabilityStatus(self.status)
        except ValueError as exc:
            raise ProtocolError("unknown capability status") from exc
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "source_capability", _identifier(self.source_capability, "source_capability"))
        if not isinstance(self.summary, str) or len(self.summary) > 4000:
            raise ProtocolError("capability summary is too long")
        for name, maximum in (("artifact_refs", 32), ("evidence_refs", 64),
                              ("completed_items", 32), ("unresolved_items", 32)):
            object.__setattr__(self, name, _strings(getattr(self, name), name, maximum=maximum))
        if self.input_request is not None and not isinstance(self.input_request, TerminalQuestion):
            raise ProtocolError("input_request must be a TerminalQuestion")
        if status == CapabilityStatus.NEEDS_INPUT and self.input_request is None:
            raise ProtocolError("needs_input requires input_request")
        if status != CapabilityStatus.NEEDS_INPUT and self.input_request is not None:
            raise ProtocolError("only needs_input may carry input_request")
        if self.error is not None and (not isinstance(self.error, str) or len(self.error) > 4000):
            raise ProtocolError("capability error is invalid")


@dataclass(frozen=True)
class TerminalProposal:
    """A sealed, non-persistent proposal containing all selected candidates."""

    id: str
    context: RequestContext
    artifacts: tuple[Artifact, ...]
    evidence: tuple[Artifact, ...]
    selected_artifact_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    workflow_state: FrozenDict | None
    answer: str
    question: TerminalQuestion | None
    failed: bool
    reason: str
    _workspace_token: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _identifier(self.id, "proposal id"))
        if not isinstance(self.context, RequestContext):
            raise ProtocolError("proposal context must be RequestContext")
        artifacts = tuple(self.artifacts)
        evidence = tuple(self.evidence)
        if any(not isinstance(item, Artifact) or item.kind == "evidence" for item in artifacts):
            raise ProtocolError("proposal artifacts contain an invalid artifact")
        if any(not isinstance(item, Artifact) or item.kind != "evidence" for item in evidence):
            raise ProtocolError("proposal evidence contains a non-evidence artifact")
        if any(not item.belongs_to(self.context) for item in artifacts + evidence):
            raise ProtocolError("proposal contains a foreign artifact")
        if len({item.id for item in artifacts + evidence}) != len(artifacts) + len(evidence):
            raise ProtocolError("proposal contains duplicate artifact identifiers")
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "evidence", evidence)
        selected = _strings(self.selected_artifact_ids, "selected_artifact_ids", maximum=32)
        refs = _strings(self.evidence_refs, "evidence_refs", maximum=64)
        if selected != tuple(item.id for item in artifacts):
            raise ProtocolError("selected_artifact_ids do not match embedded artifacts")
        if refs != tuple(item.id for item in evidence):
            raise ProtocolError("evidence_refs do not match embedded evidence")
        object.__setattr__(self, "selected_artifact_ids", selected)
        object.__setattr__(self, "evidence_refs", refs)
        if self.workflow_state is not None and not isinstance(self.workflow_state, FrozenDict):
            object.__setattr__(self, "workflow_state", FrozenDict(self.workflow_state))
        if not isinstance(self.answer, str) or len(self.answer) > 50000:
            raise ProtocolError("terminal answer is invalid")
        if self.question is not None and not isinstance(self.question, TerminalQuestion):
            raise ProtocolError("terminal question is invalid")
        if type(self.failed) is not bool:
            raise ProtocolError("failed must be boolean")
        if not isinstance(self.reason, str) or len(self.reason) > 4000:
            raise ProtocolError("terminal reason is invalid")
        if not isinstance(self._workspace_token, str):
            raise ProtocolError("workspace token is invalid")

    @property
    def workspace_verified(self) -> bool:
        return bool(self._workspace_token)


@dataclass(frozen=True)
class RejectedArtifact:
    artifact_id: str
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _identifier(self.artifact_id, "artifact_id"))
        if not isinstance(self.reason, str) or not self.reason.strip() or len(self.reason) > 2000:
            raise ProtocolError("rejection reason is invalid")


@dataclass(frozen=True)
class CommitReceipt:
    """What the Store actually committed, never what the model claimed."""

    trip_id: str
    execution_task_id: str
    request_id: str
    committed: bool
    accepted_artifact_ids: tuple[str, ...] = ()
    rejected_artifacts: tuple[RejectedArtifact, ...] = ()
    after_requirements_revision: int = 0
    after_plan_basis: PlanBasis = PlanBasis("", "", "", "", None, False)
    after_workflow_revision: int = 0
    after_sequence: int = 0
    after_epoch: int = 0
    message_id: str | None = None
    message_status: str = ""
    task_status: str = ""
    conversation_task_states: FrozenDict = FrozenDict()
    reason: str = ""

    def __post_init__(self) -> None:
        for name in ("trip_id", "execution_task_id", "request_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        if type(self.committed) is not bool:
            raise ProtocolError("committed must be boolean")
        object.__setattr__(self, "accepted_artifact_ids", _strings(
            self.accepted_artifact_ids, "accepted_artifact_ids", maximum=32
        ))
        rejected = tuple(self.rejected_artifacts)
        if any(not isinstance(item, RejectedArtifact) for item in rejected):
            raise ProtocolError("rejected_artifacts is invalid")
        object.__setattr__(self, "rejected_artifacts", rejected)
        for name in ("after_requirements_revision", "after_workflow_revision", "after_sequence", "after_epoch"):
            _revision(getattr(self, name), name)
        object.__setattr__(self, "after_plan_basis", PlanBasis.from_value(self.after_plan_basis))
        object.__setattr__(self, "message_id", _identifier(self.message_id, "message_id", optional=True))
        for name in ("message_status", "task_status", "reason"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) > 4000:
                raise ProtocolError(f"{name} is invalid")
        states = self.conversation_task_states
        object.__setattr__(self, "conversation_task_states", states if isinstance(states, FrozenDict) else FrozenDict(states))

    @classmethod
    def rejected(
        cls,
        context: RequestContext,
        reason: str,
        *,
        after_requirements_revision: int,
        after_plan_basis: PlanBasis,
        after_workflow_revision: int,
        after_sequence: int,
        after_epoch: int,
        message_id: str | None = None,
        message_status: str = "failed",
        task_status: str = "failed",
        rejected_artifacts: tuple[RejectedArtifact, ...] = (),
    ) -> CommitReceipt:
        return cls(
            trip_id=context.trip_id,
            execution_task_id=context.execution_task_id,
            request_id=context.request_id,
            committed=False,
            rejected_artifacts=rejected_artifacts,
            after_requirements_revision=after_requirements_revision,
            after_plan_basis=after_plan_basis,
            after_workflow_revision=after_workflow_revision,
            after_sequence=after_sequence,
            after_epoch=after_epoch,
            message_id=message_id,
            message_status=message_status,
            task_status=task_status,
            reason=reason,
        )


_PROPOSAL_SECRET = secrets.token_bytes(32)


def _proposal_signature(proposal: TerminalProposal) -> str:
    value = {
        "id": proposal.id,
        "context": thaw(proposal.context),
        "artifacts": thaw(proposal.artifacts),
        "evidence": thaw(proposal.evidence),
        "selected_artifact_ids": proposal.selected_artifact_ids,
        "evidence_refs": proposal.evidence_refs,
        "workflow_state": thaw(proposal.workflow_state),
        "answer": proposal.answer,
        "question": thaw(proposal.question),
        "failed": proposal.failed,
        "reason": proposal.reason,
    }
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hmac.new(_PROPOSAL_SECRET, canonical.encode("utf-8"), sha256).hexdigest()


def _seal_terminal_proposal(proposal: TerminalProposal) -> TerminalProposal:
    """Seal a workspace-built proposal (internal; not a model-facing API)."""
    unsigned = replace(proposal, _workspace_token="")
    return replace(unsigned, _workspace_token=_proposal_signature(unsigned))


def validate_terminal_proposal(proposal: TerminalProposal, context: RequestContext | None = None) -> None:
    """Reject proposals not built intact by a live ``TurnWorkspace``.

    Store must additionally compare ``context`` with its current persisted
    request and revalidate each selected business payload before committing.
    """
    if not isinstance(proposal, TerminalProposal) or not proposal._workspace_token:
        raise ProtocolError("terminal proposal was not produced by a workspace")
    if context is not None and (not isinstance(context, RequestContext) or proposal.context != context):
        raise ProtocolError("terminal proposal context does not match the captured request")
    unsigned = replace(proposal, _workspace_token="")
    if not hmac.compare_digest(proposal._workspace_token, _proposal_signature(unsigned)):
        raise ProtocolError("terminal proposal changed after workspace validation")


IdFactory = Callable[[str], str]


__all__ = [
    "ARTIFACT_KINDS", "CONDITION_STATES", "EFFECT_KINDS", "ENTRY_KINDS", "STRATEGIES",
    "Artifact", "CapabilityResult", "CapabilityStatus", "CommitReceipt", "FrozenDict",
    "IdFactory", "PlanBasis", "ProtocolError", "RejectedArtifact", "RequestContext",
    "TerminalProposal", "TerminalQuestion", "freeze", "plan_basis", "thaw",
    "validate_terminal_proposal",
]
