import importlib
from copy import deepcopy
from dataclasses import asdict
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient

from app.agent_client import AgentReply
from app.auth import AuthStore
from app.models import AgentCard
from app.store import DemoStore
from app.tasks import TaskConflict

store_module = importlib.import_module('app.store')
main_module = importlib.import_module('app.main')


def successful(local, planning):
    return AgentReply('完成', AgentCard('itinerary-draft', '已处理', '', [], []), [],
                      asdict(local._seed_plan()) if planning else None)


def test_duplicate_submission_and_execution_are_idempotent(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    calls = []
    monkeypatch.setattr(store_module, 'generate_agent_reply', lambda **kwargs: calls.append(True) or successful(local, True))
    first, created = local.submit_task(trip.id, 'plan', {}, 'request-1')
    duplicate, again = local.submit_task(trip.id, 'plan', {}, 'request-1')
    assert created and not again and first['id'] == duplicate['id']
    local.run_task(first['id'])
    local.run_task(first['id'])
    assert calls == [True]
    assert not hasattr(local, 'plan_versions')
    final, created = local.submit_task(trip.id, 'plan', {}, 'request-1')
    assert not created and final['status'] == 'succeeded'
    with pytest.raises(TaskConflict): local.submit_task(trip.id, 'chat', {'body': '不同内容'}, 'request-1')


def test_slow_task_status_is_readable_and_other_trips_can_submit(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    other = local.create_trip('济南', '', '', '', '')
    entered, release = Event(), Event()
    def slow(**kwargs):
        entered.set()
        assert release.wait(5)
        return successful(local, True)
    monkeypatch.setattr(store_module, 'generate_agent_reply', slow)
    task, _ = local.submit_task(trip.id, 'plan', {}, 'first')
    worker = Thread(target=local.run_task, args=(task['id'],))
    worker.start()
    try:
        assert entered.wait(3)
        assert local.list_tasks(trip.id)[0]['status'] == 'running'
        with pytest.raises(TaskConflict): local.submit_task(trip.id, 'plan', {}, 'second')
        assert local.submit_task(other.id, 'plan', {}, 'other')[1]
    finally:
        release.set()
        worker.join(5)
    assert local.tasks[task['id']]['status'] == 'succeeded'


def test_failed_chat_retry_keeps_original_user_message(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    monkeypatch.setattr(store_module, 'generate_agent_reply', lambda **kwargs: None)
    task, _ = local.submit_task(trip.id, 'chat', {'body': '少步行'}, 'chat-1')
    local.run_task(task['id'])
    assert local.tasks[task['id']]['status'] == 'failed'
    monkeypatch.setattr(store_module, 'generate_agent_reply', lambda **kwargs: successful(local, False))
    retried, _ = local.submit_task(trip.id, 'chat', {'body': '少步行'}, 'retry-1', task['id'])
    duplicate, created = local.submit_task(trip.id, 'chat', {'body': '少步行'}, 'retry-1', task['id'])
    assert not created and duplicate['id'] == retried['id']
    local.run_task(retried['id'])
    assert local.tasks[retried['id']]['status'] == 'succeeded'
    recovered, created = local.submit_task(trip.id, 'chat', {'body': '少步行'}, 'retry-lost-response', task['id'])
    assert not created and recovered['id'] == retried['id']
    assert sum(message.sender_type == 'user' for message in local.messages[trip.id]) == 1


def test_restart_recovers_interrupted_tasks_as_retryable(tmp_path):
    db = str(tmp_path / 'tasks.sqlite3')
    local = DemoStore(db_path=db, persistence_enabled=True)
    trip = local.create_trip('杭州', '', '', '', '')
    task, _ = local.submit_task(trip.id, 'chat', {'body': '不吃海鲜'}, 'lost-request')
    restored = DemoStore(db_path=db, persistence_enabled=True)
    saved = restored.tasks[task['id']]
    assert saved['status'] == 'failed' and '重启' in saved['message']
    same, created = restored.submit_task(trip.id, 'chat', {'body': '不吃海鲜'}, 'lost-request')
    assert not created and same['id'] == task['id']
    assert any(message.body == '不吃海鲜' for message in restored.messages[trip.id])
    assert not any(message.status == 'thinking' for message in restored.messages[trip.id])
    reopened = DemoStore(db_path=db, persistence_enabled=True)
    assert reopened.tasks[task['id']]['status'] == 'failed'


def test_stale_queued_plan_does_not_overwrite_new_requirements(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    task, _ = local.submit_task(trip.id, 'plan', {}, 'plan-1')
    local.update_trip_requirements(trip.id, {'dietary': ['花生过敏']}, 0)
    calls = []
    monkeypatch.setattr(store_module, 'generate_agent_reply', lambda **kwargs: calls.append(True))
    local.run_task(task['id'])
    assert not calls and not local.plans[trip.id].days
    assert local.tasks[task['id']]['status'] == 'failed'
    assert '已变化' in local.tasks[task['id']]['message']


@pytest.fixture
def web(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    user = local.create_user('任务测试')
    auth = AuthStore('unused.sqlite3', False)
    auth.create_account('tasks@example.com', user.id, 'tasks-password-2026')
    monkeypatch.setattr(main_module, 'store', local)
    monkeypatch.setattr(main_module, 'auth', auth)
    client = TestClient(main_module.app)
    client.post('/login', data={'email': 'tasks@example.com', 'password': 'tasks-password-2026'})
    trip = local.create_trip('杭州', '', '', '', '', user)
    return client, local, trip


def test_task_api_accepts_then_queries_persisted_result_and_deduplicates(web, monkeypatch):
    client, local, trip = web
    calls = []
    monkeypatch.setattr(store_module, 'generate_agent_reply', lambda **kwargs: calls.append(True) or successful(local, True))
    url = f'/api/trips/{trip.id}/tasks'
    response = client.post(url, json={'kind': 'plan', 'request_id': 'plan-request'})
    assert response.status_code == 202 and response.json()['task']['status'] == 'queued'
    saved = client.get(url).json()['tasks'][0]
    assert saved['status'] == 'succeeded' and saved['result_url'].endswith('tab=itinerary')
    duplicate = client.post(url, json={'kind': 'plan', 'request_id': 'plan-request'})
    assert duplicate.status_code == 200 and len(calls) == 1
    assert client.post(url, json={'kind': 'chat', 'request_id': 'plan-request', 'payload': {'body': '不同'}}).status_code == 409
    assert client.post(url + '/' + saved['id'] + '/retry', json={'request_id': 'retry'}).status_code == 409


def test_task_api_failed_generation_retry_and_authorization(web, monkeypatch):
    client, local, trip = web
    url = f'/api/trips/{trip.id}/tasks'
    monkeypatch.setattr(store_module, 'generate_agent_reply', lambda **kwargs: None)
    task = client.post(url, json={'kind': 'plan', 'request_id': 'first'}).json()['task']
    assert client.get(url).json()['tasks'][0]['status'] == 'failed'
    monkeypatch.setattr(store_module, 'generate_agent_reply', lambda **kwargs: successful(local, True))
    retry = client.post(url + '/' + task['id'] + '/retry', json={'request_id': 'retry'})
    assert retry.status_code == 202
    assert client.get(url).json()['tasks'][-1]['status'] == 'succeeded'
    other = local.create_trip('别人的旅行', '', '', '', '')
    assert client.get(f'/api/trips/{other.id}/tasks').status_code == 404
    assert client.post(f'/api/trips/{other.id}/tasks', json={'kind': 'plan', 'request_id': 'x'}).status_code == 404
    anonymous = TestClient(main_module.app)
    assert anonymous.get(url).status_code == 401
    assert anonymous.post(url, json={'kind': 'plan', 'request_id': 'x'}).status_code == 401


@pytest.mark.parametrize('data', [None, [], {}, {'kind': 'invented', 'request_id': 'x'},
    {'kind': 'plan', 'request_id': ''}, {'kind': 'revision', 'request_id': 'x', 'payload': {'feedback': ''}},
    {'kind': 'chat', 'request_id': 'x', 'payload': {'body': []}}])
def test_invalid_task_api_payloads_do_not_create_tasks(web, data):
    client, local, trip = web
    assert client.post(f'/api/trips/{trip.id}/tasks', json=data).status_code == 400
    assert not local.list_tasks(trip.id)


def test_plan_changed_during_task_is_not_overwritten(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    trip_id = 'trip-hangzhou'
    entered, release = Event(), Event()
    def slow(**kwargs):
        entered.set()
        assert release.wait(5)
        return successful(local, True)
    monkeypatch.setattr(store_module, 'generate_agent_reply', slow)
    task, _ = local.submit_task(trip_id, 'plan', {}, 'slow-plan')
    worker = Thread(target=local.run_task, args=(task['id'],))
    worker.start()
    try:
        assert entered.wait(3)
        local.confirm_plan(trip_id)
        restored = deepcopy(local.plans[trip_id])
    finally:
        release.set()
        worker.join(5)
    assert local.plans[trip_id] == restored
    assert not hasattr(local, 'plan_versions')
    assert local.tasks[task['id']]['status'] == 'failed'
    assert '已变化' in local.tasks[task['id']]['message']
