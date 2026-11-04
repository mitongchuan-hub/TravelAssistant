"""Offline contracts for execution assembly and the lock-bound workflow wiring."""
from copy import deepcopy
from dataclasses import fields
from datetime import date
from types import SimpleNamespace

import pytest

from app.storage.store import DemoStore
from app.application.contracts import ExecutionResult


TRIP = 'trip-hangzhou'


def test_execution_assembly_is_detached_and_preserves_metadata():
    from app.agents.inputs.execution import TaskExecutionContext, build_execution_context
    trip = SimpleNamespace(requirements={'revision': 3})
    plan = SimpleNamespace(text='old plan', display={'days': []})
    sent = SimpleNamespace(id='sent', status='sent', body='hello')
    messages = [sent, SimpleNamespace(id='pending', status='thinking'),
                SimpleNamespace(id='failed', status='failed')]
    ideas, metrics = [{'body': ['idea']}], {'nested': {'count': 2}}
    payload = {'item_id': 'item-old'}
    target = {'title': 'saved title', 'day_index': 0, 'item_index': 0}
    execution = build_execution_context(trip=trip, plan=plan, messages=messages,
         metrics=metrics, current_date='2030-01-02',
        entry='main', payload=payload, display_target=target)
    assert [f.name for f in fields(TaskExecutionContext)] == [
        'trip', 'messages', 'plan', 'metrics', 'current_date',
        'entry', 'scope', 'target_item_id', 'display_target']
    assert [m.id for m in execution.messages] == ['sent']
    assert (execution.current_date, execution.entry, execution.scope, execution.target_item_id) == (
        '2030-01-02', 'main', 'item', 'item-old')
    assert execution.display_target == target
    execution.trip.requirements['revision'] = 9
    execution.plan.display['days'].append('changed')
    execution.messages[0].body = 'changed'
    execution.metrics['nested']['count'] = 9
    execution.display_target['title'] = 'changed'
    assert trip.requirements == {'revision': 3}
    assert plan.display == {'days': []}
    assert sent.body == 'hello' and ideas == [{'body': ['idea']}]
    assert metrics == {'nested': {'count': 2}} and target['title'] == 'saved title'
    payload['item_id'] = 'new'
    assert execution.target_item_id == 'item-old'


@pytest.mark.parametrize('payload,target,scope,item', [
    ({}, None, '', None), ({'item_id': ''}, None, '', ''),
    ({'item_id': 'old'}, None, 'item', 'old'),
    ({}, {'title': 'display'}, 'item', None),
])
def test_execution_assembly_scope_matches_legacy(payload, target, scope, item):
    from app.agents.inputs.execution import build_execution_context
    result = build_execution_context(trip=None, plan=None, messages=[],
        metrics={}, current_date='2030-01-02', entry='main', payload=payload,
        display_target=target)
    assert (result.scope, result.target_item_id) == (scope, item)


@pytest.mark.parametrize('kwargs,error', [
    ({'entry': 'invalid'}, '未知任务执行入口'),
    ({'scope': 'invalid'}, '任务执行范围无效'),
    ({'entry': 'confirm', 'display_target': {}}, '展示目标只能用于聊天入口'),
])
def test_context_keeps_validation_and_positional_order(kwargs, error):
    from app.agents.inputs.execution import TaskExecutionContext
    with pytest.raises(ValueError, match=error):
        TaskExecutionContext(None, [],  None, {}, '2030-01-02', **kwargs)


@pytest.fixture
def store(tmp_path):
    return DemoStore(db_path=str(tmp_path / 'inputs.sqlite3'), persistence_enabled=False,
                     display_worker_enabled=False)


@pytest.mark.parametrize('kind,payload', [
    ('chat', {'body': '你好'}),
])
def test_execute_turn_assembles_under_lock_and_freezes_target(store, monkeypatch, kind, payload):
    from app.agents.inputs import execution as inputs
    from app.agents import entrypoints
    from app.workflow import tasks
    task, _ = store.submit_task(TRIP, kind, payload, 'assembly-' + kind)
    task = store.tasks[task['id']]
    from tests.automated.task_fixtures import activate_task
    activate_task(store, task['trip_id'], task['id'])
    original = inputs.build_execution_context
    captured = {}
    target = {'title': 'server target', 'day_index': 0, 'item_index': 0}

    def resolve(plan, raw):
        assert store._lock._is_owned()
        assert plan == store.plans[TRIP]
        assert raw == payload
        captured['resolved'] = True
        return None

    def assemble(**kwargs):
        assert store._lock._is_owned()
        captured['kwargs'] = deepcopy(kwargs)
        result = original(**kwargs)
        # Simulate another writer immediately after assembly. Model input must
        # not take target metadata from the mutable task after leaving the lock.
        task['payload']['item_id'] = 'new-target'
        target['title'] = 'changed later'
        return result

    def generate(context, execution, *, cancelled, on_progress):
        assert not store._lock._is_owned()
        assert execution.current_date == date.today().isoformat()
        assert execution.entry == context.entry
        assert all(m.status == 'sent' for m in execution.messages)
        assert execution.target_item_id == payload.get('item_id')
        assert execution.scope == ('item' if kind == 'revision' else '')
        assert execution.display_target == (dict(title='server target', day_index=0, item_index=0)
                                            if kind == 'revision' else None)
        assert not cancelled()
        on_progress('offline', {'status': 'running'})
        return ExecutionResult(answer='离线回复')

    monkeypatch.setattr(tasks, 'resolve_display_target', resolve)
    monkeypatch.setattr(inputs, 'build_execution_context', assemble)
    monkeypatch.setattr(entrypoints, 'generate_task_result', generate)
    monkeypatch.setattr(store, 'record_task_progress', lambda *args: captured.setdefault('progress', args))
    receipt = store.execute_turn(task['id'])
    assert receipt.committed and store.get_task(task['id'])['status'] == 'succeeded'
    assert captured.get('resolved', False)
    assert 'progress' in captured


def test_execute_turn_conflict_does_not_call_agent(store, monkeypatch):
    from app.agents import entrypoints
    task, _ = store.submit_task(TRIP, 'chat', {'body': '旧请求'}, 'stale-assembly')
    task = store.tasks[task['id']]
    from tests.automated.task_fixtures import activate_task
    activate_task(store, task['trip_id'], task['id'])
    store.trips[TRIP].requirements.revision += 1
    monkeypatch.setattr(entrypoints, 'generate_task_result', lambda *a, **kw: pytest.fail('stale agent call'))
    receipt = store.execute_turn(task['id'])
    assert not receipt.committed and receipt.task_status == 'failed'


def test_execute_turn_cancelled_during_agent_does_not_save_answer(store, monkeypatch):
    from app.agents import entrypoints
    task, _ = store.submit_task(TRIP, 'chat', {'body': '取消请求'}, 'cancelled-assembly')
    task = store.tasks[task['id']]
    from tests.automated.task_fixtures import activate_task
    activate_task(store, task['trip_id'], task['id'])

    def generate(context, execution, *, cancelled, on_progress):
        assert not cancelled()
        task['status'] = 'cancelled'
        assert cancelled()
        return ExecutionResult(answer='must not save')

    monkeypatch.setattr(entrypoints, 'generate_task_result', generate)
    receipt = store.execute_turn(task['id'])
    assert not receipt.committed
    assert not any(m.body == 'must not save' for m in store.messages[TRIP])
