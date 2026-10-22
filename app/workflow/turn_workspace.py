"""Trusted, in-memory candidate workspace for one agent turn."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any
from uuid import uuid4

from app.agents.agent_protocol import (
    Artifact,
    CapabilityResult,
    CapabilityStatus,
    FrozenDict,
    IdFactory,
    PlanBasis,
    ProtocolError,
    RequestContext,
    TerminalProposal,
    TerminalQuestion,
    _seal_terminal_proposal,
)


_CAPABILITY_KINDS = {
    "prepare_requirements": frozenset({"requirements", "evidence"}),
    "research": frozenset({"evidence"}),
    "draft_itinerary": frozenset({"itinerary", "evidence"}),
    "revise_itinerary": frozenset({"itinerary", "evidence"}),
    "confirm_itinerary": frozenset({"confirmation"}),
    "manage_task": frozenset({"task"}),
}


class WorkspaceError(ProtocolError):
    """A capability or main decision referenced invalid workspace state."""


class TurnWorkspace:
    """Collect candidates without granting them persistence authority.

    Candidates are never selected merely because a capability returned them.
    ``adopt`` (or ``finish(selected_artifact_ids=...)``) is mandatory, and a
    sealed terminal proposal embeds the selected values so Store does not need
    access to this workspace.
    """

    def __init__(
        self,
        context: RequestContext,
        *,
        workflow_state: Mapping[str, Any] | None = None,
        authorized_message_ids: Iterable[str] = (),
        visible_task_ids: Iterable[str] = (),
        id_factory: IdFactory | None = None,
    ) -> None:
        if not isinstance(context, RequestContext):
            raise WorkspaceError("context must be RequestContext")
        self.context = context
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid4().hex}")
        self._authorized_messages = {context.message_id, *self._clean_ids(authorized_message_ids, "message")}
        tasks = set(self._clean_ids(visible_task_ids, "task"))
        if context.conversation_task_id:
            tasks.add(context.conversation_task_id)
        self._visible_tasks = tasks
        self._reserved_task_ids: set[str] = set()
        self._reserved_message_ids: set[str] = set()
        self._artifacts: dict[str, Artifact] = {}
        self._adopted: list[str] = []
        self._invalidated: dict[str, str] = {}
        self._observations: list[CapabilityResult] = []
        self._workflow_state: FrozenDict | None = None
        self._workflow_adopted = False
        self._initial_workflow_state = FrozenDict(workflow_state) if workflow_state is not None else None
        self._requirements_generation = 0
        self._artifact_generation: dict[str, int] = {}
        self._terminal: TerminalProposal | None = None

    @staticmethod
    def _clean_ids(values: Iterable[str], label: str) -> tuple[str, ...]:
        if isinstance(values, str):
            raise WorkspaceError(f"{label} identifiers must be an iterable, not text")
        result = []
        for value in values:
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise WorkspaceError(f"invalid {label} identifier")
            if value not in result:
                result.append(value)
        return tuple(result)

    def _new_id(self, prefix: str) -> str:
        value = self._id_factory(prefix)
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise WorkspaceError("id_factory returned an invalid identifier")
        if value in self._artifacts or value in self._reserved_task_ids or value in self._reserved_message_ids:
            raise WorkspaceError("id_factory returned a duplicate identifier")
        return value

    @property
    def observations(self) -> tuple[CapabilityResult, ...]:
        return tuple(self._observations)

    @property
    def artifacts(self) -> tuple[Artifact, ...]:
        return tuple(self._artifacts.values())

    @property
    def ready_to_commit(self) -> tuple[Artifact, ...]:
        return tuple(self._artifacts[item_id] for item_id in self._adopted if item_id not in self._invalidated)

    @property
    def workflow_candidate(self) -> FrozenDict | None:
        return self._workflow_state

    def reserve_task_id(self) -> str:
        """Allocate a server-owned conversation-task ID for ``add_task``."""
        self._ensure_open()
        task_id = self._new_id("conversation-task")
        self._reserved_task_ids.add(task_id)
        return task_id

    def reserve_message_id(self) -> str:
        """Allocate the pending assistant-message reference for one terminal question."""
        self._ensure_open()
        message_id = self._new_id("pending-message")
        self._reserved_message_ids.add(message_id)
        self._authorized_messages.add(message_id)
        return message_id

    def allow_task(self, task_id: str) -> None:
        """Expose only a task ID allocated here or present in validated workflow.

        Runtime should normally call ``reserve_task_id`` and pass that ID to
        ``task_context.add_task``.  Calling this method with a model-invented ID
        is rejected.
        """
        self._ensure_open()
        clean = self._clean_ids((task_id,), "task")[0]
        validated_ids = set()
        if self._workflow_state is not None:
            tasks = self._workflow_state.get("tasks", ())
            validated_ids = {
                task.get("id") for task in tasks
                if isinstance(task, FrozenDict) and isinstance(task.get("id"), str)
            }
        if clean not in self._reserved_task_ids and clean not in validated_ids:
            raise WorkspaceError("task ID was not allocated or validated by the server")
        self._visible_tasks.add(clean)
        self._reserved_task_ids.discard(clean)

    def get_artifact(self, artifact_id: str) -> Artifact:
        artifact = self._artifacts.get(artifact_id)
        if artifact is None:
            raise WorkspaceError("unknown artifact reference")
        return artifact

    def invalidation_reason(self, artifact_id: str) -> str | None:
        self.get_artifact(artifact_id)
        return self._invalidated.get(artifact_id)

    def register_artifact(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        source_capability: str,
        conversation_task_id: str | None = None,
        authorization_refs: Iterable[str] | None = None,
        depends_on: Iterable[str] = (),
        evidence_refs: Iterable[str] = (),
        effect: str = "independent",
        condition: str = "satisfied",
    ) -> Artifact:
        self._ensure_open()
        allowed = _CAPABILITY_KINDS.get(source_capability)
        if allowed is None or kind not in allowed:
            raise WorkspaceError("capability may not create this artifact kind")
        if not isinstance(payload, Mapping):
            raise WorkspaceError("artifact payload must be a mapping")
        task_id = conversation_task_id if conversation_task_id is not None else self.context.conversation_task_id
        if task_id is not None and task_id not in self._visible_tasks:
            raise WorkspaceError("artifact references an unknown conversation task")
        auth = (self.context.message_id,) if authorization_refs is None else self._clean_ids(
            authorization_refs, "authorization"
        )
        if kind != "evidence" and not auth:
            raise WorkspaceError("a write candidate needs a real authorization message")
        if set(auth) - self._authorized_messages:
            raise WorkspaceError("artifact references an unauthorized message")
        dependencies = self._clean_ids(depends_on, "dependency")
        evidences = self._clean_ids(evidence_refs, "evidence")
        for dependency in dependencies:
            item = self.get_artifact(dependency)
            if item.kind == "evidence":
                raise WorkspaceError("evidence belongs in evidence_refs, not depends_on")
            if dependency in self._invalidated:
                raise WorkspaceError("artifact depends on an invalidated candidate")
        for evidence_id in evidences:
            item = self.get_artifact(evidence_id)
            if item.kind != "evidence":
                raise WorkspaceError("evidence_refs contains a non-evidence artifact")
            if evidence_id in self._invalidated:
                raise WorkspaceError("artifact references invalidated evidence")
        if kind != "evidence" and effect == "conditional" and not evidences:
            raise WorkspaceError("a conditional write requires server-registered evidence")

        if kind == "requirements":
            self._requirements_generation += 1
            for item_id, item in self._artifacts.items():
                if item.kind == "itinerary" and self._artifact_generation.get(item_id, 0) < self._requirements_generation:
                    self._invalidate(item_id, "requirements candidate changed")
        elif kind == "itinerary":
            requirement_candidates = [
                item.id for item in self._artifacts.values()
                if item.kind == "requirements" and item.id not in self._invalidated
            ]
            if requirement_candidates and requirement_candidates[-1] not in dependencies:
                raise WorkspaceError("itinerary must depend on the latest requirements candidate")
        elif kind == "confirmation":
            if not self.context.plan_basis.has_content:
                raise WorkspaceError("there is no committed itinerary to confirm")
            supplied = payload.get("plan_basis")
            try:
                supplied_basis = PlanBasis.from_value(supplied)
            except ProtocolError as exc:
                raise WorkspaceError("confirmation must bind the committed plan basis") from exc
            if supplied_basis != self.context.plan_basis:
                raise WorkspaceError("confirmation cannot bind an uncommitted or stale itinerary")

        artifact = Artifact(
            id=self._new_id("evidence" if kind == "evidence" else "artifact"),
            kind=kind,
            payload=FrozenDict(payload),
            source_capability=source_capability,
            trip_id=self.context.trip_id,
            owner_id=self.context.owner_id,
            execution_task_id=self.context.execution_task_id,
            conversation_task_id=task_id,
            request_id=self.context.request_id,
            message_id=self.context.message_id,
            authorization_refs=auth,
            requirements_revision=self.context.requirements_revision,
            plan_basis=self.context.plan_basis,
            workflow_revision=self.context.workflow_revision,
            sequence=self.context.sequence,
            epoch=self.context.epoch,
            depends_on=dependencies,
            evidence_refs=evidences,
            effect=effect,
            condition=condition,
        )
        self._artifacts[artifact.id] = artifact
        self._artifact_generation[artifact.id] = self._requirements_generation
        return artifact

    def register_evidence(
        self,
        payload: Mapping[str, Any],
        *,
        source_capability: str = "research",
        conversation_task_id: str | None = None,
        authorization_refs: Iterable[str] | None = None,
        depends_on: Iterable[str] = (),
    ) -> Artifact:
        """Register server-owned evidence; it is never a business write itself."""
        return self.register_artifact(
            "evidence",
            payload,
            source_capability=source_capability,
            conversation_task_id=conversation_task_id,
            authorization_refs=authorization_refs,
            depends_on=depends_on,
        )

    def observe(self, result: CapabilityResult) -> CapabilityResult:
        """Validate a capability envelope without adopting any candidate."""
        self._ensure_open()
        if not isinstance(result, CapabilityResult):
            raise WorkspaceError("capability result has the wrong type")
        for artifact_id in result.artifact_refs:
            artifact = self.get_artifact(artifact_id)
            if artifact.kind == "evidence" or artifact.source_capability != result.source_capability:
                raise WorkspaceError("capability result contains a foreign artifact reference")
            if artifact_id in self._invalidated:
                raise WorkspaceError("capability result contains an invalidated artifact")
        for evidence_id in result.evidence_refs:
            evidence = self.get_artifact(evidence_id)
            if evidence.kind != "evidence" or evidence.source_capability != result.source_capability:
                raise WorkspaceError("capability result contains a foreign evidence reference")
        if result.input_request is not None:
            self._validate_question(result.input_request)
        self._observations.append(result)
        return result

    def adopt(self, artifact_id: str) -> Artifact:
        """Explicitly select one valid write candidate for terminal submission."""
        self._ensure_open()
        artifact = self.get_artifact(artifact_id)
        if artifact.kind == "evidence":
            raise WorkspaceError("evidence is referenced, not adopted as a business effect")
        if artifact_id in self._invalidated:
            raise WorkspaceError(f"artifact is invalidated: {self._invalidated[artifact_id]}")
        if artifact.effect == "hypothetical":
            raise WorkspaceError("hypothetical artifacts cannot be adopted")
        if artifact.condition == "unknown":
            raise WorkspaceError("an unknown condition does not authorize a write")
        if artifact.condition == "not_satisfied":
            raise WorkspaceError("an unsatisfied condition does not authorize a write")
        missing_dependencies = [item for item in artifact.depends_on if item not in self._adopted]
        if missing_dependencies:
            raise WorkspaceError("artifact dependencies must be adopted explicitly first")
        if artifact.kind == "itinerary" and any(
            self._artifacts[item].kind == "itinerary" for item in self._adopted
        ):
            raise WorkspaceError("a proposal may contain at most one itinerary")
        if artifact.kind == "confirmation" and any(
            self._artifacts[item].kind == "itinerary" for item in self._adopted
        ):
            raise WorkspaceError("confirmation cannot include an uncommitted itinerary")
        if artifact.kind == "itinerary" and any(
            self._artifacts[item].kind == "confirmation" for item in self._adopted
        ):
            raise WorkspaceError("itinerary and confirmation cannot be adopted together")
        if artifact_id not in self._adopted:
            self._adopted.append(artifact_id)
        return artifact

    def drop(self, artifact_id: str, reason: str = "not selected") -> None:
        """Explicitly remove a candidate and all dependent candidates."""
        self._ensure_open()
        self.get_artifact(artifact_id)
        self._invalidate(artifact_id, reason or "not selected")

    def _invalidate(self, artifact_id: str, reason: str) -> None:
        if artifact_id in self._invalidated:
            return
        self._invalidated[artifact_id] = reason
        if artifact_id in self._adopted:
            self._adopted.remove(artifact_id)
        for item in self._artifacts.values():
            if artifact_id in item.depends_on:
                self._invalidate(item.id, f"dependency {artifact_id} is invalid")

    def set_workflow_candidate(self, workflow_state: Mapping[str, Any]) -> FrozenDict:
        """Register, but do not adopt, a complete validated workflow v2 candidate."""
        self._ensure_open()
        from app.workflow.task_context import normalize_workflow

        normalized = normalize_workflow(
            workflow_state,
            trip_id=self.context.trip_id,
            messages=self._authorized_messages,
            plan_basis=self.context.plan_basis,
        )
        if normalized["revision"] < self.context.workflow_revision:
            raise WorkspaceError("workflow candidate is older than the captured workflow")
        if normalized["sequence"] != self.context.sequence or normalized["epoch"] != self.context.epoch:
            raise WorkspaceError("workflow candidate belongs to another request generation")
        self._workflow_state = FrozenDict(normalized)
        self._workflow_adopted = False
        return self._workflow_state

    def adopt_workflow(self) -> FrozenDict:
        """Explicitly select the validated workflow candidate for terminal commit."""
        self._ensure_open()
        if self._workflow_state is None:
            raise WorkspaceError("there is no workflow candidate to adopt")
        self._workflow_adopted = True
        return self._workflow_state

    def finish(
        self,
        answer: str = "",
        *,
        selected_artifact_ids: Iterable[str] = (),
        evidence_refs: Iterable[str] = (),
        workflow_state: Mapping[str, Any] | None = None,
        question: TerminalQuestion | None = None,
        failed: bool = False,
        reason: str = "",
    ) -> TerminalProposal:
        """Build and seal the only proposal Store should accept."""
        self._ensure_open()
        if not isinstance(answer, str) or len(answer) > 50000:
            raise WorkspaceError("terminal answer is invalid")
        if type(failed) is not bool or not isinstance(reason, str) or len(reason) > 4000:
            raise WorkspaceError("terminal failure fields are invalid")
        if workflow_state is not None:
            self.set_workflow_candidate(workflow_state)
            self.adopt_workflow()
        if question is not None:
            self._validate_question(question)
        requested = self._clean_ids(selected_artifact_ids, "selected artifact")
        for artifact_id in requested:
            self.adopt(artifact_id)
        # A terminal choice is authoritative: previously staged selections not
        # named by finish are not silently committed.
        self._adopted = [item for item in self._adopted if item in requested]
        for artifact_id in requested:
            artifact = self.get_artifact(artifact_id)
            if any(dependency not in self._adopted for dependency in artifact.depends_on):
                raise WorkspaceError("terminal selection omitted an adopted dependency")
        selected = tuple(self._artifacts[item] for item in self._adopted)
        if sum(item.kind == "itinerary" for item in selected) > 1:
            raise WorkspaceError("a terminal proposal may contain at most one itinerary")
        if any(item.condition != "satisfied" or item.effect == "hypothetical" for item in selected):
            raise WorkspaceError("terminal selection contains an unauthorized conditional effect")

        refs = list(self._clean_ids(evidence_refs, "evidence"))
        for item in selected:
            for evidence_id in item.evidence_refs:
                if evidence_id not in refs:
                    refs.append(evidence_id)
        evidence: list[Artifact] = []
        for evidence_id in refs:
            item = self.get_artifact(evidence_id)
            if item.kind != "evidence" or evidence_id in self._invalidated:
                raise WorkspaceError("terminal evidence reference is invalid")
            evidence.append(item)

        if question is not None and self._workflow_state is None:
            raise WorkspaceError("a terminal question requires a validated workflow candidate")
        if question is not None:
            active = self._workflow_state.get("active_question") if self._workflow_state else None
            if not isinstance(active, FrozenDict) or active.get("id") != question.id:
                raise WorkspaceError("terminal question does not match workflow active_question")
            if active.get("assistant_message_id") not in self._reserved_message_ids:
                raise WorkspaceError("terminal question must use a workspace-reserved assistant message ID")
        proposal = TerminalProposal(
            id=self._new_id("proposal"),
            context=self.context,
            artifacts=selected,
            evidence=tuple(evidence),
            selected_artifact_ids=tuple(item.id for item in selected),
            evidence_refs=tuple(item.id for item in evidence),
            workflow_state=self._workflow_state if self._workflow_adopted else None,
            answer=answer,
            question=question,
            failed=failed,
            reason=reason,
        )
        self._terminal = _seal_terminal_proposal(proposal)
        return self._terminal

    def fail_closed(self, reason: str, *, answer: str = "") -> TerminalProposal:
        """Finish without another model call, preserving only adopted independent effects."""
        self._ensure_open()
        safe_ids: list[str] = []
        for artifact in self.ready_to_commit:
            if artifact.effect != "independent" or artifact.condition != "satisfied":
                continue
            if all(dependency in safe_ids for dependency in artifact.depends_on):
                safe_ids.append(artifact.id)
        # A merely registered workflow candidate is not ready-to-commit.  Keep
        # it only when runtime explicitly adopted it before the failure.
        candidate = self._workflow_state if self._workflow_adopted else None
        return self.finish(
            answer,
            selected_artifact_ids=safe_ids,
            workflow_state=candidate,
            failed=True,
            reason=reason,
        )

    def _validate_question(self, question: TerminalQuestion) -> None:
        if not isinstance(question, TerminalQuestion):
            raise WorkspaceError("question must be TerminalQuestion")
        if question.conversation_task_id not in self._visible_tasks:
            raise WorkspaceError("question references an unknown conversation task")
        if set(question.source_message_ids) - self._authorized_messages:
            raise WorkspaceError("question references an unauthorized source message")

    def _ensure_open(self) -> None:
        if self._terminal is not None:
            raise WorkspaceError("workspace already produced a terminal proposal")


__all__ = ["TurnWorkspace", "WorkspaceError"]
