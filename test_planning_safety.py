"""Planning safety regressions; no real model calls or persistent business data."""
import importlib
import json
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from app.agent_client import AgentReply, _build_context, _parse_reply
from app.models import AgentCard
from app.store import DemoStore

store_module = importlib.import_module('app.store')


def response(action='continue_chat', plan=None):
    return AgentReply('回复', AgentCard('missing-info', '回复', '', [], []), [], plan, action)


@pytest.mark.parametrize('text,action', [
    ('我们两个人', 'ask_plan_confirmation'),
    ('不要生成计划', 'continue_chat'),
    ('行程先不做', 'continue_chat'),
    ('先聊聊安排', 'continue_chat'),
    ('帮我看看已有行程', 'show_plan'),
])
def test_chat_routes_by_intent_not_keywords_or_buttons(monkeypatch, text, action):
    local = DemoStore(persistence_enabled=False)
    parsed = _parse_reply(json.dumps({'body': '回复', 'card': {'next_action': action}}))
    calls = []
    def generate(**kwargs):
        calls.append(kwargs['planning_mode'])
        return parsed
    monkeypatch.setattr(store_module, 'generate_agent_reply', generate)
    original = deepcopy(local.plans['trip-hangzhou'])
    local.add_user_message('trip-hangzhou', text)
    assert calls == [False]
    assert local.plans['trip-hangzhou'] == original


def test_semantic_confirmation_runs_planning_once(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    calls = []
    def generate(**kwargs):
        calls.append(kwargs['planning_mode'])
        return response('show_plan', asdict(local._seed_plan())) if kwargs['planning_mode'] else response('generate_plan')
    monkeypatch.setattr(store_module, 'generate_agent_reply', generate)
    versions = len(local.plan_versions['trip-hangzhou'])
    local.add_user_message('trip-hangzhou', '可以，就按这些出一版吧')
    assert calls == [False, True]
    assert len(local.plan_versions['trip-hangzhou']) == versions + 1


@pytest.mark.parametrize('failure', [None, response()])
def test_failed_generation_keeps_empty_trip_empty(monkeypatch, failure):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('北京', '待定', '待定', '轻松', '')
    monkeypatch.setattr(store_module, 'generate_agent_reply', lambda **kw: failure)
    local.generate_plan(trip.id)
    assert not local.plans[trip.id].days
    assert not local.plan_versions[trip.id]
    assert '已生成' not in local.messages[trip.id][-1].body


@pytest.mark.parametrize('content', ['计划已生成', '{"body":"完成","plan":{}}', '{bad json'])
def test_invalid_model_plan_is_failure_and_never_replaces_draft(monkeypatch, content):
    from app import agent_client
    local = DemoStore(persistence_enabled=False)
    original = deepcopy(local.plans['trip-hangzhou'])
    versions = deepcopy(local.plan_versions['trip-hangzhou'])
    monkeypatch.setattr(agent_client, 'is_llm_enabled', lambda: True)
    monkeypatch.setattr(agent_client.ConversationTrace, 'write', lambda *a, **kw: None)
    monkeypatch.setattr('openai.OpenAI', lambda **kw: object())
    monkeypatch.setattr(agent_client, '_request_model', lambda *a, **kw: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]))
    local.generate_plan('trip-hangzhou')
    assert local.plans['trip-hangzhou'] == original
    assert local.plan_versions['trip-hangzhou'] == versions
    assert local.messages['trip-hangzhou'][-1].agent_card.title == '模型调用暂时失败'


def test_strict_plan_preserves_seven_days_and_nine_items():
    local = DemoStore(persistence_enabled=False)
    plan = asdict(local._seed_plan())
    template = plan['days'][0]
    plan['days'] = []
    for day in range(7):
        data = deepcopy(template)
        data['label'] = f'Day {day + 1}'
        data['items'] = [dict(template['items'][0], title=f'安排{day}-{i}') for i in range(9)]
        plan['days'].append(data)
    parsed = _parse_reply(json.dumps({'body': '完成', 'plan': plan}), strict_plan=True)
    local._apply_agent_plan('trip-hangzhou', parsed.plan)
    stored = local.plans['trip-hangzhou']
    assert len(stored.days) == 7
    assert [len(day.items) for day in stored.days] == [9] * 7
    assert stored.days[-1].items[-1].title == '安排6-8'
    context = _build_context(local.trips['trip-hangzhou'], [], '修改', plan=stored)
    assert '安排6-8' in context
    assert stored.days[-1].items[-1].id in context


def test_all_visible_ideas_including_old_allergy_reach_model():
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('北京', '待定', '待定', '轻松', '')
    for text in ['严重花生过敏'] + [f'想法{i}' for i in range(7)]:
        local.add_idea(trip.id, text)
    context = _build_context(trip, [], '生成', idea_cards=local.idea_cards[trip.id])
    for card in local.idea_cards[trip.id]:
        assert card.body in context


def test_revision_changes_only_requested_item_and_preserves_old_version(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    original = deepcopy(local.plans['trip-hangzhou'])
    original_versions = deepcopy(local.plan_versions['trip-hangzhou'])
    target = original.days[0].items[0]
    proposed = asdict(original)
    proposed['days'][0]['items'][0]['time'] = '14:30'
    # Deliberate unrelated model edits must never reach the saved plan.
    proposed['days'][-1]['items'][-1]['title'] = '错误地改动其他安排'
    proposed['overview'] = '错误地改动概览'
    proposed['preparation'] = {'clothing': [], 'accommodation': []}
    calls = []
    def generate(**kwargs):
        calls.append(kwargs)
        return response(plan=proposed)
    monkeypatch.setattr(store_module, 'generate_agent_reply', generate)
    local.request_revision('trip-hangzhou', target.id, '推迟半小时')
    actual = local.plans['trip-hangzhou']
    assert calls[0]['planning_mode'] is True
    assert calls[0]['plan'] == original
    expected = deepcopy(original)
    expected.days[0].items[0] = replace(target, time='14:30')
    assert actual.days == expected.days
    assert actual.preparation == original.preparation
    assert actual.overview == original.overview
    assert local.plan_versions['trip-hangzhou'][:-1] == original_versions


@pytest.mark.parametrize('mode', ['missing_id', 'duplicate_id', 'unavailable', 'concurrent'])
def test_revision_failure_does_not_overwrite_plan(monkeypatch, mode):
    local = DemoStore(persistence_enabled=False)
    original = deepcopy(local.plans['trip-hangzhou'])
    versions = deepcopy(local.plan_versions['trip-hangzhou'])
    target = original.days[0].items[0]
    proposed = asdict(original)
    if mode == 'missing_id':
        proposed['days'][0]['items'][0].pop('id')
    if mode == 'duplicate_id':
        proposed['days'][0]['items'][1]['id'] = target.id
    def generate(**kwargs):
        if mode == 'concurrent':
            local.plans['trip-hangzhou'].title = '另一项操作保存的新标题'
        return None if mode == 'unavailable' else response(plan=proposed)
    monkeypatch.setattr(store_module, 'generate_agent_reply', generate)
    local.request_revision('trip-hangzhou', target.id, '晚一点')
    if mode == 'concurrent':
        original.title = '另一项操作保存的新标题'
    assert local.plans['trip-hangzhou'] == original
    assert local.plan_versions['trip-hangzhou'] == versions
