"""Offline migration contracts: old rows remain readable; only chat is created."""
from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.automated.task_fixtures import save_history, activate_task
from app.application import execution
from app.storage.store import DemoStore

TRIP = 'trip-hangzhou'


@pytest.fixture
def store(tmp_path):
    return DemoStore(db_path=str(tmp_path / 'chat-only.sqlite3'), persistence_enabled=False,
                     display_worker_enabled=False)


@pytest.mark.parametrize('submit', [lambda s, *a: s.submit_task(*a), execution.submit_task])
@pytest.mark.parametrize('kind', ['plan', 'revision', 'planning', 'unknown'])
def test_new_task_kinds_rejected_without_mutation(store, submit, kind):
    before = deepcopy((store.tasks, store.messages, store.trips))
    with pytest.raises(ValueError, match='任务类型'):
        submit(store, TRIP, kind, {'feedback': '修改'}, uuid4().hex)
    assert (store.tasks, store.messages, store.trips) == before


def test_application_and_workflow_capture_use_the_same_chat_entry(store):
    task, _ = execution.submit_task(store, TRIP, 'chat', {'body': '请生成并修改行程'}, 'application-chat')
    activate_task(store, TRIP, task['id'])
    assert store.capture_request_context(TRIP, task['id']).entry == 'main'
    assert execution.capture_request_context(store, TRIP, task['id']).entry == 'main'
    assert task['kind'] == 'chat'


def old_task(store, kind):
    body = '页面操作：生成当前行程' if kind == 'plan' else '请把西湖安排改到下午'
    task, _ = store.submit_task(TRIP, 'chat', {'body': body}, uuid4().hex)
    activate_task(store, TRIP, task['id'])
    row = store.tasks[task['id']]
    row.update(kind=kind, payload={} if kind == 'plan' else {'feedback': body}, status='failed')
    return save_history(store, row)


@pytest.mark.parametrize('kind', ['plan', 'revision', 'chat'])
def test_failed_history_is_readable_and_resend_is_a_new_chat(store, kind):
    original = old_task(store, kind)
    original['retry_of'] = 'historical-parent'
    save_history(store, original)
    saved = deepcopy(original)
    body = next(m.body for m in store.messages[TRIP] if m.id == original['message_id'])
    task, created = store.submit_task(TRIP, 'chat', {'body': body}, 'new-chat')
    assert created and task['id'] != original['id']
    assert task['message_id'] != original['message_id']
    assert 'retry_of' not in task
    assert store.get_task(original['id']) == saved
    activate_task(store, TRIP, task['id'])
    assert store.capture_request_context(TRIP, task['id']).entry == 'main'
    replay, created = store.submit_task(TRIP, 'chat', {'body': body}, 'new-chat')
    assert not created and replay['id'] == task['id']


@pytest.mark.parametrize('submit', [lambda s, *a, **kw: s.submit_task(*a, **kw), execution.submit_task])
def test_no_server_retry_submission_or_payload_recovery(store, submit):
    row = old_task(store, 'revision')
    before = deepcopy((store.tasks, store.messages, store.trips))
    assert not hasattr(execution, 'retry_payload')
    with pytest.raises(TypeError):
        submit(store, TRIP, 'chat', {'body': '修改'}, 'new-id', retry_of=row['id'])
    assert (store.tasks, store.messages, store.trips) == before


@pytest.mark.parametrize('damage', ['owner', 'body', 'missing', 'basis', 'partial'])
def test_new_chat_does_not_recover_historical_authority(store, damage):
    row = old_task(store, 'revision')
    message = next(m for m in store.messages[TRIP] if m.id == row['message_id'])
    if damage == 'owner':
        from dataclasses import replace
        message.sender = replace(message.sender, id='different-owner')
    elif damage == 'body':
        message.body = '不是原始修改意见'
    elif damage == 'missing':
        store.messages[TRIP].remove(message)
    elif damage == 'basis':
        row['plan_basis']['plan_id'] = 'stale'
    else:
        row['receipt'] = {'accepted_artifact_ids': ['already-applied']}
    save_history(store, row)
    saved = deepcopy(row)
    task, created = store.submit_task(TRIP, 'chat', {'body': '请修改行程'}, 'fresh-authority')
    source = next(m for m in store.messages[TRIP] if m.id == task['message_id'])
    assert created and source.id != row['message_id']
    assert source.sender.id == store.trips[TRIP].owner.id
    assert source.body == '请修改行程'
    assert 'plan_basis' not in task
    activate_task(store, TRIP, task['id'])
    assert store.tasks[task['id']]['plan_basis']['plan_id'] == store.plans[TRIP].id
    assert store.get_task(row['id']) == saved
    assert 'retry_of' not in task


def test_failed_request_acknowledgement_is_not_reexecuted(store):
    task, _ = store.submit_task(TRIP, 'chat', {'body': '你好'}, 'delivered')
    task['status'] = 'failed'
    save_history(store, task)
    replay, created = store.submit_task(TRIP, 'chat', {'body': '你好'}, 'delivered')
    assert not created and replay['status'] == 'failed'
    assert store.tasks == {}


def test_task_api_rejects_old_types_and_keeps_history_readable(store, monkeypatch):
    from app import main
    from app.api import dependencies as deps
    from tests.automated.test_web_contract import csrf_headers
    rows = [old_task(store, kind) for kind in ('plan', 'revision', 'chat')]
    rows[-1]['retry_of'] = rows[0]['id']
    for row in rows:
        save_history(store, row)
    saved = deepcopy(rows)
    monkeypatch.setattr(deps, 'store', store)
    monkeypatch.setattr(deps, 'current_user', lambda request: store.trips[TRIP].owner)
    monkeypatch.setattr(store, 'run_task', lambda task_id: None)
    with TestClient(main.app) as client:
        headers = csrf_headers(client)
        for kind in ('plan', 'revision'):
            response = client.post(f'/api/trips/{TRIP}/tasks', json={
                'kind': kind, 'payload': {}, 'request_id': uuid4().hex}, headers=headers)
            assert response.status_code == 422
        history = client.get(f'/api/trips/{TRIP}/tasks').json()['tasks']
        assert [task['kind'] for task in history] == ['plan', 'revision', 'chat']
        assert history[-1]['retry_of'] == rows[0]['id']
        for row in rows:
            response = client.post(f'/api/trips/{TRIP}/tasks/{row["id"]}/retry',
                                   json={'request_id': 'migrated'}, headers=headers)
            assert response.status_code == 404
        assert [store.get_task(row['id']) for row in rows] == saved
        response = client.post(f'/api/trips/{TRIP}/messages',
                               json={'body': '请生成行程', 'request_id': 'generation'}, headers=headers)
        assert response.status_code == 202 and response.json()['task']['kind'] == 'chat'
