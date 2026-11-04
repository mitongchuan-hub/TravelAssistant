from __future__ import annotations
import json
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime
from threading import RLock
from contextlib import contextmanager
from uuid import uuid4
from app.domain.models import AgentAction, AgentCard, Message, Trip, TripPlan, User
from app.domain.requirements import update_requirements
from app.storage.state import next_id
from copy import copy, deepcopy
from app.storage.persistence import save_snapshot, serialize_state

def _stage_turn(self, trip_id):
    """Reuse the normal builders against private aggregates, never live objects."""
    stage = copy(self)
    stage.agent_undo = deepcopy(self.agent_undo)
    stage.dispatchers = deepcopy(self.dispatchers)
    stage.trips = dict(self.trips)
    stage.plans = dict(self.plans)
    stage.messages = dict(self.messages)
    for name in ('trips', 'plans', 'messages'):
        getattr(stage, name)[trip_id] = deepcopy(getattr(self, name)[trip_id])
    stage.tasks = {key: deepcopy(task) for key, task in self.tasks.items()
                   if task.get('trip_id') == trip_id}
    stage.display_jobs = deepcopy(self.display_jobs)
    stage._display_changed = _StagingSignal()
    stage._defer_persist = 1
    stage.persistence_enabled = False
    return stage

def _publish_turn(self, stage, trip_id):
    for name in ('trips', 'plans', 'messages'):
        getattr(self, name)[trip_id] = getattr(stage, name)[trip_id]
    from app.storage.task_repository import publish_tasks
    publish_tasks(self, stage, trip_id)
    self.agent_undo = stage.agent_undo
    self.dispatchers = stage.dispatchers
    self.display_jobs = stage.display_jobs
    self._display_changed.notify_all()

def _persist_staged_turn(self, stage, trip_id):
    from app.storage.task_repository import task_changes
    upserts = task_changes(self, stage, trip_id)
    try:
        if self.persistence_enabled:
            save_snapshot(self.db_path, serialize_state(stage, _users(self)), initialize=False, task_upserts=upserts)
        else:
            self.task_repository.apply(upserts)
    except Exception as exc:
        raise TurnPersistenceError('最终提交未成功，当前需求、行程与任务状态未改变。') from exc
    _publish_turn(self, stage, trip_id)

from app.storage.errors import TurnPersistenceError

class _StagingSignal:

    def notify_all(self):
        pass


def delete_trip_atomically(store, trip_id):
    with store._lock:
        if trip_id in store._drain_owners or any(task.get('trip_id') == trip_id and task.get('status') in {'waiting', 'queued', 'running'}
               for task in store.tasks.values()):
            raise ValueError('这趟旅行还有任务正在进行，请稍后再删除')
        if trip_id not in store.trips:
            return False
        stage = _stage_turn(store, trip_id)
        for name in ('trips', 'plans', 'messages', 'agent_undo', 'dispatchers'):
            getattr(stage, name).pop(trip_id, None)
        stage.display_jobs = {key: job for key, job in stage.display_jobs.items() if job.get('trip_id') != trip_id}
        stage.tasks = {}
        if store.persistence_enabled:
            save_snapshot(store.db_path, serialize_state(stage, _users(store)), initialize=False,
                          delete_trip_ids=[trip_id])
        else:
            store.task_repository.apply(delete_trip_ids=[trip_id])
        for name in ('trips', 'plans', 'messages', 'agent_undo', 'dispatchers'):
            getattr(store, name).pop(trip_id, None)
        store.display_jobs = stage.display_jobs
        from app.storage.task_repository import publish_tasks
        publish_tasks(store, stage, trip_id)
        store._agent_locks.pop(trip_id, None)
        store._display_changed.notify_all()
        return True


def _users(store):
    if hasattr(store, "users"):
        return store.users
    from app.storage.store import USERS
    return USERS
