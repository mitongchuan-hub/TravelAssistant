from __future__ import annotations

import json
import re
import sqlite3
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from app.models import (
    AgentAction,
    AgentCard,
    IdeaCard,
    Message,
    MealRecommendation,
    RouteDetails,
    PlaceDetails,
    PreparationItem,
    Trip,
    TripDay,
    TripItem,
    TripPlan,
    TripRequirements,
    TravelPreparation,
    User,
)

from app.services.plan_display import migrate_stored_display
from app.services.requirements import initial_requirements

SNAPSHOT_KEY = "demo_store_v1"
CURRENT_SCHEMA_VERSION = 6



def ensure_agent_runs_schema(db_path: str | Path) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_runs (
                id INTEGER PRIMARY KEY,
                request_id TEXT NOT NULL,
                trip_id TEXT,
                user_input TEXT NOT NULL,
                process TEXT,
                output TEXT,
                ok INTEGER NOT NULL DEFAULT 1,
                error TEXT,
                model TEXT,
                tokens_in INTEGER,
                tokens_out INTEGER,
                tool_calls INTEGER,
                duration_ms INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )


def insert_agent_run(db_path: str | Path, row: dict[str, Any]) -> None:
    ensure_agent_runs_schema(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agent_runs (
                request_id, trip_id, user_input, process, output, ok, error,
                model, tokens_in, tokens_out, tool_calls, duration_ms, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["request_id"],
                row.get("trip_id"),
                row["user_input"],
                json.dumps(row["process"], ensure_ascii=False) if isinstance(row.get("process"), list) else None,
                row.get("output"),
                1 if row.get("ok") else 0,
                row.get("error"),
                row.get("model"),
                row.get("tokens_in"),
                row.get("tokens_out"),
                int(row.get("tool_calls") or 0),
                row.get("duration_ms"),
                row["created_at"],
            ),
        )


def backup_database(db_path: str | Path) -> Path:
    path = Path(db_path).resolve()
    directory = path.parent / 'backups'
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (path.stem + '-before-schema-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.sqlite3')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as source, sqlite3.connect(target) as destination:
        source.backup(destination)
    return target


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
        connection.execute("DROP TABLE IF EXISTS plan_versions")
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
            CREATE TABLE IF NOT EXISTS app_snapshots (
                key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        if "requirements_json" not in {row[1] for row in connection.execute("PRAGMA table_info(trips)")}:
            connection.execute("ALTER TABLE trips ADD COLUMN requirements_json TEXT NOT NULL DEFAULT '{}'")


def sync_relational_tables(connection: sqlite3.Connection, snapshot: dict[str, Any]) -> None:
    # Keep this function DML-only: final turn persistence calls it inside the same
    # transaction as app_snapshots, and SQLite DDL belongs to schema initialization.
    for table in ("plans", "ideas", "messages", "trips", "users"):
        connection.execute(f"DELETE FROM {table}")
    for user in snapshot.get("users", {}).values():
        connection.execute(
            "INSERT INTO users (id, name, initials) VALUES (?, ?, ?)",
            (user["id"], user["name"], user["initials"]),
        )
    for trip_id, trip in snapshot.get("trips", {}).items():
        connection.execute(
            """
            INSERT INTO trips (id, destination, date_range, budget, style, note, owner_id, status, last_activity, requirements_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                json.dumps(trip.get("requirements", {}), ensure_ascii=False),
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

def save_snapshot(
    db_path: str | Path,
    snapshot: dict[str, Any],
    *,
    initialize: bool = True,
) -> None:
    """Persist one complete snapshot.

    Schema creation belongs to startup/migration.  ``initialize=False`` is used by
    the final turn commit so no DDL can implicitly commit part of that business
    transaction.
    """
    path = Path(db_path)
    if initialize:
        ensure_relational_schema(path)
    with sqlite3.connect(path) as connection:
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
        "tasks": deepcopy(store.tasks),
        "display_jobs": deepcopy(store.display_jobs),
        "schema_version": CURRENT_SCHEMA_VERSION,
        "users": {user_id: asdict(user) for user_id, user in users.items()},
        "trips": {trip_id: asdict(trip) for trip_id, trip in store.trips.items()},
        "messages": {trip_id: [asdict(message) for message in messages] for trip_id, messages in store.messages.items()},
        "plans": {trip_id: asdict(plan) for trip_id, plan in store.plans.items()},
        "idea_cards": {trip_id: [asdict(card) for card in cards] for trip_id, cards in store.idea_cards.items()},
    }


def restore_state(store: Any, snapshot: dict[str, Any], users: dict[str, User]) -> int:
    schema_version = int(snapshot.get("schema_version", 1))
    legacy = schema_version < 2
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
    store.idea_cards = {
        trip_id: [_idea(raw_card, legacy) for raw_card in raw_cards]
        for trip_id, raw_cards in snapshot.get("idea_cards", {}).items()
        if trip_id in store.trips
    }
    store.tasks = _restore_tasks(snapshot.get("tasks", {}), store.trips)
    store.display_jobs = {jid: deepcopy(job) for jid, job in snapshot.get('display_jobs', {}).items()
                          if job.get('trip_id') in store.trips}

    from app.agents.agent_protocol import plan_basis
    from app.workflow.task_context import normalize_workflow
    for trip_id, trip in store.trips.items():
        trip.workflow_state = normalize_workflow(
            trip.workflow_state,
            trip=trip,
            trip_id=trip_id,
            messages=store.messages.get(trip_id, []),
            plan_basis=plan_basis(store.plans.get(trip_id)) if trip_id in store.plans else None,
        )
    return _next_numeric_id(snapshot)


def _restore_tasks(raw_tasks: Any, trips: dict[str, Trip]) -> dict[str, dict[str, Any]]:
    """Restore execution records defensively; they never revive on process start."""
    if not isinstance(raw_tasks, dict):
        return {}
    restored: dict[str, dict[str, Any]] = {}
    for task_id, raw in raw_tasks.items():
        if not isinstance(task_id, str) or not isinstance(raw, dict):
            continue
        trip_id = raw.get('trip_id')
        if trip_id not in trips or raw.get('id') != task_id:
            continue
        task = deepcopy(raw)
        # The legacy strategy was removed; historical records migrate to the
        # single task_execution framework.
        task['execution_strategy'] = 'task_execution'
        status = task.get('status')
        if status not in {'queued', 'running', 'succeeded', 'failed', 'superseded'}:
            task['status'] = 'failed'
            task['message'] = '任务记录格式已更新，请基于当前状态重新提交。'
        restored[task_id] = task
    return restored


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
        last_activity=str(raw["last_activity"]),
        requirements=(
            TripRequirements(**raw["requirements"]) if isinstance(raw.get("requirements"), dict)
            else initial_requirements(str(raw["budget"]), str(raw["style"]))
        ),
        workflow_state=deepcopy(raw.get("workflow_state") or {}),
        mode=str(raw.get('mode') or 'city'),
        lodging=deepcopy(raw.get('lodging') or {}),
    )


def _action(raw: dict[str, Any], legacy: bool = False) -> AgentAction:
    target = str(raw["target"])
    label = str(raw["label"])
    if legacy and target == "members":
        return AgentAction(label="查看旅行想法", target="board")
    return AgentAction(label=label, target=target)


def _card(raw: dict[str, Any] | None, legacy: bool = False) -> AgentCard | None:
    if not raw:
        return None
    return AgentCard(
        kind=str(raw["kind"]),
        title=str(raw["title"]),
        summary=str(raw["summary"]),
        bullets=[str(item) for item in raw["bullets"]],
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
        body=body,
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
    return TripItem(
        id=str(raw["id"]),
        time=str(raw["time"]),
        title=str(raw["title"]),
        location=str(raw["location"]),
        duration=str(raw["duration"]),
        reason=str(raw["reason"]),
        notes=str(raw["notes"]),
        satisfies=satisfies,
        category=str(raw.get("category") or "activity"),
        meal=_meal(raw.get("meal")),
        place=PlaceDetails(**raw["place"]) if isinstance(raw.get("place"), dict) else None,
        travel_mode=str(raw.get('travel_mode') or 'transit'),
        incoming_route=RouteDetails(**raw['incoming_route']) if isinstance(raw.get('incoming_route'), dict) else None,
        departure_route=RouteDetails(**raw['departure_route']) if isinstance(raw.get('departure_route'), dict) else None,
        place_scope=str(raw.get("place_scope") or "unknown"),
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
        text=str(raw.get("text") or ""),
        evidence=deepcopy(raw.get("evidence") or []),
        requirements_revision=raw.get("requirements_revision"),
        display=migrate_stored_display(raw.get("display")),
        display_source_hash=str(raw.get("display_source_hash") or ""),
        display_status=str(raw.get("display_status") or "not_requested"),
        requirements_snapshot=deepcopy(raw.get("requirements_snapshot") or {}),
        lodging_snapshot=deepcopy(raw["lodging_snapshot"]) if isinstance(raw.get("lodging_snapshot"), dict) else {},
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
        title=title,
        body=body,
        author=author,
        status=str(raw["status"]),
        rotation=str(raw.get("rotation", "0deg")),
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
