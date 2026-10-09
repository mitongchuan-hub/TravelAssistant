"""Text itinerary lifecycle: generation, editing, versions, persistence and pages."""
from copy import deepcopy
import importlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.agent_client import AgentReply
from app.auth import AuthStore
from app.models import AgentCard
from app.store import DemoStore
from test_agent_roles import completion, delegate, scripted, ready_for_planning, requirements_unchanged

store_module = importlib.import_module('app.store')
main_module = importlib.import_module('app.main')

PLAN = '杭州一日行程\n\n上午：西湖散步。\n中午：附近用餐，不吃海鲜。\n下午：原地休息。\n\n开放、餐厅预约与交通时间待核实。'
REVISED = PLAN.replace('原地休息', '湖边休息，减少步行')


def test_complete_long_text_is_saved_once_without_conversion(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    ready_for_planning(local, trip.id)
    source = PLAN + '\n' + ('完整说明，保留原文，不截断。\n' * 1500) + '最后一行'
    calls = scripted(monkeypatch, [completion(source)])
    local.generate_plan(trip.id)
    plan = local.plans[trip.id]
    assert plan.text == source and not plan.days and plan.has_content
    assert len(local.plan_versions[trip.id]) == 1
    assert local.plan_versions[trip.id][0].text == source
    assert local.messages[trip.id][-1].body == source
    assert len(calls) == 1 and calls[0][1]['agent_role'] == 'planning'
    assert 'response_format' not in calls[0][1]


def test_revise_text_uses_current_plan_and_keeps_previous_version(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    local._apply_text_plan(trip.id, PLAN)
    calls = scripted(monkeypatch, [requirements_unchanged(),
        completion(calls=[('read_context', {'section': 'plan'})]), completion(REVISED)])
    local.request_revision(trip.id, feedback='下午减少步行')
    assert local.plans[trip.id].text == REVISED
    assert [v.text for v in local.plan_versions[trip.id]] == [PLAN, REVISED]
    context = json.loads(calls[-1][0][-1]['content'])
    assert context['plan']['text'] == PLAN
    assert all('response_format' not in kw for _, kw in calls)


def test_chat_explanation_cannot_create_a_text_version(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    local._apply_text_plan(trip.id, PLAN)
    original = deepcopy(local.plans[trip.id])
    text = '只解释下午为什么休息，不修改'
    calls = scripted(monkeypatch, [delegate('revision', text, scope='explain'),
        completion(calls=[('read_context', {'section': 'plan'})]), completion('下午保留休息，照顾轻松节奏。')])
    local.add_user_message(trip.id, text)
    assert local.plans[trip.id] == original and len(local.plan_versions[trip.id]) == 1
    assert {s['function']['name'] for s in calls[1][1]['tools']} == {'read_context'}


def test_needs_input_does_not_replace_text_or_create_version(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    local._apply_text_plan(trip.id, PLAN)
    original = deepcopy(local.plans[trip.id])
    calls = scripted(monkeypatch, [requirements_unchanged(), completion('需要补充：你要调整哪一天的午餐？')])
    local.request_revision(trip.id, feedback='午餐换一家')
    assert len(calls) == 2 and local.plans[trip.id] == original
    assert len(local.plan_versions[trip.id]) == 1
    assert '需要补充' in local.messages[trip.id][-1].body


@pytest.mark.parametrize('operation', ['confirm', 'requirements', 'restore'])
def test_late_text_cannot_overwrite_concurrent_changes(monkeypatch, operation):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    ready_for_planning(local, trip.id)
    local._apply_text_plan(trip.id, PLAN)
    observed = []
    def changed(messages, kwargs):
        if operation == 'confirm':
            local.confirm_plan(trip.id)
        elif operation == 'restore':
            local.restore_plan_version(trip.id, local.plan_versions[trip.id][0].id)
        else:
            local.update_trip_requirements(trip.id, {'dietary': ['花生过敏']}, 0)
        observed.append(deepcopy(local.plans[trip.id]))
        return completion(REVISED)
    scripted(monkeypatch, [changed])
    local.generate_plan(trip.id)
    assert local.plans[trip.id] == observed[0]
    assert all(v.text == PLAN for v in local.plan_versions[trip.id])
    assert any(m.status == 'failed' and ('已变化' in m.body or '已更新' in m.body)
               for m in local.messages[trip.id])


def test_task_rejected_after_confirmation_reports_its_own_failure(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    ready_for_planning(local, trip.id)
    local._apply_text_plan(trip.id, PLAN)
    def confirm_during_generation(messages, kwargs):
        local.confirm_plan(trip.id)
        return completion(REVISED)
    scripted(monkeypatch, [confirm_during_generation])
    task, _ = local.submit_task(trip.id, 'plan', {}, 'late-text')
    local.run_task(task['id'])
    assert local.plans[trip.id].text == PLAN and local.plans[trip.id].status == '已确认'
    assert local.tasks[task['id']]['status'] == 'failed'
    assert '已变化' in local.tasks[task['id']]['message']


def test_text_and_legacy_versions_survive_restart_and_restore(tmp_path):
    path = tmp_path / 'text.sqlite3'
    local = DemoStore(db_path=str(path), persistence_enabled=True)
    trip_id = 'trip-hangzhou'
    legacy = deepcopy(local.plans[trip_id])
    legacy_version = local.plan_versions[trip_id][0].id
    local._apply_text_plan(trip_id, PLAN)
    text_version = local.plan_versions[trip_id][-1].id
    local.confirm_plan(trip_id)
    local.clear_chat(trip_id)
    restored = DemoStore(db_path=str(path), persistence_enabled=True)
    assert restored.plans[trip_id].text == PLAN and restored.plans[trip_id].status == '已确认'
    assert not restored.messages[trip_id]
    assert restored.restore_plan_version(trip_id, legacy_version)
    assert restored.plans[trip_id].days == legacy.days and restored.plans[trip_id].text == ''
    assert restored.restore_plan_version(trip_id, text_version)
    assert restored.plans[trip_id].text == PLAN and not restored.plans[trip_id].days
    assert restored.plans[trip_id].status == '草案'
    reopened = DemoStore(db_path=str(path), persistence_enabled=True)
    assert reopened.plans[trip_id].text == PLAN
    with sqlite3.connect(path) as connection:
        raw = json.loads(connection.execute('SELECT plan_json FROM plans WHERE trip_id=?', (trip_id,)).fetchone()[0])
    assert raw['text'] == PLAN


@pytest.mark.parametrize('flag', ['failed', 'needs_input'])
def test_failed_or_question_reply_does_not_save_even_with_text(monkeypatch, flag):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    reply = AgentReply('本次未完成', AgentCard('missing-info', '未完成', '', [], []), [],
                       plan_text=PLAN, **{flag: True})
    monkeypatch.setattr(store_module, 'generate_agent_reply', lambda **kw: reply)
    local.generate_plan(trip.id)
    assert not local.plans[trip.id].has_content and not local.plan_versions[trip.id]


@pytest.fixture
def web(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    user = local.create_user('文字行程测试')
    auth = AuthStore('unused.sqlite3', False)
    auth.create_account('text-plan@example.com', user.id, 'text-plan-password-2026')
    monkeypatch.setattr(main_module, 'store', local)
    monkeypatch.setattr(main_module, 'auth', auth)
    client = TestClient(main_module.app)
    client.post('/login', data={'email': 'text-plan@example.com', 'password': 'text-plan-password-2026'})
    trip = local.create_trip('杭州', '', '', '', '', user)
    ready_for_planning(local, trip.id)
    return client, local, trip


def test_task_generation_and_revision_both_return_to_itinerary_page(web, monkeypatch):
    client, local, trip = web
    calls = scripted(monkeypatch, [completion(PLAN), requirements_unchanged(), completion(REVISED)])
    url = f'/api/trips/{trip.id}/tasks'
    for kind, payload, expected in [('plan', {}, PLAN), ('revision', {'feedback': '下午减少步行'}, REVISED)]:
        response = client.post(url, json={'kind': kind, 'payload': payload, 'request_id': kind})
        assert response.status_code == 202
        task = client.get(url).json()['tasks'][-1]
        assert task['status'] == 'succeeded' and task['result_url'].endswith('tab=itinerary')
        assert client.get(task['result_url']).status_code == 200
        assert local.plans[trip.id].text == expected
    assert len(calls) == 3 and len(local.plan_versions[trip.id]) == 2
    assert client.post(url, json={'kind': 'revision', 'payload': {'feedback': '旧目标', 'item_id': 'missing'}, 'request_id': 'invalid-target'}).status_code == 400


def test_page_displays_escaped_text_and_has_revision_and_confirmation_controls(web):
    client, local, trip = web
    source = PLAN + '\n<script>window.untrusted = true</script>\n' + ('长内容' * 300)
    local._apply_text_plan(trip.id, source)
    page = client.get(f'/workspace/{trip.id}?tab=itinerary')
    assert page.status_code == 200 and 'data-itinerary-text' in page.text
    assert '&lt;script&gt;window.untrusted = true&lt;/script&gt;' in page.text
    assert '<script>window.untrusted' not in page.text
    assert '还没有行程' not in page.text and 'data-day-nav' not in page.text
    assert '天已安排' not in page.text and '确认当前行程' in page.text
    assert '更新行程' in page.text and 'name="feedback"' in page.text
    refreshed = client.get(f'/workspace/{trip.id}?tab=itinerary')
    assert PLAN in refreshed.text and source.splitlines()[-1] in refreshed.text
    assert local.plans[trip.id].text == source


def test_page_confirm_restore_and_clear_chat_preserve_text(web):
    client, local, trip = web
    local._apply_text_plan(trip.id, PLAN)
    version_id = local.plan_versions[trip.id][0].id
    local._apply_text_plan(trip.id, REVISED)
    response = client.post(f'/workspace/{trip.id}/plan/confirm', follow_redirects=False)
    assert response.status_code == 303 and local.plans[trip.id].status == '已确认'
    assert local.plans[trip.id].text == REVISED
    response = client.post(f'/workspace/{trip.id}/plan/restore', data={'version_id': version_id}, follow_redirects=False)
    assert response.status_code == 303 and local.plans[trip.id].text == PLAN
    client.post(f'/workspace/{trip.id}/messages/clear')
    page = client.get(f'/workspace/{trip.id}?tab=itinerary')
    assert PLAN in page.text and not local.messages[trip.id]
    assert '版本记录' in page.text
