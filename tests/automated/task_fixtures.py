"""Explicit task fixtures: active execution and detached historical rows."""
from copy import deepcopy
from uuid import uuid4


def running_context(store, trip_id, body):
    task, _ = store.submit_task(trip_id, 'chat', {'body': body}, uuid4().hex)
    store.tasks[task['id']]['status'] = 'running'
    store._persist(task_upserts=[store.tasks[task['id']]])
    return store.capture_request_context(trip_id, task['id'])


def save_history(store, row):
    row = deepcopy(row)
    store._persist(task_upserts=[row])
    if row['status'] not in {'queued', 'running'}:
        store.tasks.pop(row['id'], None)
    return row
