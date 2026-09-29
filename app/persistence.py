from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import AgentAction, AgentCard, IdeaCard, MemberPreference, Message, PlanVersion, TripDay, TripGroup, TripItem, TripPlan, User

SNAPSHOT_KEY = "demo_store_v1"



def ensure_relational_schema(db_path: str | Path) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                initials TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS trips (
                id TEXT PRIMARY KEY,
                destination TEXT NOT NULL,
                date_range TEXT NOT NULL,
                budget TEXT NOT NULL,
                style TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                initiator_id TEXT NOT NULL,
                status TEXT NOT NULL,
                last_activity TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS trip_members (
                trip_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (trip_id, user_id)
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
    connection.executescript(
        """
        DELETE FROM plan_versions;
        DELETE FROM plans;
        DELETE FROM ideas;
        DELETE FROM messages;
        DELETE FROM trip_members;
        DELETE FROM trips;
        DELETE FROM users;
        """
    )
    for user in snapshot.get("users", {}).values():
        connection.execute(
            "INSERT INTO users (id, name, initials) VALUES (?, ?, ?)",
            (user["id"], user["name"], user["initials"]),
        )
    for trip_id, trip in snapshot.get("trips", {}).items():
        connection.execute(
            """
            INSERT INTO trips (id, destination, date_range, budget, style, note, initiator_id, status, last_activity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trip_id,
                trip["destination"],
                trip["date_range"],
                trip["budget"],
                trip["style"],
                trip.get("note", ""),
                trip["initiator"]["id"],
                trip["status"],
                trip["last_activity"],
            ),
        )
    for trip_id, member_ids in snapshot.get("group_members", {}).items():
        for user_id in member_ids:
            connection.execute(
                "INSERT OR IGNORE INTO trip_members (trip_id, user_id) VALUES (?, ?)",
                (trip_id, user_id),
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
        row = connection.execute("SELECT payload FROM app_snapshots WHERE key = ?", (SNAPSHOT_KEY,)).fetchone()
    if not row:
        return None
    return json.loads(row[0])


def serialize_state(store: Any, users: dict[str, User]) -> dict[str, Any]:
    return {
        "users": {user_id: asdict(user) for user_id, user in users.items()},
        "trips": {trip_id: asdict(trip) for trip_id, trip in store.trips.items()},
        "group_members": store.group_members,
        "messages": {trip_id: [asdict(message) for message in messages] for trip_id, messages in store.messages.items()},
        "plans": {trip_id: asdict(plan) for trip_id, plan in store.plans.items()},
        "plan_versions": {trip_id: [asdict(version) for version in versions] for trip_id, versions in store.plan_versions.items()},
        "preferences": {trip_id: [asdict(preference) for preference in preferences] for trip_id, preferences in store.preferences.items()},
        "idea_cards": {trip_id: [asdict(card) for card in cards] for trip_id, cards in store.idea_cards.items()},
    }


def restore_state(store: Any, snapshot: dict[str, Any], users: dict[str, User]) -> int:
    users.clear()
    users.update({user_id: _user(raw_user) for user_id, raw_user in snapshot.get("users", {}).items()})
    if "agent" not in users:
        users["agent"] = User(id="agent", name="旅行规划 Agent", initials="AI")

    store.trips = {trip_id: _trip(raw_trip) for trip_id, raw_trip in snapshot.get("trips", {}).items()}
    store.group_members = {trip_id: list(member_ids) for trip_id, member_ids in snapshot.get("group_members", {}).items()}
    store.messages = {
        trip_id: [_message(raw_message) for raw_message in raw_messages]
        for trip_id, raw_messages in snapshot.get("messages", {}).items()
    }
    store.plans = {trip_id: _plan(raw_plan) for trip_id, raw_plan in snapshot.get("plans", {}).items()}
    store.plan_versions = {
        trip_id: [_version(raw_version) for raw_version in raw_versions]
        for trip_id, raw_versions in snapshot.get("plan_versions", {}).items()
    }
    store.preferences = {
        trip_id: [_preference(raw_preference) for raw_preference in raw_preferences]
        for trip_id, raw_preferences in snapshot.get("preferences", {}).items()
    }
    store.idea_cards = {
        trip_id: [_idea(raw_card) for raw_card in raw_cards]
        for trip_id, raw_cards in snapshot.get("idea_cards", {}).items()
    }
    return _next_numeric_id(snapshot)


def _user(raw: dict[str, Any]) -> User:
    return User(id=str(raw["id"]), name=str(raw["name"]), initials=str(raw["initials"]))


def _trip(raw: dict[str, Any]) -> TripGroup:
    return TripGroup(
        id=str(raw["id"]),
        destination=str(raw["destination"]),
        date_range=str(raw["date_range"]),
        budget=str(raw["budget"]),
        style=str(raw["style"]),
        note=str(raw["note"]),
        initiator=_user(raw["initiator"]),
        member_initials=list(raw["member_initials"]),
        status=str(raw["status"]),
        last_activity=str(raw["last_activity"]),
    )


def _action(raw: dict[str, Any]) -> AgentAction:
    return AgentAction(label=str(raw["label"]), target=str(raw["target"]))


def _card(raw: dict[str, Any] | None) -> AgentCard | None:
    if not raw:
        return None
    return AgentCard(
        kind=str(raw["kind"]),
        title=str(raw["title"]),
        summary=str(raw["summary"]),
        bullets=list(raw["bullets"]),
        actions=[_action(action) for action in raw["actions"]],
    )


def _message(raw: dict[str, Any]) -> Message:
    return Message(
        id=str(raw["id"]),
        sender=_user(raw["sender"]),
        sender_type=str(raw["sender_type"]),
        body=str(raw["body"]),
        created_at=str(raw["created_at"]),
        status=str(raw.get("status", "sent")),
        agent_card=_card(raw.get("agent_card")),
    )


def _item(raw: dict[str, Any]) -> TripItem:
    return TripItem(
        id=str(raw["id"]),
        time=str(raw["time"]),
        title=str(raw["title"]),
        location=str(raw["location"]),
        duration=str(raw["duration"]),
        reason=str(raw["reason"]),
        notes=str(raw["notes"]),
        satisfies=list(raw.get("satisfies", [])),
    )


def _day(raw: dict[str, Any]) -> TripDay:
    return TripDay(id=str(raw["id"]), label=str(raw["label"]), items=[_item(item) for item in raw.get("items", [])])


def _plan(raw: dict[str, Any]) -> TripPlan:
    return TripPlan(id=str(raw["id"]), title=str(raw["title"]), status=str(raw["status"]), days=[_day(day) for day in raw.get("days", [])])


def _version(raw: dict[str, Any]) -> PlanVersion:
    return PlanVersion(
        id=str(raw["id"]),
        label=str(raw["label"]),
        title=str(raw["title"]),
        status=str(raw["status"]),
        created_at=str(raw["created_at"]),
        change_summary=str(raw["change_summary"]),
        days=[_day(day) for day in raw.get("days", [])],
    )


def _idea(raw: dict[str, Any]) -> IdeaCard:
    return IdeaCard(
        id=str(raw["id"]),
        kind=str(raw["kind"]),
        title=str(raw["title"]),
        body=str(raw["body"]),
        author=str(raw["author"]),
        status=str(raw["status"]),
        rotation=str(raw.get("rotation", "0deg")),
    )


def _preference(raw: dict[str, Any]) -> MemberPreference:
    return MemberPreference(
        member=_user(raw["member"]),
        known=list(raw.get("known", [])),
        missing=list(raw.get("missing", [])),
        conflicts=list(raw.get("conflicts", [])),
    )


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
