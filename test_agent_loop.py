import json
from types import SimpleNamespace

from app import agent_client
from app.agent_loop import (
    AgentLoop,
    AgentLoopState,
    LoopAction,
    LoopObservation,
    LOOP_BUDGET_EXHAUSTED,
    LOOP_COMPLETED,
    LOOP_FAILED,
    LOOP_WAITING_USER,
)


def test_agent_loop_runs_actions_until_explicit_completion():
    decisions = iter([
        LoopAction("query", {"name": "weather"}, "需要天气事实"),
        LoopAction("complete", reason="事实已足够"),
    ])
    executed = []

    def execute(action, state):
        executed.append(action.name)
        return LoopObservation(next_phase="planning" if action.name == "query" else None, done=action.name == "complete")

    state = AgentLoopState("session-1", "planning", "collecting_facts", max_steps=3)
    result = AgentLoop(state, lambda current: next(decisions), execute, allowed_actions={"query", "complete"}).run()

    assert result.status == LOOP_COMPLETED
    assert result.phase == "planning"
    assert result.step_count == 2
    assert executed == ["query", "complete"]


def test_agent_loop_stops_for_user_without_running_more_actions():
    calls = []

    def execute(action, state):
        calls.append(action.name)
        return LoopObservation(wait_for_user=True, error="需要确认重名地点")

    state = AgentLoopState("session-2", "chat", "understanding", max_steps=4)
    result = AgentLoop(state, lambda current: LoopAction("wait_user"), execute, allowed_actions={"wait_user"}).run()

    assert result.status == LOOP_WAITING_USER
    assert result.stop_reason == "需要确认重名地点"
    assert result.step_count == 1
    assert calls == ["wait_user"]


def test_agent_loop_enforces_step_budget():
    state = AgentLoopState("session-3", "planning", "collecting_facts", max_steps=2)
    result = AgentLoop(
        state,
        lambda current: LoopAction("query"),
        lambda action, current: LoopObservation(),
        allowed_actions={"query"},
    ).run()

    assert result.status == LOOP_BUDGET_EXHAUSTED
    assert result.step_count == 2
    assert result.stop_reason


def test_agent_loop_rejects_action_outside_policy():
    state = AgentLoopState("session-4", "planning", "validating", max_steps=2)
    result = AgentLoop(
        state,
        lambda current: LoopAction("write_database"),
        lambda action, current: LoopObservation(),
        allowed_actions={"complete"},
    ).run()

    assert result.status == LOOP_FAILED
    assert "不允许" in result.stop_reason


def test_conversation_loop_queries_a_tool_before_returning_reply(monkeypatch):
    tool_call = SimpleNamespace(
        id="weather-1",
        function=SimpleNamespace(name="query_weather", arguments=json.dumps({"city": "杭州"})),
    )
    responses = iter([
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[tool_call]))]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({
                "body": "杭州天气需要结合具体日期确认。",
                "card": {"kind": "missing-info", "title": "天气", "summary": "待确认日期", "bullets": [], "action_target": "board", "next_action": "continue_chat"},
                "idea_cards": [], "plan": None,
            }, ensure_ascii=False),
            tool_calls=[],
        ))]),
    ])
    requests = []

    def create(client, model, messages, **kwargs):
        requests.append((messages, kwargs))
        return next(responses)

    monkeypatch.setattr(agent_client, "_create_chat_completion", create)
    monkeypatch.setattr(agent_client, "run_tool", lambda name, arguments, **kwargs: {
        "status": "available", "city": arguments["city"], "condition": "晴"
    })
    monkeypatch.setattr(agent_client, "collect_places", lambda messages: {})
    trace = agent_client.ConversationTrace("loop-test", "now", "trip", "杭州天气")

    from app.agent_runtime import TravelAgentRuntime
    cache = {}
    runtime = TravelAgentRuntime(object(), 'test', {'user_body': '杭州天气'}, trace=trace,
                                 cache=cache, request=agent_client._create_chat_completion)
    runtime.run('research', '杭州天气')

    assert cache['final_reply'].body == '杭州天气需要结合具体日期确认。'
    assert len(requests) == 2
    assert "tools" in requests[0][1]
    assert any("Agent Loop 状态" in event and "completed" in event for event in trace.events)
