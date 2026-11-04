from __future__ import annotations

import json
import re
import sys
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime
from functools import wraps
from threading import RLock, local
from contextlib import contextmanager
from itertools import count

from app.core.config import boolean, database_path
from app.storage.demo_data import seed_messages, seed_plan
from app.domain.models import (
    AgentAction,
    AgentCard,
    Message,
    Trip,
    TripPlan,
    User,
)
# Keep the legacy workflow classes as explicit compatibility bases while the
# application functions are migrated incrementally. They remain preserved and
# are still the active implementation for this store during this phase.
from app.workflow.display_tasks import DisplayTasks
from app.workflow.tasks import ACTIVE, TravelTasks, validate_display_target_metadata
from app.workflow.turn_commit import TurnCommit, TurnPersistenceError
from app.domain.requirements import initialize_requirements, update_requirements
from app.domain.summaries import legacy_summaries
from app.views.trip_welcome import trip_welcome
from app.storage.persistence import CURRENT_SCHEMA_VERSION, backup_database, ensure_relational_schema, load_snapshot, restore_state, save_snapshot, serialize_state

USERS = {
    "u1": User(id="u1", name="林夏", initials="林"),
    "agent": User(id="agent", name="旅行规划 Agent", initials="AI"),
}

_id_counter = count(100)




def _date_range_bounds(value: str) -> tuple[date, date] | None:
    text = (value or "").strip()
    chinese = re.search(
        r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日\s*[-—至]\s*(?:(\d{4})年)?(?:(\d{1,2})月)?(\d{1,2})日",
        text,
    )
    if chinese:
        start_year = int(chinese.group(1) or datetime.now().year)
        start_month = int(chinese.group(2))
        end_year = int(chinese.group(4) or start_year)
        end_month = int(chinese.group(5) or start_month)
        try:
            return date(start_year, start_month, int(chinese.group(3))), date(end_year, end_month, int(chinese.group(6)))
        except ValueError:
            return None
    dotted = re.search(
        r"(?:(\d{4})[./])?(\d{1,2})[./](\d{1,2})\s*[-—至]\s*(?:(\d{4})[./])?(\d{1,2})[./](\d{1,2})",
        text,
    )
    if dotted:
        start_year = int(dotted.group(1) or datetime.now().year)
        end_year = int(dotted.group(4) or start_year)
        try:
            return date(start_year, int(dotted.group(2)), int(dotted.group(3))), date(end_year, int(dotted.group(5)), int(dotted.group(6)))
        except ValueError:
            return None
    return None


def _date_coverage_note(trip: Trip, plan: TripPlan) -> str:
    bounds = _date_range_bounds(trip.date_range)
    if not bounds or not plan.days:
        return ""
    start, end = bounds
    planned_dates = {day.date.strip() for day in plan.days if day.date.strip()}
    if not planned_dates:
        return f"日期范围共 {(end - start).days + 1} 天，具体安排待补充"
    missing = []
    current = start
    while current <= end:
        if current.isoformat() not in planned_dates:
            missing.append(current)
        current = date.fromordinal(current.toordinal() + 1)
    if not missing:
        return ""
    if len(missing) == 1 and missing[0] == end:
        return f"返程日（{end.month}月{end.day}日）尚未安排"
    return f"还有 {len(missing)} 天未安排"


def _state_locked(method):
    """Keep in-memory mutations and their persisted snapshot in one critical section."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped

def _manual_business_write(method):
    @wraps(method)
    def transaction(self, trip_id, *args, **kwargs):
        from app.application.undo import manual_barrier
        with self._lock:
            stage = self._stage_turn(trip_id)
            result = method(stage, trip_id, *args, **kwargs)
            if result is not False:
                manual_barrier(stage, trip_id)
                from app.workflow.counters import bump_epoch
                stage.trips[trip_id].workflow_state = bump_epoch(stage.trips[trip_id].workflow_state)
            self._persist_staged_turn(stage, trip_id)
            return result
    return transaction


class DemoStore(TurnCommit, TravelTasks, DisplayTasks):
    def __init__(self, db_path: str | None = None, persistence_enabled: bool | None = None, *, display_worker_enabled: bool | None = None, seed_demo: bool = True) -> None:
        self.db_path = db_path or database_path()
        self.persistence_enabled = self._resolve_persistence_enabled(persistence_enabled)
        self._lock = RLock()
        self.agent_undo = {}
        self._task_context = local()
        self.tasks: dict[str, dict] = {}
        from app.storage.task_repository import MemoryTaskRepository
        self.task_repository = MemoryTaskRepository() if not self.persistence_enabled else None
        self._agent_locks: dict[str, RLock] = {}
        self._init_display_tasks(display_worker_enabled)
        # Demo content is opt-in for isolated demos/tests. Production starts
        # with empty business collections and only restores an existing snapshot.
        self.trips: dict[str, Trip] = {}
        self.messages: dict[str, list[Message]] = {}
        self.plans: dict[str, TripPlan] = {}
        if seed_demo:
            self.trips = {
                "trip-hangzhou": Trip(
                    id="trip-hangzhou",
                    destination="杭州周末旅行",
                    legacy_metadata=legacy_summaries("8月24日 - 8月26日"),
                    owner=USERS["u1"],
                    status="草案",
                    last_activity="Agent 已生成初版行程",
                )
            }
            self.messages = {"trip-hangzhou": seed_messages(USERS)}
            self.plans = {"trip-hangzhou": seed_plan()}
            self.trips["trip-hangzhou"].requirements.text = "想去西湖，节奏轻松。"
        self._load_persisted_state()
        if self.persistence_enabled:
            ensure_relational_schema(self.db_path)

    @staticmethod
    def _resolve_persistence_enabled(value: bool | None) -> bool:
        if value is not None:
            return value
        return boolean('TRAVEL_PERSISTENCE') and "pytest" not in sys.modules

    def _load_persisted_state(self) -> None:
        if not self.persistence_enabled:
            return
        from app.storage.persistence import load_persisted_state
        next_value = load_persisted_state(self, USERS)
        if next_value is None:
            return
        global _id_counter
        _id_counter = count(next_value)
        # Background tasks cannot survive a process restart. Keep the user's text
        # and replace interrupted placeholders with the existing failure response.
        self.recover_tasks()
        self.recover_display_jobs()

    @_state_locked
    def _persist(self, *, task_upserts=(), delete_trip_ids=()) -> None:
        if getattr(self, '_defer_persist', 0):
            return
        if self.persistence_enabled:
            save_snapshot(self.db_path, serialize_state(self, USERS), initialize=False,
                          task_upserts=task_upserts, delete_trip_ids=delete_trip_ids)
        else:
            self.task_repository.apply(task_upserts, delete_trip_ids)

    def list_trips(self, owner_id: str | None = None) -> list[Trip]:
        trips = list(self.trips.values())
        if owner_id is not None:
            trips = [trip for trip in trips if trip.owner.id == owner_id]
        return trips

    def get_trip(self, trip_id: str) -> Trip:
        return self.trips[trip_id]

    @_state_locked
    def delete_trip(self, trip_id: str) -> bool:
        active = next(
            (task for task in self.tasks.values()
             if task.get("trip_id") == trip_id and task.get("status") in ACTIVE),
            None,
        )
        if active is not None:
            raise ValueError("这趟旅行还有任务正在进行，请稍后再删除")
        if trip_id not in self.trips:
            return False
        from app.storage.transactions import delete_trip_atomically
        return delete_trip_atomically(self, trip_id)

    def create_user(self, display_name: str) -> User:
        clean_name = display_name.strip()[:20] or "旅行者"
        with self._lock:
            user_id = f"u{next(_id_counter)}"
            user = User(id=user_id, name=clean_name, initials=clean_name[:1].upper())
            USERS[user_id] = user
            self._persist()
            return user

    def trip_metrics(self, trip_id: str) -> dict[str, int | str]:
        plan = self.plans[trip_id]
        display_days = (plan.display or {}).get('days', [])
        plan_items = sum(len(day['items']) for day in display_days) if display_days else sum(len(day.items) for day in plan.days)
        basis = plan.requirements_snapshot if isinstance(plan.requirements_snapshot, dict) else {}
        plan_date_range = (f"{basis['start_date']} — {basis['end_date']}" if basis.get('start_date') and basis.get('end_date') else self.trips[trip_id].date_range)
        return {
            "message_count": len(self.messages[trip_id]),
            "plan_day_count": len(display_days) or len((plan.display or {}).get('dates', [])) or len(plan.days),
            "plan_date_range": plan_date_range,
            "plan_item_count": plan_items,
            "date_coverage_note": _date_coverage_note(self.trips[trip_id], plan),
        }

    @_state_locked
    def create_trip(self, destination: str, date_range: str = '', owner: User | None = None, *, requirements: dict | None = None, mode='city', lodging=None) -> Trip:
        owner = owner or USERS["u1"]
        trip_id = f"trip-{next(_id_counter)}"
        trip = Trip(
            id=trip_id,
            destination=destination.strip() or "未命名旅行",
            legacy_metadata=legacy_summaries(date_range.strip()),
            owner=owner,
            status="未生成",
            last_activity="旅行已创建，开始和 Agent 对话",

            mode=mode,
        )
        if mode != 'city':
            raise ValueError('不支持的行程类型')
        if requirements is None:
            raise ValueError('请通过完整创建表单新建旅行')
        if requirements is not None:
            trip = initialize_requirements(trip, requirements)
            if mode == 'city':
                trip.lodging = self._validated_lodging(lodging, trip.requirements.city)
            from app.domain.itinerary.prerequisites import planning_brief
            _, question = planning_brief(trip, require_all=True)
            if question:
                raise ValueError(question)
        self.trips[trip_id] = trip
        welcome_body, welcome_card = trip_welcome(trip)
        self.messages[trip_id] = [
            Message(
                id=f"m-{next(_id_counter)}",
                sender=USERS["agent"],
                sender_type="agent",
                body=welcome_body,
                created_at=_now_label(),
                agent_card=welcome_card,
            )
        ]
        self.plans[trip_id] = TripPlan(id="empty", title="还没有行程计划", status="未生成", days=[])
        self._persist()
        return trip

    @staticmethod
    def _validated_lodging(lodging, city):
        from app.domain.lodging import collect_places
        trusted = collect_places([{'role': 'tool', 'name': 'search_places', 'content':
            json.dumps({'status': 'available', 'candidates': [lodging]}, ensure_ascii=False)}])
        if not trusted or not city or lodging['city'] != city:
            raise ValueError('请重新确认住宿的地图位置')
        return deepcopy(next(iter(trusted.values())))

    @_manual_business_write
    def save_requirements_text(self, trip_id, text, expected_revision):
        trip = self.trips[trip_id]
        if expected_revision != trip.requirements.revision:
            raise ValueError('需求正文已经更新，请刷新后重新保存，当前内容未被覆盖')
        updated = update_requirements(trip, {'text': text})
        if updated.requirements.revision == trip.requirements.revision:
            return False
        updated.last_activity = '需求正文已更新'
        self.trips[trip_id] = updated
        return True

    def clear_chat(self, trip_id: str) -> None:
        from app.workflow.counters import clear_workflow
        with self._lock:
            stage = self._stage_turn(trip_id)
            stage.trips[trip_id].workflow_state = clear_workflow(stage.trips[trip_id].workflow_state)
            for task in stage.tasks.values():
                if task['trip_id'] == trip_id and task['status'] in {'queued', 'running'}:
                    task.update(status='superseded', message='聊天已清空，本轮已取消', result_url='')
            stage.messages[trip_id] = []
            stage.cancel_display_jobs(trip_id)
            stage.trips[trip_id].last_activity = "聊天记录已清空"
            self._persist_staged_turn(stage, trip_id)
        self._notify_display_worker()

    def add_user_message(self, trip_id: str, body: str, sender_id: str = "u1") -> Message:
        with self._lock:
            clean_body = body.strip()
            owner = self.trips[trip_id].owner
            sender = USERS.get(sender_id, owner)
            if sender.id != owner.id:
                sender = owner
            message = Message(
                id=f"m-{next(_id_counter)}",
                sender=sender,
                sender_type="user",
                body=clean_body,
                created_at=_now_label(),
            )
            self.messages[trip_id].append(message)
            self.trips[trip_id].last_activity = "已发送消息，Agent 正在思考"
            self._persist()
        return message

    @_state_locked
    def queue_user_message(self, trip_id: str, body: str, sender_id: str) -> tuple[Message, Message]:
        message = self.add_user_message(trip_id, body, sender_id)
        return message, self.start_agent_task(trip_id)

    def start_agent_task(self, trip_id: str) -> Message:
        from app.application.progress import new_progress
        with self._lock:
            thinking_message = Message(
                id=f"m-{next(_id_counter)}",
                sender=USERS["agent"],
                sender_type="agent",
                body="思考中 •••",
                created_at=_now_label(),
                status="thinking",
                execution_progress=new_progress('queued'),
            )
            self.messages[trip_id].append(thinking_message)
            task_id = getattr(self._task_context, 'task_id', None)
            if task_id:
                self.tasks[task_id]['thinking_id'] = thinking_message.id
            self.trips[trip_id].last_activity = "Agent 正在整理旅行信息"
            self._persist()
            return thinking_message

    def _task_current(self) -> bool:
        task_id = getattr(self._task_context, 'task_id', None)
        with self._lock:
            return task_id is None or self.tasks.get(task_id, {}).get('status') == 'running'

    @contextmanager
    def _agent_lock(self, trip_id: str):
        with self._foreground_operation(trip_id):
            if getattr(self._task_context, 'task_id', None):
                yield
            else:
                with self._lock:
                    lock = self._agent_locks.setdefault(trip_id, RLock())
                with lock:
                    yield

    def fail_agent_task(self, trip_id: str, thinking_message_id: str | None = None) -> None:
        with self._lock:
            if not self._task_current():
                return
            error_message = Message(
                id=f"m-{next(_id_counter)}",
                sender=USERS["agent"],
                sender_type="agent",
                status="failed",
                body="本次处理未完成，请重新发送消息。已有内容已保留。",
                created_at=_now_label(),
                agent_card=AgentCard(
                    kind="missing-info",
                    title="Agent 暂时没有回复成功",
                    summary="你的消息已经保留，这次处理没有完成。",
                    bullets=["消息不会丢失", "可以重新发送上一条消息", "也可以先继续补充旅行想法"],
                    actions=[AgentAction(label="继续聊天", target="chat")],
                ),
            )
            if thinking_message_id:
                for index, message in enumerate(self.messages[trip_id]):
                    if message.id == thinking_message_id:
                        from app.application.progress import finish_progress
                        error_message.execution_progress = finish_progress(message.execution_progress, 'failed')
                        self.messages[trip_id][index] = error_message
                        break
                else:
                    self.messages[trip_id].append(error_message)
            else:
                self.messages[trip_id].append(error_message)
            self.trips[trip_id].last_activity = "Agent 整理失败，等待新消息"
            self._persist()

    def confirm_plan(self, trip_id: str):
        return self.confirm_current_turn(trip_id)

    @_state_locked
    def _apply_text_plan(self, trip_id: str, text: str, evidence: list[dict] | None = None, *, display=None, display_source_hash='', display_status='not_requested', preserve_plan_basis=False) -> None:
        if not isinstance(text, str) or not text.strip():
            raise ValueError('行程内容不能为空')
        from app.services.itinerary.plan_display import validate_display, text_hash, dates_from_snapshot
        trip, previous = self.trips[trip_id], self.plans[trip_id]
        requirements_snapshot = deepcopy(previous.requirements_snapshot) if preserve_plan_basis else asdict(trip.requirements)
        requirements_revision = previous.requirements_revision if preserve_plan_basis else trip.requirements.revision
        lodging_snapshot = deepcopy(previous.lodging_snapshot if preserve_plan_basis else trip.lodging)
        if display is not None:
            try:
                if display_source_hash != text_hash(text):
                    raise ValueError('展示数据与原文不对应')
                display = validate_display(display, dates_from_snapshot(requirements_snapshot))
            except Exception:
                display, display_status = None, 'fallback'
        self.plans[trip_id] = TripPlan(
            id=f"plan-{next(_id_counter)}", title=(display['title'] if display else f"{self.trips[trip_id].requirements.city or self.trips[trip_id].destination}行程"),
            status="草案", text=text, evidence=deepcopy(evidence or []),
            requirements_revision=requirements_revision,
            display=deepcopy(display), display_source_hash=display_source_hash, display_status=display_status,
            display_style=previous.display_style,
            requirements_snapshot=requirements_snapshot, lodging_snapshot=lodging_snapshot,
            requirements_basis_complete=previous.requirements_basis_complete if preserve_plan_basis else True,
            requirements_legacy_stale=previous.requirements_legacy_stale if preserve_plan_basis else False,
        )
        self.trips[trip_id].status = "草案"
        self._discard_display_jobs(trip_id)
        if display_status == 'pending':
            self._queue_display(trip_id)


def _now_label() -> str:
    return datetime.now().strftime("%H:%M")


def _normalize_idea_text(value: str) -> str:
    return "".join(value.lower().split())


# Production singleton is empty on a fresh database; demo fixtures opt in via
# DemoStore(seed_demo=True), preserving the legacy test/demo constructor.
store = DemoStore(seed_demo=False)
