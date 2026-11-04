"""Explicit process-owned business state. No application mixins or import-time instance."""
from copy import deepcopy
from itertools import count
from threading import Condition, Event, RLock, local

from app.core.config import boolean, database_path
from app.domain.models import User
from app.storage.persistence import (
    CURRENT_SCHEMA_VERSION, backup_database, ensure_relational_schema,
    load_snapshot, restore_state, save_snapshot, serialize_state,
)




class State:
    def __init__(self, db_path=None, persistence_enabled=None, *, display_worker_enabled=True):
        self.db_path = db_path or database_path()
        self.persistence_enabled = boolean('TRAVEL_PERSISTENCE') if persistence_enabled is None else persistence_enabled
        self._lock = RLock()
        self.agent_undo = {}
        self.dispatchers = {}
        self._drain_owners = {}
        self._task_context = local()
        self._ids = count(100)
        self.users = {'agent': User('agent', '旅行规划 Agent', 'AI')}
        self.trips, self.messages, self.plans = {}, {}, {}
        self.tasks, self.display_jobs, self._agent_locks = {}, {}, {}
        from app.storage.task_repository import MemoryTaskRepository
        self.task_repository = MemoryTaskRepository() if not self.persistence_enabled else None
        self._foreground_operations = {}
        self._display_changed = Condition(self._lock)
        self._display_wakeup, self._display_stop = Event(), Event()
        self._display_worker = None
        self._display_executing = {}
        self.display_worker_enabled = display_worker_enabled
        if self.persistence_enabled:
            from app.storage.persistence import load_persisted_state
            next_value = load_persisted_state(self, self.users)
            if next_value is not None:
                self._ids = count(next_value)
            from app.application.execution import recover_tasks
            recover_tasks(self)


def next_id(state):
    return next(state._ids)


def _persist(state, *, task_upserts=(), delete_trip_ids=()):
    with state._lock:
        if getattr(state, '_defer_persist', 0):
            return
        if state.persistence_enabled:
            from app.storage.transactions import _users
            users = _users(state)
            save_snapshot(state.db_path, serialize_state(state, users), initialize=False,
                          task_upserts=task_upserts, delete_trip_ids=delete_trip_ids)
        else:
            state.task_repository.apply(task_upserts, delete_trip_ids)
