from __future__ import annotations

import json
import os
import re
import sys
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime
from functools import wraps
from threading import RLock, local
from contextlib import contextmanager
from itertools import count

from .models import (
    AgentAction,
    AgentCard,
    IdeaCard,
    Message,
    Trip,
    TripDay,
    TripItem,
    TripPlan,
    User,
)
from .display_tasks import DisplayTasks
from .tasks import ACTIVE, TravelTasks, validate_display_target_metadata
from .turn_commit import TurnCommit, TurnPersistenceError
from .requirements import chat_changes, initial_requirements, update_requirements
from .trip_welcome import trip_welcome
from .persistence import CURRENT_SCHEMA_VERSION, backup_database, ensure_relational_schema, load_snapshot, restore_state, save_snapshot, serialize_state

USERS = {
    "u1": User(id="u1", name="林夏", initials="林"),
    "agent": User(id="agent", name="旅行规划 Agent", initials="AI"),
}

_id_counter = count(100)

IDEA_WALL_LIMIT = 8
IDEA_ROTATIONS = ["-1.4deg", "1deg", "0.6deg", "-0.8deg", "1.3deg", "0.8deg", "-0.7deg", "1.1deg"]


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


class DemoStore(TurnCommit, TravelTasks, DisplayTasks):
    def __init__(self, db_path: str | None = None, persistence_enabled: bool | None = None, *, display_worker_enabled: bool | None = None) -> None:
        self.db_path = db_path or os.getenv("CITY_DB_PATH", ".cache/cityassistant.sqlite3")
        self.persistence_enabled = self._resolve_persistence_enabled(persistence_enabled)
        self._lock = RLock()
        self._task_context = local()
        self.tasks: dict[str, dict] = {}
        self._agent_locks: dict[str, RLock] = {}
        self._init_display_tasks(display_worker_enabled)
        self.trips: dict[str, Trip] = {
            "trip-hangzhou": Trip(
                id="trip-hangzhou",
                destination="杭州周末旅行",
                date_range="8月24日 - 8月26日",
                budget="人均 1500",
                style="轻松、少排队、适合慢慢逛",
                note="想轻松一点，少排队，多留自由时间。",
                owner=USERS["u1"],
                status="草案",
                last_activity="Agent 已生成初版行程",
            )
        }
        self.messages: dict[str, list[Message]] = {"trip-hangzhou": self._seed_messages()}
        self.plans: dict[str, TripPlan] = {"trip-hangzhou": self._seed_plan()}
        self.idea_cards: dict[str, list[IdeaCard]] = {"trip-hangzhou": self._seed_idea_cards()}
        for trip in self.trips.values():
            trip.requirements = initial_requirements(trip.budget, trip.style)
        self._load_persisted_state()
        if self.persistence_enabled:
            ensure_relational_schema(self.db_path)

    @staticmethod
    def _resolve_persistence_enabled(value: bool | None) -> bool:
        if value is not None:
            return value
        configured = os.getenv("TRAVEL_PERSISTENCE", "1").strip().lower()
        if configured in {"0", "false", "no", "off"}:
            return False
        return "pytest" not in sys.modules

    def _load_persisted_state(self) -> None:
        if not self.persistence_enabled:
            return
        snapshot = load_snapshot(self.db_path)
        if not snapshot:
            return
        legacy_snapshot = int(snapshot.get("schema_version", 1)) < CURRENT_SCHEMA_VERSION
        if legacy_snapshot:
            backup_database(self.db_path)
        global _id_counter
        _id_counter = count(restore_state(self, snapshot, USERS))
        # Restoring a snapshot preserves every card. Content and titles are not
        # reliable identifiers for placeholders or duplicate records.
        if legacy_snapshot:
            self._persist()
        # Background tasks cannot survive a process restart. Keep the user's text
        # and replace interrupted placeholders with the existing failure response.
        for trip_id, messages in self.messages.items():
            for message in list(messages):
                if message.status == "thinking":
                    self.fail_agent_task(trip_id, message.id)
        self.recover_tasks()
        self.recover_display_jobs()

    @_state_locked
    def _persist(self) -> None:
        if self.persistence_enabled and not getattr(self, '_defer_persist', 0):
            save_snapshot(self.db_path, serialize_state(self, USERS))

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
        del self.trips[trip_id]
        self.messages.pop(trip_id, None)
        self.plans.pop(trip_id, None)
        self.idea_cards.pop(trip_id, None)
        self._agent_locks.pop(trip_id, None)
        self._discard_display_jobs(trip_id)
        self.tasks = {
            task_id: task for task_id, task in self.tasks.items()
            if task.get("trip_id") != trip_id
        }
        self._persist()
        return True

    def create_user(self, display_name: str) -> User:
        clean_name = display_name.strip()[:20] or "旅行者"
        with self._lock:
            user_id = f"u{next(_id_counter)}"
            user = User(id=user_id, name=clean_name, initials=clean_name[:1].upper())
            USERS[user_id] = user
            self._persist()
            return user

    def trip_metrics(self, trip_id: str) -> dict[str, int | str]:
        cards = self.idea_cards[trip_id]
        plan = self.plans[trip_id]
        display_days = (plan.display or {}).get('days', [])
        plan_items = sum(len(day['items']) for day in display_days) if plan.display else sum(len(day.items) for day in plan.days)
        basis = plan.requirements_snapshot
        plan_date_range = (f"{basis['start_date']} — {basis['end_date']}" if basis.get('start_date') and basis.get('end_date') else self.trips[trip_id].date_range)
        return {
            "message_count": len(self.messages[trip_id]),
            "idea_count": len(cards),
            "constraint_count": sum(card.kind != "待归类" for card in cards),
            "reminder_count": sum(card.kind == "禁忌" or card.status == "冲突提醒" for card in cards),
            "plan_day_count": len(display_days) if plan.display else len(plan.days),
            "plan_date_range": plan_date_range,
            "plan_item_count": plan_items,
            "date_coverage_note": _date_coverage_note(self.trips[trip_id], plan),
        }

    @_state_locked
    def create_trip(self, destination: str, date_range: str, budget: str, style: str, note: str, owner: User | None = None, *, requirements: dict | None = None, mode='travel', lodging=None) -> Trip:
        owner = owner or USERS["u1"]
        trip_id = f"trip-{next(_id_counter)}"
        trip = Trip(
            id=trip_id,
            destination=destination.strip() or "未命名旅行",
            date_range=date_range.strip() or "时间待定",
            budget=budget.strip() or "预算待定",
            style=style.strip() or "风格待定",
            note=note.strip(),
            owner=owner,
            status="未生成",
            last_activity="旅行已创建，开始和 Agent 对话",
            requirements=initial_requirements(budget, style),
            mode=mode,
        )
        if mode not in {'travel', 'city'}:
            raise ValueError('不支持的行程类型')
        if requirements is not None:
            trip = update_requirements(trip, requirements)
            if mode == 'city':
                trip.lodging = self._validated_lodging(lodging, trip.requirements.city)
            from .planning import planning_brief
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
        self.idea_cards[trip_id] = []
        self._persist()
        return trip

    @staticmethod
    def _validated_lodging(lodging, city):
        from .places import collect_places
        trusted = collect_places([{'role': 'tool', 'name': 'search_places', 'content':
            json.dumps({'status': 'available', 'candidates': [lodging]}, ensure_ascii=False)}])
        if not trusted or not city or lodging['city'] != city:
            raise ValueError('请重新确认住宿的地图位置')
        return deepcopy(next(iter(trusted.values())))

    @_state_locked
    def update_city_outing(self, trip_id, changes, expected_revision, lodging):
        trip = self.trips[trip_id]
        if trip.mode != 'city' or trip.requirements.revision != expected_revision:
            raise ValueError('游玩设置已经更新，请刷新后重新保存')
        updated = update_requirements(trip, changes)
        updated.lodging = self._validated_lodging(lodging, updated.requirements.city)
        if trip.destination == f'{trip.requirements.city}同城游玩':
            updated.destination = f'{updated.requirements.city}同城游玩'
        updated.last_activity = '游玩设置已更新'
        self.trips[trip_id] = updated
        self._persist()
        return updated

    @_state_locked
    def update_trip_requirements(self, trip_id: str, changes: dict, expected_revision: int) -> Trip:
        trip = self.trips[trip_id]
        if expected_revision != trip.requirements.revision:
            raise ValueError("旅行需求已经更新，请刷新后再编辑，当前内容未被覆盖")
        updated = update_requirements(trip, changes)
        updated.last_activity = "旅行需求已更新"
        self.trips[trip_id] = updated
        self._persist()
        return updated

    def clear_chat(self, trip_id: str) -> None:
        from .task_context import clear_workflow
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
        message = self.add_user_message(trip_id, body, sender_id, process_agent=False)
        return message, self.start_agent_task(trip_id)

    def start_agent_task(self, trip_id: str) -> Message:
        with self._lock:
            thinking_message = Message(
                id=f"m-{next(_id_counter)}",
                sender=USERS["agent"],
                sender_type="agent",
                body="思考中 •••",
                created_at=_now_label(),
                status="thinking",
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
                body="我刚刚没有完成回复，可以直接重新发送上一条消息。",
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
                        self.messages[trip_id][index] = error_message
                        break
                else:
                    self.messages[trip_id].append(error_message)
            else:
                self.messages[trip_id].append(error_message)
            self.trips[trip_id].last_activity = "Agent 整理失败，等待重试"
            self._persist()

    @_state_locked
    def summarize_chat(self, trip_id: str) -> Message:
        message = self._local_summary_reply(trip_id)
        self.messages[trip_id].append(message)
        self.trips[trip_id].last_activity = "Agent 已整理最近对话"
        self._persist()
        return message

    @_state_locked
    def delete_idea(self, trip_id: str, idea_id: str) -> bool:
        cards = self.idea_cards[trip_id]
        remaining = [card for card in cards if card.id != idea_id]
        if len(remaining) == len(cards):
            return False
        self.idea_cards[trip_id] = remaining
        self.trips[trip_id].last_activity = "已删除一条旅行想法"
        self._persist()
        return True

    @_state_locked
    def add_idea(self, trip_id: str, body: str) -> IdeaCard:
        rotation = IDEA_ROTATIONS[len(self.idea_cards[trip_id]) % len(IDEA_ROTATIONS)]
        card = IdeaCard(
            id=f"idea-{next(_id_counter)}",
            kind="待归类",
            title=body.strip(),
            body=body.strip(),
            author="你",
            status="待整理",
            rotation=rotation,
        )
        self.idea_cards[trip_id].append(card)
        self.trips[trip_id].last_activity = "已添加新的旅行想法"
        self._persist()
        return card

    def request_revision(self, trip_id: str, item_id: str | None = None, feedback: str = "", sender: User | None = None, *, display_target: dict | None = None) -> Message | None:
        if not getattr(self._task_context, 'task_id', None):
            from uuid import uuid4
            payload = {'feedback': feedback.strip() or '希望安排更轻松一点'}
            if item_id:
                payload['item_id'] = item_id
            if display_target is not None:
                from .plan_display import text_hash
                with self._lock:
                    target = validate_display_target_metadata(self.plans[trip_id], display_target)
                    payload.update(display_target={key: target[key] for key in ('day_index', 'item_index')},
                        expected_plan_id=self.plans[trip_id].id, source_hash=text_hash(self.plans[trip_id].text),
                        expected_requirements_revision=self.trips[trip_id].requirements.revision)
            task, _ = self.submit_task(trip_id, 'revision', payload, uuid4().hex)
            receipt = self.run_task(task['id'])
            return next((row for row in self.messages[trip_id] if receipt and row.id == receipt.message_id), None)

    def generate_plan(self, trip_id: str) -> Message | None:
        if not getattr(self._task_context, 'task_id', None):
            from uuid import uuid4
            task, _ = self.submit_task(trip_id, 'plan', {}, uuid4().hex)
            receipt = self.run_task(task['id'])
            return next((row for row in self.messages[trip_id] if receipt and row.id == receipt.message_id), None)

    def confirm_plan(self, trip_id: str):
        return self.confirm_current_turn(trip_id)

    def _add_agent_idea_cards(self, trip_id: str, raw_cards: list[dict], sender: User | None = None) -> None:
        if not raw_cards:
            return

        source = "对话" if sender else "Agent"
        for raw_card in raw_cards:
            kind = str(raw_card.get("kind") or "待归类")
            title = str(raw_card.get("title") or "新的旅行想法").strip()
            body = str(raw_card.get("body") or title).strip()
            status = str(raw_card.get("status") or "已整理").strip()
            if not title or not body:
                continue
            existing_index = self._find_similar_idea_index(trip_id, kind, title, body)
            if existing_index is not None:
                previous = self.idea_cards[trip_id][existing_index]
                self.idea_cards[trip_id][existing_index] = IdeaCard(
                    id=previous.id,
                    kind=kind,
                    title=title,
                    body=body,
                    author=previous.author if previous.author == "你" else source,
                    status=status,
                    rotation=previous.rotation,
                )
                continue
            rotation = IDEA_ROTATIONS[len(self.idea_cards[trip_id]) % len(IDEA_ROTATIONS)]
            self.idea_cards[trip_id].append(
                IdeaCard(
                    id=f"idea-{next(_id_counter)}",
                    kind=kind,
                    title=title,
                    body=body,
                    author=source,
                    status=status,
                    rotation=rotation,
                )
            )

    def _local_summary_reply(self, trip_id: str) -> Message:
        recent_user_messages = [message for message in self.messages[trip_id][-12:] if message.sender_type == "user"]
        raw_cards = []
        bullets = []
        for message in recent_user_messages:
            kind = self._kind_from_text(message.body)
            if not kind:
                continue
            status = "冲突提醒" if kind == "禁忌" or any(word in message.body for word in ("不要", "不能", "太累", "冲突")) else "已整理"
            raw_cards.append(
                {
                    "kind": kind,
                    "title": self._title_from_text(kind, message.body),
                    "body": message.body,
                    "status": status,
                }
            )
            bullet = f"{kind}：{message.body[:36]}"
            if bullet not in bullets:
                bullets.append(bullet)
        self._add_agent_idea_cards(trip_id, raw_cards, self.trips[trip_id].owner)
        return Message(
            id=f"m-{next(_id_counter)}",
            sender=USERS["agent"],
            sender_type="agent",
            body="我整理了最近对话里的旅行需求。",
            created_at=_now_label(),
            agent_card=AgentCard(
                kind="requirement-summary",
                title="旅行偏好整理好了",
                summary="最近对话已拆成预算、地点、节奏和不能接受的安排。",
                bullets=bullets[-4:] or ["先补充预算", "再确认想去地点", "最后补充不能接受的安排"],
                actions=[AgentAction(label="查看旅行想法", target="board")],
            ),
        )

    @staticmethod
    def _kind_from_text(body: str) -> str | None:
        if any(word in body for word in ("预算", "人均", "钱", "贵", "便宜")):
            return "预算"
        if any(word in body for word in ("想去", "地点", "景点", "海", "湖", "古城", "地铁", "住")):
            return "地点"
        if any(word in body for word in ("不要", "不能", "忌口", "不想")):
            return "禁忌"
        if any(word in body for word in ("节奏", "轻松", "太累", "休息", "慢")):
            return "节奏"
        return None

    @staticmethod
    def _title_from_text(kind: str, body: str) -> str:
        title_by_kind = {"预算": "预算约束", "地点": "地点偏好", "节奏": "节奏偏好", "禁忌": "不能接受"}
        return title_by_kind.get(kind, body.strip()[:12] or "新的旅行想法")

    def _find_similar_idea_index(self, trip_id: str, kind: str, title: str, body: str) -> int | None:
        normalized_title = _normalize_idea_text(title)
        normalized_body = _normalize_idea_text(body)
        for index, card in enumerate(self.idea_cards[trip_id]):
            if _normalize_idea_text(card.title) == normalized_title:
                return index
            if _normalize_idea_text(card.body) == normalized_body:
                return index
            if card.kind == kind == "预算":
                return index
        return None

    @_state_locked
    def _apply_text_plan(self, trip_id: str, text: str, evidence: list[dict] | None = None, *, display=None, display_source_hash='', display_status='not_requested', preserve_plan_basis=False) -> None:
        if not isinstance(text, str) or not text.strip():
            raise ValueError('行程内容不能为空')
        from .plan_display import validate_display, text_hash, dates_from_snapshot
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
            requirements_snapshot=requirements_snapshot, lodging_snapshot=lodging_snapshot,
        )
        self.trips[trip_id].status = "草案"
        self._discard_display_jobs(trip_id)
        if display_status == 'pending':
            self._queue_display(trip_id)

    def _fallback_plan_from_ideas(self, trip_id: str) -> dict:
        trip = self.trips[trip_id]
        cards = self.idea_cards[trip_id]
        place_titles = [card.title for card in cards if card.kind in {"地点", "待归类"}][:3]
        pace_cards = [card.title for card in cards if card.kind == "节奏"][:2]
        budget_cards = [card.title for card in cards if card.kind == "预算"][:1]
        first_place = place_titles[0] if place_titles else "目的地核心区域"
        second_place = place_titles[1] if len(place_titles) > 1 else "轻松散步和自由时间"
        return {
            "title": f"{trip.destination}行程草案",
            "status": "草案",
            "overview": "围绕核心景点安排慢走和当地用餐，每天保留自由调整空间。",
            "constraints_met": ["轻松节奏", "减少跨区域往返", "优先安排已提到的地点"],
            "pending_items": ["住宿区域和具体酒店", "餐厅与预约情况", "天气对应的衣物准备"],
            "risks": ["周末景点可能拥挤，建议避开热门时段"],
            "preparation": {
                "clothing": [{"title": "根据天气准备衣物", "body": "温度和降雨待确认，建议准备舒适鞋和方便增减的外套。", "status": "待确认"}],
                "accommodation": [{"title": "住宿区域待确认", "body": "建议选择靠近主要景点且方便公共交通的区域。", "status": "待确认"}],
            },
            "days": [
                {
                    "label": "Day 1",
                    "date": "出发日",
                    "theme": "抵达与西湖慢走",
                    "items": [
                        {
                            "time": "10:00",
                            "title": first_place,
                            "location": trip.destination,
                            "duration": "2-3 小时",
                            "reason": "优先满足对话里明确提到的地点需求。",
                            "notes": "具体交通和预约信息后续继续确认。",
                            "satisfies": ["地点偏好"],
                            "category": "activity",
                        },
                        {
                            "time": "12:30",
                            "title": "当地特色午餐",
                            "location": "第一处景点附近",
                            "duration": "1 小时",
                            "reason": "在景点附近用餐，减少往返移动。",
                            "notes": "具体菜品、忌口和预算待确认。",
                            "satisfies": budget_cards or ["预算可控"],
                            "category": "meal",
                            "meal": {
                                "meal_type": "午餐",
                                "recommendation": "当地特色菜（待确认）",
                                "cuisine": "当地菜",
                                "budget": "预算待确认",
                                "reservation": "待确认",
                            },
                        },
                        {
                            "time": "15:00",
                            "title": second_place,
                            "location": "同区域附近",
                            "duration": "2 小时",
                            "reason": "保持轻松节奏，减少来回移动。",
                            "notes": "可按当天体力临时调整。",
                            "satisfies": pace_cards or ["轻松节奏"],
                            "category": "activity",
                        },
                    ],
                },
                {
                    "label": "Day 2",
                    "date": "第二天",
                    "theme": "茶园与自由时间",
                    "items": [
                        {
                            "time": "10:30",
                            "title": "补充候选地点",
                            "location": "待确认",
                            "duration": "半天",
                            "reason": "保留弹性，等待你继续补充想法。",
                            "notes": "Agent 会根据新消息继续调整下一版。",
                            "satisfies": budget_cards or ["预算可控"],
                            "category": "activity",
                        }
                    ],
                },
            ],
        }

    @staticmethod
    def _is_summary_command(body: str) -> bool:
        return any(keyword in body for keyword in ("整理", "总结", "需求", "偏好", "冲突", "缺失"))

    def _seed_messages(self) -> list[Message]:
        return [
            Message(
                id="m1",
                sender=USERS["u1"],
                sender_type="user",
                body="我 8 月底想去杭州，安排两晚，节奏别太赶。",
                created_at="19:10",
            ),
            Message(
                id="m2",
                sender=USERS["agent"],
                sender_type="agent",
                body="收到，我先按轻松的 3 天 2 晚来整理。",
                created_at="19:11",
                agent_card=AgentCard(
                    kind="requirement-summary",
                    title="旅行框架已经记下",
                    summary="杭州、两晚、轻松节奏已经进入计划约束。",
                    bullets=["8 月底出发", "安排两晚", "避免行程太赶", "还需要预算和必去地点"],
                    actions=[AgentAction(label="查看旅行想法", target="board")],
                ),
            ),
            Message(
                id="m3",
                sender=USERS["u1"],
                sender_type="user",
                body="我想去西湖和茶园，预算控制在人均 1500 左右。",
                created_at="19:12",
            ),
            Message(
                id="m4",
                sender=USERS["agent"],
                sender_type="agent",
                body="信息够用了，我生成了第一版行程。",
                created_at="19:16",
                agent_card=AgentCard(
                    kind="itinerary-draft",
                    title="杭州 3 天 2 晚初版行程",
                    summary="西湖、龙井茶园、运河街区，整体偏轻松。",
                    bullets=["每天 2-3 个主要安排", "下午保留休息时间", "预算按人均 1500 控制"],
                    actions=[AgentAction(label="查看行程", target="itinerary")],
                ),
            ),
        ]

    def _seed_idea_cards(self) -> list[IdeaCard]:
        return [
            IdeaCard(
                id="idea-place-1",
                kind="地点",
                title="西湖傍晚散步",
                body="第一天傍晚去断桥和白堤，避开中午高温，也留出自由时间。",
                author="你",
                status="已采纳",
                rotation=IDEA_ROTATIONS[0],
            ),
            IdeaCard(
                id="idea-place-2",
                kind="地点",
                title="龙井茶园",
                body="安排半天慢一点的茶园体验，不要只打卡拍照。",
                author="对话",
                status="候选",
                rotation=IDEA_ROTATIONS[1],
            ),
            IdeaCard(
                id="idea-budget-1",
                kind="预算",
                title="人均 1500 左右",
                body="住宿、餐饮和交通都按轻松但不奢侈来筛选。",
                author="对话",
                status="约束",
                rotation=IDEA_ROTATIONS[2],
            ),
            IdeaCard(
                id="idea-pace-1",
                kind="节奏",
                title="不要每天太赶",
                body="每天最多 2-3 个主要安排，下午保留休息窗口。",
                author="你",
                status="已采纳",
                rotation=IDEA_ROTATIONS[3],
            ),
            IdeaCard(
                id="idea-taboo-1",
                kind="禁忌",
                title="避免连续长距离移动",
                body="Agent 提醒：如果茶园、运河街区和西湖分散安排，会和轻松旅行目标冲突。",
                author="Agent",
                status="冲突提醒",
                rotation=IDEA_ROTATIONS[4],
            ),
        ]

    def _seed_plan(self) -> TripPlan:
        return TripPlan(
            id="plan-v1",
            title="杭州 3 天 2 晚轻松行程",
            status="草案",
            days=[
                TripDay(
                    id="day-1",
                    label="Day 1",
                    items=[
                        TripItem(
                            id="item-1",
                            time="14:00",
                            title="抵达杭州并入住",
                            location="湖滨商圈附近",
                            duration="1.5 小时",
                            reason="方便晚间步行到西湖，也便于第二天出发。",
                            notes="住宿预算还需要继续确认。",
                            satisfies=["交通便利", "轻松节奏"],
                        ),
                        TripItem(
                            id="item-2",
                            time="17:00",
                            title="西湖傍晚散步",
                            location="断桥 - 白堤",
                            duration="2 小时",
                            reason="符合轻松节奏，也满足西湖需求。",
                            notes="避开中午高温和人流。",
                            satisfies=["西湖", "轻松节奏"],
                        ),
                    ],
                ),
                TripDay(
                    id="day-2",
                    label="Day 2",
                    items=[
                        TripItem(
                            id="item-3",
                            time="10:00",
                            title="龙井茶园",
                            location="龙井村",
                            duration="3 小时",
                            reason="满足茶园偏好，适合慢节奏体验。",
                            notes="建议提前确认是否需要预约体验。",
                            satisfies=["茶园体验", "预算可控"],
                        )
                    ],
                ),
            ],
        )


def _now_label() -> str:
    return datetime.now().strftime("%H:%M")


def _normalize_idea_text(value: str) -> str:
    return "".join(value.lower().split())


store = DemoStore()
