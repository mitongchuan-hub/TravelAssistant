"""Combined user requests and bounded external operations remain explicit actions."""
from copy import deepcopy
import json
import pytest
from app import agent_client
from app.agent_runtime import TravelAgentRuntime, TurnBudget, AgentBudgetError
from test_agent_roles import local_trip, route, completion, scripted, update_call


def test_update_and_query_syncs_requirements_before_query_without_changing_plan(monkeypatch):
    local, trip = local_trip()
    before = deepcopy(local.plans[trip.id])
    user = '有人花生严重过敏，先记录再查询午餐，不要改行程。'
    calls = scripted(monkeypatch, [route('planning', user, 'update_and_query'),
        update_call(user, {'dietary': {'add': ['花生严重过敏']}}),
        completion(calls=[('search_food', {'city': '杭州', 'keywords': '午餐'})]),
        completion('没有成分和交叉污染资料，不能保证过敏安全。')])
    queried = []
    monkeypatch.setattr(agent_client, 'run_tool', lambda *a, **kw: queried.append(a) or {'status': 'available'})
    local.add_user_message(trip.id, user)
    assert local.plans[trip.id] == before
    assert '花生严重过敏' in local.trips[trip.id].requirements.dietary
    assert len(queried) == 1
    payload = json.loads(calls[2][0][-1]['content'])
    assert '花生严重过敏' in payload['requirements']['requirements']['dietary']


def test_search_declines_before_start_if_detail_budget_cannot_fit(monkeypatch):
    local, trip = local_trip()
    budget = TurnBudget(max_external=7)
    current = TravelAgentRuntime(object(), 'fixture', {'trip': trip, 'user_body': '规划。'}, cache={'_budget': budget})
    current.action = 'generate'
    current.requirements_checked = True
    queries = []
    def fetched(*a, **kw):
        queries.append(a)
        for _ in range(5):
            budget.external()
        return {'status': 'available'}
    monkeypatch.setattr(agent_client, 'run_tool', fetched)
    assert current.execute_tool('planning', 'search_places', {'city': '杭州', 'keywords': '站'})[0]['status'] == 'available'
    denied, _ = current.execute_tool('planning', 'search_food', {'city': '杭州'})
    assert denied['status'] == 'budget_exhausted'
    assert len(queries) == len(current.evidence) == 1 and budget.external_calls == 6
    assert current.finish_role('planning', '草案：餐厅尚未查询，保持未知。', '', None).plan_text
    budget.cancelled = lambda: True
    with pytest.raises(AgentBudgetError):
        current.execute_tool('planning', 'search_food', {'city': '杭州'})
