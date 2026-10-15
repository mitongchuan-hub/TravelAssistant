"""Critical journeys must be queried before accepting a generated itinerary."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest
from app import agent_client
from app.agent_runtime import TravelAgentRuntime, TurnBudget
from app.planning_evidence import critical_route_review
from test_agent_roles import local_trip, scripted, completion, requirements_unchanged


CATALOG = {key: {'poi_id': key, 'name': name, 'longitude': 120.1 + index * .01,
                 'latitude': 30.1, 'city': '杭州', 'district': '', 'address': name,
                 'source': '高德地图 POI', 'fetched_at': '2026-10-14T00:00:00Z'}
           for index, (key, name) in enumerate((('arrival', '抵达站'), ('core', '核心景区'),
                                                ('departure', '离开站')))}
FORWARD = {'origin_poi_id': 'arrival', 'destination_poi_id': 'core', 'mode': 'driving'}
RETURN = {'origin_poi_id': 'core', 'destination_poi_id': 'departure', 'mode': 'driving'}


def setup_runtime(monkeypatch, responses, *, route_status='available', budget=32):
    local, trip = local_trip()
    trip.requirements.arrival_location = '抵达站'
    trip.requirements.departure_location = '离开站'
    trip.requirements.must_visit = ['核心景区']
    requests = scripted(monkeypatch, responses)
    tools = []

    def fetch(name, arguments, **kwargs):
        tools.append((name, deepcopy(arguments)))
        if name == 'query_route':
            kwargs['route_cache']['_budget'].external()
            return {'status': route_status, 'duration_minutes': 25 if route_status == 'available' else None,
                    'origin_poi_id': arguments['origin_poi_id'],
                    'destination_poi_id': arguments['destination_poi_id']}
        return {'status': 'available', 'recommendations': []}

    monkeypatch.setattr(agent_client, 'run_tool', fetch)
    current = TravelAgentRuntime(object(), 'fixture', {'trip': trip, 'user_body': '生成行程',
                                 'plan': local.plans[trip.id]}, catalog=deepcopy(CATALOG),
                                 cache={'_budget': TurnBudget(max_external=budget)},
                                 request=agent_client._create_chat_completion)
    return current, requests, tools


def test_skipped_critical_routes_get_one_review_and_only_revised_text_is_accepted(monkeypatch):
    current, requests, tools = setup_runtime(monkeypatch, [completion('缺乏接驳依据的草稿'),
        completion(calls=[('query_route', FORWARD), ('query_route', RETURN)]), completion('依据接驳调整后的完整方案')])
    current.run('planning', current.user_body)
    instruction = json.loads(requests[1][0][-1]['content'])
    assert [row['status'] for row in instruction['route_review']] == ['not_attempted', 'not_attempted']
    assert tools == [('query_route', FORWARD), ('query_route', RETURN)]
    assert current.cache['final_reply'].plan_text == '依据接驳调整后的完整方案'
    assert len(requests) == 3 and current.budget.external_calls == 2


def test_repeatedly_skipping_routes_cannot_save_a_plan(monkeypatch):
    current, requests, tools = setup_runtime(monkeypatch, [completion('没有查询的草稿'), completion('仍然未查询的草稿')])
    before = asdict(current.plan)
    with pytest.raises(ValueError, match='未保存新行程'):
        current.run('planning', current.user_body)
    assert len(requests) == 2 and not tools
    assert 'final_reply' not in current.cache and asdict(current.plan) == before


def test_forward_route_is_not_return_evidence(monkeypatch):
    current, requests, tools = setup_runtime(monkeypatch, [completion('已查去程但漏查返程'),
        completion(calls=[('query_route', RETURN)]), completion('已分别查过双向接驳的方案')])
    current.requirements_checked = True
    current.action = 'generate'
    current.execute_tool('planning', 'query_route', FORWARD)
    current.run('planning', current.user_body)
    instruction = json.loads(requests[1][0][-1]['content'])
    assert [row['status'] for row in instruction['route_review']] == ['available', 'not_attempted']
    assert tools == [('query_route', FORWARD), ('query_route', RETURN)]


@pytest.mark.parametrize('status', ['available', 'unavailable'])
def test_real_attempts_accept_success_or_conditional_draft_without_retry(monkeypatch, status):
    current, requests, tools = setup_runtime(monkeypatch, [
        completion(calls=[('query_route', FORWARD), ('query_route', RETURN)]), completion('接驳失败时保留未知的条件草案')],
        route_status=status)
    current.run('planning', current.user_body)
    assert len(requests) == 2 and len(tools) == 2
    assert [row['status'] for row in current.planning_route_review()] == [status, status]
    assert current.cache['final_reply'].plan_text


def test_exhausted_budget_does_not_start_more_queries(monkeypatch):
    current, requests, tools = setup_runtime(monkeypatch, [completion('查询预算不足，只按活动顺序给条件草案')], budget=0)
    current.run('planning', current.user_body)
    assert not tools and len(requests) == 1
    assert [row['status'] for row in current.planning_route_review()] == ['budget_exhausted', 'budget_exhausted']


def test_optional_food_is_deferred_without_network_or_external_budget(monkeypatch):
    current, _, tools = setup_runtime(monkeypatch, [])
    current.action = 'generate'
    current.requirements_checked = current.route_review_enabled = True
    result, direct = current.execute_tool('planning', 'search_food', {'city': '杭州', 'keywords': '核心景区 清淡简餐'})
    assert result['status'] == 'prerequisite_needed' and direct is None
    assert not tools and current.budget.external_calls == 0
    current.execute_tool('planning', 'query_route', FORWARD)
    current.execute_tool('planning', 'query_route', RETURN)
    assert current.execute_tool('planning', 'search_food', {'city': '杭州', 'keywords': '核心景区 清淡简餐'})[0]['status'] == 'available'
    assert tools[-1][0] == 'search_food'


def test_query_and_revision_keep_their_existing_scope(monkeypatch):
    current, requests, tools = setup_runtime(monkeypatch, [requirements_unchanged(), completion('仅按已授权范围修改的原文')])
    current.execute_tool('research', 'search_food', {'city': '杭州', 'keywords': '清淡'})
    current.plan.text = '已有行程原文'
    current.run('revision', current.user_body, scope='all')
    assert tools == [('search_food', {'city': '杭州', 'keywords': '清淡'})]
    assert len(requests) == 2 and current.cache['final_reply'].plan_text


def test_ambiguous_names_are_not_guessed_and_old_route_evidence_is_not_silently_reused(monkeypatch):
    current, _, _ = setup_runtime(monkeypatch, [])
    old = {'tool': 'query_route', 'arguments': FORWARD, 'result': {'status': 'available'}}
    current.plan.evidence = [old]
    assert current.planning_route_review()[0]['status'] == 'not_attempted'
    catalog = deepcopy(CATALOG)
    catalog['ambiguous'] = dict(catalog['core'], poi_id='ambiguous')
    assert critical_route_review(current.trip, catalog, [], 32) == []
    assert catalog['core']['poi_id'] == 'core'
