"""SQLite-backed history, migration and fail-closed transaction contracts."""
from copy import deepcopy
import json
import sqlite3

import pytest

from app.application import execution
from app.application.contracts import ExecutionResult
from app.storage import persistence
from app.storage.errors import TurnPersistenceError
from app.storage.store import DemoStore, USERS
from app.storage.task_repository import SQLiteTaskRepository
from tests.automated.task_fixtures import running_context

TRIP = 'trip-hangzhou'


@pytest.fixture
def store(tmp_path):
    return DemoStore(db_path=str(tmp_path / 'history.sqlite3'), persistence_enabled=True,
                     display_worker_enabled=False)


def reload(store):
    return DemoStore(db_path=store.db_path, persistence_enabled=True, display_worker_enabled=False)


@pytest.mark.parametrize('commit', [lambda s, c, p: s.commit_turn(c, p), execution.commit_turn])
def test_sql_history_capacity_replay_and_order(store, commit, monkeypatch):
    first = None
    for index in range(45):
        context = running_context(store, TRIP, f'消息 {index}')
        proposal = ExecutionResult.failure('离线失败') if index % 3 == 0 else ExecutionResult(answer='离线成功')
        receipt = commit(store, context, proposal)
        assert receipt.committed
        if first is None:
            first = context, proposal, receipt
        assert not store.tasks
    context, proposal, receipt = first
    before = deepcopy(store.messages)
    replay, created = store.submit_task(TRIP, 'chat', {'body': context.body}, context.request_id)
    assert not created and replay['id'] == context.execution_task_id
    assert store.messages == before
    with pytest.raises(execution.TaskConflict):
        store.submit_task(TRIP, 'chat', {'body': '不同正文'}, context.request_id)
    assert len(store.list_tasks(TRIP)) == 20
    assert store.list_tasks(TRIP, context.request_id) == [replay]
    assert store.list_tasks(TRIP, 'missing') == []
    assert 'tasks' not in persistence.load_snapshot(store.db_path)
    assert vars(store.task_repository) == {'db_path': store.db_path}
    restored = reload(store)
    assert restored.tasks == {}
    assert commit(restored, context, proposal) == receipt
    with pytest.raises(ValueError, match='摘要'):
        commit(restored, context, ExecutionResult(answer='不同结果'))
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM execution_tasks').fetchone()[0] == 45
        assert connection.execute('SELECT id FROM execution_tasks ORDER BY accepted_seq LIMIT 1').fetchone()[0] == context.execution_task_id


def test_repository_queries_close_connection_on_success_and_failure(store, monkeypatch):
    from app.storage import task_repository
    connect = sqlite3.connect
    connections = []
    def tracked(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection
    monkeypatch.setattr(task_repository.sqlite3, 'connect', tracked)
    assert store.task_repository.get('missing') is None
    with pytest.raises(sqlite3.OperationalError):
        store.task_repository._query('SELECT payload FROM nonexistent_table')
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            connection.execute('SELECT 1')


def test_progress_empty_write_set_and_callbacks_no_sql(store, monkeypatch):
    context = running_context(store, TRIP, '你好')
    calls = []
    from app.storage import task_repository
    write = task_repository.write_tasks
    def spy(connection, upserts=(), delete_trip_ids=()):
        calls.append(list(upserts))
        return write(connection, calls[-1], delete_trip_ids)
    monkeypatch.setattr(task_repository, 'write_tasks', spy)
    def forbidden(*args):
        pytest.fail('active callback queried task history')
    monkeypatch.setattr(store.task_repository, 'get', forbidden)
    monkeypatch.setattr(store.task_repository, 'find_request', forbidden)
    assert store._execution_current(context)
    store.record_task_progress(context.execution_task_id, '模型请求', {'agent_role': 'main_execution'})
    assert calls == [[]]


@pytest.mark.parametrize('operation', ['submit', 'commit', 'cancel', 'clear', 'delete'])
@pytest.mark.parametrize('boundary', ['tasks', 'projection', 'snapshot'])
def test_sql_faults_roll_back_every_aggregate(store, monkeypatch, operation, boundary):
    context = running_context(store, TRIP, '原始消息')
    if operation == 'delete':
        store.cancel_task(TRIP, context.execution_task_id)
    before = deepcopy((store.trips, store.messages, store.plans, store.trips,
                       store.tasks, store.agent_undo, store.display_jobs))
    snapshot = persistence.load_snapshot(store.db_path)
    historical = store.get_task(context.execution_task_id)
    from app.storage import task_repository
    def fail(*args, **kwargs):
        raise OSError('offline injected storage failure')
    if boundary == 'tasks':
        monkeypatch.setattr(task_repository, 'write_tasks', fail)
    elif boundary == 'projection':
        monkeypatch.setattr(persistence, 'sync_relational_tables', fail)
    else:
        # Fails SQLite's final transaction commit after all task/snapshot DML.
        with sqlite3.connect(store.db_path) as connection:
            connection.execute('CREATE TABLE commit_guard (id INTEGER PRIMARY KEY)')
        connect = sqlite3.connect
        class FaultConnection(sqlite3.Connection):
            def __exit__(self, typ, value, traceback):
                if typ is None:
                    self.rollback()
                    raise OSError('offline final commit failure')
                return super().__exit__(typ, value, traceback)
        monkeypatch.setattr(persistence.sqlite3, 'connect', lambda *a, **kw: connect(*a, **kw, factory=FaultConnection))
    with pytest.raises((TurnPersistenceError, OSError)):
        if operation == 'submit':
            store.submit_task(TRIP, 'chat', {'body': '替代消息'}, 'replacement')
        elif operation == 'commit':
            store.commit_turn(context, ExecutionResult(answer='不应发布'))
        elif operation == 'cancel':
            store.cancel_task(TRIP, context.execution_task_id)
        elif operation == 'clear':
            store.clear_chat(TRIP)
        else:
            store.delete_trip(TRIP)
    monkeypatch.undo()
    assert (store.trips, store.messages, store.plans, store.trips,
            store.tasks, store.agent_undo, store.display_jobs) == before
    assert persistence.load_snapshot(store.db_path) == snapshot
    assert store.get_task(context.execution_task_id) == historical


@pytest.mark.parametrize('operation', ['cancel', 'replace', 'clear', 'delete'])
def test_late_progress_failure_result_cannot_revive(store, operation):
    context = running_context(store, TRIP, '请求')
    if operation in {'cancel', 'delete'}:
        store.cancel_task(TRIP, context.execution_task_id)
        if operation == 'delete':
            store.delete_trip(TRIP)
    elif operation == 'replace':
        store.submit_task(TRIP, 'chat', {'body': '新请求'}, 'replacement')
        assert store._execution_current(context)
        # Follow-up itself is not cancellation; explicit cancellation still fences late results.
        store.cancel_task(TRIP, context.execution_task_id)
    else:
        store.clear_chat(TRIP)
    before = deepcopy((store.trips, store.messages, store.tasks))
    store.record_task_progress(context.execution_task_id, '模型响应', {})
    assert store.fail_execution(context.execution_task_id, ValueError('迟到异常')) is None
    assert store.execute_turn(context.execution_task_id) is None
    assert not store.commit_turn(context, ExecutionResult(answer='迟到结果')).committed
    assert (store.trips, store.messages, store.tasks) == before
    assert context.execution_task_id not in store.tasks
    if operation == 'delete':
        assert store.get_task(context.execution_task_id) is None
        from app.storage.task_repository import write_tasks
        with sqlite3.connect(store.db_path) as connection, pytest.raises(ValueError, match='已删除'):
            write_tasks(connection, [{'id': 'late', 'trip_id': TRIP, 'request_id': 'late', 'status': 'failed'}])


def legacy_database(tmp_path, *, duplicate=False):
    path = str(tmp_path / 'legacy.sqlite3')
    source = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    row, _ = source.submit_task(TRIP, 'chat', {'body': '旧正文'}, 'legacy-request')
    row.update(kind='revision', retry_of='parent', unknown={'retained': True})
    snapshot = persistence.serialize_state(source, USERS)
    snapshot.update(schema_version=6, tasks={row['id']: row, 'orphan': {'id': 'orphan', 'trip_id': 'gone'}})
    if duplicate:
        snapshot['tasks']['duplicate'] = dict(row, id='duplicate')
    persistence.save_snapshot(path, snapshot)
    return path, row, snapshot


def test_v6_backup_marker_unknown_fields_and_interruption(tmp_path, monkeypatch):
    path, row, old = legacy_database(tmp_path)
    monkeypatch.setattr(DemoStore, 'execute_turn', lambda *args: pytest.fail('startup must not run agent'))
    store = DemoStore(db_path=path, persistence_enabled=True, display_worker_enabled=False)
    restored = store.get_task(row['id'])
    assert restored['kind'] == 'revision' and restored['retry_of'] == 'parent'
    assert restored['unknown'] == {'retained': True} and restored['status'] == 'failed'
    assert store.tasks == {} and not any(m.status == 'thinking' for m in store.messages[TRIP])
    assert store.get_task('orphan') is None
    assert 'tasks' not in persistence.load_snapshot(path)
    backups = list((tmp_path / 'backups').glob('*.sqlite3'))
    assert len(backups) == 1 and persistence.load_snapshot(backups[0]) == old
    assert reload(store).get_task(row['id']) == restored
    assert len(list((tmp_path / 'backups').glob('*.sqlite3'))) == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT name FROM storage_migrations ORDER BY name').fetchall() == [('followup_v11',), ('requirements_text_v10',), ('task_history_v7',), ('trip_model_v9',)]


def test_duplicate_migration_rolls_back_and_can_retry(tmp_path):
    path, row, old = legacy_database(tmp_path, duplicate=True)
    with pytest.raises(sqlite3.IntegrityError):
        DemoStore(db_path=path, persistence_enabled=True, display_worker_enabled=False)
    assert persistence.load_snapshot(path) == old
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM execution_tasks').fetchone()[0] == 0
        assert connection.execute('SELECT COUNT(*) FROM storage_migrations').fetchone()[0] == 0
    old['tasks'].pop('duplicate')
    persistence.save_snapshot(path, old)
    store = DemoStore(db_path=path, persistence_enabled=True, display_worker_enabled=False)
    assert store.get_task(row['id'])['status'] == 'failed'


def test_exact_request_api_is_owner_scoped_sql_and_public_dto(store, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    from app.api import dependencies as deps
    from app.domain.models import User
    first = None
    for index in range(22):
        context = running_context(store, TRIP, f'消息 {index}')
        store.commit_turn(context, ExecutionResult(answer='离线回答'))
        first = first or context
    monkeypatch.setattr(deps, 'store', store)
    monkeypatch.setattr(deps, 'current_user', lambda request: store.trips[TRIP].owner)
    with TestClient(main.app) as client:
        response = client.get(f'/api/trips/{TRIP}/tasks', params={'request_id': first.request_id})
        assert response.status_code == 200
        rows = response.json()['tasks']
        assert len(rows) == 1 and rows[0]['id'] == first.execution_task_id
        assert not {'payload', 'receipt', 'submission_digest', 'owner_id'}.intersection(rows[0])
        assert client.get(f'/api/trips/{TRIP}/tasks', params={'request_id': 'missing'}).json() == {'tasks': []}
        monkeypatch.setattr(deps, 'current_user', lambda request: User('other', '其他人', '他'))
        assert client.get(f'/api/trips/{TRIP}/tasks', params={'request_id': first.request_id}).status_code == 404


def test_staging_preserves_other_trip_active_references(store):
    from copy import deepcopy
    from app.domain.models import TripPlan
    other = deepcopy(store.trips[TRIP])
    other.id = 'other-trip'
    store.trips[other.id] = other
    store.messages[other.id] = []
    store.plans[other.id] = TripPlan('empty', '', '未生成')
    first = running_context(store, TRIP, '第一趟')
    second = running_context(store, other.id, '第二趟')
    reference = store.tasks[second.execution_task_id]
    before = deepcopy(reference)
    store.cancel_task(TRIP, first.execution_task_id)
    assert store.tasks[second.execution_task_id] is reference
    assert reference == before
    assert store.get_task(second.execution_task_id) == before


def test_migration_task_dml_failure_preserves_snapshot_and_marker(tmp_path, monkeypatch):
    path, row, old = legacy_database(tmp_path)
    from app.storage import task_repository
    def fail(*args, **kwargs):
        raise OSError('migration disk full')
    with monkeypatch.context() as patch:
        patch.setattr(task_repository, 'write_tasks', fail)
        with pytest.raises(OSError, match='disk full'):
            DemoStore(db_path=path, persistence_enabled=True, display_worker_enabled=False)
    assert persistence.load_snapshot(path) == old
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM storage_migrations').fetchone()[0] == 0
        assert connection.execute('SELECT COUNT(*) FROM execution_tasks').fetchone()[0] == 0
    store = DemoStore(db_path=path, persistence_enabled=True, display_worker_enabled=False)
    assert store.get_task(row['id'])['status'] == 'failed'


def test_migration_refuses_existing_sql_conflict(tmp_path):
    path, row, old = legacy_database(tmp_path)
    different = dict(row, status='succeeded', message='new authoritative state')
    persistence.save_snapshot(path, old, task_upserts=[different])
    with pytest.raises(ValueError, match='冲突'):
        DemoStore(db_path=path, persistence_enabled=True, display_worker_enabled=False)
    assert persistence.load_snapshot(path) == old
    assert SQLiteTaskRepository(path).get(row['id']) == different


def test_state_application_storage_path_uses_same_repository(tmp_path):
    from app.storage.state import State
    from app.application import trips
    state = State(str(tmp_path / 'state.sqlite3'), persistence_enabled=True, display_worker_enabled=False)
    state.users['u1'] = USERS['u1']
    from app.domain.models import Trip, TripPlan
    trip = Trip('legacy-trip', '杭州', state.users['u1'], '未生成', '')
    state.trips[trip.id] = trip
    state.messages[trip.id] = []
    state.plans[trip.id] = TripPlan('empty', '', '未生成')
    task, created = execution.submit_task(state, trip.id, 'chat', {'body': '你好'}, 'state-request')
    assert created
    from tests.automated.task_fixtures import activate_task
    activate_task(state, state.tasks[task['id']]['trip_id'], task['id'])
    from app.storage.state import _persist
    _persist(state, task_upserts=[state.tasks[task['id']]])
    context = execution.capture_request_context(state, trip.id, task['id'])
    receipt = execution.commit_turn(state, context, ExecutionResult(answer='离线回答'))
    assert receipt.committed and not state.tasks
    restored = State(state.db_path, persistence_enabled=True, display_worker_enabled=False)
    assert execution.get_task(restored, task['id'])['status'] == 'succeeded'
    assert execution.commit_turn(restored, context, ExecutionResult(answer='离线回答')) == receipt


def test_missing_snapshot_with_sql_history_refuses_orphan_recovery(store):
    running_context(store, TRIP, '不能脱离快照恢复')
    with sqlite3.connect(store.db_path) as connection:
        connection.execute('DELETE FROM app_snapshots')
    with pytest.raises(ValueError, match='快照缺失'):
        reload(store)


def test_missing_main_fails_closed_but_confirm_remains_legal(store):
    context = store.capture_request_context(TRIP, body='无任务请求')
    assert not store.commit_turn(context, ExecutionResult(answer='不允许')).committed
    assert store.confirm_plan(TRIP).committed
