"""Schema-nine migration regressions; all persistence uses temporary databases."""
from copy import deepcopy
from dataclasses import asdict
import json
import sqlite3

import pytest

from app.domain.models import Trip, TripPlan, TripRequirements, User
from app.domain.requirements import update_requirements
from app.domain.summaries import legacy_summaries
from app.domain.itinerary.freshness import plan_freshness
from app.storage.model_migration import REMOVED, migrate_snapshot
from app.storage.persistence import restore_state, serialize_state, save_snapshot, load_snapshot
from app.storage.store import DemoStore, USERS
from app.application.undo import capture, fingerprint, metadata


def trip():
    return Trip('t', '测试', User('u', '用户', '用'), '草案', '', mode='legacy',
                legacy_metadata=legacy_summaries('暑假某周'))


def old_snapshot():
    store = DemoStore(persistence_enabled=False, display_worker_enabled=False, seed_demo=False)
    value = trip()
    store.trips = {'t': value}
    store.messages = {'t': []}
    store.plans = {'t': TripPlan('p', '方案', '草案', text='旧行程', requirements_revision=0,
                                 requirements_snapshot=asdict(value.requirements))}
    raw = serialize_state(store, USERS)
    raw['schema_version'] = 7
    raw['trips']['t'].update(date_range='暑假某周', budget='约几千元', style='随心')
    raw['trips']['t'].pop('legacy_metadata')
    raw['trips']['t']['workflow_state'] = {'sequence': 14, 'epoch': 3, 'revision': 19,
                                         'tasks': [{'id': 'old', 'status': 'waiting_user'}],
                                         'active_question': {'task_id': 'old'}}
    for req in (raw['trips']['t']['requirements'], raw['plans']['t']['requirements_snapshot']):
        req.update({key: '' for key in REMOVED})
    for key in ('requirements_basis_complete', 'requirements_legacy_stale'):
        raw['plans']['t'].pop(key)
    return raw


def restore(raw):
    store = DemoStore(persistence_enabled=False, display_worker_enabled=False, seed_demo=False)
    restore_state(store, raw, {})
    return store


def test_date_only_summary_and_rejected_optional_fields():
    value = trip()
    assert value.date_range == '暑假某周'
    assert not {'note', 'budget', 'style'} & asdict(value).keys()
    assert not hasattr(value, 'budget') and not hasattr(value, 'style')
    with pytest.raises(ValueError):
        update_requirements(value, {'day_count': None})
    for key in ('style', 'date_range', 'note', *REMOVED):
        with pytest.raises(ValueError):
            update_requirements(value, {key: ''})


@pytest.mark.parametrize('case,expected', [('complete', 'unknown'), ('missing_removed', 'unknown'),
                                         ('missing_current', 'unknown'), ('wrong_type', 'stale'),
                                         ('removed_diff', 'stale'), ('removed_type', 'stale')])
def test_migrated_freshness_not_laundered(case, expected):
    raw = old_snapshot()
    basis = raw['plans']['t']['requirements_snapshot']
    if case == 'missing_removed':
        for key in REMOVED:
            basis.pop(key)
    elif case == 'missing_current':
        basis.pop('pace')
    elif case == 'wrong_type':
        basis['revision'] = False
        expected = 'unknown'
    elif case == 'removed_diff':
        basis['arrival_time'] = '18:00'
    elif case == 'removed_type':
        basis['arrival_time'] = None
    store = restore(raw)
    assert plan_freshness(store.trips['t'], store.plans['t'])['basis_status'] == expected
    migrated = asdict(store.plans['t'])
    assert not REMOVED & migrated['requirements_snapshot'].keys()
    assert 'pace' not in migrated['requirements_snapshot']
    assert store.trips['t'].workflow_state == {'sequence': 14, 'epoch': 3, 'revision': 19}
    assert 'workflow_state' not in store.trips['t'].legacy_metadata


@pytest.mark.parametrize('version', [6, 7, 8])
def test_old_undo_is_barrier_not_rebound(version):
    raw = old_snapshot()
    raw['schema_version'] = version
    receipt = {'payload': {'entry_id': 'old', 'expected_version': 2}, 'result': {'done': True}}
    raw['agent_undo'] = {'t': {'version': 3, 'entries': [{'id': 'old', 'before': {'broken': True}}],
                             'receipts': {'consumed': receipt}}}
    store = restore(raw)
    assert not metadata(store, 't')['can_undo']
    assert store.agent_undo['t'] == {'version': 4, 'entries': [], 'receipts': {'consumed': receipt}}
    assert migrate_snapshot(migrate_snapshot(raw)) == migrate_snapshot(raw)


@pytest.mark.parametrize('kind', ['unknown', 'stale'])
@pytest.mark.parametrize('production', [True, False])
def test_text_edit_preserves_migrated_basis_constraints(kind, production):
    raw = old_snapshot()
    basis = raw['plans']['t']['requirements_snapshot']
    if kind == 'unknown':
        for key in REMOVED:
            basis.pop(key)
    else:
        basis['arrival_time'] = '09:00'
    store = restore(raw)
    if production:
        store._apply_text_plan('t', '精确编辑后的正文', preserve_plan_basis=True)
    else:
        from app.application.execution import _apply_text_plan
        from itertools import count
        store._ids = count(100)
        _apply_text_plan(store, 't', '精确编辑后的正文', preserve_plan_basis=True)
    assert plan_freshness(store.trips['t'], store.plans['t'])['basis_status'] == kind


def test_new_three_step_undo_after_model_barrier():
    from app.application.undo import undo, record_change
    store = restore(old_snapshot())
    for title in ('一', '二', '三'):
        before = capture(store, 't')
        store.trips['t'].requirements.text = title
        record_change(store, 't', before, title)
    assert metadata(store, 't')['available_count'] == 3
    for expected in ('二', '一', ''):
        public = metadata(store, 't')
        undo(store, 't', public['latest_entry_id'], expected, public['version'])
        assert store.trips['t'].requirements.text == expected


@pytest.mark.parametrize('version', [6, 7, 8])
@pytest.mark.parametrize('state_class', ['store', 'state'])
def test_migration_preserves_sql_historical_receipt_digest_and_sequence(tmp_path, version, state_class):
    from app.storage.state import State
    from app.storage.task_repository import write_tasks
    raw = old_snapshot()
    raw['schema_version'] = version
    task = {'id': 'historic', 'trip_id': 't', 'request_id': 'request', 'status': 'succeeded',
            'kind': 'chat', 'payload': {'body': '旧消息'}, 'execution_strategy': 'task_execution',
            'conversation_task_id': 'old-conversation', 'submission_digest': 'immutable-old-digest',
            'receipt': {'conversation_task_states': {'old-conversation': 'completed'}, 'unknown': '历史原样'}}
    path = tmp_path / 'history.sqlite3'
    if version == 6:
        raw['tasks'] = {'historic': deepcopy(task)}
    save_snapshot(path, raw)
    if version >= 7:
        with sqlite3.connect(path) as connection:
            write_tasks(connection, [task])
            connection.execute("INSERT INTO storage_migrations(name) VALUES ('task_history_v7')")
            initial = connection.execute('SELECT accepted_seq, payload FROM execution_tasks').fetchall()
    factory = (lambda: DemoStore(db_path=str(path), persistence_enabled=True, display_worker_enabled=False, seed_demo=False)) if state_class == 'store' else (lambda: State(db_path=str(path), persistence_enabled=True, display_worker_enabled=False))
    saved = factory()
    assert saved.task_repository.get('historic') == task
    with sqlite3.connect(path) as connection:
        after = connection.execute('SELECT accepted_seq, payload FROM execution_tasks').fetchall()
    if version >= 7:
        assert after == initial
    else:
        assert after[0][0] == 1 and json.loads(after[0][1]) == task
    factory()
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT accepted_seq, payload FROM execution_tasks').fetchall() == after
    assert load_snapshot(path)['schema_version'] == 10


@pytest.mark.parametrize('basis', [[], None, '', 7, {'unknown': []}])
def test_migration_keeps_wrong_snapshot_types_without_defaults(basis):
    raw = old_snapshot()
    raw['plans']['t']['requirements_snapshot'] = deepcopy(basis)
    store = restore(raw)
    assert store.plans['t'].requirements_snapshot == basis
    assert type(store.plans['t'].requirements_snapshot) is type(basis)
    assert plan_freshness(store.trips['t'], store.plans['t'])['basis_status'] == 'unknown'


def test_v7_migration_failure_rolls_back_snapshot_markers_and_projection(tmp_path, monkeypatch):
    from app.storage import persistence
    path = tmp_path / 'rollback.sqlite3'
    original = old_snapshot()
    save_snapshot(path, original)
    real_sync = persistence.sync_relational_tables
    def fail_after_projection(connection, snapshot):
        real_sync(connection, snapshot)
        raise OSError('injected disk failure')
    with monkeypatch.context() as patch:
        patch.setattr(persistence, 'sync_relational_tables', fail_after_projection)
        with pytest.raises(OSError):
            DemoStore(db_path=str(path), persistence_enabled=True, display_worker_enabled=False, seed_demo=False)
    assert load_snapshot(path) == original
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM storage_migrations').fetchone()[0] == 0
        assert connection.execute('SELECT date_range FROM trips WHERE id = ?', ('t',)).fetchone()[0] == '暑假某周'
    DemoStore(db_path=str(path), persistence_enabled=True, display_worker_enabled=False, seed_demo=False)
    assert load_snapshot(path)['schema_version'] == 10


@pytest.mark.parametrize('operation', ['replace', 'cancel', 'clear', 'manual', 'undo'])
def test_current_counters_preserve_migrated_fences_and_reject_late_turn(operation):
    from app.application.contracts import ExecutionResult
    from app.application.undo import record_change, undo
    store = restore(old_snapshot())
    # The saved legacy mode is deliberate; this test is about request fences.
    if operation == 'undo':
        before = capture(store, 't')
        store.trips['t'].requirements.text = '可撤销'
        record_change(store, 't', before, 'm')
    task, _ = store.submit_task('t', 'chat', {'body': '旧请求'}, 'old-request')
    store.tasks[task['id']]['status'] = 'running'
    context = store.capture_request_context('t', task['id'])
    assert (context.sequence, context.epoch, context.workflow_revision) == (15, 3, 20)
    assert context.conversation_task_id is None
    if operation == 'replace':
        store.submit_task('t', 'chat', {'body': '新请求'}, 'new-request')
    elif operation in {'cancel', 'undo'}:
        store.cancel_task('t', task['id'])
        if operation == 'undo':
            public = metadata(store, 't')
            undo(store, 't', public['latest_entry_id'], 'undo-request', public['version'])
    elif operation == 'clear':
        store.clear_chat('t')
    else:
        store.save_requirements_text('t', '手动改动', 0)
    counters = store.trips['t'].workflow_state
    assert set(counters) == {'revision', 'sequence', 'epoch'}
    assert counters['sequence'] > context.sequence and counters['revision'] > context.workflow_revision
    assert counters['epoch'] >= context.epoch if operation == 'replace' else counters['epoch'] > context.epoch
    receipt = store.commit_turn(context, ExecutionResult(answer='迟到结果'))
    assert not receipt.committed and receipt.conversation_task_states == {}
    assert not any(message.body == '迟到结果' for message in store.messages['t'])


def test_v7_sql_history_is_not_imported_again(tmp_path):
    path = tmp_path / 'state.sqlite3'
    raw = old_snapshot()
    raw['tasks'] = {'malformed': {'id': 'malformed'}}
    save_snapshot(path, raw)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO storage_migrations(name) VALUES ('task_history_v7')")
    store = DemoStore(db_path=str(path), persistence_enabled=True, display_worker_enabled=False, seed_demo=False)
    assert load_snapshot(path)['schema_version'] == 10
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT name FROM storage_migrations ORDER BY name').fetchall() == [('requirements_text_v10',), ('task_history_v7',), ('trip_model_v9',)]
    DemoStore(db_path=str(path), persistence_enabled=True, display_worker_enabled=False, seed_demo=False)
    assert len(list((tmp_path / 'backups').glob('*.sqlite3'))) == 1


@pytest.mark.parametrize('version', [6, 7, 8])
@pytest.mark.parametrize('fail', [False, True])
def test_schema9_physically_drops_optional_columns_atomically(tmp_path, monkeypatch, version, fail):
    from app.storage import persistence
    raw = old_snapshot()
    raw['schema_version'] = version
    if version == 8:
        for target in (raw['trips']['t']['requirements'], raw['plans']['t']['requirements_snapshot']):
            for key in {'arrival_time', 'departure_time', 'arrival_location', 'departure_location'}:
                target.pop(key)
    raw['trips']['t']['legacy_metadata'] = {'requirements': {'pace': '慢'}, 'summaries': {'budget': '100'}}
    path = tmp_path / 'physical.sqlite3'
    save_snapshot(path, raw)
    with sqlite3.connect(path) as connection:
        for key in ('budget', 'style', 'note'):
            connection.execute(f"ALTER TABLE trips ADD COLUMN {key} TEXT NOT NULL DEFAULT 'old'")
        connection.execute("UPDATE trips SET created_at='2001-02-03 04:05:06'")
        connection.execute("INSERT INTO auth_accounts(email,user_id,password_hash) VALUES ('a@b.c','u','hash')")
    original_sync = persistence.sync_relational_tables
    if fail:
        def broken(connection, snapshot):
            original_sync(connection, snapshot)
            raise OSError('injected after destructive DDL')
        with monkeypatch.context() as patch:
            patch.setattr(persistence, 'sync_relational_tables', broken)
            with pytest.raises(OSError):
                DemoStore(db_path=str(path), persistence_enabled=True, display_worker_enabled=False, seed_demo=False)
        assert load_snapshot(path) == raw
        with sqlite3.connect(path) as connection:
            assert {'budget', 'style', 'note'} <= {r[1] for r in connection.execute('PRAGMA table_info(trips)')}
            assert connection.execute('SELECT COUNT(*) FROM storage_migrations').fetchone()[0] == 0
    DemoStore(db_path=str(path), persistence_enabled=True, display_worker_enabled=False, seed_demo=False)
    with sqlite3.connect(path) as connection:
        assert not {'budget', 'style', 'note'} & {r[1] for r in connection.execute('PRAGMA table_info(trips)')}
        assert connection.execute('SELECT created_at FROM trips').fetchone()[0] == '2001-02-03 04:05:06'
        assert connection.execute('SELECT password_hash FROM auth_accounts').fetchone()[0] == 'hash'
    saved = load_snapshot(path)
    assert saved['schema_version'] == 10
    assert saved['trips']['t']['legacy_metadata'] == {'summaries': {'date_range': '暑假某周'}, 'fallback': {'date_range': True}}
    assert saved['plans']['t']['text'] == '旧行程'
    backups = list((tmp_path / 'backups').glob('*.sqlite3'))
    assert backups and all(load_snapshot(backup) == raw for backup in backups)


@pytest.mark.parametrize('basis', [None, [], '', 7])
@pytest.mark.parametrize('ready', [True, False])
def test_malformed_old_basis_remains_unknown_but_workspace_and_undo_work(basis, ready, monkeypatch):
    from app.api import dependencies as deps
    from app.main import app
    from fastapi.testclient import TestClient
    from app.services.itinerary.plan_display import dates_from_snapshot, text_hash
    from app.services.itinerary.guide_html import build_guide_display
    raw = old_snapshot()
    plan = raw['plans']['t']
    plan['requirements_snapshot'] = deepcopy(basis)
    if ready:
        plan.update(display=build_guide_display('<!doctype html><html><head><title>旧攻略</title></head><body><main><h1>旧攻略</h1><p>原始正文和日期未知的合法攻略保留。</p></main></body></html>', []),
                    display_status='ready', display_source_hash=text_hash(plan['text']))
    store = restore(raw)
    owner = store.trips['t'].owner
    monkeypatch.setattr(deps, 'store', store)
    monkeypatch.setattr(deps, 'current_user', lambda request: owner)
    assert dates_from_snapshot(basis) is None
    with TestClient(app) as client:
        response = client.get('/api/trips/t/workspace')
        assert response.status_code == 200
        assert response.json()['display_state']['basis_status'] == 'unknown'
    captured = capture(store, 't')
    assert captured['plan']['requirements_snapshot'] == basis
    assert store.plans['t'].requirements_snapshot == basis
    assert plan_freshness(store.trips['t'], store.plans['t'])['basis_status'] == 'unknown'
    from app.application.undo import record_change, undo
    store.trips['t'].requirements.text = '新的正文'
    record_change(store, 't', captured, 'changed')
    public = metadata(store, 't')
    undo(store, 't', public['latest_entry_id'], 'restore-malformed', public['version'])
    assert store.plans['t'].requirements_snapshot == basis
    assert plan_freshness(store.trips['t'], store.plans['t'])['basis_status'] == 'unknown'
    if ready:
        assert store.plans['t'].display_status == 'ready'
        assert store.plans['t'].display == plan['display']



def test_consumed_old_undo_receipt_stays_idempotent_after_barrier():
    from app.application.undo import undo
    raw = old_snapshot()
    result = {'entry_id': 'old', 'done': True}
    raw['agent_undo'] = {'t': {'version': 5, 'entries': [{'id': 'old'}], 'receipts': {
        'consumed': {'payload': {'entry_id': 'old', 'expected_version': 4}, 'result': result}}}}
    store = restore(raw)
    before = deepcopy(store.trips['t'])
    assert undo(store, 't', 'old', 'consumed', 4) == result
    assert store.trips['t'] == before and store.agent_undo['t']['version'] == 6
