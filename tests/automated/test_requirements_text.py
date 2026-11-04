"""Prose-only requirements, fixed creation and schema-ten atomic migration."""
from copy import deepcopy
from dataclasses import asdict
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.application.contracts import ExecutionResult
from app.application.undo import capture, metadata, record_change, undo
from app.domain.changes import prepare_changes
from app.domain.requirements import update_requirements
from app.storage import persistence
from app.storage.model_migration import migrate_snapshot
from app.storage.store import DemoStore, USERS
from tests.automated.task_fixtures import running_context
from tests.automated.test_web_contract import csrf_headers

TRIP = 'trip-hangzhou'


def store(tmp_path, persistent=False):
    return DemoStore(str(tmp_path / 'text.sqlite3'), persistence_enabled=persistent,
                     display_worker_enabled=False)


def test_prose_replace_empty_noop_unicode_and_legacy_oversize(tmp_path):
    saved = store(tmp_path)
    trip = saved.trips[TRIP]
    original = deepcopy(trip)
    revision = trip.requirements.revision
    assert saved.save_requirements_text(TRIP, '🌍' * 20000, revision)
    assert saved.trips[TRIP].requirements.revision == revision + 1
    before = deepcopy((saved.trips, saved.agent_undo))
    assert saved.save_requirements_text(TRIP, '🌍' * 20000, revision + 1) is False
    assert (saved.trips, saved.agent_undo) == before
    with pytest.raises(ValueError, match='20,000'):
        saved.save_requirements_text(TRIP, '🌍' * 20001, revision + 1)
    assert saved.save_requirements_text(TRIP, '', revision + 1)
    assert saved.trips[TRIP].requirements.text == ''
    saved.trips[TRIP].requirements.text = '文' * 23000
    revision = saved.trips[TRIP].requirements.revision
    assert saved.save_requirements_text(TRIP, '文' * 23000, revision) is False
    assert saved.save_requirements_text(TRIP, '文' * 22000, revision)
    with pytest.raises(ValueError):
        saved.save_requirements_text(TRIP, '文' * 22001, revision + 1)
    fixed = asdict(saved.trips[TRIP].requirements)
    for key, value in asdict(original.requirements).items():
        if key not in {'text', 'revision'}:
            assert fixed[key] == value
    for changes in ({'city': '北京'}, {'lodging_address': '新酒店'}, {'day_count': 3}, {'text': '', 'transport_mode': 'driving'}):
        with pytest.raises(ValueError):
            update_requirements(saved.trips[TRIP], changes)


def test_manual_write_conflict_atomicity_and_inflight_fence(tmp_path, monkeypatch):
    saved = store(tmp_path, True)
    context = running_context(saved, TRIP, '记录夜骑')
    execution = SimpleNamespace(trip=deepcopy(saved.trips[TRIP]), plan=deepcopy(saved.plans[TRIP]))
    payload = prepare_changes({'user_quote': context.body, 'requirements_text': '夜骑'}, context, execution)
    revision = saved.trips[TRIP].requirements.revision
    saved.save_requirements_text(TRIP, '手动正文', revision)
    with pytest.raises(ValueError):
        saved.save_requirements_text(TRIP, '另一标签页草稿', revision)
    assert not saved.commit_turn(context, ExecutionResult(changes=payload)).committed
    assert saved.trips[TRIP].requirements.text == '手动正文'
    before = deepcopy((saved.trips, saved.agent_undo))
    def fail(*args, **kwargs):
        raise OSError('disk failure')
    monkeypatch.setattr('app.application.execution.save_snapshot', fail)
    with pytest.raises(Exception):
        saved.save_requirements_text(TRIP, '不能发布', revision + 1)
    assert (saved.trips, saved.agent_undo) == before


def test_agent_clear_prose_by_presence_and_noop_revision(tmp_path):
    saved = store(tmp_path)
    for text, body in [('', '请清空需求正文'), ('', '保持需求正文不变')]:
        context = running_context(saved, TRIP, body)
        execution = SimpleNamespace(trip=deepcopy(saved.trips[TRIP]), plan=deepcopy(saved.plans[TRIP]))
        previous = saved.trips[TRIP].requirements.revision
        old = saved.trips[TRIP].requirements.text
        payload = prepare_changes({'user_quote': body, 'requirements_text': text}, context, execution)
        assert saved.commit_turn(context, ExecutionResult(changes=payload)).committed
        assert saved.trips[TRIP].requirements.text == ''
        assert saved.trips[TRIP].requirements.revision == previous + (old != text)


@pytest.mark.parametrize('body,text', [
    ('删除想去西湖这一条，保留花生过敏要求', ''),
    ('清空聊天', ''), ('不要清空需求正文', ''),
    ('他说“清空需求正文”', ''), ('如果清空需求正文会怎样', ''),
    ('删除想去西湖这一条，保留花生过敏要求', '  '),
])
def test_agent_cannot_confuse_partial_delete_or_other_target_with_full_clear(tmp_path, body, text):
    saved = store(tmp_path)
    saved.trips[TRIP].requirements.text = '想去西湖\n花生过敏'
    context = running_context(saved, TRIP, body)
    execution = SimpleNamespace(trip=deepcopy(saved.trips[TRIP]), plan=deepcopy(saved.plans[TRIP]))
    with pytest.raises(ValueError):
        prepare_changes({'user_quote': body, 'requirements_text': text}, context, execution)
    assert saved.trips[TRIP].requirements.text == '想去西湖\n花生过敏'


def test_agent_explicit_all_requirements_clear_is_accepted(tmp_path):
    saved = store(tmp_path)
    context = running_context(saved, TRIP, '清空所有需求正文')
    execution = SimpleNamespace(trip=deepcopy(saved.trips[TRIP]), plan=deepcopy(saved.plans[TRIP]))
    payload = prepare_changes({'user_quote': context.body, 'requirements_text': ''}, context, execution)
    assert saved.commit_turn(context, ExecutionResult(changes=payload)).committed
    assert saved.trips[TRIP].requirements.text == ''


def test_api_prose_is_narrow_owned_csrf_and_revision_bound(tmp_path, monkeypatch):
    from app.main import app
    from app.api import dependencies as deps
    saved = store(tmp_path)
    monkeypatch.setattr(deps, 'store', saved)
    owner = saved.trips[TRIP].owner
    monkeypatch.setattr(deps, 'current_user', lambda request: owner)
    path = '/api/trips/' + TRIP + '/requirements-text'
    revision = saved.trips[TRIP].requirements.revision
    with TestClient(app) as client:
        headers = csrf_headers(client)
        payload = {'text': '  夜骑\n预算以实际为准。  ', 'expected_revision': revision}
        assert client.put(path, json=payload).status_code == 403
        assert client.put(path, json=payload | {'lodging_address': '不允许'}, headers=headers).status_code == 422
        result = client.put(path, json=payload, headers=headers)
        assert result.status_code == 200
        assert result.json()['trip']['requirements']['text'] == payload['text']
        assert 'idea_cards' not in result.json() and 'wall_cards' not in result.json()
        assert client.put(path, json=payload, headers=headers).status_code == 409
        assert client.put(path, json={'text': '🌍' * 20001, 'expected_revision': revision + 1}, headers=headers).status_code == 400
        assert client.put('/api/trips/' + TRIP + '/requirements', json={}, headers=headers).status_code == 405
        assert client.post('/api/trips/conversations', json={}, headers=headers).status_code in {404, 405}
        assert client.post('/api/trips/' + TRIP + '/ideas', json={'body': '旧入口'}, headers=headers).status_code == 404
        assert client.put(path.replace(TRIP, 'missing'), json=payload, headers=headers).status_code == 404


def test_parallel_state_application_path_uses_same_prose_transaction(tmp_path):
    from app.storage.state import State
    from app.application import trips
    from app.domain.models import Trip, TripPlan
    state = State(str(tmp_path / 'state.sqlite3'), persistence_enabled=False, display_worker_enabled=False)
    owner = USERS['u1']
    state.trips[TRIP] = Trip(TRIP, '旧旅行', owner, '未生成', '')
    state.messages[TRIP] = []
    state.plans[TRIP] = TripPlan('empty', '', '未生成')
    assert trips.save_requirements_text(state, TRIP, '旧旅行仍可保存正文', 0)
    assert state.trips[TRIP].requirements.text == '旧旅行仍可保存正文'
    assert state.trips[TRIP].requirements.revision == 1
    assert state.trips[TRIP].workflow_state['epoch'] == 1
    assert trips.save_requirements_text(state, TRIP, '旧旅行仍可保存正文', 1) is False
    assert state.trips[TRIP].workflow_state['epoch'] == 1
    with pytest.raises(ValueError):
        trips.save_requirements_text(state, TRIP, '过期草稿', 0)
    with pytest.raises(ValueError):
        trips.create_trip(state, '缺创建资料', owner=owner)


def schema9(saved):
    raw = persistence.serialize_state(saved, USERS)
    raw['schema_version'] = 9
    requirements = raw['trips'][TRIP]['requirements']
    requirements['text'] = '已有正文'
    raw['idea_cards'] = {TRIP: [
        {'id': 'second', 'kind': '地点', 'title': '原序第二', 'body': '🌍' * 21000, 'author': '你', 'status': '已取消'},
        {'id': 'first', 'kind': '禁忌', 'title': '原序第一', 'body': '重复\n重复', 'author': '对话', 'status': '冲突提醒'}]}
    plan = raw['plans'][TRIP]
    plan['requirements_snapshot'] = {key: value for key, value in requirements.items() if key != 'text'}
    plan['requirements_revision'] = requirements['revision']
    raw['agent_undo'] = {TRIP: {'version': 7, 'entries': [{'id': 'old'}], 'receipts': {'consumed': {'immutable': True}}}}
    return raw


@pytest.mark.parametrize('fail', [False, True])
def test_schema9_to_10_lossless_backup_atomic_mirrors_history_and_idempotence(tmp_path, monkeypatch, fail):
    path = tmp_path / 'migration.sqlite3'
    raw = schema9(store(tmp_path))
    persistence.save_snapshot(path, raw)
    historical = {'id': 'historical', 'trip_id': TRIP, 'kind': 'chat', 'status': 'succeeded',
                  'request_id': 'old', 'payload': {'ideas': [{'old': True}]},
                  'receipt': {'old': '原样'}, 'submission_digest': 'untouched'}
    from app.storage.task_repository import write_tasks
    with sqlite3.connect(path) as connection:
        write_tasks(connection, [historical])
        connection.execute('CREATE TABLE ideas(id TEXT, body TEXT)')
        connection.execute("INSERT INTO ideas VALUES ('old', '原始镜像')")
        initial = connection.execute('SELECT accepted_seq,payload FROM execution_tasks').fetchall()
    original_sync = persistence.sync_relational_tables
    def broken(connection, snapshot):
        original_sync(connection, snapshot)
        raise OSError('failure after mirror write')
    if fail:
        with monkeypatch.context() as patch:
            patch.setattr(persistence, 'sync_relational_tables', broken)
            with pytest.raises(OSError):
                DemoStore(str(path), persistence_enabled=True, display_worker_enabled=False)
        assert persistence.load_snapshot(path) == raw
        with sqlite3.connect(path) as connection:
            assert connection.execute('SELECT body FROM ideas').fetchall() == [('原始镜像',)]
            assert connection.execute('SELECT COUNT(*) FROM storage_migrations').fetchone()[0] == 0
    migrated = DemoStore(str(path), persistence_enabled=True, display_worker_enabled=False)
    result = persistence.load_snapshot(path)
    text = migrated.trips[TRIP].requirements.text
    assert text.startswith('已有正文\n\n地点\n原序第二\n')
    assert '🌍' * 21000 in text and text.endswith('禁忌\n原序第一\n重复\n重复\n对话\n冲突提醒')
    assert '已取消' in text and 'idea_cards' not in result and not hasattr(migrated, 'idea_cards')
    assert migrated.agent_undo[TRIP] == {'version': 8, 'entries': [], 'receipts': {'consumed': {'immutable': True}}}
    assert migrated.task_repository.get('historical') == historical
    from app.domain.itinerary.freshness import plan_freshness
    assert plan_freshness(migrated.trips[TRIP], migrated.plans[TRIP])['basis_status'] == 'unknown'
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT accepted_seq,payload FROM execution_tasks').fetchall() == initial
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='ideas'").fetchall() == []
        assert json.loads(connection.execute('SELECT requirements_json FROM trips').fetchone()[0])['text'] == text
    assert all(persistence.load_snapshot(p) == raw for p in (tmp_path / 'backups').glob('*.sqlite3'))
    assert migrate_snapshot(result) == result
    DemoStore(str(path), persistence_enabled=True, display_worker_enabled=False)
    assert persistence.load_snapshot(path) == result


def test_new_undo_cannot_restore_fixed_fields_even_if_snapshot_is_tampered(tmp_path):
    saved = store(tmp_path)
    before = capture(saved, TRIP)
    assert set(before['requirements']) == {'text', 'revision'}
    assert set(before['trip']) == {'status'}
    fixed = deepcopy(saved.trips[TRIP].lodging)
    saved.trips[TRIP].requirements.text = '可撤销正文'
    record_change(saved, TRIP, before, 'message')
    entry = saved.agent_undo[TRIP]['entries'][-1]
    entry['before']['trip']['lodging'] = {'fake': True}
    entry['before']['requirements']['city'] = '伪造城市'
    public = metadata(saved, TRIP)
    undo(saved, TRIP, public['latest_entry_id'], 'undo', public['version'])
    assert saved.trips[TRIP].lodging == fixed
    assert saved.trips[TRIP].requirements.city == ''
