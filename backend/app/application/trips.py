from __future__ import annotations
import json
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime
from threading import RLock
from contextlib import contextmanager
from uuid import uuid4
from app.domain.models import AgentAction, AgentCard, Message, Trip, TripPlan, User
from app.domain.requirements import initialize_requirements, update_requirements
from app.domain.summaries import legacy_summaries
from app.storage.state import next_id
from app.views.trip_welcome import trip_welcome

def _now_label():
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.storage.state import _persist as _fn__persist
    return datetime.now().strftime('%H:%M')

def delete_trip(self, trip_id: str) -> bool:
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.storage.state import _persist as _fn__persist
    active = next((task for task in self.tasks.values() if task.get('trip_id') == trip_id and task.get('status') in {'queued', 'running'}), None)
    if active is not None:
        raise ValueError('这趟旅行还有任务正在进行，请稍后再删除')
    if trip_id not in self.trips:
        return False
    from app.storage.transactions import delete_trip_atomically
    return delete_trip_atomically(self, trip_id)

def create_user(self, display_name: str) -> User:
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.storage.state import _persist as _fn__persist
    clean_name = display_name.strip()[:20] or '旅行者'
    with self._lock:
        user_id = f'u{next_id(self)}'
        user = User(id=user_id, name=clean_name, initials=clean_name[:1].upper())
        self.users[user_id] = user
        _fn__persist(self)
        return user

def list_trips(self, owner_id: str | None=None) -> list[Trip]:
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.storage.state import _persist as _fn__persist
    trips = list(self.trips.values())
    if owner_id is not None:
        trips = [trip for trip in trips if trip.owner.id == owner_id]
    return trips

def get_trip(self, trip_id: str) -> Trip:
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.storage.state import _persist as _fn__persist
    return self.trips[trip_id]

def create_trip(self, destination: str, date_range: str = '', owner: User | None=None, *, requirements: dict | None=None, mode='city', lodging=None) -> Trip:
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.storage.state import _persist as _fn__persist
    owner = owner or self.users['u1']
    trip_id = f'trip-{next_id(self)}'
    trip = Trip(id=trip_id, destination=destination.strip() or '未命名旅行', legacy_metadata=legacy_summaries(date_range.strip()), owner=owner, status='未生成', last_activity='旅行已创建，开始和 Agent 对话',  mode=mode)
    if mode != 'city':
        raise ValueError('不支持的行程类型')
    if requirements is None:
        raise ValueError('请通过完整创建表单新建旅行')
    if requirements is not None:
        trip = initialize_requirements(trip, requirements)
        if mode == 'city':
            trip.lodging = _validated_lodging(self, lodging, trip.requirements.city)
        from app.domain.itinerary.prerequisites import planning_brief
        _, question = planning_brief(trip, require_all=True)
        if question:
            raise ValueError(question)
    self.trips[trip_id] = trip
    welcome_body, welcome_card = trip_welcome(trip)
    self.messages[trip_id] = [Message(id=f'm-{next_id(self)}', sender=self.users['agent'], sender_type='agent', body=welcome_body, created_at=_now_label(), agent_card=welcome_card)]
    self.plans[trip_id] = TripPlan(id='empty', title='还没有行程计划', status='未生成', days=[])
    _fn__persist(self)
    return trip

def _validated_lodging(self, lodging, city):
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.storage.state import _persist as _fn__persist
    from app.domain.lodging import collect_places
    trusted = collect_places([{'role': 'tool', 'name': 'search_places', 'content': json.dumps({'status': 'available', 'candidates': [lodging]}, ensure_ascii=False)}])
    if not trusted or not city or lodging['city'] != city:
        raise ValueError('请重新确认住宿的地图位置')
    return deepcopy(next(iter(trusted.values())))

def save_requirements_text(self, trip_id, text, expected_revision):
    from app.application.undo import manual_barrier
    from app.storage.transactions import _stage_turn, _persist_staged_turn
    from app.workflow.counters import bump_epoch
    with self._lock:
        stage = _stage_turn(self, trip_id)
        changed = _save_requirements_text(stage, trip_id, text, expected_revision)
        if changed:
            manual_barrier(stage, trip_id)
            stage.trips[trip_id].workflow_state = bump_epoch(stage.trips[trip_id].workflow_state)
        _persist_staged_turn(self, stage, trip_id)
        return changed


def _save_requirements_text(self, trip_id, text, expected_revision):
    trip = self.trips[trip_id]
    if expected_revision != trip.requirements.revision:
        raise ValueError('需求正文已经更新，请刷新后重新保存，当前内容未被覆盖')
    updated = update_requirements(trip, {'text': text})
    if updated.requirements.revision == trip.requirements.revision:
        return False
    updated.last_activity = '需求正文已更新'
    self.trips[trip_id] = updated
    return True
