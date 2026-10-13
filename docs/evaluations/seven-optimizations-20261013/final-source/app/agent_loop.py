"""Bounded, stateful control loop for the TravelAssistant Agent.

The loop owns control flow only. It does not know how to query weather, match a
place, validate a plan, or persist a trip. Callers provide a decision function
and an action executor; this keeps model decisions separate from deterministic
services.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


LOOP_RUNNING = "running"
LOOP_WAITING_USER = "waiting_user"
LOOP_COMPLETED = "completed"
LOOP_FAILED = "failed"
LOOP_BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True)
class LoopAction:
    """A single action selected by the Agent for the next loop step."""

    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class LoopObservation:
    """The result returned by a deterministic action executor."""

    status: str = "applied"
    data: dict[str, Any] = field(default_factory=dict)
    next_phase: str | None = None
    tool_calls: int = 0
    wait_for_user: bool = False
    done: bool = False
    error: str = ""


@dataclass
class AgentLoopState:
    """Serializable state needed to continue or inspect one Agent turn."""

    session_id: str
    mode: str
    phase: str
    goal: str = ""
    max_steps: int = 8
    max_tool_calls: int = 32
    step_count: int = 0
    tool_call_count: int = 0
    status: str = LOOP_RUNNING
    stop_reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def can_continue(self) -> bool:
        return self.status == LOOP_RUNNING

    def record(self, action: LoopAction, observation: LoopObservation) -> None:
        self.history.append({
            "step": self.step_count,
            "phase": self.phase,
            "action": action.name,
            "reason": action.reason,
            "observation_status": observation.status,
            "next_phase": observation.next_phase,
            "tool_calls": observation.tool_calls,
            "error": observation.error,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        if observation.data:
            self.evidence.update(_safe_copy(observation.data))
        if observation.next_phase:
            self.phase = observation.next_phase
        self.tool_call_count += observation.tool_calls


class LoopActionError(ValueError):
    """The model or caller selected an action outside the current policy."""


class AgentLoop:
    """Run a bounded observe-decide-act loop.

    ``decide`` chooses one action from the current state. ``execute`` performs
    that action outside the model and returns an observation. Terminal actions
    are deliberately explicit so a provider cannot keep the loop alive by
    returning prose that merely sounds complete.
    """

    TERMINAL_ACTIONS = {"complete", "wait_user", "fail"}

    def __init__(
        self,
        state: AgentLoopState,
        decide: Callable[[AgentLoopState], LoopAction],
        execute: Callable[[LoopAction, AgentLoopState], LoopObservation],
        *,
        allowed_actions: set[str] | None = None,
    ) -> None:
        self.state = state
        self.decide = decide
        self.execute = execute
        self.allowed_actions = allowed_actions

    def run(self) -> AgentLoopState:
        while self.state.can_continue:
            if self.state.step_count >= self.state.max_steps:
                self.state.status = LOOP_BUDGET_EXHAUSTED
                self.state.stop_reason = "达到本次 Agent 工作步数上限。"
                break
            if self.state.tool_call_count >= self.state.max_tool_calls:
                self.state.status = LOOP_BUDGET_EXHAUSTED
                self.state.stop_reason = "达到本次工具调用上限。"
                break

            try:
                action = self.decide(self.state)
                self._validate_action(action)
                observation = self.execute(action, self.state)
                if not isinstance(observation, LoopObservation):
                    raise LoopActionError("动作执行器必须返回 LoopObservation")
            except Exception as exc:
                self.state.status = LOOP_FAILED
                self.state.stop_reason = str(exc) or type(exc).__name__
                self.state.history.append({
                    "step": self.state.step_count + 1,
                    "phase": self.state.phase,
                    "action": "loop_error",
                    "error": self.state.stop_reason,
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                })
                break

            self.state.step_count += 1
            self.state.record(action, observation)
            if self.state.tool_call_count > self.state.max_tool_calls:
                self.state.status = LOOP_BUDGET_EXHAUSTED
                self.state.stop_reason = "本次动作超过工具调用上限。"
                break
            if action.name == "wait_user" or observation.wait_for_user:
                self.state.status = LOOP_WAITING_USER
                self.state.stop_reason = observation.error or "等待用户补充或确认。"
                break
            if observation.error or observation.status == "failed":
                self.state.status = LOOP_FAILED
                self.state.stop_reason = observation.error or "动作执行失败。"
                break
            if action.name == "complete" or observation.done:
                self.state.status = LOOP_COMPLETED
                self.state.stop_reason = observation.error or "Agent 已完成本次工作。"
                break
            if action.name == "fail":
                self.state.status = LOOP_FAILED
                self.state.stop_reason = observation.error or "Agent 无法完成本次工作。"
                break

        return self.state

    def _validate_action(self, action: LoopAction) -> None:
        if not isinstance(action, LoopAction):
            raise LoopActionError("Agent 决策必须返回 LoopAction")
        if not action.name or self.allowed_actions is not None and action.name not in self.allowed_actions:
            raise LoopActionError(f"不允许的 Agent 动作：{action.name or '空动作'}")
        if not isinstance(action.payload, dict):
            raise LoopActionError("Agent 动作参数必须是对象")


def _safe_copy(value: Any) -> Any:
    try:
        return deepcopy(value)
    except (TypeError, ValueError):
        return str(value)
