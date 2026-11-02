from __future__ import annotations
import json
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime
from threading import RLock
from contextlib import contextmanager
from uuid import uuid4
from app.domain.models import AgentAction, AgentCard, IdeaCard, Message, Trip, TripPlan, User
from app.domain.requirements import initial_requirements, update_requirements
from app.storage.state import next_id, IDEA_WALL_LIMIT, IDEA_ROTATIONS
from copy import copy, deepcopy
from app.storage.persistence import save_snapshot, serialize_state

def _stage_turn(self, trip_id):
    """Reuse the normal builders against private aggregates, never live objects."""
    stage = copy(self)
    stage.agent_undo = deepcopy(self.agent_undo)
    stage.trips = dict(self.trips)
    stage.plans = dict(self.plans)
    stage.messages = dict(self.messages)
    stage.idea_cards = dict(self.idea_cards)
    for name in ('trips', 'plans', 'messages', 'idea_cards'):
        getattr(stage, name)[trip_id] = deepcopy(getattr(self, name)[trip_id])
    stage.tasks = deepcopy(self.tasks)
    stage.display_jobs = deepcopy(self.display_jobs)
    stage._display_changed = _StagingSignal()
    stage._defer_persist = 1
    stage.persistence_enabled = False
    return stage

def _publish_turn(self, stage, trip_id):
    for name in ('trips', 'plans', 'messages', 'idea_cards'):
        getattr(self, name)[trip_id] = getattr(stage, name)[trip_id]
    for task_id, task in stage.tasks.items():
        if task.get('trip_id') == trip_id:
            if task_id in self.tasks:
                self.tasks[task_id].clear()
                self.tasks[task_id].update(task)
            else:
                self.tasks[task_id] = task
    self.agent_undo = stage.agent_undo
    self.display_jobs = stage.display_jobs
    self._display_changed.notify_all()

def _persist_staged_turn(self, stage, trip_id):
    try:
        if self.persistence_enabled:
            save_snapshot(self.db_path, serialize_state(stage, _users(self)), initialize=False)
    except Exception as exc:
        raise TurnPersistenceError('最终提交未成功，当前需求、行程与任务状态未改变。') from exc
    _publish_turn(self, stage, trip_id)

class TurnPersistenceError(RuntimeError):
    """Persistence failed before any business state was published."""

class _StagingSignal:

    def notify_all(self):
        pass


def _users(store):
    if hasattr(store, "users"):
        return store.users
    from app.storage.store import USERS
    return USERS
