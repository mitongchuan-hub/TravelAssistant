from __future__ import annotations

import os
import sys
from datetime import datetime
from threading import RLock
from itertools import count

from .agent_client import generate_agent_reply
from .models import AgentAction, AgentCard, IdeaCard, MemberPreference, Message, PlanVersion, TripDay, TripGroup, TripItem, TripPlan, User
from .persistence import load_snapshot, restore_state, save_snapshot, serialize_state

USERS = {
    "u1": User(id="u1", name="林夏", initials="林"),
    "u2": User(id="u2", name="周予", initials="周"),
    "u3": User(id="u3", name="陈安", initials="陈"),
    "agent": User(id="agent", name="旅行规划 Agent", initials="AI"),
}

_id_counter = count(100)

MAX_IDEA_CARDS = 8
IDEA_ROTATIONS = ["-1.4deg", "1deg", "0.6deg", "-0.8deg", "1.3deg", "0.8deg", "-0.7deg", "1.1deg"]


class DemoStore:
    def __init__(self, db_path: str | None = None, persistence_enabled: bool | None = None) -> None:
        self.db_path = db_path or os.getenv("TRAVEL_DB_PATH", ".cache/travelassistant.sqlite3")
        self.persistence_enabled = self._resolve_persistence_enabled(persistence_enabled)
        self._lock = RLock()
        self.trips: dict[str, TripGroup] = {
            "trip-hangzhou": TripGroup(
                id="trip-hangzhou",
                destination="杭州周末旅行",
                date_range="8月24日 - 8月26日",
                budget="人均 1500",
                style="轻松、少排队、适合聊天",
                note="想轻松一点，少排队，多留聊天时间。",
                initiator=USERS["u1"],
                member_initials=["林", "周", "陈"],
                status="草案",
                last_activity="Agent 已生成初版行程",
            )
        }
        self.group_members: dict[str, list[str]] = {"trip-hangzhou": ["u1", "u2", "u3"]}
        self.messages: dict[str, list[Message]] = {"trip-hangzhou": self._seed_messages()}
        self.plans: dict[str, TripPlan] = {"trip-hangzhou": self._seed_plan()}
        self.plan_versions: dict[str, list[PlanVersion]] = {"trip-hangzhou": []}
        self._save_plan_version("trip-hangzhou", "初版行程草案")
        self.preferences: dict[str, list[MemberPreference]] = {"trip-hangzhou": self._seed_preferences()}
        self.idea_cards: dict[str, list[IdeaCard]] = {"trip-hangzhou": self._seed_idea_cards()}
        self.agent_steps = [
            {"label": "听大家说想法", "state": "done", "detail": "4 条偏好"},
            {"label": "整理偏好", "state": "done", "detail": "1 个提醒"},
            {"label": "安排行程", "state": "active", "detail": "第 1 版"},
            {"label": "等大家确认", "state": "idle", "detail": "继续聊"},
        ]
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
        global _id_counter
        _id_counter = count(restore_state(self, snapshot, USERS))

    def _persist(self) -> None:
        if self.persistence_enabled:
            save_snapshot(self.db_path, serialize_state(self, USERS))

    def list_trips(self) -> list[TripGroup]:
        return list(self.trips.values())

    def get_trip(self, trip_id: str) -> TripGroup:
        return self.trips[trip_id]

    def member_choices(self) -> list[User]:
        return [USERS["u1"], USERS["u2"], USERS["u3"]]

    def trip_member_choices(self, trip_id: str) -> list[User]:
        member_ids = self.group_members.get(trip_id) or ["u1"]
        return [USERS[user_id] for user_id in member_ids if user_id in USERS]

    def get_or_create_user(self, nickname: str) -> User:
        clean_name = nickname.strip()[:12] or "旅行成员"
        for user in USERS.values():
            if user.name == clean_name and user.id != "agent":
                return user
        user_id = f"u{next(_id_counter)}"
        user = User(id=user_id, name=clean_name, initials=clean_name[:1].upper())
        USERS[user_id] = user
        self._persist()
        return user

    def add_member(self, trip_id: str, user: User) -> None:
        member_ids = self.group_members.setdefault(trip_id, [])
        if user.id not in member_ids:
            member_ids.append(user.id)
        initials = [USERS[user_id].initials for user_id in member_ids if user_id in USERS]
        self.trips[trip_id].member_initials = initials
        if not any(preference.member.id == user.id for preference in self.preferences[trip_id]):
            self.preferences[trip_id].append(
                MemberPreference(member=user, known=[], missing=["预算", "出发时间", "想去的地方"], conflicts=[])
            )
        self._persist()

    def trip_metrics(self, trip_id: str) -> dict[str, int | str]:
        preferences = self.preferences[trip_id]
        plan = self.plans[trip_id]
        versions = self.plan_versions[trip_id]
        confirmed_count = sum(len(preference.known) for preference in preferences)
        missing_count = sum(len(preference.missing) for preference in preferences)
        conflict_count = sum(len(preference.conflicts) for preference in preferences)
        plan_items = sum(len(day.items) for day in plan.days)
        latest_version = versions[-1].label if versions else "未生成"
        latest_change = versions[-1].change_summary if versions else "等待 Agent 生成第一版"
        return {
            "message_count": len(self.messages[trip_id]),
            "idea_count": len(self.idea_cards[trip_id]),
            "confirmed_count": confirmed_count,
            "missing_count": missing_count,
            "conflict_count": conflict_count,
            "plan_day_count": len(plan.days),
            "plan_item_count": plan_items,
            "version_count": len(versions),
            "latest_version": latest_version,
            "latest_change": latest_change,
        }

    def create_trip(self, destination: str, date_range: str, budget: str, style: str, note: str, initiator: User | None = None) -> TripGroup:
        initiator = initiator or USERS["u1"]
        trip_id = f"trip-{next(_id_counter)}"
        trip = TripGroup(
            id=trip_id,
            destination=destination.strip() or "未命名旅行",
            date_range=date_range.strip() or "时间待定",
            budget=budget.strip() or "预算待定",
            style=style.strip() or "风格待定",
            note=note.strip(),
            initiator=initiator,
            member_initials=[initiator.initials],
            status="未生成",
            last_activity="旅行群已创建，等待成员补充需求",
        )
        self.trips[trip_id] = trip
        self.group_members[trip_id] = [initiator.id]
        self.messages[trip_id] = [
            Message(
                id=f"m-{next(_id_counter)}",
                sender=USERS["agent"],
                sender_type="agent",
                body="旅行群已创建。大家可以先自由聊预算、时间、想去的地方和不能接受的安排；需要我整理时，在群里发 @旅行助手 就能叫醒我。",
                created_at=_now_label(),
                agent_card=AgentCard(
                    kind="missing-info",
                    title="先自由聊，@我整理",
                    summary="普通聊天不会自动触发 Agent；发 @旅行助手 后，我再把大家的意见整理成计划约束。",
                    bullets=["预算范围", "出发时间", "必须去和不想去的地方", "需要整理时发 @旅行助手"],
                    actions=[AgentAction(label="查看成员偏好", target="members")],
                ),
            )
        ]
        self.plans[trip_id] = TripPlan(id="empty", title="还没有行程计划", status="未生成", days=[])
        self.plan_versions[trip_id] = []
        self.preferences[trip_id] = [
            MemberPreference(member=initiator, known=[], missing=["预算", "出发时间", "想去的地方"], conflicts=[])
        ]
        self.idea_cards[trip_id] = [
            IdeaCard(
                id=f"idea-{next(_id_counter)}",
                kind="节奏",
                title="先让大家说想法",
                body="Agent 会把地点、预算、节奏和禁忌自动归类，等信息足够后再生成行程草案。",
                author="AI",
                status="待补充",
                rotation=IDEA_ROTATIONS[0],
            )
        ]
        self._persist()
        return trip

    def add_user_message(self, trip_id: str, body: str, sender_id: str = "u1", process_agent: bool = True) -> Message:
        with self._lock:
            clean_body = body.strip()
            sender = USERS.get(sender_id, USERS["u1"])
            self.add_member(trip_id, sender)
            message = Message(
                id=f"m-{next(_id_counter)}",
                sender=sender,
                sender_type="user",
                body=clean_body,
                created_at=_now_label(),
            )
            self.messages[trip_id].append(message)
            if process_agent and self._should_trigger_agent(clean_body):
                self._append_agent_reply_for_message(trip_id, clean_body, sender)
            else:
                self.trips[trip_id].last_activity = f"{sender.name} 有新群聊消息，等待 Agent 整理"
            self._persist()
            return message

    def should_trigger_agent(self, body: str) -> bool:
        return self._should_trigger_agent(body.strip())

    def start_agent_task(self, trip_id: str, body: str, sender: User) -> Message:
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

    def finish_agent_task(self, trip_id: str, body: str, sender_id: str, thinking_message_id: str | None = None) -> None:
        with self._lock:
            sender = USERS.get(sender_id, USERS["u1"])
            if thinking_message_id:
                self.messages[trip_id] = [message for message in self.messages[trip_id] if message.id != thinking_message_id]
            else:
                self.messages[trip_id] = [message for message in self.messages[trip_id] if message.status != "thinking"]
            self._append_agent_reply_for_message(trip_id, body.strip(), sender)
            self._persist()

    def fail_agent_task(self, trip_id: str, thinking_message_id: str | None = None) -> None:
        with self._lock:
            error_message = Message(
                id=f"m-{next(_id_counter)}",
                sender=USERS["agent"],
                sender_type="agent",
                body="我刚刚整理失败了，可以稍后再 @旅行助手 试一次。",
                created_at=_now_label(),
                agent_card=AgentCard(
                    kind="missing-info",
                    title="Agent 暂时没整理成功",
                    summary="你的群聊消息已经保留，只是这次整理没有完成。",
                    bullets=["消息不会丢失", "稍后可以重新 @旅行助手", "也可以先继续补充旅行想法"],
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

    def _append_agent_reply_for_message(self, trip_id: str, clean_body: str, sender: User) -> None:
        instruction = self._strip_agent_mention(clean_body)
        if self._is_plan_command(instruction):
            self.messages[trip_id].append(self._agent_plan_message(trip_id, instruction, sender))
            self.trips[trip_id].status = "草案"
            self.trips[trip_id].last_activity = "Agent 已生成行程草案"
        else:
            self.messages[trip_id].append(self._agent_reply(trip_id, instruction, sender))
            self.trips[trip_id].last_activity = "Agent 已更新需求摘要"

    def summarize_chat(self, trip_id: str) -> Message:
        message = self._agent_reply(trip_id, "请整理最近群聊里的旅行偏好、冲突和缺失信息。", self._latest_user_sender(trip_id))
        self.messages[trip_id].append(message)
        self.trips[trip_id].last_activity = "Agent 已整理最近群聊"
        self._persist()
        return message

    def add_idea(self, trip_id: str, body: str) -> IdeaCard:
        rotation = IDEA_ROTATIONS[len(self.idea_cards[trip_id]) % len(IDEA_ROTATIONS)]
        card = IdeaCard(
            id=f"idea-{next(_id_counter)}",
            kind="待归类",
            title=body.strip(),
            body="Agent 会结合群聊上下文判断它属于地点、预算、节奏还是禁忌。",
            author=USERS["u1"].name,
            status="待归类",
            rotation=rotation,
        )
        self.idea_cards[trip_id].append(card)
        self.idea_cards[trip_id] = self.idea_cards[trip_id][-MAX_IDEA_CARDS:]
        self.trips[trip_id].last_activity = "Agent 正在归类新的旅行想法"
        self._persist()
        return card

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
                author=sender.name,
                status="修改反馈",
                rotation=rotation,
            )
        )
        self.idea_cards[trip_id] = self.idea_cards[trip_id][-MAX_IDEA_CARDS:]
        self._sync_preferences_from_ideas(trip_id, [self.idea_cards[trip_id][-1]])

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
        self.messages[trip_id].append(self._agent_plan_message(trip_id, "请根据当前群聊和想法墙，生成一版结构化旅行行程。", self._latest_user_sender(trip_id)))
        self.trips[trip_id].status = "草案"
        self.trips[trip_id].last_activity = "Agent 已生成行程草案"
        self._persist()

    def confirm_plan(self, trip_id: str) -> None:
        plan = self.plans[trip_id]
        if not plan.days:
            return
        plan.status = "已确认"
        self.trips[trip_id].status = "已确认"
        self.trips[trip_id].last_activity = "大家已确认当前行程"
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

    def restore_plan_version(self, trip_id: str, version_id: str) -> bool:
        version = next((item for item in self.plan_versions[trip_id] if item.id == version_id), None)
        if not version:
            return False
        self.plans[trip_id] = TripPlan(
            id=f"plan-{next(_id_counter)}",
            title=version.title,
            status="草案",
            days=version.days,
        )
        self.trips[trip_id].status = "草案"
        self.trips[trip_id].last_activity = f"已回退到 {version.label}"
        self._save_plan_version(trip_id, f"从 {version.label} 恢复为新草案")
        self._persist()
        return True

    def _agent_plan_message(self, trip_id: str, instruction: str, sender: User | None = None) -> Message:
        change_summary = self._plan_change_summary(trip_id)
        reply = generate_agent_reply(
            self.trips[trip_id],
            self.messages[trip_id],
            instruction or "请根据当前群聊和想法墙，生成一版结构化旅行行程。",
            idea_cards=self.idea_cards[trip_id],
            preferences=self.preferences[trip_id],
            plan=self.plans[trip_id],
            plan_versions=self.plan_versions[trip_id],
            metrics=self.trip_metrics(trip_id),
        )
        if reply:
            self._add_agent_idea_cards(trip_id, reply.idea_cards, sender)
            if reply.plan:
                self._apply_agent_plan(trip_id, reply.plan, change_summary)
            else:
                self._apply_agent_plan(trip_id, self._fallback_plan_from_ideas(trip_id), change_summary)
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

    def _agent_reply(self, trip_id: str, body: str, sender: User | None = None) -> Message:
        if self._is_summary_command(body):
            return self._local_summary_reply(trip_id)

        llm_reply = generate_agent_reply(
            self.trips[trip_id],
            self.messages[trip_id],
            body,
            idea_cards=self.idea_cards[trip_id],
            preferences=self.preferences[trip_id],
            plan=self.plans[trip_id],
            plan_versions=self.plan_versions[trip_id],
            metrics=self.trip_metrics(trip_id),
        )
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
                actions=[AgentAction(label="查看成员偏好", target="members")],
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
                bullets=["按天安排", "标注地点与停留时间", "说明每个安排满足谁的需求"],
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
                bullets=["谁提出了这个需求", "它影响哪一天", "是否和其他成员偏好冲突"],
                actions=[AgentAction(label="查看成员偏好", target="members")],
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

        applied_cards = []
        for raw_card in raw_cards:
            kind = str(raw_card.get("kind") or "待归类")
            title = str(raw_card.get("title") or "新的旅行想法").strip()
            body = str(raw_card.get("body") or title).strip()
            status = str(raw_card.get("status") or "已整理").strip()
            author = str(raw_card.get("author") or (sender.name if sender else "Agent")).strip()
            if not title or not body:
                continue
            existing_index = self._find_similar_idea_index(trip_id, kind, title, body)
            if existing_index is not None:
                previous = self.idea_cards[trip_id][existing_index]
                updated_card = IdeaCard(
                    id=previous.id,
                    kind=kind,
                    title=title,
                    body=body,
                    author=author if author != "Agent" else previous.author,
                    status=status,
                    rotation=previous.rotation,
                )
                self.idea_cards[trip_id][existing_index] = updated_card
                applied_cards.append(updated_card)
                continue
            rotation = IDEA_ROTATIONS[len(self.idea_cards[trip_id]) % len(IDEA_ROTATIONS)]
            new_card = IdeaCard(
                id=f"idea-{next(_id_counter)}",
                kind=kind,
                title=title,
                body=body,
                author=author,
                status=status,
                rotation=rotation,
            )
            self.idea_cards[trip_id].append(new_card)
            applied_cards.append(new_card)
        self.idea_cards[trip_id] = self.idea_cards[trip_id][-MAX_IDEA_CARDS:]
        self._sync_preferences_from_ideas(trip_id, applied_cards)

    def _sync_preferences_from_ideas(self, trip_id: str, cards: list[IdeaCard]) -> None:
        if not cards:
            return
        self._ensure_member_preferences(trip_id)

        preferences_by_name = {preference.member.name: preference for preference in self.preferences[trip_id]}
        updated_by_name = {}
        for card in cards:
            member = self._member_for_author(card.author)
            preference = preferences_by_name.get(member.name) or MemberPreference(
                member=member,
                known=[],
                missing=["预算", "出发时间", "想去的地方"],
                conflicts=[],
            )
            known = list(preference.known)
            missing = list(preference.missing)
            conflicts = list(preference.conflicts)
            summary = self._preference_summary_from_card(card)
            if card.kind == "禁忌" or card.status == "冲突提醒":
                if summary not in conflicts:
                    conflicts.append(summary)
            elif summary not in known:
                known.append(summary)
            missing = [item for item in missing if item not in self._missing_labels_for_kind(card.kind)]
            updated_by_name[member.name] = MemberPreference(
                member=member,
                known=known[-8:],
                missing=missing,
                conflicts=conflicts[-4:],
            )

        self.preferences[trip_id] = [updated_by_name.get(preference.member.name, preference) for preference in self.preferences[trip_id]]

    def _ensure_member_preferences(self, trip_id: str) -> None:
        existing = {preference.member.id: preference for preference in self.preferences.get(trip_id, [])}
        preferences = []
        for member in self.member_choices():
            preferences.append(
                existing.get(
                    member.id,
                    MemberPreference(member=member, known=[], missing=["预算", "出发时间", "想去的地方"], conflicts=[]),
                )
            )
        self.preferences[trip_id] = preferences

    def _member_for_author(self, author: str) -> User:
        for member in self.member_choices():
            if author == member.name or author == member.initials:
                return member
        return USERS["u1"]

    def _latest_user_sender(self, trip_id: str) -> User:
        for message in reversed(self.messages[trip_id]):
            if message.sender_type == "user":
                return message.sender
        return USERS["u1"]

    def _local_summary_reply(self, trip_id: str) -> Message:
        self._ensure_member_preferences(trip_id)
        recent_user_messages = [message for message in self.messages[trip_id][-12:] if message.sender_type == "user"]
        raw_cards = []
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
                    "author": message.sender.name,
                }
            )
        self._add_agent_idea_cards(trip_id, raw_cards)
        bullets = self._summary_bullets_by_member(trip_id)
        return Message(
            id=f"m-{next(_id_counter)}",
            sender=USERS["agent"],
            sender_type="agent",
            body="我按成员整理了最近的旅行需求。",
            created_at=_now_label(),
            agent_card=AgentCard(
                kind="requirement-summary",
                title="按成员整理好了",
                summary="我把最近聊天拆成预算、地点、节奏和不能接受的安排。",
                bullets=bullets,
                actions=[AgentAction(label="查看成员偏好", target="members")],
            ),
        )

    def _summary_bullets_by_member(self, trip_id: str) -> list[str]:
        bullets = []
        for preference in self.preferences[trip_id]:
            details = preference.known[-2:] + preference.conflicts[-1:]
            if details:
                bullets.append(f"{preference.member.name}：{'；'.join(details[:2])}")
        return bullets[:4] or ["先补充预算", "再确认想去地点", "最后处理不能接受的安排"]

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

    @staticmethod
    def _preference_summary_from_card(card: IdeaCard) -> str:
        if card.kind == "预算" or card.status == "冲突提醒":
            return card.body
        return card.title

    @staticmethod
    def _missing_labels_for_kind(kind: str) -> set[str]:
        return {
            "预算": {"预算", "预算范围"},
            "地点": {"想去的地方", "目的地"},
            "节奏": {"节奏", "旅行节奏"},
            "禁忌": {"不能接受", "忌口", "禁忌"},
        }.get(kind, set())

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
                        reason=str(raw_item.get("reason") or "根据群聊偏好安排。"),
                        notes=str(raw_item.get("notes") or "可继续在群聊里调整。"),
                        satisfies=[str(item) for item in raw_item.get("satisfies", [])],
                    )
                )
            if items:
                days.append(TripDay(id=f"day-{day_index}-{next(_id_counter)}", label=str(raw_day.get("label") or f"Day {day_index}"), items=items))
        if days:
            self.plans[trip_id] = TripPlan(
                id=f"plan-{next(_id_counter)}",
                title=str(raw_plan.get("title") or f"{self.trips[trip_id].destination}行程草案"),
                status=str(raw_plan.get("status") or "草案"),
                days=days,
            )
            self._save_plan_version(trip_id, change_summary or self._plan_change_summary(trip_id))

    def _plan_change_summary(self, trip_id: str) -> str:
        version_number = len(self.plan_versions.get(trip_id, [])) + 1
        if version_number <= 1:
            return "第一版：按群聊和想法墙生成基础行程"
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
        )

    def _fallback_plan_from_ideas(self, trip_id: str) -> dict:
        trip = self.trips[trip_id]
        cards = self.idea_cards[trip_id]
        place_titles = [card.title for card in cards if card.kind in {"地点", "待归类"}][:3]
        pace_cards = [card.title for card in cards if card.kind == "节奏"][:2]
        budget_cards = [card.title for card in cards if card.kind == "预算"][:1]
        first_place = place_titles[0] if place_titles else "目的地核心区域"
        second_place = place_titles[1] if len(place_titles) > 1 else "轻松散步和聊天时间"
        return {
            "title": f"{trip.destination}行程草案",
            "status": "草案",
            "days": [
                {
                    "label": "Day 1",
                    "items": [
                        {
                            "time": "10:00",
                            "title": first_place,
                            "location": trip.destination,
                            "duration": "2-3 小时",
                            "reason": "优先满足群聊里明确提到的地点需求。",
                            "notes": "具体交通和预约信息后续继续确认。",
                            "satisfies": ["地点偏好"],
                        },
                        {
                            "time": "15:00",
                            "title": second_place,
                            "location": "同区域附近",
                            "duration": "2 小时",
                            "reason": "保持轻松节奏，减少来回移动。",
                            "notes": "可按大家体力临时调整。",
                            "satisfies": pace_cards or ["轻松节奏"],
                        },
                    ],
                },
                {
                    "label": "Day 2",
                    "items": [
                        {
                            "time": "10:30",
                            "title": "补充候选地点",
                            "location": "待大家确认",
                            "duration": "半天",
                            "reason": "保留弹性，等待成员继续补充想法。",
                            "notes": "Agent 会根据新消息继续调整下一版。",
                            "satisfies": budget_cards or ["预算可控"],
                        }
                    ],
                },
            ],
        }

    @staticmethod
    def _should_trigger_agent(body: str) -> bool:
        normalized = body.strip().lower()
        return normalized.startswith("@agent") or normalized.startswith("@ai") or body.strip().startswith("@旅行助手")

    @staticmethod
    def _is_plan_command(body: str) -> bool:
        return any(keyword in body for keyword in ("生成", "行程", "计划", "安排", "第一版"))

    @staticmethod
    def _is_summary_command(body: str) -> bool:
        return any(keyword in body for keyword in ("整理", "总结", "需求", "偏好", "冲突", "缺失"))

    @staticmethod
    def _strip_agent_mention(body: str) -> str:
        clean_body = body.strip()
        for mention in ("@旅行助手", "@Agent", "@agent", "@AI", "@ai"):
            if clean_body.startswith(mention):
                return clean_body[len(mention) :].strip() or "请整理最近群聊里的旅行偏好。"
        return clean_body

    def _seed_messages(self) -> list[Message]:
        return [
            Message(
                id="m1",
                sender=USERS["u1"],
                sender_type="user",
                body="大家 8 月底去杭州吧，我想安排两晚，节奏别太赶。",
                created_at="19:10",
            ),
            Message(
                id="m2",
                sender=USERS["u2"],
                sender_type="user",
                body="我想去西湖和茶园，预算控制在人均 1500 左右。",
                created_at="19:12",
            ),
            Message(
                id="m3",
                sender=USERS["agent"],
                sender_type="agent",
                body="我先把大家的需求整理成计划约束。",
                created_at="19:13",
                agent_card=AgentCard(
                    kind="requirement-summary",
                    title="已收集到 4 条关键需求",
                    summary="目前适合做一个轻松的杭州 3 天 2 晚计划。",
                    bullets=["节奏不要太赶", "预算约人均 1500", "想去西湖和茶园", "需要继续确认住宿偏好"],
                    actions=[AgentAction(label="查看成员偏好", target="members")],
                ),
            ),
            Message(
                id="m4",
                sender=USERS["agent"],
                sender_type="agent",
                body="我生成了第一版行程，可以先看结构再继续改。",
                created_at="19:16",
                agent_card=AgentCard(
                    kind="itinerary-draft",
                    title="杭州 3 天 2 晚初版行程",
                    summary="西湖、龙井茶园、运河街区，整体偏轻松。",
                    bullets=["每天 2-3 个主要安排", "下午保留休息时间", "晚餐优先本地菜"],
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
                body="第一天傍晚去断桥和白堤，避开中午高温，也留出聊天时间。",
                author="林夏",
                status="已采纳",
                rotation=IDEA_ROTATIONS[0],
            ),
            IdeaCard(
                id="idea-place-2",
                kind="地点",
                title="龙井茶园",
                body="想安排半天慢一点的茶园体验，不要只打卡拍照。",
                author="周予",
                status="候选",
                rotation=IDEA_ROTATIONS[1],
            ),
            IdeaCard(
                id="idea-budget-1",
                kind="预算",
                title="人均 1500 左右",
                body="住宿、餐饮和交通都按轻松但不奢侈来筛选。",
                author="周予",
                status="约束",
                rotation=IDEA_ROTATIONS[2],
            ),
            IdeaCard(
                id="idea-pace-1",
                kind="节奏",
                title="不要每天太赶",
                body="每天最多 2-3 个主要安排，下午保留休息窗口。",
                author="林夏",
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
                            notes="住宿预算需要大家继续确认。",
                            satisfies=["林夏"],
                        ),
                        TripItem(
                            id="item-2",
                            time="17:00",
                            title="西湖傍晚散步",
                            location="断桥 - 白堤",
                            duration="2 小时",
                            reason="符合轻松节奏，也满足西湖需求。",
                            notes="避开中午高温和人流。",
                            satisfies=["林夏", "周予"],
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
                            satisfies=["周予"],
                        )
                    ],
                ),
            ],
        )

    def _seed_preferences(self) -> list[MemberPreference]:
        return [
            MemberPreference(member=USERS["u1"], known=["两晚", "轻松节奏", "少排队"], missing=["酒店预算"], conflicts=[]),
            MemberPreference(member=USERS["u2"], known=["西湖", "茶园", "人均 1500"], missing=["出发时间"], conflicts=[]),
            MemberPreference(
                member=USERS["u3"],
                known=["晚餐想吃本地菜"],
                missing=["是否能早起", "忌口"],
                conflicts=["可能不想安排太多步行"],
            ),
        ]


def _now_label() -> str:
    return datetime.now().strftime("%H:%M")


def _normalize_idea_text(value: str) -> str:
    return "".join(value.lower().split())


store = DemoStore()
