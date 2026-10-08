import importlib
import json
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from app import agent_client
from app.agent_client import AgentReply
from app.models import AgentCard
from app.plan_validation import PlanValidationError, validate_plan
from app.store import DemoStore


def fixture():
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    trip = local.update_trip_requirements(trip.id, {
        'start_date': '2026-11-01', 'end_date': '2026-11-02',
        'arrival_time': '15:00', 'departure_time': '12:00',
    }, 0)
    raw = asdict(local._seed_plan())
    raw['days'] = raw['days'][:2]
    for i, day in enumerate(raw['days']):
        day['date'] = f'2026-11-0{i + 1}'
        day['items'] = day['items'][:2]
        for j, item in enumerate(day['items']):
            item['time'] = (['15:00-16:00', '16:00-17:00'] if i == 0 else ['09:00-10:00', '10:00-11:00'])[j]
    return local, trip, raw


def test_valid_partial_first_last_days():
    _, trip, raw = fixture()
    validate_plan(raw, trip)


@pytest.mark.parametrize('mode,reason', [
    ('missing', '需要安排'), ('invalid_date', '日期无效'), ('duplicate', '重复日期'),
    ('extra', '超出'), ('reverse_dates', '先后顺序'), ('overlap', '重叠'),
    ('before_arrival', '抵达时间'), ('after_departure', '离开时间'),
    ('reverse_time', '结束时间'), ('bad_clock', '时刻无效'), ('ambiguous_clock', '格式不明确'),
])
def test_rejects_known_invalid_arrangements(mode, reason):
    _, trip, raw = fixture()
    if mode == 'missing': raw['days'].pop()
    elif mode == 'invalid_date': raw['days'][0]['date'] = '2026-02-31'
    elif mode == 'duplicate': raw['days'][1]['date'] = raw['days'][0]['date']
    elif mode == 'extra': raw['days'][1]['date'] = '2026-11-03'
    elif mode == 'reverse_dates': raw['days'].reverse()
    elif mode == 'overlap': raw['days'][0]['items'][1]['time'] = '15:30-17:00'
    elif mode == 'before_arrival': raw['days'][0]['items'][0]['time'] = '14:00-15:00'
    elif mode == 'after_departure': raw['days'][1]['items'][0]['time'] = '10:00-13:00'
    elif mode == 'reverse_time': raw['days'][0]['items'][0]['time'] = '16:00-15:00'
    elif mode == 'bad_clock': raw['days'][0]['items'][0]['time'] = '25:00'
    elif mode == 'ambiguous_clock': raw['days'][0]['items'][0]['time'] = '15:00之后'
    with pytest.raises(PlanValidationError, match=reason): validate_plan(raw, trip)


def test_unknown_time_not_invented_and_day_count_still_checked():
    local, trip, raw = fixture()
    trip = local.update_trip_requirements(trip.id, {'start_date': '', 'end_date': '', 'day_count': 2}, 1)
    for day in raw['days']:
        day['date'] = '待确认'
        for item in day['items']: item['time'] = '下午'
    validate_plan(raw, trip)
    raw['days'].pop()
    with pytest.raises(PlanValidationError, match='需要安排 2 天'): validate_plan(raw, trip)


def content(raw):
    return json.dumps({'body': '行程已整理', 'card': {}, 'idea_cards': [], 'plan': raw}, ensure_ascii=False)


@pytest.mark.parametrize('recovery_succeeds', [True, False])
def test_business_recovery_reuses_tools_and_stops_after_one_retry(monkeypatch, recovery_succeeds):
    _, trip, good = fixture()
    bad = deepcopy(good)
    bad['days'][0]['items'][0]['time'] = '09:00-10:00'
    requests = []
    outputs = [content(bad), content(good if recovery_succeeds else bad)]
    def create(client, model, messages, **kwargs):
        requests.append(deepcopy(messages))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=outputs.pop(0)))])
    monkeypatch.setattr(agent_client, '_create_chat_completion', create)
    if recovery_succeeds:
        agent_client._request_model(object(), 'test', '上下文', True, trip=trip)
    else:
        with pytest.raises(PlanValidationError):
            agent_client._request_model(object(), 'test', '上下文', True, trip=trip)
    assert len(requests) == 2
    assert '抵达时间' in requests[1][-1]['content']


def test_bad_generation_keeps_original_plan_and_versions(monkeypatch):
    local, trip, good = fixture()
    local._apply_agent_plan(trip.id, good)
    before = deepcopy(local.plans[trip.id])
    versions = deepcopy(local.plan_versions[trip.id])
    bad = deepcopy(good)
    bad['days'][0]['items'][0]['time'] = '08:00'
    monkeypatch.setattr(importlib.import_module('app.store'), 'generate_agent_reply', lambda **kwargs:
        AgentReply('已生成', AgentCard('itinerary-draft', '行程', '', [], []), [], bad))
    local.generate_plan(trip.id)
    assert local.plans[trip.id] == before and local.plan_versions[trip.id] == versions
    assert '抵达时间' in local.messages[trip.id][-1].body


def test_single_revision_validates_actual_merged_plan(monkeypatch):
    local, trip, good = fixture()
    local._apply_agent_plan(trip.id, good)
    before = deepcopy(local.plans[trip.id])
    versions = deepcopy(local.plan_versions[trip.id])
    response = asdict(before)
    target = response['days'][0]['items'][0]
    target['time'] = '15:00-16:30'
    # Model also moves the following item, but only the requested target is applied.
    response['days'][0]['items'][1]['time'] = '16:30-17:00'
    monkeypatch.setattr(importlib.import_module('app.store'), 'generate_agent_reply', lambda **kwargs:
        AgentReply('已调整', AgentCard('revision', '行程', '', [], []), [], response))
    local.request_revision(trip.id, target['id'], '延长半小时')
    assert local.plans[trip.id] == before and local.plan_versions[trip.id] == versions
    assert '时间重叠' in local.messages[trip.id][-1].body
