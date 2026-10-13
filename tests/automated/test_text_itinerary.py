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


def test_complete_long_text_is_saved_once_with_display_fallback(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    ready_for_planning(local, trip.id)
    source = PLAN + '\n' + ('完整说明，保留原文，不截断。\n' * 1500) + '最后一行'
    calls = scripted(monkeypatch, [completion(source)])
    local.generate_plan(trip.id)
    assert len(calls) == 1 and local.plans[trip.id].display_status == 'pending'
    local.run_pending_display_jobs()
    plan = local.plans[trip.id]
    assert plan.text == source and not plan.days and plan.has_content
    assert not hasattr(local, 'plan_versions')
    assert local.messages[trip.id][-1].body == source
    assert len(calls) == 2 and calls[0][1]['agent_role'] == 'planning' and calls[1][1]['agent_role'] == 'display'
    assert 'response_format' not in calls[0][1]


def test_revise_text_uses_current_plan_and_replaces_previous_plan(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    local._apply_text_plan(trip.id, PLAN)
    calls = scripted(monkeypatch, [requirements_unchanged(),
        completion(calls=[('read_context', {'section': 'plan'})]), completion(REVISED)])
    local.request_revision(trip.id, feedback='下午减少步行')
    assert local.plans[trip.id].text == REVISED
    assert not hasattr(local, 'plan_versions')
    context = json.loads([m for m,kw in calls if kw['agent_role']=='revision'][-1][-1]['content'])
    assert context['plan']['text'] == PLAN
    assert all('response_format' not in kw for _, kw in calls if kw['agent_role']=='revision')


def test_chat_explanation_cannot_change_text(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    local._apply_text_plan(trip.id, PLAN)
    original = deepcopy(local.plans[trip.id])
    text = '只解释下午为什么休息，不修改'
    calls = scripted(monkeypatch, [delegate('revision', text, scope='explain'),
        completion(calls=[('read_context', {'section': 'plan'})]), completion('下午保留休息，照顾轻松节奏。')])
    local.add_user_message(trip.id, text)
    assert local.plans[trip.id] == original
    assert {s['function']['name'] for s in calls[1][1]['tools']} == {'read_context'}


def test_needs_input_does_not_replace_text_or_change_plan(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    local._apply_text_plan(trip.id, PLAN)
    original = deepcopy(local.plans[trip.id])
    calls = scripted(monkeypatch, [requirements_unchanged(), completion('需要补充：你要调整哪一天的午餐？')])
    local.request_revision(trip.id, feedback='午餐换一家')
    assert len(calls) == 2 and local.plans[trip.id] == original
    assert not hasattr(local, 'plan_versions')
    assert '需要补充' in local.messages[trip.id][-1].body


@pytest.mark.parametrize('operation', ['confirm', 'requirements', 'replace'])
def test_late_text_cannot_overwrite_concurrent_changes(monkeypatch, operation):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    ready_for_planning(local, trip.id)
    local._apply_text_plan(trip.id, PLAN)
    observed = []
    def changed(messages, kwargs):
        if operation == 'confirm':
            local.confirm_plan(trip.id)
        elif operation == 'replace':
            local._apply_text_plan(trip.id,PLAN)
        else:
            local.update_trip_requirements(trip.id, {'dietary': ['花生过敏']}, 0)
        observed.append(deepcopy(local.plans[trip.id]))
        return completion(REVISED)
    scripted(monkeypatch, [changed])
    local.generate_plan(trip.id)
    assert local.plans[trip.id] == observed[0]
    assert not hasattr(local, 'plan_versions')
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


def test_only_latest_text_survives_restart(tmp_path):
    path=tmp_path/'text.sqlite3';local=DemoStore(db_path=str(path),persistence_enabled=True)
    tid='trip-hangzhou';local._apply_text_plan(tid,PLAN);local._persist()
    local._apply_text_plan(tid,REVISED);local.confirm_plan(tid);local.clear_chat(tid)
    reopened=DemoStore(db_path=str(path),persistence_enabled=True)
    assert reopened.plans[tid].text==REVISED and reopened.plans[tid].status=='已确认'
    assert not reopened.messages[tid] and not hasattr(reopened,'plan_versions')
    with sqlite3.connect(path) as connection:
        raw=json.loads(connection.execute('SELECT plan_json FROM plans WHERE trip_id=?',(tid,)).fetchone()[0])
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='plan_versions'").fetchone()
    assert raw['text']==REVISED


@pytest.mark.parametrize('flag', ['failed', 'needs_input'])
def test_failed_or_question_reply_does_not_save_even_with_text(monkeypatch, flag):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    reply = AgentReply('本次未完成', AgentCard('missing-info', '未完成', '', [], []), [],
                       plan_text=PLAN, **{flag: True})
    monkeypatch.setattr(store_module, 'generate_agent_reply', lambda **kw: reply)
    local.generate_plan(trip.id)
    assert not local.plans[trip.id].has_content


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
    calls = scripted(monkeypatch, [completion(PLAN), ValueError('受控转换失败'), requirements_unchanged(), completion(REVISED), ValueError('受控转换失败')])
    url = f'/api/trips/{trip.id}/tasks'
    for kind, payload, expected in [('plan', {}, PLAN), ('revision', {'feedback': '下午减少步行'}, REVISED)]:
        response = client.post(url, json={'kind': kind, 'payload': payload, 'request_id': kind})
        assert response.status_code == 202
        task = client.get(url).json()['tasks'][-1]
        assert task['status'] == 'succeeded' and task['result_url'].endswith('tab=itinerary')
        assert client.get(task['result_url']).status_code == 200
        assert local.plans[trip.id].text == expected
        local.run_pending_display_jobs()
    assert len(calls) == 5
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


def test_page_confirms_latest_and_has_no_history_controls(web):
    client,local,trip=web
    local._apply_text_plan(trip.id,PLAN);local._apply_text_plan(trip.id,REVISED)
    response=client.post(f'/workspace/{trip.id}/plan/confirm',follow_redirects=False)
    assert response.status_code==303 and local.plans[trip.id].status=='已确认'
    assert local.plans[trip.id].text==REVISED
    assert client.post(f'/workspace/{trip.id}/plan/restore',data={'version_id':'old'}).status_code==404
    client.post(f'/workspace/{trip.id}/messages/clear')
    page=client.get(f'/workspace/{trip.id}?tab=itinerary')
    assert REVISED in page.text and not local.messages[trip.id]
    assert '版本记录' not in page.text and '/plan/restore' not in page.text
