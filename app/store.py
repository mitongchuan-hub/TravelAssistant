from __future__ import annotations

import os
import sys
from copy import deepcopy
from datetime import datetime
from functools import wraps
from threading import RLock
from itertools import count

from .agent_client import AgentReply, generate_agent_reply
from .models import (
    AgentAction,
    AgentCard,
    IdeaCard,
    Message,
    MealRecommendation,
    PlanVersion,
    PreparationItem,
    Trip,
    TripDay,
    TripItem,
    TripPlan,
    TravelPreparation,
    User,
)
from .persistence import CURRENT_SCHEMA_VERSION, load_snapshot, restore_state, save_snapshot, serialize_state

USERS = {
    "u1": User(id="u1", name="林夏", initials="林"),
    "agent": User(id="agent", name="旅行规划 Agent", initials="AI"),
}

_id_counter = count(100)

MAX_IDEA_CARDS = 8
IDEA_ROTATIONS = ["-1.4deg", "1deg", "0.6deg", "-0.8deg", "1.3deg", "0.8deg", "-0.7deg", "1.1deg"]


def _state_locked(method):
    """Keep in-memory mutations and their persisted snapshot in one critical section."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


def _meal_from_raw(raw: object) -> MealRecommendation | None:
    if not isinstance(raw, dict):
        return None
    return MealRecommendation(
        meal_type=str(raw.get("meal_type") or "用餐"),
        recommendation=str(raw.get("recommendation") or "当地特色餐食待确认"),
        cuisine=str(raw.get("cuisine") or "当地菜"),
        budget=str(raw.get("budget") or "预算待确认"),
        reservation=str(raw.get("reservation") or "待确认"),
    )


def _preparation_from_raw(raw: object) -> TravelPreparation:
    if not isinstance(raw, dict):
        return TravelPreparation()

    def items(key: str) -> list[PreparationItem]:
        values = raw.get(key)
        if not isinstance(values, list):
            return []
        return [
            PreparationItem(
                title=str(item.get("title") or "待确认事项"),
                body=str(item.get("body") or "可以在生成行程后继续补充。"),
                status=str(item.get("status") or "待确认"),
            )
            for item in values[:8]
            if isinstance(item, dict)
        ]

    return TravelPreparation(clothing=items("clothing"), accommodation=items("accommodation"))


class DemoStore:
    def __init__(self, db_path: str | None = None, persistence_enabled: bool | None = None) -> None:
        self.db_path = db_path or os.getenv("TRAVEL_DB_PATH", ".cache/travelassistant.sqlite3")
        self.persistence_enabled = self._resolve_persistence_enabled(persistence_enabled)
        self._lock = RLock()
        self._agent_locks: dict[str, RLock] = {}
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
        self.plan_versions: dict[str, list[PlanVersion]] = {"trip-hangzhou": []}
        self._save_plan_version("trip-hangzhou", "初版行程草案")
        self.idea_cards: dict[str, list[IdeaCard]] = {"trip-hangzhou": self._seed_idea_cards()}
        self._load_persisted_state()

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
        global _id_counter
        _id_counter = count(restore_state(self, snapshot, USERS))
        removed_placeholder = False
        for trip_id, cards in self.idea_cards.items():
            filtered_cards = [
                card for card in cards
                if not (
                    card.author == "Agent"
                    and card.title == "从一句话开始"
                    and card.body == "Agent 会在对话中把地点、预算、节奏和禁忌整理成可执行的旅行约束。"
                )
            ]
            removed_placeholder = removed_placeholder or len(filtered_cards) != len(cards)
            self.idea_cards[trip_id] = filtered_cards
        if legacy_snapshot or removed_placeholder:
            self._persist()
        # Background tasks cannot survive a process restart. Keep the user's text
        # and replace interrupted placeholders with the existing failure response.
        for trip_id, messages in self.messages.items():
            for message in list(messages):
                if message.status == "thinking":
                    self.fail_agent_task(trip_id, message.id)

    @_state_locked
    def _persist(self) -> None:
        if self.persistence_enabled:
            save_snapshot(self.db_path, serialize_state(self, USERS))

    def list_trips(self, owner_id: str | None = None) -> list[Trip]:
        trips = list(self.trips.values())
        if owner_id is not None:
            trips = [trip for trip in trips if trip.owner.id == owner_id]
        return trips

    def get_trip(self, trip_id: str) -> Trip:
        return self.trips[trip_id]

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
        versions = self.plan_versions[trip_id]
        plan_items = sum(len(day.items) for day in plan.days)
        latest_version = versions[-1].label if versions else "未生成"
        latest_change = versions[-1].change_summary if versions else "等待 Agent 生成第一版"
        return {
            "message_count": len(self.messages[trip_id]),
            "idea_count": len(cards),
            "constraint_count": sum(card.kind != "待归类" for card in cards),
            "reminder_count": sum(card.kind == "禁忌" or card.status == "冲突提醒" for card in cards),
            "plan_day_count": len(plan.days),
            "plan_item_count": plan_items,
            "version_count": len(versions),
            "latest_version": latest_version,
            "latest_change": latest_change,
        }

    @_state_locked
    def create_trip(self, destination: str, date_range: str, budget: str, style: str, note: str, owner: User | None = None) -> Trip:
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
        )
        self.trips[trip_id] = trip
        self.messages[trip_id] = [
            Message(
                id=f"m-{next(_id_counter)}",
                sender=USERS["agent"],
                sender_type="agent",
                body="旅行已经建好。告诉我你最在意的时间、预算、地点或节奏，我会边聊边整理。",
                created_at=_now_label(),
                agent_card=AgentCard(
                    kind="missing-info",
                    title="先从最确定的事聊起",
                    summary="不必一次说完整，我会在每轮对话后更新旅行想法。",
                    bullets=["大概什么时候出发", "预算范围", "最想去的地方", "不想接受的安排"],
                    actions=[AgentAction(label="查看旅行想法", target="board")],
                ),
            )
        ]
        self.plans[trip_id] = TripPlan(id="empty", title="还没有行程计划", status="未生成", days=[])
        self.plan_versions[trip_id] = []
        self.idea_cards[trip_id] = []
        self._persist()
        return trip

    @_state_locked
    def clear_chat(self, trip_id: str) -> None:
        self.messages[trip_id] = []
        self.trips[trip_id].last_activity = "聊天记录已清空"
        self._persist()

    @_state_locked
    def add_user_message(self, trip_id: str, body: str, sender_id: str = "u1", process_agent: bool = True) -> Message:
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
            thinking = self.start_agent_task(trip_id) if process_agent else None
        if thinking:
            try:
                self.finish_agent_task(trip_id, clean_body, sender.id, thinking.id)
            except Exception:
                self.fail_agent_task(trip_id, thinking.id)
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
            self.trips[trip_id].last_activity = "Agent 正在整理旅行信息"
            self._persist()
            return thinking_message

    def _agent_lock(self, trip_id: str):
        with self._lock:
            return self._agent_locks.setdefault(trip_id, RLock())

    def _generate_reply(self, trip_id: str, body: str, thinking_message_id: str | None = None) -> AgentReply | None:
        if self._is_summary_command(body) and not self._is_plan_command(body):
            return None
        with self._lock:
            messages = self.messages[trip_id]
            if thinking_message_id:
                index = next(i for i, message in enumerate(messages) if message.id == thinking_message_id)
                messages = messages[:index]
            context = deepcopy({
                "trip": self.trips[trip_id],
                "messages": [message for message in messages if message.status == "sent"],
                "idea_cards": self.idea_cards[trip_id],
                "plan": self.plans[trip_id],
                "plan_versions": self.plan_versions[trip_id],
                "metrics": self.trip_metrics(trip_id),
            })
        # Network calls must never hold the global store lock.
        # A first draft can still be created from the stored ideas when the provider
        # times out or returns malformed structured output. Never replace a confirmed plan.
        planning_requested = self._is_plan_command(body)
        reply = generate_agent_reply(
            user_body=body,
            planning_mode=planning_requested,
            **context,
        )
        if (
            not planning_requested
            and reply
            and any(action.target == "generate" for action in reply.card.actions)
        ):
            # Let the model classify a semantic confirmation such as “出一版方案吧”.
            # Only after that classification do we run the tool and final-plan pass.
            planning_reply = generate_agent_reply(
                user_body=body,
                planning_mode=True,
                **context,
            )
            if planning_reply is not None:
                reply = planning_reply
        if (
            reply
            and reply.plan is None
            and reply.card.title in {"模型调用暂时失败", "模型响应超时", "模型服务暂时限流"}
            and (not self._is_plan_command(body) or self.plans[trip_id].status != "已确认")
        ):
            return None
        return reply

    def finish_agent_task(self, trip_id: str, body: str, sender_id: str, thinking_message_id: str | None = None) -> None:
        with self._agent_lock(trip_id):
            with self._lock:
                if thinking_message_id and not any(
                    message.id == thinking_message_id and message.status == "thinking"
                    for message in self.messages[trip_id]
                ):
                    return
            reply = self._generate_reply(trip_id, body.strip(), thinking_message_id)
            with self._lock:
                owner = self.trips[trip_id].owner
                sender = USERS.get(sender_id, owner)
                if sender.id != owner.id:
                    sender = owner
                self._append_agent_reply_for_message(trip_id, body.strip(), sender, reply)
                if thinking_message_id:
                    # Replace in place so a slow reply stays next to its user's message.
                    completed = self.messages[trip_id].pop()
                    for index, message in enumerate(self.messages[trip_id]):
                        if message.id == thinking_message_id:
                            self.messages[trip_id][index] = completed
                            break
                    else:
                        self.messages[trip_id].append(completed)
                self._persist()

    def fail_agent_task(self, trip_id: str, thinking_message_id: str | None = None) -> None:
        with self._lock:
            error_message = Message(
                id=f"m-{next(_id_counter)}",
                sender=USERS["agent"],
                sender_type="agent",
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

    def _append_agent_reply_for_message(self, trip_id: str, clean_body: str, sender: User, reply: AgentReply | None) -> None:
        instruction = clean_body.strip()
        previous_versions = len(self.plan_versions[trip_id])
        if self._is_plan_command(instruction):
            self.messages[trip_id].append(self._agent_plan_message(trip_id, instruction, sender, reply))
        else:
            self.messages[trip_id].append(self._agent_reply(trip_id, instruction, sender, reply))
        if len(self.plan_versions[trip_id]) > previous_versions:
            self.trips[trip_id].last_activity = "Agent 已生成行程草案"
        else:
            self.trips[trip_id].last_activity = "Agent 已更新需求摘要"

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
            body="Agent 会结合当前对话判断它属于地点、预算、节奏还是禁忌。",
            author="你",
            status="待归类",
            rotation=rotation,
        )
        self.idea_cards[trip_id].append(card)
        self.idea_cards[trip_id] = self.idea_cards[trip_id][-MAX_IDEA_CARDS:]
        self.trips[trip_id].last_activity = "Agent 正在归类新的旅行想法"
        self._persist()
        return card

    @_state_locked
    def request_revision(self, trip_id: str, item_id: str | None = None, feedback: str = "", sender: User | None = None) -> None:
        sender = sender or USERS["u1"]
        clean_feedback = feedback.strip() or "希望这项安排更轻松一点"
        item_title = self._item_title(trip_id, item_id)
        self._add_revision_idea(trip_id, item_title, clean_feedback, sender)
        change_summary = self._revision_change_summary(trip_id, item_title, clean_feedback)
        self._apply_agent_plan(trip_id, self._fallback_plan_from_ideas(trip_id), change_summary)
        self.plans[trip_id].status = "草案"
        self.trips[trip_id].status = "草案"
        self.trips[trip_id].last_activity = "Agent 已根据反馈生成新草案"
        self.messages[trip_id].append(
            Message(
                id=f"m-{next(_id_counter)}",
                sender=USERS["agent"],
                sender_type="agent",
                body="我已根据这条反馈生成新草案。",
                created_at=_now_label(),
                agent_card=AgentCard(
                    kind="revision",
                    title="已生成新草案",
                    summary=change_summary,
                    bullets=[f"反馈项：{item_title}", clean_feedback[:32], "旧版本已保留，可在版本记录恢复"],
                    actions=[AgentAction(label="查看行程", target="itinerary")],
                ),
            )
        )
        self._persist()

    def _item_title(self, trip_id: str, item_id: str | None) -> str:
        if item_id:
            for day in self.plans[trip_id].days:
                for item in day.items:
                    if item.id == item_id:
                        return item.title
        return "当前行程"

    def _add_revision_idea(self, trip_id: str, item_title: str, feedback: str, sender: User) -> None:
        kind = self._feedback_kind(feedback)
        rotation = IDEA_ROTATIONS[len(self.idea_cards[trip_id]) % len(IDEA_ROTATIONS)]
        self.idea_cards[trip_id].append(
            IdeaCard(
                id=f"idea-{next(_id_counter)}",
                kind=kind,
                title=f"调整：{item_title}"[:24],
                body=feedback,
                author="你的反馈",
                status="修改反馈",
                rotation=rotation,
            )
        )
        self.idea_cards[trip_id] = self.idea_cards[trip_id][-MAX_IDEA_CARDS:]

    @staticmethod
    def _feedback_kind(feedback: str) -> str:
        if any(word in feedback for word in ("预算", "贵", "便宜", "人均")):
            return "预算"
        if any(word in feedback for word in ("不要", "不能", "太赶", "太累", "换")):
            return "禁忌"
        if any(word in feedback for word in ("轻松", "慢", "休息", "节奏")):
            return "节奏"
        return "待归类"

    def _revision_change_summary(self, trip_id: str, item_title: str, feedback: str) -> str:
        version_number = len(self.plan_versions.get(trip_id, [])) + 1
        return f"第 {version_number} 版：根据对{item_title}的反馈调整：{feedback[:28]}"

    def generate_plan(self, trip_id: str) -> None:
        thinking = self.start_agent_task(trip_id)
        try:
            self.finish_agent_task(
                trip_id, "请根据当前对话和想法墙，生成一版结构化旅行行程。",
                self.trips[trip_id].owner.id, thinking.id,
            )
        except Exception:
            self.fail_agent_task(trip_id, thinking.id)

    @_state_locked
    def confirm_plan(self, trip_id: str) -> None:
        plan = self.plans[trip_id]
        if not plan.days:
            return
        plan.status = "已确认"
        self.trips[trip_id].status = "已确认"
        self.trips[trip_id].last_activity = "你已确认当前行程"
        self._save_plan_version(trip_id, "确认当前行程")
        self.messages[trip_id].append(
            Message(
                id=f"m-{next(_id_counter)}",
                sender=USERS["agent"],
                sender_type="agent",
                body="当前行程已确认，我会把后续修改作为新版本处理。",
                created_at=_now_label(),
                agent_card=AgentCard(
                    kind="itinerary-draft",
                    title="行程已确认",
                    summary="这版计划会作为当前确认版本保留，后续修改会生成新草案。",
                    bullets=["保留当前安排", "后续反馈进入新版本", "可继续查看行程"],
                    actions=[AgentAction(label="查看行程", target="itinerary")],
                ),
            )
        )
        self._persist()

    @_state_locked
    def restore_plan_version(self, trip_id: str, version_id: str) -> bool:
        version = next((item for item in self.plan_versions[trip_id] if item.id == version_id), None)
        if not version:
            return False
        self.plans[trip_id] = TripPlan(
            id=f"plan-{next(_id_counter)}",
            title=version.title,
            status="草案",
            days=version.days,
            preparation=version.preparation,
            overview=version.overview,
            constraints_met=version.constraints_met,
            pending_items=version.pending_items,
            risks=version.risks,
        )
        self.trips[trip_id].status = "草案"
        self.trips[trip_id].last_activity = f"已回退到 {version.label}"
        self._save_plan_version(trip_id, f"从 {version.label} 恢复为新草案")
        self._persist()
        return True

    def _agent_plan_message(self, trip_id: str, instruction: str, sender: User | None = None, reply: AgentReply | None = None) -> Message:
        change_summary = self._plan_change_summary(trip_id)
        if reply:
            self._add_agent_idea_cards(trip_id, reply.idea_cards, sender)
            if reply.plan:
                self._apply_agent_plan(trip_id, reply.plan, change_summary)
            card = reply.card
            body = reply.body
        else:
            self._apply_agent_plan(trip_id, self._fallback_plan_from_ideas(trip_id), change_summary)
            card = AgentCard(
                kind="itinerary-draft",
                title="第一版行程已生成",
                summary=change_summary,
                bullets=self._plan_change_bullets(trip_id),
                actions=[AgentAction(label="查看行程", target="itinerary")],
            )
            body = "我已生成行程草案，也整理了这一版的变化。"
        return Message(
            id=f"m-{next(_id_counter)}",
            sender=USERS["agent"],
            sender_type="agent",
            body=body,
            created_at=_now_label(),
            agent_card=card,
        )

    def _agent_reply(self, trip_id: str, body: str, sender: User | None = None, llm_reply: AgentReply | None = None) -> Message:
        if self._is_summary_command(body):
            return self._local_summary_reply(trip_id)

        if llm_reply:
            self._add_agent_idea_cards(trip_id, llm_reply.idea_cards, sender)
            if llm_reply.plan:
                self._apply_agent_plan(trip_id, llm_reply.plan)
            return Message(
                id=f"m-{next(_id_counter)}",
                sender=USERS["agent"],
                sender_type="agent",
                body=llm_reply.body,
                created_at=_now_label(),
                agent_card=llm_reply.card,
            )

        lower_body = body.lower()
        if "预算" in body or "钱" in body or "贵" in body:
            self._add_agent_idea_cards(
                trip_id,
                [{"kind": "预算", "title": "预算约束", "body": body.strip(), "status": "约束"}],
                sender,
            )
            card = AgentCard(
                kind="requirement-summary",
                title="预算约束已更新",
                summary="我会把预算作为筛选住宿、餐饮和交通方式的重要约束。",
                bullets=["优先公共交通和步行友好区域", "避免高价网红餐厅", "保留一段弹性支出"],
                actions=[AgentAction(label="查看旅行想法", target="board")],
            )
        elif "改" in body or "不要" in body or "太累" in body or "累" in body:
            self._add_agent_idea_cards(
                trip_id,
                [{"kind": "节奏", "title": "节奏提醒", "body": body.strip(), "status": "冲突提醒"}],
                sender,
            )
            card = AgentCard(
                kind="conflict",
                title="发现一个节奏风险",
                summary="当前计划需要避免连续长距离移动，否则会和轻松旅行目标冲突。",
                bullets=["每天控制 2-3 个主要安排", "下午保留休息窗口", "把同区域地点合并"],
                actions=[AgentAction(label="查看行程", target="itinerary")],
            )
        elif "plan" in lower_body or "行程" in body or "安排" in body:
            card = AgentCard(
                kind="itinerary-draft",
                title="可以生成下一版行程",
                summary="我已经有足够信息生成结构化计划。",
                bullets=["按天安排", "标注地点与停留时间", "说明每个安排对应的旅行偏好"],
                actions=[AgentAction(label="查看行程", target="itinerary")],
            )
        else:
            self._add_agent_idea_cards(
                trip_id,
                [{"kind": "待归类", "title": "新的旅行想法", "body": body.strip(), "status": "待归类"}],
                sender,
            )
            card = AgentCard(
                kind="missing-info",
                title="我记录了一条新偏好",
                summary="继续补充预算、时间、忌口和不能接受的安排，会让计划更稳定。",
                bullets=["这项偏好是否必须满足", "它可能影响哪一天", "还有没有相关限制"],
                actions=[AgentAction(label="查看旅行想法", target="board")],
            )
        return Message(
            id=f"m-{next(_id_counter)}",
            sender=USERS["agent"],
            sender_type="agent",
            body="我已经把这条消息纳入计划约束。",
            created_at=_now_label(),
            agent_card=card,
        )

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
                    author=source,
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
        self.idea_cards[trip_id] = self.idea_cards[trip_id][-MAX_IDEA_CARDS:]

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
            if card.kind != kind:
                continue
            if kind == "预算":
                return index
            if _normalize_idea_text(card.title) == normalized_title:
                return index
            if _normalize_idea_text(card.body) == normalized_body:
                return index
        return None

    def _apply_agent_plan(self, trip_id: str, raw_plan: dict, change_summary: str | None = None) -> None:
        days = []
        for day_index, raw_day in enumerate(raw_plan.get("days", []), start=1):
            items = []
            for item_index, raw_item in enumerate(raw_day.get("items", []), start=1):
                items.append(
                    TripItem(
                        id=f"item-{next(_id_counter)}",
                        time=str(raw_item.get("time") or "待定"),
                        title=str(raw_item.get("title") or "待定安排"),
                        location=str(raw_item.get("location") or "地点待定"),
                        duration=str(raw_item.get("duration") or "时长待定"),
                        reason=str(raw_item.get("reason") or "根据你的旅行偏好安排。"),
                        notes=str(raw_item.get("notes") or "可以继续在对话里调整。"),
                        satisfies=[str(item) for item in raw_item.get("satisfies", [])],
                        category=str(raw_item.get("category") or "activity"),
                        meal=_meal_from_raw(raw_item.get("meal")),
                    )
                )
            if items:
                days.append(TripDay(
                    id=f"day-{day_index}-{next(_id_counter)}",
                    label=str(raw_day.get("label") or f"Day {day_index}"),
                    date=str(raw_day.get("date") or ""),
                    theme=str(raw_day.get("theme") or ""),
                    items=items,
                ))
        if days:
            self.plans[trip_id] = TripPlan(
                id=f"plan-{next(_id_counter)}",
                title=str(raw_plan.get("title") or f"{self.trips[trip_id].destination}行程草案"),
                status=str(raw_plan.get("status") or "草案"),
                days=days,
                preparation=_preparation_from_raw(raw_plan.get("preparation")),
                overview=str(raw_plan.get("overview") or ""),
                constraints_met=[str(item) for item in raw_plan.get("constraints_met", [])],
                pending_items=[str(item) for item in raw_plan.get("pending_items", [])],
                risks=[str(item) for item in raw_plan.get("risks", [])],
            )
            self.trips[trip_id].status = self.plans[trip_id].status
            self._save_plan_version(trip_id, change_summary or self._plan_change_summary(trip_id))

    def _plan_change_summary(self, trip_id: str) -> str:
        version_number = len(self.plan_versions.get(trip_id, [])) + 1
        if version_number <= 1:
            return "第一版：按对话和想法墙生成基础行程"
        latest_cards = self.idea_cards[trip_id][-2:]
        if latest_cards:
            names = "、".join(card.title for card in latest_cards)
            return f"第 {version_number} 版：根据{names}等新想法调整"
        return f"第 {version_number} 版：根据最新反馈调整行程"

    def _plan_change_bullets(self, trip_id: str) -> list[str]:
        latest_cards = self.idea_cards[trip_id][-3:]
        bullets = [f"纳入：{card.title}" for card in latest_cards[:2]]
        bullets.append("保留轻松节奏和预算约束")
        bullets.append("后续反馈会进入新版本")
        return bullets[:4]

    def _save_plan_version(self, trip_id: str, change_summary: str) -> None:
        plan = self.plans[trip_id]
        if not plan.days:
            return
        self.plan_versions[trip_id].append(self._version_from_plan(trip_id, plan, change_summary))

    def _version_from_plan(self, trip_id: str, plan: TripPlan, change_summary: str) -> PlanVersion:
        version_number = len(self.plan_versions.get(trip_id, [])) + 1
        return PlanVersion(
            id=f"version-{next(_id_counter)}",
            label=f"v{version_number}",
            title=plan.title,
            status=plan.status,
            created_at=_now_label(),
            change_summary=change_summary,
            days=plan.days,
            preparation=plan.preparation,
            overview=plan.overview,
            constraints_met=plan.constraints_met,
            pending_items=plan.pending_items,
            risks=plan.risks,
        )

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
    def _is_plan_command(body: str) -> bool:
        return any(keyword in body for keyword in (
            "生成", "行程", "计划", "安排", "第一版", "出一版", "出方案", "方案",
            "现在出", "现在生成", "开始生成", "可以生成", "做一版",
        ))

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
