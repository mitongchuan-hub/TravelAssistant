"""Read-only reuse reduces IO while preserving permissions, evidence and freshness."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from app import agent_client
from app.agent_runtime import TravelAgentRuntime, TurnBudget, AgentBudgetError
from test_agent_roles import completion, local_trip


def runtime():
    local, trip = local_trip()
    return TravelAgentRuntime(object(), 'fixture', {'trip': trip, 'user_body': '原话全部保留。'},
                              cache={'_budget': TurnBudget(max_external=12)})


@pytest.mark.parametrize('tool,args', [
    ('search_places', {'city': '甲城', 'keywords': '城市站'}),
    ('search_food', {'city': '甲城', 'keywords': '清淡'}),
    ('query_weather', {'city': '甲城', 'date': '2026-11-14'}),
])
def test_successful_exact_query_reused_without_extra_budget_or_evidence(monkeypatch, tool, args):
    calls = []
    def fetch(name, arguments, **kwargs):
        calls.append((name, deepcopy(arguments)))
        return {'status': 'available', 'notes': ['仍待核实']}
    monkeypatch.setattr(agent_client, 'run_tool', fetch)
    current = runtime()
    first, _ = current.execute_tool('research', tool, args)
    first['notes'].clear()
    second, _ = current.execute_tool('research', tool, dict(reversed(list(args.items()))))
    assert second['notes'] == ['仍待核实']
    assert len(calls) == len(current.evidence) == current.budget.external_calls == 1
    assert current.evidence[0]['result']['notes'] == ['仍待核实']
    # A fresh turn must requery; successful previous-turn data is not silently reused.
    runtime().execute_tool('research', tool, args)
    assert len(calls) == 2


def test_different_arguments_and_failures_are_not_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(agent_client, 'run_tool', lambda name, args, **kwargs:
                        calls.append(deepcopy(args)) or {'status': 'unavailable', 'reason': '未知'})
    current = runtime()
    current.budget.max_external = 2
    args = {'city': '甲城', 'date': '2026-11-14'}
    for _ in range(2):
        assert current.execute_tool('research', 'query_weather', args)[0]['status'] == 'unavailable'
    assert len(calls) == 2
    assert current.execute_tool('research', 'query_weather', args)[0]['status'] == 'budget_exhausted'
    assert len(calls) == 2
    current = runtime()
    args = {'city': '甲城', 'keywords': '城市站'}
    monkeypatch.setattr(agent_client, 'run_tool', lambda name, args, **kwargs:
                        calls.append(deepcopy(args)) or {'status': 'available'})
    current.execute_tool('research', 'search_places', args)
    current.execute_tool('research', 'search_places', {**args, 'keywords': '另一个站'})
    assert current.budget.external_calls == 2


def test_cache_hit_still_checks_role_permissions_and_cancellation(monkeypatch):
    monkeypatch.setattr(agent_client, 'run_tool', lambda *a, **kw: {'status': 'available'})
    current = runtime()
    args = {'city': '甲城', 'keywords': '城市站'}
    current.execute_tool('research', 'search_places', args)
    current.action = 'update_requirements'
    with pytest.raises(ValueError):
        current.execute_tool('planning', 'search_places', args)
    current.budget.cancelled = lambda: True
    with pytest.raises(AgentBudgetError):
        current.execute_tool('research', 'search_places', args)


def test_initial_context_keeps_full_quote_once_instead_of_repeating_user_message():
    current = runtime()
    messages = current._initial_messages('planning', current.user_body, '', None)
    assert len(messages) == 2
    payload = json.loads(messages[1]['content'])
    assert payload['user_message'] == current.user_body and 'task' not in payload
    assert messages[0]['role'] == 'system'
    assert all(message['role'] != 'system' for message in messages[1:])
