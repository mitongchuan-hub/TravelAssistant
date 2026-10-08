"""Behavioral coverage for main-agent delegation, scope and shared budgets."""
from copy import deepcopy
from dataclasses import asdict
import json
from types import SimpleNamespace
import importlib

import pytest

from app import agent_client
from app.agent_runtime import TravelAgentRuntime, TurnBudget, AgentBudgetError
from app.agent_roles import roles
from app.models import TripRequirements
from app.store import DemoStore


def completion(content='', calls=()):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
        tool_calls=[SimpleNamespace(id=f'call-{i}', function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)))
                    for i, (name, args) in enumerate(calls)],
    ))])


def delegate(agent, text, *, finish=True, scope=None):
    args = {'task': text, 'user_quote': text, 'finish_turn': finish}
    if scope:
        args['scope'] = scope
    return completion(calls=[('delegate_' + agent, args)])


def scripted(monkeypatch, responses):
    requests = []
    iterator = iter(responses)
    def create(client, model, messages, **kwargs):
        if kwargs.get('budget'):
            kwargs['budget'].model()
        requests.append((deepcopy(messages), kwargs))
        response = next(iterator)
        if callable(response):
            response = response(messages, kwargs)
        if isinstance(response, Exception):
            raise response
        return response
    monkeypatch.setattr(agent_client, '_create_chat_completion', create)
    monkeypatch.setattr(agent_client, 'is_llm_enabled', lambda: True)
    monkeypatch.setattr(agent_client.ConversationTrace, 'write', lambda *a, **kw: None)
    monkeypatch.setattr('openai.OpenAI', lambda **kwargs: object())
    return requests


def local_trip():
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '人均1500', '轻松', '')
    return local, trip


def test_casual_chat_has_no_business_context_no_tools_and_one_call(monkeypatch):
    local, trip = local_trip()
    local.update_trip_requirements(trip.id, {'dietary': ['只允许子 Agent 读取的忌口']}, 0)
    local.plans[trip.id].title = 'private-plan-payload'
    requests = scripted(monkeypatch, [completion('你好，想聊些什么？')])
    local.add_user_message(trip.id, '你好')
    assert len(requests) == 1
    payload = requests[0][0][1]['content']
    assert 'private-plan-payload' not in payload and '只允许子 Agent 读取的忌口' not in payload
    assert 'requirements' not in json.loads(payload)
    assert not local.plan_versions[trip.id]
    assert local.messages[trip.id][-1].body == '你好，想聊些什么？'
    assert 'query_weather' not in {s['function']['name'] for s in requests[0][1]['tools']}


def test_requirements_delegation_updates_once_without_replanning(monkeypatch):
    local, trip = local_trip()
    local.update_trip_requirements(trip.id, {'dietary': ['花生过敏']}, 0)
    text = '预算改成人均2000，不吃海鲜'
    requests = scripted(monkeypatch, [delegate('requirements', text), completion({
        'body': '已更新预算与忌口。', 'status': 'complete',
        'requirements_patch': {'changes': {'budget_amount': '2000', 'dietary': {'add': ['不吃海鲜']}}, 'evidence': text},
    })])
    local.add_user_message(trip.id, text)
    assert len(requests) == 2
    assert local.trips[trip.id].requirements.revision == 2
    assert local.trips[trip.id].requirements.dietary == ['花生过敏', '不吃海鲜']
    assert local.trips[trip.id].budget == '人均 2000 元'
    assert not local.plan_versions[trip.id]
    assert '花生过敏' in requests[1][0][1]['content']


@pytest.mark.parametrize('planning_fails', [False, True])
def test_combined_request_uses_staged_requirements_and_preserves_partial_success(monkeypatch, planning_fails):
    local, trip = local_trip()
    text = '预算改为2000，然后生成行程'
    raw = asdict(local._seed_plan())
    replies = [delegate('requirements', text, finish=False), completion({
        'body': '预算已整理。', 'requirements_patch': {'changes': {'budget_amount': '2000'}, 'evidence': text}}),
        delegate('planning', text)]
    replies += [TimeoutError('upstream timeout')] if planning_fails else [completion({'body': '方案已整理。', 'plan': raw})]
    requests = scripted(monkeypatch, replies)
    local.add_user_message(trip.id, text)
    assert len(requests) == 4
    plan_input = json.loads(requests[3][0][1]['content'])
    assert plan_input['requirements']['requirements']['budget_amount'] == '2000'
    assert local.trips[trip.id].requirements.budget_amount == '2000'
    assert bool(local.plan_versions[trip.id]) is not planning_fails
    if planning_fails:
        assert local.messages[trip.id][-1].status == 'failed'
        assert '需求变更已保存' in local.messages[trip.id][-1].body


def test_research_can_fetch_context_and_tools_but_cannot_write_plan(monkeypatch):
    local, trip = local_trip()
    local.update_trip_requirements(trip.id, {'city': '杭州'}, 0)
    text = '查一下杭州天气'
    requests = scripted(monkeypatch, [delegate('research', text),
        completion(calls=[('read_context', {'section': 'requirements'})]),
        completion(calls=[('query_weather', {'city': '杭州'})]),
        completion({'body': '日期还未确定，天气待确认。', 'plan': asdict(local._seed_plan()),
                    'requirements_patch': {'changes': {'travelers': 99}, 'evidence': text}})])
    queries = []
    monkeypatch.setattr(agent_client, 'run_tool', lambda name, args: queries.append((name, args)) or {'status': 'unavailable', 'reason': '日期待定'})
    local.add_user_message(trip.id, text)
    assert queries == [('query_weather', {'city': '杭州'})]
    assert not local.plan_versions[trip.id]
    assert local.trips[trip.id].requirements.travelers is None
    tool_messages = [m for m in requests[-1][0] if m['role'] == 'tool']
    assert [m['name'] for m in tool_messages] == ['read_context', 'query_weather']


def test_unknown_tool_and_nested_delegation_are_denied(monkeypatch):
    local, trip = local_trip()
    text = '查询景点'
    requests = scripted(monkeypatch, [delegate('research', text),
        completion(calls=[('delegate_planning', {'task': '写计划'}), ('delete_trip', {})]),
        completion('本次只查询信息。')])
    queries = []
    monkeypatch.setattr(agent_client, 'run_tool', lambda *a, **kw: queries.append(a))
    local.add_user_message(trip.id, text)
    assert not queries and not local.plan_versions[trip.id]
    assert all('不允许' in m['content'] for m in requests[-1][0] if m['role'] == 'tool')


def test_chat_item_revision_preserves_neighbor_ids_and_arrangements(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    trip_id = 'trip-hangzhou'
    original = deepcopy(local.plans[trip_id])
    target = original.days[0].items[0]
    raw = asdict(original)
    raw['days'][0]['items'][0]['notes'] = '用户要求的新备注'
    raw['days'][-1]['items'][-1]['title'] = '未授权的邻居改动'
    text = '改一下第一个安排的备注，其他别动'
    scripted(monkeypatch, [delegate('revision', text, scope='item'),
        completion(calls=[('read_context', {'section': 'plan'})]),
        completion({'body': '目标备注已更新。', 'plan': raw, 'target_item_id': target.id})])
    local.add_user_message(trip_id, text)
    saved = local.plans[trip_id]
    assert saved.days[0].items[0].id == target.id
    assert saved.days[0].items[0].notes == '用户要求的新备注'
    assert saved.days[-1].items[-1].title == original.days[-1].items[-1].title
    assert saved.days[-1].items[-1].id == original.days[-1].items[-1].id
    assert len(local.plan_versions[trip_id]) == 2


def test_ambiguous_revision_stops_before_further_delegation(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    text = '晚饭换一家'
    requests = scripted(monkeypatch, [delegate('revision', text, scope='item', finish=False),
        completion({'status': 'needs_input', 'body': '你要换哪一天的晚餐？', 'plan': None})])
    original = deepcopy(local.plans['trip-hangzhou'])
    local.add_user_message('trip-hangzhou', text)
    assert len(requests) == 2
    assert local.messages['trip-hangzhou'][-1].body == '你要换哪一天的晚餐？'
    assert local.plans['trip-hangzhou'] == original


def test_explanation_role_is_read_only_even_if_model_returns_plan(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    text = '解释一下第一天的安排'
    original = deepcopy(local.plans['trip-hangzhou'])
    requests = scripted(monkeypatch, [delegate('revision', text, scope='explain'),
        completion(calls=[('read_context', {'section': 'plan'})]),
        completion({'body': '第一天主要安排抵达与休息。', 'plan': asdict(original)})])
    local.add_user_message('trip-hangzhou', text)
    assert {s['function']['name'] for s in requests[1][1]['tools']} == {'read_context'}
    assert local.plans['trip-hangzhou'] == original
    assert len(local.plan_versions['trip-hangzhou']) == 1


def test_multiple_delegations_in_one_batch_execute_none(monkeypatch):
    local, trip = local_trip()
    text = '修改预算'
    call = ('delegate_requirements', {'task': text, 'user_quote': text, 'finish_turn': True})
    requests = scripted(monkeypatch, [completion(calls=[call, call]), completion('请明确预算金额。')])
    local.add_user_message(trip.id, text)
    assert len(requests) == 2 and local.trips[trip.id].requirements.revision == 0
    assert all('串行' in m['content'] for m in requests[-1][0] if m['role'] == 'tool')


def test_snapshot_reads_do_not_leak_other_trips_and_paginate():
    local, trip = local_trip()
    for number in range(25):
        local.add_user_message(trip.id, f'自己的消息{number}', process_agent=False)
    other = local.create_trip('别人旅行', '', '', '', '')
    local.add_user_message(other.id, 'other-trip-secret', process_agent=False)
    runtime = TravelAgentRuntime(object(), 'test', {'trip': trip, 'messages': local.messages[trip.id]})
    first = runtime.read_context({'section': 'messages', 'query': '自己的消息', 'limit': 20})
    second = runtime.read_context({'section': 'messages', 'query': '自己的消息', 'offset': first['next_offset']})
    assert first['total'] == 25 and len(second['items']) == 5
    assert 'other-trip-secret' not in json.dumps([first, second], ensure_ascii=False)
    assert runtime.read_context({'section': 'messages', 'query': 'other-trip-secret'})['total'] == 0


def test_shared_model_budget_counts_parent_and_child_before_request(monkeypatch):
    text = '预算改成2000'
    budget = TurnBudget(max_models=1)
    requests = scripted(monkeypatch, [delegate('requirements', text), completion('should not run')])
    runtime = TravelAgentRuntime(object(), 'test', {'user_body': text}, cache={'_budget': budget}, request=agent_client._create_chat_completion)
    runtime.run()
    assert len(requests) == 1 and budget.model_calls == 1
    assert runtime.cache['final_reply'].failed


def test_tool_batch_is_rejected_before_any_external_query(monkeypatch):
    queries = []
    monkeypatch.setattr(agent_client, 'run_tool', lambda *a, **kw: queries.append(a))
    budget = TurnBudget(max_tools=1)
    requests = scripted(monkeypatch, [completion(calls=[('query_weather', {'city': '杭州'}), ('search_places', {'city': '杭州', 'keywords': '灵隐寺'})])])
    runtime = TravelAgentRuntime(object(), 'test', {'user_body': '查资料'}, cache={'_budget': budget}, request=agent_client._create_chat_completion)
    with pytest.raises(AgentBudgetError): runtime.run('research')
    assert not queries and budget.tool_calls == 0


@pytest.mark.parametrize('operation', ['confirm', 'restore'])
def test_version_action_uses_read_targets_and_preserves_unknowns(monkeypatch, operation):
    local = DemoStore(persistence_enabled=False)
    plan = local.plans['trip-hangzhou']
    plan.pending_items = ['门票待确认']
    local._save_plan_version('trip-hangzhou', '保留待核实事项')
    target = plan.id if operation == 'confirm' else local.plan_versions['trip-hangzhou'][-1].id
    text = '确认当前行程' if operation == 'confirm' else '恢复上一版'
    scripted(monkeypatch, [completion(calls=[('read_versions', {})]),
        completion(calls=[('change_version', {'operation': operation, 'version_id': target, 'user_quote': text})])])
    versions = len(local.plan_versions['trip-hangzhou'])
    local.add_user_message('trip-hangzhou', text)
    assert len(local.plan_versions['trip-hangzhou']) == versions + 1
    assert local.plans['trip-hangzhou'].pending_items == ['门票待确认']
    assert local.plans['trip-hangzhou'].status == ('已确认' if operation == 'confirm' else '草案')


def test_same_id_plan_change_during_model_request_blocks_overwrite(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    original = deepcopy(local.plans['trip-hangzhou'])
    def concurrent(messages, kwargs):
        local.confirm_plan('trip-hangzhou')  # confirmation changes content but retains plan.id
        return completion({'body': '新草案', 'plan': asdict(original)})
    scripted(monkeypatch, [concurrent])
    local.generate_plan('trip-hangzhou')
    assert local.plans['trip-hangzhou'].status == '已确认'
    assert len(local.plan_versions['trip-hangzhou']) == 2
    assert any('已变化' in message.body for message in local.messages['trip-hangzhou'])


def test_late_requirement_response_does_not_apply_changes(monkeypatch):
    local, trip = local_trip()
    text = '预算改成2000'
    def late(messages, kwargs):
        kwargs['budget'].deadline = 0
        return completion({'body': '已更新', 'requirements_patch': {
            'changes': {'budget_amount': '2000'}, 'evidence': text}})
    scripted(monkeypatch, [delegate('requirements', text), late])
    local.add_user_message(trip.id, text)
    assert local.trips[trip.id].requirements.budget_amount == '1500'
    assert local.trips[trip.id].requirements.revision == 0
    assert local.messages[trip.id][-1].status == 'failed'


def test_delegation_with_fabricated_user_quote_is_not_executed(monkeypatch):
    local, trip = local_trip()
    requests = scripted(monkeypatch, [delegate('requirements', '预算改成2000'), completion('你好。')])
    local.add_user_message(trip.id, '你好')
    assert len(requests) == 2
    assert local.trips[trip.id].requirements.revision == 0
    assert '最新用户消息' in requests[-1][0][-1]['content']


def test_general_interest_can_create_idea_without_becoming_must_visit(monkeypatch):
    local, trip = local_trip()
    text = '想去西湖看看，还没决定'
    scripted(monkeypatch, [delegate('requirements', text), completion({
        'body': '先记下西湖这个想法。', 'requirements_patch': {},
        'idea_cards': [{'kind': '地点', 'title': '西湖', 'body': text, 'status': '待确认'}],
    })])
    local.add_user_message(trip.id, text)
    assert local.idea_cards[trip.id][-1].title == '西湖'
    assert local.trips[trip.id].requirements.must_visit == []
    assert local.trips[trip.id].requirements.revision == 0
