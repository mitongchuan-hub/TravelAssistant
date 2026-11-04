"""Single-process FIFO drain. SQL state is not the physical execution owner."""
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from app.domain.models import Message
from app.domain.itinerary.identity import plan_basis
from app.application.progress import new_progress, finish_progress
from app.workflow.counters import advance_request, bump_epoch, normalize_workflow


def defaults():
    return dict(paused=False, pause_reason='', revision=0, chat_generation=0)


def metadata(store, trip_id):
    return getattr(store, 'dispatchers', {}).get(trip_id, defaults())


def touch(store, trip_id, **changes):
    row = store.dispatchers.setdefault(trip_id, defaults())
    row.update(changes)
    row['revision'] += 1
    return row


def pause(store, trip_id, reason):
    touch(store, trip_id, paused=True, pause_reason=reason)


def invalidate(store, trip_id, reason='manual_write'):
    """Call only inside the transaction of an effective manual business write."""
    from app.application.execution import now
    store.trips[trip_id].workflow_state = bump_epoch(store.trips[trip_id].workflow_state)
    for task in store.tasks.values():
        if task['trip_id'] == trip_id and task['status'] in {'running', 'queued'}:
            task.update(status='cancelled', message='保存内容已变化，本轮已终止', updated_at=now(), result_url='')
            for row in store.messages[trip_id]:
                if row.id == task.get('thinking_id'):
                    row.status, row.body = 'cancelled', '保存内容已变化，本轮已终止。'
                    row.execution_progress = finish_progress(row.execution_progress, 'superseded', reason='cancelled')
    if trip_id in store._drain_owners or any(
            task['trip_id'] == trip_id and task['status'] in {'waiting', 'queued', 'running', 'cancelled'}
            for task in store.tasks.values()):
        pause(store, trip_id, reason)


def queue_state(store, trip_id):
    rows = store.task_repository.unfinished(trip_id)
    return {**deepcopy(metadata(store, trip_id)),
            'current_task': next((row for row in rows if row['status'] in {'running', 'queued'}), None),
            'waiting_tasks': [row for row in rows if row['status'] == 'waiting'],
            'draining': trip_id in store._drain_owners}


def resume(store, trip_id, expected_revision, chat_generation):
    from app.workflow.tasks import TaskConflict
    with store._lock:
        row = metadata(store, trip_id)
        if row['revision'] != expected_revision or row['chat_generation'] != chat_generation:
            raise TaskConflict('队列已变化，请刷新后基于当前保存内容继续')
        if row['paused']:
            stage = store._stage_turn(trip_id)
            touch(stage, trip_id, paused=False, pause_reason='')
            store._persist_staged_turn(stage, trip_id)
        return queue_state(store, trip_id)


def activate(store, trip_id):
    """Activate exactly one oldest waiting item, publishing its latest baseline atomically."""
    from app.application.execution import now
    rows = store.task_repository.waiting(trip_id)
    if not rows:
        return None
    from app.storage.transactions import _stage_turn, _persist_staged_turn
    stage = _stage_turn(store, trip_id)
    task = stage.tasks[rows[0]['id']]
    trip = stage.trips[trip_id]
    workflow = advance_request(normalize_workflow(trip.workflow_state, trip=trip,
        messages=stage.messages[trip_id], plan=stage.plans[trip_id]))
    trip.workflow_state = workflow
    thinking_id = 'm-' + uuid4().hex
    from app.storage.transactions import _users
    stage.messages[trip_id].append(Message(thinking_id, _users(store)['agent'], 'agent',
        '思考中 •••', datetime.now().strftime('%H:%M'), 'thinking', execution_progress=new_progress('running')))
    for row in stage.messages[trip_id]:
        if row.id == task['message_id']:
            row.status = 'sent'
    task.update(status='running', thinking_id=thinking_id, message='正在处理，请稍候',
        updated_at=now(), plan_id=stage.plans[trip_id].id,
        plan_basis=plan_basis(stage.plans[trip_id]).to_dict(), requirements_revision=trip.requirements.revision,
        workflow_revision=workflow['revision'], sequence=workflow['sequence'], epoch=workflow['epoch'])
    touch(stage, trip_id)
    _persist_staged_turn(store, stage, trip_id)
    return task['id']


def drain(store, trip_id):
    from app.storage.errors import TurnPersistenceError
    token = object()
    with store._lock:
        if trip_id not in store.trips or trip_id in store._drain_owners:
            return None
        store._drain_owners[trip_id] = token
    last = None
    try:
        while True:
            with store._lock:
                if trip_id not in store.trips or metadata(store, trip_id)['paused']:
                    # Release under the decision lock: a concurrent resume/submit
                    # must either observe this owner or claim the now-free slot.
                    store._drain_owners.pop(trip_id, None)
                    break
                # A failed persistence leaves a running record authoritative: never hand off.
                if any(t['trip_id'] == trip_id and t['status'] in {'running', 'queued'} for t in store.tasks.values()):
                    break
                task_id = activate(store, trip_id)
                if task_id is None:
                    store._drain_owners.pop(trip_id, None)
                    break
            store._task_context.task_id = task_id
            try:
                with store._agent_lock(trip_id):
                    last = store.execute_turn(task_id)
            except TurnPersistenceError:
                break
            except Exception as exc:
                try:
                    last = store.fail_execution(task_id, exc)
                except TurnPersistenceError:
                    break
            finally:
                if hasattr(store._task_context, 'task_id'):
                    del store._task_context.task_id
            # execute_turn's own finally has fully unwound before the next activation.
            with store._lock:
                terminal = store.task_repository.get(task_id)
                if not terminal or terminal['status'] not in {'succeeded', 'cancelled', 'failed'}:
                    break
                # cancelled may have been explicitly resumed while physical cleanup ran.
        return last
    except TurnPersistenceError:
        return None
    finally:
        with store._lock:
            if store._drain_owners.get(trip_id) is token:
                del store._drain_owners[trip_id]
        store._notify_display_worker()
