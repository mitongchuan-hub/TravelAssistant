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

IDEA_WALL_LIMIT = 8
IDEA_ROTATIONS = ['-1.4deg', '1deg', '0.6deg', '-0.8deg', '1.3deg', '0.8deg', '-0.7deg', '1.1deg']


class State:
    def __init__(self, db_path=None, persistence_enabled=None, *, display_worker_enabled=True):
        self.db_path = db_path or database_path()
        self.persistence_enabled = boolean('TRAVEL_PERSISTENCE') if persistence_enabled is None else persistence_enabled
        self._lock = RLock()
        self._task_context = local()
        self._ids = count(100)
        self.users = {'agent': User('agent', '旅行规划 Agent', 'AI')}
        self.trips, self.messages, self.plans, self.idea_cards = {}, {}, {}, {}
        self.tasks, self.display_jobs, self._agent_locks = {}, {}, {}
        self._foreground_operations = {}
        self._display_changed = Condition(self._lock)
        self._display_wakeup, self._display_stop = Event(), Event()
        self._display_worker = None
        self._display_executing = {}
        self.display_worker_enabled = display_worker_enabled
        if self.persistence_enabled:
            snapshot = load_snapshot(self.db_path)
            if snapshot:
                legacy = int(snapshot.get('schema_version', 1)) < CURRENT_SCHEMA_VERSION
                if legacy:
                    backup_database(self.db_path)
                self._ids = count(restore_state(self, snapshot, self.users))
            ensure_relational_schema(self.db_path)
            if snapshot and legacy:
                _persist(self)


def next_id(state):
    return next(state._ids)


def _persist(state):
    with state._lock:
        if state.persistence_enabled and not getattr(state, '_defer_persist', 0):
            save_snapshot(state.db_path, serialize_state(state, state.users), initialize=False)
