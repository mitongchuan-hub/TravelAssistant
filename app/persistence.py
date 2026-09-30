from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

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

SNAPSHOT_KEY = "demo_store_v1"
CURRENT_SCHEMA_VERSION = 2



def ensure_relational_schema(db_path: str | Path) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        trip_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(trips)").fetchall()
        }
        if trip_columns and "owner_id" not in trip_columns:
            connection.execute("DROP TABLE trips")
        connection.execute("DROP TABLE IF EXISTS trip_members")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                initials TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS auth_accounts (
                email TEXT PRIMARY KEY COLLATE NOCASE,
                user_id TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_id ON auth_sessions (user_id);
            CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires_at ON auth_sessions (expires_at);
            CREATE TABLE IF NOT EXISTS trips (
                id TEXT PRIMARY KEY,
                destination TEXT NOT NULL,
                date_range TEXT NOT NULL,
                budget TEXT NOT NULL,
                style TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                owner_id TEXT NOT NULL,
                status TEXT NOT NULL,
                last_activity TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                trip_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                sender_type TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'sent',
                agent_card_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ideas (
                id TEXT PRIMARY KEY,
                trip_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                author TEXT NOT NULL,
                status TEXT NOT NULL,
                rotation TEXT NOT NULL DEFAULT '0deg'
            );
            CREATE TABLE IF NOT EXISTS plans (
                trip_id TEXT PRIMARY KEY,
                plan_json TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS plan_versions (
                id TEXT PRIMARY KEY,
                trip_id TEXT NOT NULL,
                label TEXT NOT NULL,
                change_summary TEXT NOT NULL,
                version_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


def sync_relational_tables(connection: sqlite3.Connection, snapshot: dict[str, Any]) -> None:
    # executescript() commits an open transaction, which would leave the snapshot
    # and the cleared tables committed even if a subsequent INSERT fails.
    for table in ("plan_versions", "plans", "ideas", "messages", "trips", "users"):
        connection.execute(f"DELETE FROM {table}")
    for user in snapshot.get("users", {}).values():
        connection.execute(
            "INSERT INTO users (id, name, initials) VALUES (?, ?, ?)",
            (user["id"], user["name"], user["initials"]),
        )
    for trip_id, trip in snapshot.get("trips", {}).items():
        connection.execute(
            """
            INSERT INTO trips (id, destination, date_range, budget, style, note, owner_id, status, last_activity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trip_id,
                trip["destination"],
                trip["date_range"],
                trip["budget"],
                trip["style"],
                trip.get("note", ""),
                trip["owner"]["id"],
                trip["status"],
                trip["last_activity"],
            ),
        )
    for trip_id, messages in snapshot.get("messages", {}).items():
        for message in messages:
            connection.execute(
                """
                INSERT INTO messages (id, trip_id, sender_id, sender_type, body, status, agent_card_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message["id"],
                    trip_id,
                    message["sender"]["id"],
                    message["sender_type"],
                    message["body"],
                    message.get("status", "sent"),
                    json.dumps(message.get("agent_card"), ensure_ascii=False) if message.get("agent_card") else None,
                    message["created_at"],
                ),
            )
    for trip_id, cards in snapshot.get("idea_cards", {}).items():
        for card in cards:
            connection.execute(
                """
                INSERT INTO ideas (id, trip_id, kind, title, body, author, status, rotation)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (card["id"], trip_id, card["kind"], card["title"], card["body"], card["author"], card["status"], card.get("rotation", "0deg")),
            )
    for trip_id, plan in snapshot.get("plans", {}).items():
        connection.execute(
            "INSERT INTO plans (trip_id, plan_json) VALUES (?, ?)",
            (trip_id, json.dumps(plan, ensure_ascii=False)),
        )
    for trip_id, versions in snapshot.get("plan_versions", {}).items():
        for version in versions:
            connection.execute(
                """
                INSERT INTO plan_versions (id, trip_id, label, change_summary, version_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    version["id"],
                    trip_id,
                    version["label"],
                    version["change_summary"],
                    json.dumps(version, ensure_ascii=False),
                    version["created_at"],
                ),
            )

def save_snapshot(db_path: str | Path, snapshot: dict[str, Any]) -> None:
    path = Path(db_path)
    ensure_relational_schema(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS app_snapshots (key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.execute(
            """
            INSERT INTO app_snapshots (key, payload, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET payload = excluded.payload, updated_at = CURRENT_TIMESTAMP
            """,
            (SNAPSHOT_KEY, json.dumps(snapshot, ensure_ascii=False)),
        )
        sync_relational_tables(connection, snapshot)


def load_snapshot(db_path: str | Path) -> dict[str, Any] | None:
    path = Path(db_path)
    if not path.exists():
        return None
    with sqlite3.connect(path) as connection:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'app_snapshots'"
        ).fetchone():
            return None
        row = connection.execute("SELECT payload FROM app_snapshots WHERE key = ?", (SNAPSHOT_KEY,)).fetchone()
    if not row:
        return None
    return json.loads(row[0])


def serialize_state(store: Any, users: dict[str, User]) -> dict[str, Any]:
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "users": {user_id: asdict(user) for user_id, user in users.items()},
        "trips": {trip_id: asdict(trip) for trip_id, trip in store.trips.items()},
        "messages": {trip_id: [asdict(message) for message in messages] for trip_id, messages in store.messages.items()},
        "plans": {trip_id: asdict(plan) for trip_id, plan in store.plans.items()},
        "plan_versions": {trip_id: [asdict(version) for version in versions] for trip_id, versions in store.plan_versions.items()},
        "idea_cards": {trip_id: [asdict(card) for card in cards] for trip_id, cards in store.idea_cards.items()},
    }


def restore_state(store: Any, snapshot: dict[str, Any], users: dict[str, User]) -> int:
    legacy = int(snapshot.get("schema_version", 1)) < CURRENT_SCHEMA_VERSION
    users.clear()
    users.update({user_id: _user(raw_user) for user_id, raw_user in snapshot.get("users", {}).items()})
    if "agent" not in users:
        users["agent"] = User(id="agent", name="旅行规划 Agent", initials="AI")

    store.trips = {trip_id: _trip(raw_trip, legacy) for trip_id, raw_trip in snapshot.get("trips", {}).items()}
    store.messages = {}
    for trip_id, trip in store.trips.items():
        restored_messages = []
        for raw_message in snapshot.get("messages", {}).get(trip_id, []):
            sender_type = str(raw_message.get("sender_type", "user"))
            sender_id = str(raw_message.get("sender", {}).get("id", ""))
            if legacy and sender_type == "user" and sender_id != trip.owner.id:
                continue
            restored_messages.append(_message(raw_message, trip.owner if legacy else None, legacy))
        store.messages[trip_id] = restored_messages

    store.plans = {
        trip_id: _plan(raw_plan, legacy)
        for trip_id, raw_plan in snapshot.get("plans", {}).items()
        if trip_id in store.trips
    }
    store.plan_versions = {
        trip_id: [_version(raw_version, legacy) for raw_version in raw_versions]
        for trip_id, raw_versions in snapshot.get("plan_versions", {}).items()
        if trip_id in store.trips
    }
    store.idea_cards = {
        trip_id: [_idea(raw_card, legacy) for raw_card in raw_cards]
        for trip_id, raw_cards in snapshot.get("idea_cards", {}).items()
        if trip_id in store.trips
    }
    return _next_numeric_id(snapshot)


def _user(raw: dict[str, Any]) -> User:
    return User(id=str(raw["id"]), name=str(raw["name"]), initials=str(raw["initials"]))


def _trip(raw: dict[str, Any], legacy: bool = False) -> Trip:
    owner = raw.get("owner") or raw.get("initiator")
    if not isinstance(owner, dict):
        raise ValueError("Trip snapshot is missing its owner")
    return Trip(
        id=str(raw["id"]),
        destination=str(raw["destination"]),
        date_range=str(raw["date_range"]),
        budget=str(raw["budget"]),
        style=str(raw["style"]),
        note=str(raw.get("note", "")),
        owner=_user(owner),
        status=str(raw["status"]),
        last_activity=_solo_text(str(raw["last_activity"])) if legacy else str(raw["last_activity"]),
    )


def _action(raw: dict[str, Any], legacy: bool = False) -> AgentAction:
    target = str(raw["target"])
    label = str(raw["label"])
    if legacy and target == "members":
        return AgentAction(label="查看旅行想法", target="board")
    return AgentAction(label=_solo_text(label) if legacy else label, target=target)


def _card(raw: dict[str, Any] | None, legacy: bool = False) -> AgentCard | None:
    if not raw:
        return None
    return AgentCard(
        kind=str(raw["kind"]),
        title=_solo_text(str(raw["title"])) if legacy else str(raw["title"]),
        summary=_solo_text(str(raw["summary"])) if legacy else str(raw["summary"]),
        bullets=[_solo_text(str(item)) if legacy else str(item) for item in raw["bullets"]],
        actions=[_action(action, legacy) for action in raw["actions"]],
    )


def _message(raw: dict[str, Any], owner: User | None = None, legacy: bool = False) -> Message:
    sender_type = str(raw["sender_type"])
    sender = owner if owner and sender_type == "user" else _user(raw["sender"])
    body = str(raw["body"])
    return Message(
        id=str(raw["id"]),
        sender=sender,
        sender_type=sender_type,
        body=_solo_text(body) if legacy else body,
        created_at=str(raw["created_at"]),
        status=str(raw.get("status", "sent")),
        agent_card=_card(raw.get("agent_card"), legacy),
    )


def _meal(raw: dict[str, Any] | None) -> MealRecommendation | None:
    if not isinstance(raw, dict):
        return None
    return MealRecommendation(
        meal_type=str(raw.get("meal_type") or "用餐"),
        recommendation=str(raw.get("recommendation") or "当地特色餐食待确认"),
        cuisine=str(raw.get("cuisine") or "当地菜"),
        budget=str(raw.get("budget") or "预算待确认"),
        reservation=str(raw.get("reservation") or "待确认"),
    )


def _item(raw: dict[str, Any], legacy: bool = False) -> TripItem:
    satisfies = [str(item) for item in raw.get("satisfies", [])]
    if legacy and satisfies:
        satisfies = ["你的偏好"]
    return TripItem(
        id=str(raw["id"]),
        time=str(raw["time"]),
        title=str(raw["title"]),
        location=str(raw["location"]),
        duration=str(raw["duration"]),
        reason=_solo_text(str(raw["reason"])) if legacy else str(raw["reason"]),
        notes=_solo_text(str(raw["notes"])) if legacy else str(raw["notes"]),
        satisfies=satisfies,
        category=str(raw.get("category") or "activity"),
        meal=_meal(raw.get("meal")),
    )


def _day(raw: dict[str, Any], legacy: bool = False) -> TripDay:
    return TripDay(
        id=str(raw["id"]),
        label=str(raw["label"]),
        items=[_item(item, legacy) for item in raw.get("items", [])],
        date=str(raw.get("date") or ""),
        theme=str(raw.get("theme") or ""),
    )


def _preparation_item(raw: dict[str, Any]) -> PreparationItem:
    return PreparationItem(
        title=str(raw.get("title") or "待确认事项"),
        body=str(raw.get("body") or "可以在生成行程后继续补充。"),
        status=str(raw.get("status") or "待确认"),
    )


def _preparation(raw: dict[str, Any] | None) -> TravelPreparation:
    raw = raw or {}
    return TravelPreparation(
        clothing=[_preparation_item(item) for item in raw.get("clothing", []) if isinstance(item, dict)],
        accommodation=[_preparation_item(item) for item in raw.get("accommodation", []) if isinstance(item, dict)],
    )


def _plan(raw: dict[str, Any], legacy: bool = False) -> TripPlan:
    return TripPlan(
        id=str(raw["id"]),
        title=str(raw["title"]),
        status=str(raw["status"]),
        days=[_day(day, legacy) for day in raw.get("days", [])],
        preparation=_preparation(raw.get("preparation")),
        overview=str(raw.get("overview") or ""),
        constraints_met=[str(item) for item in raw.get("constraints_met", [])],
        pending_items=[str(item) for item in raw.get("pending_items", [])],
        risks=[str(item) for item in raw.get("risks", [])],
    )


def _version(raw: dict[str, Any], legacy: bool = False) -> PlanVersion:
    change_summary = str(raw["change_summary"])
    return PlanVersion(
        id=str(raw["id"]),
        label=str(raw["label"]),
        title=str(raw["title"]),
        status=str(raw["status"]),
        created_at=str(raw["created_at"]),
        change_summary=_solo_text(change_summary) if legacy else change_summary,
        days=[_day(day, legacy) for day in raw.get("days", [])],
        preparation=_preparation(raw.get("preparation")),
        overview=str(raw.get("overview") or ""),
        constraints_met=[str(item) for item in raw.get("constraints_met", [])],
        pending_items=[str(item) for item in raw.get("pending_items", [])],
        risks=[str(item) for item in raw.get("risks", [])],
    )


def _idea(raw: dict[str, Any], legacy: bool = False) -> IdeaCard:
    author = str(raw.get("author", "Agent"))
    if legacy:
        author = "Agent" if author.lower() in {"agent", "ai", "旅行规划 agent"} else "你"
    title = str(raw["title"])
    body = str(raw["body"])
    return IdeaCard(
        id=str(raw["id"]),
        kind=str(raw["kind"]),
        title=_solo_text(title) if legacy else title,
        body=_solo_text(body) if legacy else body,
        author=author,
        status=str(raw["status"]),
        rotation=str(raw.get("rotation", "0deg")),
    )


def _solo_text(value: str) -> str:
    value = re.sub(r"大家\s*(.+?)吧，我想", r"我 \1，想", value)
    replacements = (
        ("旅行群", "旅行"),
        ("群聊", "对话"),
        ("成员偏好", "旅行想法"),
        ("大家的", "你的"),
        ("大家", "你"),
        ("同行人", "出行偏好"),
    )
    for old, new in replacements:
        value = value.replace(old, new)
    return value


def _next_numeric_id(snapshot: dict[str, Any]) -> int:
    numbers = [100]
    pattern = re.compile(r"(?:^|[-_])([0-9]+)$")

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            match = pattern.search(value)
            if match:
                numbers.append(int(match.group(1)))

    visit(snapshot)
    return max(numbers) + 1
