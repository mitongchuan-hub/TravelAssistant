"""Message policy stays consistent across HTTP, task and protocol boundaries."""
from copy import deepcopy
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.core.message_input import MAX_MESSAGE_LENGTH
from app.storage.store import DemoStore
from tests.automated.test_web_contract import csrf_headers


@pytest.fixture
def store():
    return DemoStore(persistence_enabled=False, display_worker_enabled=False)


@pytest.mark.parametrize('path', ['messages', 'tasks'])
@pytest.mark.parametrize('body,message', [
    (['你好'], '消息必须是文本'),
    (None, '消息必须是文本'),
    (' \n\t ', '消息不能为空'),
    ('文' * (MAX_MESSAGE_LENGTH + 1), '消息不能超过 10000 字'),
    (' ' + '文' * MAX_MESSAGE_LENGTH, '消息不能超过 10000 字'),
], ids=['array', 'null', 'blank', 'over-limit', 'padded-over-limit'])
def test_invalid_messages_never_create_tasks_or_call_agent(store, monkeypatch, path, body, message):
    from app.main import app
    from app.api import dependencies as deps
    monkeypatch.setattr(deps, 'store', store)
    monkeypatch.setattr(deps, 'current_user', lambda request: store.trips['trip-hangzhou'].owner)
    ran = []
    monkeypatch.setattr(store, 'run_task', lambda task_id: ran.append(task_id))
    before = deepcopy((store.trips, store.messages, store.tasks))
    data = {'body': body, 'request_id': 'invalid'} if path == 'messages' else {
        'kind': 'chat', 'payload': {'body': body}, 'request_id': 'invalid'}
    with TestClient(app) as client:
        response = client.post(f'/api/trips/trip-hangzhou/{path}', json=data, headers=csrf_headers(client))
    assert response.status_code in (400, 422)
    assert message in response.json()['error']['message']
    assert not ran
    assert (store.trips, store.messages, store.tasks) == before


@pytest.mark.parametrize('first_path', ['messages', 'tasks'])
def test_message_limit_and_normalized_identity_are_shared_across_endpoints(store, monkeypatch, first_path):
    from app.main import app
    from app.api import dependencies as deps
    monkeypatch.setattr(deps, 'store', store)
    monkeypatch.setattr(deps, 'current_user', lambda request: store.trips['trip-hangzhou'].owner)
    ran = []
    monkeypatch.setattr(store, 'run_task', lambda task_id: ran.append(task_id))
    with TestClient(app) as client:
        headers = csrf_headers(client)
        for path in [first_path, 'tasks' if first_path == 'messages' else 'messages']:
            data = {'body': ' \n你好\t ', 'request_id': 'same'} if path == 'messages' else {
                'kind': 'chat', 'payload': {'body': ' \n你好\t '}, 'request_id': 'same'}
            response = client.post(f'/api/trips/trip-hangzhou/{path}', json=data, headers=headers)
            assert response.status_code == 202
        assert len(ran) == 1
        task = next(iter(store.tasks.values()))
        assert task['payload']['body'] == '你好'
        assert next(row.body for row in store.messages['trip-hangzhou'] if row.id == task['message_id']) == '你好'
        response = client.post('/api/trips/trip-hangzhou/messages', headers=headers,
                               json={'body': '文' * MAX_MESSAGE_LENGTH, 'request_id': 'boundary'})
        assert response.status_code == 202


def test_internal_context_reuses_limit_without_rewriting_authority(store):
    context = store.capture_request_context('trip-hangzhou', body=' 原话 ')
    assert context.body == ' 原话 '
    assert replace(context, body='').body == ''  # Internal operations need not originate in chat.
    assert len(replace(context, body='文' * MAX_MESSAGE_LENGTH).body) == MAX_MESSAGE_LENGTH
    with pytest.raises(ValueError, match='10000'):
        replace(context, body='文' * (MAX_MESSAGE_LENGTH + 1))


def test_direct_submission_checks_before_mutating_and_normalizes_before_deduplication(store):
    before = deepcopy((store.trips, store.messages, store.tasks))
    with pytest.raises(ValueError, match='不能为空'):
        store.submit_task('trip-hangzhou', 'chat', {'body': '  '}, 'invalid')
    assert (store.trips, store.messages, store.tasks) == before
    first, created = store.submit_task('trip-hangzhou', 'chat', {'body': ' 你好 '}, 'same')
    replay, created_again = store.submit_task('trip-hangzhou', 'chat', {'body': '你好'}, 'same')
    assert created and not created_again and first['id'] == replay['id']
