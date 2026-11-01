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
from app.views.trip_welcome import trip_welcome

def _now_label():
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.storage.state import _persist as _fn__persist
    return datetime.now().strftime('%H:%M')

def delete_trip(self, trip_id: str) -> bool:
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.storage.state import _persist as _fn__persist
    active = next((task for task in self.tasks.values() if task.get('trip_id') == trip_id and task.get('status') in ACTIVE), None)
    if active is not None:
        raise ValueError('这趟旅行还有任务正在进行，请稍后再删除')
    if trip_id not in self.trips:
        return False
    del self.trips[trip_id]
    self.messages.pop(trip_id, None)
    self.plans.pop(trip_id, None)
    self.idea_cards.pop(trip_id, None)
    self._agent_locks.pop(trip_id, None)
    _fn__discard_display_jobs(self, trip_id)
    self.tasks = {task_id: task for task_id, task in self.tasks.items() if task.get('trip_id') != trip_id}
    _fn__persist(self)
    return True

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

def create_trip(self, destination: str, date_range: str, budget: str, style: str, note: str, owner: User | None=None, *, requirements: dict | None=None, mode='city', lodging=None) -> Trip:
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.storage.state import _persist as _fn__persist
    owner = owner or self.users['u1']
    trip_id = f'trip-{next_id(self)}'
    trip = Trip(id=trip_id, destination=destination.strip() or '未命名旅行', date_range=date_range.strip() or '时间待定', budget=budget.strip() or '预算待定', style=style.strip() or '风格待定', note=note.strip(), owner=owner, status='未生成', last_activity='旅行已创建，开始和 Agent 对话', requirements=initial_requirements(budget, style), mode=mode)
    if mode != 'city':
        raise ValueError('不支持的行程类型')
    if requirements is not None:
        trip = update_requirements(trip, requirements)
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
    self.idea_cards[trip_id] = []
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

def update_city_outing(self, trip_id, changes, expected_revision, lodging):
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.storage.state import _persist as _fn__persist
    trip = self.trips[trip_id]
    if trip.requirements.revision != expected_revision:
        raise ValueError('游玩设置已经更新，请刷新后重新保存')
    candidate = deepcopy(trip)
    candidate.mode = 'city'
    updated = update_requirements(candidate, changes)
    updated.lodging = _validated_lodging(self, lodging, updated.requirements.city)
    if trip.destination == f'{trip.requirements.city}同城游玩':
        updated.destination = f'{updated.requirements.city}同城游玩'
    updated.last_activity = '游玩设置已更新'
    self.trips[trip_id] = updated
    _fn__persist(self)
    return updated

def update_trip_requirements(self, trip_id: str, changes: dict, expected_revision: int) -> Trip:
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.storage.state import _persist as _fn__persist
    trip = self.trips[trip_id]
    if expected_revision != trip.requirements.revision:
        raise ValueError('旅行需求已经更新，请刷新后再编辑，当前内容未被覆盖')
    updated = update_requirements(trip, changes)
    updated.last_activity = '旅行需求已更新'
    self.trips[trip_id] = updated
    _fn__persist(self)
    return updated
