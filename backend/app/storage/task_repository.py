"""Explicit task history storage; never a mapping or a terminal-state cache."""
from copy import deepcopy
from contextlib import closing
import json
import sqlite3

ACTIVE = {'queued', 'running'}


def ensure_task_schema(connection):
    # Deliberately no FK to trips: the existing relational projection is rebuilt.
    connection.execute('''CREATE TABLE IF NOT EXISTS execution_tasks (
        accepted_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        id TEXT NOT NULL UNIQUE, trip_id TEXT NOT NULL, request_id TEXT NOT NULL,
        status TEXT NOT NULL, payload TEXT NOT NULL,
        UNIQUE(trip_id, request_id))''')
    connection.execute('CREATE INDEX IF NOT EXISTS idx_execution_tasks_trip_seq ON execution_tasks(trip_id, accepted_seq)')
    connection.execute('CREATE INDEX IF NOT EXISTS idx_execution_tasks_status ON execution_tasks(status)')
    connection.execute('CREATE TABLE IF NOT EXISTS storage_migrations (name TEXT PRIMARY KEY)')


def write_tasks(connection, upserts=(), delete_trip_ids=()):
    for trip_id in delete_trip_ids:
        connection.execute('DELETE FROM execution_tasks WHERE trip_id = ?', (trip_id,))
    for task in upserts:
        if not connection.execute('SELECT 1 FROM trips WHERE id = ?', (task['trip_id'],)).fetchone():
            raise ValueError('不能为已删除旅行保存任务')
        previous = connection.execute('SELECT trip_id, request_id FROM execution_tasks WHERE id = ?', (task['id'],)).fetchone()
        if previous and previous != (task['trip_id'], task['request_id']):
            raise ValueError('任务请求身份不能改变')
        connection.execute('''INSERT INTO execution_tasks(id, trip_id, request_id, status, payload)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
            status=excluded.status, payload=excluded.payload''',
            (task['id'], task['trip_id'], task['request_id'], task['status'], json.dumps(task, ensure_ascii=False)))


class SQLiteTaskRepository:
    def __init__(self, db_path):
        self.db_path = db_path

    def _query(self, sql, args=()):
        with closing(sqlite3.connect(self.db_path)) as connection:
            return [json.loads(row[0]) for row in connection.execute(sql, args)]

    def get(self, task_id):
        rows = self._query('SELECT payload FROM execution_tasks WHERE id = ?', (task_id,))
        return rows[0] if rows else None

    def find_request(self, trip_id, request_id):
        rows = self._query('SELECT payload FROM execution_tasks WHERE trip_id = ? AND request_id = ?', (trip_id, request_id))
        return rows[0] if rows else None

    def recent(self, trip_id, limit=20):
        return list(reversed(self._query('SELECT payload FROM execution_tasks WHERE trip_id = ? ORDER BY accepted_seq DESC LIMIT ?', (trip_id, limit))))

    def active(self):
        return self._query("SELECT payload FROM execution_tasks WHERE status IN ('queued', 'running') ORDER BY accepted_seq")


class MemoryTaskRepository:
    """History only for explicitly nonpersistent stores (including isolated tests)."""
    def __init__(self):
        self._rows = {}

    def get(self, task_id):
        return deepcopy(self._rows.get(task_id))

    def find_request(self, trip_id, request_id):
        return next((deepcopy(row) for row in self._rows.values()
                     if row['trip_id'] == trip_id and row['request_id'] == request_id), None)

    def recent(self, trip_id, limit=20):
        return deepcopy([row for row in self._rows.values() if row['trip_id'] == trip_id][-limit:])

    def active(self):
        return deepcopy([row for row in self._rows.values() if row['status'] in ACTIVE])

    def apply(self, upserts=(), delete_trip_ids=()):
        rows = deepcopy(self._rows)
        for trip_id in delete_trip_ids:
            rows = {key: row for key, row in rows.items() if row['trip_id'] != trip_id}
        for task in upserts:
            previous = rows.get(task['id'])
            if previous and (previous['trip_id'], previous['request_id']) != (task['trip_id'], task['request_id']):
                raise ValueError('任务请求身份不能改变')
            if any(row['id'] != task['id'] and (row['trip_id'], row['request_id']) ==
                   (task['trip_id'], task['request_id']) for row in rows.values()):
                raise ValueError('重复任务请求身份')
            rows[task['id']] = deepcopy(task)
        self._rows = rows


def task_changes(store, stage, trip_id):
    return [task for key, task in stage.tasks.items()
            if task.get('trip_id') == trip_id and task != store.tasks.get(key)]


def publish_tasks(store, stage, trip_id):
    for key in list(store.tasks):
        if store.tasks[key].get('trip_id') == trip_id and (
                key not in stage.tasks or stage.tasks[key]['status'] not in ACTIVE):
            del store.tasks[key]
    for key, task in stage.tasks.items():
        if task.get('trip_id') == trip_id and task['status'] in ACTIVE:
            if key in store.tasks:
                store.tasks[key].clear()
                store.tasks[key].update(deepcopy(task))
            else:
                store.tasks[key] = deepcopy(task)
