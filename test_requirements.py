import importlib
import json
import sqlite3
from copy import deepcopy
from dataclasses import asdict
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient

from app.agent_client import AgentReply, _build_context, _parse_reply
from app.auth import AuthStore
from app.models import AgentCard
from app.persistence import load_snapshot, save_snapshot, serialize_state
from app.requirements import update_requirements
from app.store import DemoStore, USERS

store_module = importlib.import_module('app.store')
main_module = importlib.import_module('app.main')


def reply(patch=None, plan=None, action='continue_chat'):
    return AgentReply('已处理', AgentCard('requirement-summary', '需求更新', '已处理', [], []), [], plan, action,
                      requirements_patch=patch or {})


@pytest.fixture
def local():
    return DemoStore(persistence_enabled=False)


@pytest.fixture
def web(monkeypatch, local):
    user = local.create_user('需求测试')
    auth = AuthStore('unused.sqlite3', persistence_enabled=False)
    auth.create_account('requirements@example.com', user.id, 'requirements-test-2026')
    monkeypatch.setattr(main_module, 'store', local)
    monkeypatch.setattr(main_module, 'auth', auth)
    client = TestClient(main_module.app)
    client.post('/login', data={'email': 'requirements@example.com', 'password': 'requirements-test-2026'})
    trip = local.create_trip('杭州', '日期待定', '人均 1500', '轻松', '', user)
    return client, local, trip


def form(trip, **changes):
    fields = {key: getattr(trip, key) for key in ('destination', 'date_range', 'style', 'note')}
    fields.update(asdict(trip.requirements))
    for key in ('must_visit', 'avoid', 'dietary'):
        fields[key] = '\n'.join(fields[key])
    fields = {key: '' if value is None else str(value) for key, value in fields.items()}
    fields.update(changes)
    return fields


def test_requirements_form_saves_current_values_and_preserves_chat_clear(web):
    client, local, trip = web
    response = client.post(f'/workspace/{trip.id}/requirements', data=form(
        trip, destination='济南', start_date='2026-11-01', end_date='2026-11-03', day_count='3',
        travelers='2', budget_amount='2000', budget_scope='total', must_visit='大明湖\n趵突泉',
        avoid='不爬山', dietary='不吃海鲜\n花生过敏', pace='少步行，不早起', note='带老人出行',
    ))
    assert response.status_code == 200
    assert '旅行需求已保存' in response.text
    saved = local.trips[trip.id]
    assert saved.destination == '济南'
    assert saved.requirements.day_count == 3
    assert saved.requirements.travelers == 2
    assert saved.budget == '总预算 2000 元'
    assert saved.style == '少步行，不早起'
    assert saved.requirements.dietary == ['不吃海鲜', '花生过敏']
    client.post(f'/workspace/{trip.id}/messages/clear')
    assert local.messages[trip.id] == []
    assert local.trips[trip.id].requirements.dietary == ['不吃海鲜', '花生过敏']
    assert '花生过敏' in client.get(f'/workspace/{trip.id}/requirements').text
    assert '总预算 2000 元' in client.get('/trips').text


@pytest.mark.parametrize('changes', [
    {'end_date': '2026-02-31', 'start_date': '2026-02-01'},
    {'start_date': '2026-11-05', 'end_date': '2026-11-01'},
    {'start_date': '2026-11-01'}, {'travelers': '0'}, {'travelers': '2.5'},
    {'budget_amount': '-10'}, {'budget_amount': 'NaN'}, {'budget_scope': 'invented'},
    {'start_date': '2026-11-01', 'end_date': '2026-11-03', 'day_count': '2'},
])
def test_invalid_editor_save_preserves_previous_state_and_input(web, changes):
    client, local, trip = web
    before = deepcopy(local.trips[trip.id])
    response = client.post(f'/workspace/{trip.id}/requirements', data=form(trip, note='尚未保存的说明', **changes))
    assert response.status_code == 400
    assert local.trips[trip.id] == before
    assert '尚未保存的说明' in response.text


def test_requirements_editor_rejects_old_form_without_overwriting(web):
    client, local, trip = web
    old_form = form(trip, budget_amount='1000')
    client.post(f'/workspace/{trip.id}/requirements', data=form(trip, budget_amount='2000'))
    response = client.post(f'/workspace/{trip.id}/requirements', data=old_form)
    assert response.status_code == 400
    assert '已经更新' in response.text
    assert local.trips[trip.id].requirements.budget_amount == '2000'


def test_requirements_routes_require_owner(web):
    client, local, trip = web
    other = local.create_trip('其他账号', '', '', '', '')
    assert client.get(f'/workspace/{other.id}/requirements').status_code == 404
    assert client.post(f'/workspace/{other.id}/requirements', data=form(other)).status_code == 404
    anonymous = TestClient(main_module.app)
    assert anonymous.get(f'/workspace/{trip.id}/requirements', follow_redirects=False).status_code == 303
    assert anonymous.post(f'/workspace/{trip.id}/requirements', data=form(trip), follow_redirects=False).status_code == 303


def test_full_ideas_and_requirements_survive_restart(tmp_path):
    db = str(tmp_path / 'requirements.sqlite3')
    first = DemoStore(db_path=db, persistence_enabled=True)
    trip = first.create_trip('杭州', '', '人均1500', '', '')
    first.update_trip_requirements(trip.id, {'dietary': ['不吃海鲜'], 'avoid': ['少步行'], 'travelers': 2}, 0)
    for index in range(20):
        first.add_idea(trip.id, f'想法{index}')
    restored = DemoStore(db_path=db, persistence_enabled=True)
    assert len(restored.idea_cards[trip.id]) == 20
    assert restored.idea_cards[trip.id][0].body == '想法0'
    assert restored.trips[trip.id].requirements.dietary == ['不吃海鲜']
    assert restored.trips[trip.id].requirements.travelers == 2
    assert load_snapshot(db)['schema_version'] == 3
    with sqlite3.connect(db) as connection:
        raw = connection.execute('SELECT requirements_json FROM trips WHERE id=?', (trip.id,)).fetchone()[0]
        assert json.loads(raw)['dietary'] == ['不吃海鲜']


def test_v2_personal_snapshot_migrates_requirements_without_rewriting_messages(tmp_path, local):
    snapshot = serialize_state(local, USERS)
    snapshot['schema_version'] = 2
    for raw in snapshot['trips'].values():
        raw.pop('requirements')
    snapshot['messages']['trip-hangzhou'][0]['body'] = '大家喜欢的风景，我也喜欢'
    db = str(tmp_path / 'v2.sqlite3')
    save_snapshot(db, snapshot)
    restored = DemoStore(db_path=db, persistence_enabled=True)
    assert restored.trips['trip-hangzhou'].requirements.budget_amount == '1500'
    assert restored.trips['trip-hangzhou'].requirements.budget_scope == 'per_person'
    assert restored.messages['trip-hangzhou'][0].body == '大家喜欢的风景，我也喜欢'
    assert load_snapshot(db)['schema_version'] == 3


def test_old_requirements_remain_in_context_after_long_chat_and_clear(local):
    trip = local.create_trip('杭州', '', '人均1500', '', '')
    local.update_trip_requirements(trip.id, {'dietary': ['不吃海鲜'], 'avoid': ['少步行']}, 0)
    for index in range(20):
        local.add_user_message(trip.id, f'之后的对话{index}', process_agent=False)
        local.add_idea(trip.id, f'之后的想法{index}')
    for messages in (local.messages[trip.id], []):
        context = _build_context(local.trips[trip.id], messages, '生成行程', idea_cards=local.idea_cards[trip.id])
        assert '不吃海鲜' in context and '少步行' in context
        assert '之后的想法0' in context and '之后的想法19' in context


def test_chat_updates_budget_and_adds_preferences_without_erasing_previous(monkeypatch, local):
    trip = local.create_trip('杭州', '', '人均1500', '', '')
    local.update_trip_requirements(trip.id, {'must_visit': ['西湖'], 'avoid': ['不爬山']}, 0)
    text = '预算改成2000，必须去灵隐寺，不吃海鲜'
    model_reply = _parse_reply(json.dumps({
        'body': '预算更新到每人2000，已记下必去与忌口', 'card': {}, 'idea_cards': [], 'plan': None,
        'requirements_patch': {'evidence': text, 'changes': {
            'budget_amount': '2000', 'must_visit': {'add': ['灵隐寺']}, 'dietary': {'add': ['不吃海鲜']},
        }},
    }, ensure_ascii=False))
    monkeypatch.setattr(store_module, 'generate_agent_reply', lambda **kwargs: model_reply)
    local.add_user_message(trip.id, text)
    saved = local.trips[trip.id]
    assert saved.budget == '人均 2000 元'
    assert saved.requirements.must_visit == ['西湖', '灵隐寺']
    assert saved.requirements.avoid == ['不爬山']
    assert saved.requirements.dietary == ['不吃海鲜']


@pytest.mark.parametrize('patch', [
    {'evidence': '不在用户消息中的文字', 'changes': {'budget_amount': '2000'}},
    {'evidence': '更新需求', 'changes': {'travelers': True}},
    {'evidence': '更新需求', 'changes': {'dietary': ['替换整个列表']}},
    {'evidence': '更新需求', 'changes': {'dietary': {'remove': ['不存在的条目']}}},
    {'evidence': '更新需求', 'changes': {'revision': 99}},
    {'evidence': '更新需求', 'changes': {'budget_amount': '2000', 'travelers': 0}},
])
def test_invalid_chat_patch_does_not_partially_update_requirements(monkeypatch, local, patch):
    trip = local.create_trip('杭州', '', '人均1500', '', '')
    before = deepcopy(trip)
    monkeypatch.setattr(store_module, 'generate_agent_reply', lambda **kwargs: reply(patch))
    local.add_user_message(trip.id, '更新需求')
    assert local.trips[trip.id].requirements == before.requirements
    assert local.trips[trip.id].budget == before.budget
    assert '原需求已保留' in local.messages[trip.id][-1].body


def test_explicit_removal_stays_removed_when_old_chat_is_still_present(monkeypatch, local):
    trip = local.create_trip('杭州', '', '', '', '')
    local.update_trip_requirements(trip.id, {'must_visit': ['西湖', '灵隐寺']}, 0)
    text = '灵隐寺不去了'
    monkeypatch.setattr(store_module, 'generate_agent_reply', lambda **kwargs: reply({
        'changes': {'must_visit': {'remove': ['灵隐寺']}}, 'evidence': text,
    }))
    local.add_user_message(trip.id, text)
    assert local.trips[trip.id].requirements.must_visit == ['西湖']
    context = _build_context(local.trips[trip.id], local.messages[trip.id], '生成')
    assert '不从历史自动恢复已移除的需求' in context


@pytest.mark.parametrize('planning_fails', [False, True])
def test_same_message_requirements_are_seen_by_final_planner(monkeypatch, local, planning_fails):
    trip = local.create_trip('杭州', '', '人均1500', '', '')
    text = '人均预算改成2000，不吃海鲜，生成行程吧'
    calls = []
    def model(**kwargs):
        calls.append(deepcopy(kwargs))
        if not kwargs['planning_mode']:
            return reply({'evidence': text, 'changes': {'budget_amount': '2000', 'dietary': {'add': ['不吃海鲜']}}}, action='generate_plan')
        return None if planning_fails else reply(plan=asdict(local._seed_plan()))
    monkeypatch.setattr(store_module, 'generate_agent_reply', model)
    local.add_user_message(trip.id, text)
    assert len(calls) == 2
    assert calls[1]['trip'].requirements.dietary == ['不吃海鲜']
    assert calls[1]['trip'].budget == '人均 2000 元'
    assert local.trips[trip.id].requirements.dietary == ['不吃海鲜']
    assert local.trips[trip.id].requirements.budget_amount == '2000'
    assert bool(local.plans[trip.id].days) != planning_fails


@pytest.mark.parametrize('operation', ['chat', 'generate', 'revision'])
def test_manual_edit_during_model_request_preserves_latest_requirements_and_plan(monkeypatch, local, operation):
    trip_id = 'trip-hangzhou'
    original_plan = deepcopy(local.plans[trip_id])
    versions = len(local.plan_versions[trip_id])
    entered, release = Event(), Event()
    errors = []
    def slow_model(**kwargs):
        entered.set()
        assert release.wait(5)
        return reply({'evidence': '预算改成1000', 'changes': {'budget_amount': '1000'}}, plan=asdict(original_plan))
    monkeypatch.setattr(store_module, 'generate_agent_reply', slow_model)
    def process():
        try:
            if operation == 'chat':
                local.add_user_message(trip_id, '预算改成1000')
            elif operation == 'generate':
                local.generate_plan(trip_id)
            else:
                local.request_revision(trip_id, original_plan.days[0].items[0].id, '晚一点')
        except Exception as exc:
            errors.append(exc)
    worker = Thread(target=process)
    worker.start()
    try:
        assert entered.wait(3)
        local.update_trip_requirements(trip_id, {'budget_amount': '2500', 'dietary': ['花生过敏']}, 0)
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not errors
    assert local.trips[trip_id].requirements.budget_amount == '2500'
    assert local.trips[trip_id].requirements.dietary == ['花生过敏']
    assert local.plans[trip_id] == original_plan
    assert len(local.plan_versions[trip_id]) == versions


def test_manual_requirement_deletion_and_unknown_values(local):
    trip = local.create_trip('杭州', '', '人均1500', '轻松', '')
    local.update_trip_requirements(trip.id, {'day_count': 3, 'travelers': 2, 'dietary': ['不吃海鲜']}, 0)
    saved = local.update_trip_requirements(trip.id, {'day_count': '', 'travelers': '', 'budget_amount': '', 'dietary': []}, 1)
    assert saved.requirements.day_count is None and saved.requirements.travelers is None
    assert saved.date_range == '时间待定' and saved.budget == '预算待定'
    assert saved.requirements.dietary == []


def test_preserves_ambiguous_legacy_budget_until_user_provides_amount(local):
    trip = local.create_trip('杭州', '', '每天吃饭100，机票另算', '', '')
    updated = update_requirements(trip, {'note': '新增说明', 'budget_amount': '', 'budget_scope': 'unspecified'})
    assert updated.budget == '每天吃饭100，机票另算'
