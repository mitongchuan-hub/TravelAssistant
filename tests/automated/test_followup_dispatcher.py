"""Follow-up uses the production dispatcher, with physical execution barriers."""
from threading import Event, Thread

from app.storage.store import DemoStore
from app.application.contracts import ExecutionResult


def test_acceptance_does_not_advance_or_freeze_and_has_no_thinking():
    store = DemoStore(persistence_enabled=False)
    trip_id = 'trip-hangzhou'
    before = dict(store.trips[trip_id].workflow_state)
    task, _ = store.submit_task(trip_id, 'chat', {'body': 'A'}, 'A')
    assert task['status'] == 'waiting'
    assert store.trips[trip_id].workflow_state == before
    assert not task.get('thinking_id')
    assert 'plan_basis' not in task


def test_fifo_owner_survives_commit_until_execute_returns(monkeypatch):
    store = DemoStore(persistence_enabled=False)
    from tests.automated.test_agent_crud_backend import ready_for_plan
    ready_for_plan(store)
    trip_id = 'trip-hangzhou'
    entered, release = Event(), Event()
    seen, baselines = [], []

    def generate(context, execution, **kwargs):
        seen.append(('A' if context.body == 'A请规划行程' else context.body, [m.body for m in execution.messages]))
        baselines.append((execution.trip.requirements.text, execution.plan.text))
        if context.body == 'A请规划行程':
            entered.set()
            assert release.wait(5)
            from app.domain.changes import prepare_changes
            changes = prepare_changes({'user_quote': context.body,
                'requirements_text': 'A最新保存需求',
                'itinerary': {'action': 'replace', 'text': '# A最新保存行程\n\n西湖慢游'}}, context, execution)
            return ExecutionResult(answer=context.body, changes=changes)
        return ExecutionResult(answer=context.body)

    monkeypatch.setattr('app.agents.entrypoints.generate_task_result', generate)
    a, _ = store.submit_task(trip_id, 'chat', {'body': 'A请规划行程'}, 'A')
    worker = Thread(target=store.run_task, args=(a['id'],))
    worker.start()
    assert entered.wait(5)
    b, _ = store.submit_task(trip_id, 'chat', {'body': 'B'}, 'B')
    c, _ = store.submit_task(trip_id, 'chat', {'body': 'C'}, 'C')
    assert store.tasks[a['id']]['status'] == 'running'
    assert store.run_task(b['id']) is None
    assert [row['id'] for row in store.get_queue(trip_id)['waiting_tasks']] == [b['id'], c['id']]
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert [body for body, _ in seen] == ['A', 'B', 'C'], store.get_task(a['id']).get('message')
    assert 'B' not in seen[0][1] and 'C' not in seen[0][1]
    assert 'C' not in seen[1][1]
    assert baselines[1] == ('A最新保存需求', '# A最新保存行程\n\n西湖慢游')


def test_cancel_waiting_and_generation_and_capacity():
    import pytest
    from app.workflow.tasks import TaskConflict
    store = DemoStore(persistence_enabled=False)
    tid = 'trip-hangzhou'
    tasks = [store.submit_task(tid, 'chat', {'body': str(i)}, str(i))[0] for i in range(50)]
    assert len(store.get_queue(tid)['waiting_tasks']) == 50
    assert store.submit_task(tid, 'chat', {'body': '0'}, '0')[1] is False
    with pytest.raises(TaskConflict):
        store.submit_task(tid, 'chat', {'body': 'full'}, 'full')
    store.cancel_task(tid, tasks[0]['id'])
    assert not store.get_queue(tid)['paused']
    store.clear_chat(tid)
    assert store.submit_task(tid, 'chat', {'body': '0'}, '0')[1] is False
    with pytest.raises(TaskConflict):
        store.submit_task(tid, 'chat', {'body': 'late'}, 'late')
    task, _ = store.submit_task(tid, 'chat', {'body': 'new'}, 'new', chat_generation=1)
    assert task['status'] == 'waiting'


def test_committed_owner_blocks_next_and_html_until_cleanup(monkeypatch):
    store = DemoStore(persistence_enabled=False)
    tid = 'trip-hangzhou'
    committed, release = Event(), Event()
    original = store.execute_turn
    seen = []
    monkeypatch.setattr('app.agents.entrypoints.generate_task_result',
        lambda context, execution, **kw: (seen.append(context.body) or ExecutionResult(answer=context.body)))
    a, _ = store.submit_task(tid, 'chat', {'body': 'A'}, 'A')
    def execute(task_id):
        result = original(task_id)
        if task_id == a['id']:
            committed.set()
            assert release.wait(5)
        return result
    monkeypatch.setattr(store, 'execute_turn', execute)
    worker = Thread(target=store.run_task, args=(a['id'],))
    worker.start()
    assert committed.wait(5)
    b, _ = store.submit_task(tid, 'chat', {'body': 'B'}, 'B')
    queue = store.get_queue(tid)
    assert queue['current_task'] is None and queue['draining']
    assert store._foreground_active(tid)
    store.run_task(b['id'])
    assert seen == ['A']
    release.set()
    worker.join(5)
    assert not worker.is_alive() and seen == ['A', 'B']


def test_cancel_resume_waits_for_physical_owner_and_excludes_cancelled_waiting(monkeypatch):
    store = DemoStore(persistence_enabled=False)
    tid = 'trip-hangzhou'
    entered, release = Event(), Event()
    seen = []
    def generate(context, execution, **kw):
        seen.append((context.body, [row.body for row in execution.messages]))
        if context.body == 'A':
            entered.set()
            assert release.wait(5)
        return ExecutionResult(answer='answer '+context.body)
    monkeypatch.setattr('app.agents.entrypoints.generate_task_result', generate)
    a, _ = store.submit_task(tid, 'chat', {'body': 'A'}, 'A')
    worker = Thread(target=store.run_task, args=(a['id'],))
    worker.start()
    assert entered.wait(5)
    b, _ = store.submit_task(tid, 'chat', {'body': 'B'}, 'B')
    c, _ = store.submit_task(tid, 'chat', {'body': 'C'}, 'C')
    before = dict(store.trips[tid].workflow_state)
    store.cancel_task(tid, b['id'])
    assert store.trips[tid].workflow_state == before and not store.get_queue(tid)['paused']
    store.cancel_task(tid, a['id'])
    queue = store.get_queue(tid)
    assert queue['paused'] and queue['pause_reason'] == 'cancelled'
    store.resume_queue(tid, queue['revision'], queue['chat_generation'])
    store.run_task(c['id'])
    assert len(seen) == 1
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert [body for body, _ in seen] == ['A', 'C']
    assert 'B' not in seen[1][1]
    assert all(row.body != 'answer A' for row in store.messages[tid])


def test_failure_pauses_backlog_resume_cas_and_no_replay(monkeypatch):
    import pytest
    from app.workflow.tasks import TaskConflict
    store = DemoStore(persistence_enabled=False)
    tid = 'trip-hangzhou'
    seen = []
    def generate(context, execution, **kw):
        seen.append(context.body)
        return ExecutionResult.failure('offline failed') if context.body == 'A' else ExecutionResult(answer='ok')
    monkeypatch.setattr('app.agents.entrypoints.generate_task_result', generate)
    a, _ = store.submit_task(tid, 'chat', {'body': 'A'}, 'A')
    store.submit_task(tid, 'chat', {'body': 'B'}, 'B')
    store.run_task(a['id'])
    queue = store.get_queue(tid)
    assert seen == ['A'] and queue['paused'] and queue['pause_reason'] == 'failed'
    assert not store._foreground_active(tid)  # paused waiting cannot starve HTML
    store.submit_task(tid, 'chat', {'body': 'C'}, 'C')
    with pytest.raises(TaskConflict):
        store.resume_queue(tid, queue['revision'], queue['chat_generation'])
    queue = store.get_queue(tid)
    store.resume_queue(tid, queue['revision'], queue['chat_generation'])
    store.drain_queue(tid)
    assert seen == ['A', 'B', 'C']


def test_waiting_restart_keeps_all_fifo_without_model_calls(tmp_path, monkeypatch):
    from app.storage.persistence import load_snapshot
    tid = 'trip-hangzhou'
    path = str(tmp_path / 'queue.sqlite3')
    store = DemoStore(db_path=path, persistence_enabled=True, display_worker_enabled=False)
    tasks = [store.submit_task(tid, 'chat', {'body': str(i)}, str(i))[0] for i in range(25)]
    assert len(store.list_tasks(tid)) == 20
    monkeypatch.setattr('app.agents.entrypoints.generate_task_result', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('restart ran model')))
    restored = DemoStore(db_path=path, persistence_enabled=True, display_worker_enabled=False)
    queue = restored.get_queue(tid)
    assert queue['paused'] and queue['pause_reason'] == 'restart'
    assert [row['id'] for row in queue['waiting_tasks']] == [row['id'] for row in tasks]
    assert load_snapshot(path)['dispatchers'][tid]['paused']


def test_effective_manual_write_pauses_but_noop_does_not():
    store = DemoStore(persistence_enabled=False)
    tid = 'trip-hangzhou'
    store.submit_task(tid, 'chat', {'body': 'B'}, 'B')
    before = store.get_queue(tid)
    trip = store.trips[tid]
    store.save_requirements_text(tid, trip.requirements.text, trip.requirements.revision)
    assert store.get_queue(tid) == before
    store.save_requirements_text(tid, '新的想法', trip.requirements.revision)
    assert store.get_queue(tid)['paused']
    assert store.get_queue(tid)['pause_reason'] == 'manual_write'


import pytest
@pytest.mark.parametrize('operation', ['accept', 'activate', 'resume', 'complete'])
def test_queue_transactions_fail_closed(tmp_path, monkeypatch, operation):
    from copy import deepcopy
    from app.storage import transactions
    from app.storage.errors import TurnPersistenceError
    from tests.automated.task_fixtures import activate_task
    from app.workflow.dispatcher import pause, activate
    store = DemoStore(persistence_enabled=False)
    tid = 'trip-hangzhou'
    a, _ = store.submit_task(tid, 'chat', {'body': 'A'}, 'A')
    if operation == 'complete':
        activate_task(store, tid, a['id'])
        context = store.capture_request_context(tid, a['id'])
    if operation == 'resume':
        with store._lock:
            stage = store._stage_turn(tid)
            pause(stage, tid, 'failed')
            store._persist_staged_turn(stage, tid)
    before = deepcopy((store.trips, store.messages, store.tasks, store.dispatchers))
    history = store.task_repository.get(a['id'])
    def fail(*a, **kw):
        raise OSError('injected transaction failure')
    monkeypatch.setattr(store.task_repository, 'apply', fail)
    with pytest.raises(TurnPersistenceError):
        if operation == 'accept':
            store.submit_task(tid, 'chat', {'body': 'B'}, 'B')
        elif operation == 'activate':
            activate(store, tid)
        elif operation == 'complete':
            store.commit_turn(context, ExecutionResult.failure('failed'))
        else:
            queue = store.get_queue(tid)
            store.resume_queue(tid, queue['revision'], queue['chat_generation'])
    assert (store.trips, store.messages, store.tasks, store.dispatchers) == before
    assert store.task_repository.get(a['id']) == history


def test_empty_manual_write_does_not_pause_future_first_message(monkeypatch):
    store = DemoStore(persistence_enabled=False)
    tid = 'trip-hangzhou'
    store.save_requirements_text(tid, 'new manual text', store.trips[tid].requirements.revision)
    assert not store.get_queue(tid)['paused']
    monkeypatch.setattr('app.agents.entrypoints.generate_task_result',
                        lambda *a, **kw: ExecutionResult(answer='first reply'))
    task, _ = store.submit_task(tid, 'chat', {'body': 'first'}, 'first')
    store.run_task(task['id'])
    assert store.get_task(task['id'])['status'] == 'succeeded'


@pytest.mark.parametrize('fail', [False, True])
def test_v10_with_existing_markers_upgrades_atomically_without_rewriting_history(tmp_path, monkeypatch, fail):
    import sqlite3
    import json
    from app.storage import persistence
    tid = 'trip-hangzhou'
    path = str(tmp_path / 'migration.sqlite3')
    store = DemoStore(db_path=path, persistence_enabled=True, display_worker_enabled=False)
    task, _ = store.submit_task(tid, 'chat', {'body': 'historical'}, 'historical')
    store.cancel_task(tid, task['id'])
    raw = persistence.load_snapshot(path)
    raw['schema_version'] = 10
    raw.pop('dispatchers', None)
    persistence.save_snapshot(path, raw, initialize=False,
        migration=['trip_model_v9', 'requirements_text_v10'])
    with sqlite3.connect(path) as conn:
        history = conn.execute('SELECT accepted_seq,payload FROM execution_tasks').fetchall()
    if fail:
        original = persistence.sync_relational_tables
        monkeypatch.setattr(persistence, 'sync_relational_tables',
            lambda *a, **kw: (_ for _ in ()).throw(OSError('migration fail')))
        with pytest.raises(OSError):
            DemoStore(db_path=path, persistence_enabled=True, display_worker_enabled=False)
        assert persistence.load_snapshot(path) == raw
        with sqlite3.connect(path) as conn:
            assert not conn.execute("SELECT 1 FROM storage_migrations WHERE name='followup_v11'").fetchone()
        monkeypatch.setattr(persistence, 'sync_relational_tables', original)
    restored = DemoStore(db_path=path, persistence_enabled=True, display_worker_enabled=False)
    assert persistence.load_snapshot(path)['schema_version'] == 11
    assert restored.get_queue(tid)['chat_generation'] == 0
    DemoStore(db_path=path, persistence_enabled=True, display_worker_enabled=False)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT accepted_seq,payload FROM execution_tasks').fetchall() == history
        assert conn.execute("SELECT COUNT(*) FROM storage_migrations WHERE name='followup_v11'").fetchone()[0] == 1


def test_clear_during_owner_prevents_late_reply_and_delete_until_exit(monkeypatch):
    store = DemoStore(persistence_enabled=False)
    tid = 'trip-hangzhou'
    entered, release = Event(), Event()
    seen = []
    def generate(context, execution, **kw):
        seen.append(context.body)
        entered.set()
        assert release.wait(5)
        return ExecutionResult(answer='must never publish')
    monkeypatch.setattr('app.agents.entrypoints.generate_task_result', generate)
    a, _ = store.submit_task(tid, 'chat', {'body': 'A'}, 'A')
    worker = Thread(target=store.run_task, args=(a['id'],))
    worker.start()
    assert entered.wait(5)
    store.submit_task(tid, 'chat', {'body': 'B'}, 'B')
    store.clear_chat(tid)
    assert store.get_queue(tid)['waiting_tasks'] == []
    with pytest.raises(ValueError):
        store.delete_trip(tid)
    release.set()
    worker.join(5)
    assert not worker.is_alive() and store.messages[tid] == [] and seen == ['A']
    assert store.delete_trip(tid)
    assert tid not in store.dispatchers and store.get_task(a['id']) is None
