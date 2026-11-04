from __future__ import annotations

import json
import re
import sqlite3
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from app.domain.models import (
    AgentAction,
    AgentCard,
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

from app.services.itinerary.plan_display import migrate_stored_display

SNAPSHOT_KEY = "demo_store_v1"
CURRENT_SCHEMA_VERSION = 11
TASK_HISTORY_SCHEMA_VERSION = 7



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
        # 先取得写锁，使存在性检查和插入与删除旅行的快照事务串行。
        connection.execute('BEGIN IMMEDIATE')
        # 展示任务可能在旅行删除后才写日志；只记录仍存在的业务旅行。
        if row.get('trip_id') and connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trips'"
        ).fetchone() and not connection.execute(
            "SELECT 1 FROM trips WHERE id = ?", (row['trip_id'],)
        ).fetchone():
            return
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
        # Preserve legacy business tables during this reversible phase. Add the
        # minimum ownership column in place; destructive conversion belongs to
        # the backed-up business snapshot transaction, never this initializer.
        if trip_columns and "owner_id" not in trip_columns:
            connection.execute("ALTER TABLE trips ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'u1'")
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
        from app.storage.task_repository import ensure_task_schema
        ensure_task_schema(connection)
        if "requirements_json" not in {row[1] for row in connection.execute("PRAGMA table_info(trips)")}:
            connection.execute("ALTER TABLE trips ADD COLUMN requirements_json TEXT NOT NULL DEFAULT '{}'")


def sync_relational_tables(connection: sqlite3.Connection, snapshot: dict[str, Any]) -> None:
    created_at = dict(connection.execute('SELECT id, created_at FROM trips'))
    # Keep this function DML-only: final turn persistence calls it inside the same
    # transaction as app_snapshots. Controlled mirror DDL runs before this call.
    for table in ("plans", "messages", "trips", "users"):
        connection.execute(f"DELETE FROM {table}")
    for user in snapshot.get("users", {}).values():
        connection.execute(
            "INSERT INTO users (id, name, initials) VALUES (?, ?, ?)",
            (user["id"], user["name"], user["initials"]),
        )
    for trip_id, raw_trip in snapshot.get("trips", {}).items():
        trip = {**raw_trip, **{key: getattr(_trip(raw_trip), key) for key in ("date_range",)}}
        connection.execute(
            """
            INSERT INTO trips (id, destination, date_range, owner_id, status, last_activity, requirements_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            (
                trip_id,
                trip["destination"],
                trip["date_range"],
                trip["owner"]["id"],
                trip["status"],
                trip["last_activity"],
                json.dumps(trip.get("requirements", {}), ensure_ascii=False),
                created_at.get(trip_id),
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
    for trip_id, plan in snapshot.get("plans", {}).items():
        connection.execute(
            "INSERT INTO plans (trip_id, plan_json) VALUES (?, ?)",
            (trip_id, json.dumps(plan, ensure_ascii=False)),
        )
    # 删除旅行时，在同一快照事务内清理其模型运行记录。
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_runs'").fetchone():
        connection.execute(
            "DELETE FROM agent_runs WHERE trip_id IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM trips WHERE trips.id = agent_runs.trip_id)"
        )

def migrate_trip_table(connection):
    """Rebuild the old NOT NULL mirror inside the business snapshot transaction."""
    columns = {row[1] for row in connection.execute('PRAGMA table_info(trips)')}
    if not columns & {'budget', 'style', 'note'}:
        return
    connection.execute("""CREATE TABLE trips_schema9 (
        id TEXT PRIMARY KEY, destination TEXT NOT NULL, date_range TEXT NOT NULL,
        owner_id TEXT NOT NULL, status TEXT NOT NULL, last_activity TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, requirements_json TEXT NOT NULL DEFAULT '{}'
    )""")
    kept = 'id, destination, date_range, owner_id, status, last_activity, created_at, requirements_json'
    connection.execute(f'INSERT INTO trips_schema9 ({kept}) SELECT {kept} FROM trips')
    connection.execute('DROP TABLE trips')
    connection.execute('ALTER TABLE trips_schema9 RENAME TO trips')


def save_snapshot(
    db_path: str | Path,
    snapshot: dict[str, Any],
    *,
    initialize: bool = True,
    task_upserts=(),
    delete_trip_ids=(),
    migration=None,
) -> None:
    """Persist one complete snapshot.

    Schema creation belongs to startup. The schema-nine mirror rebuild, if
    needed, uses transactional execute() DDL after BEGIN IMMEDIATE; no executescript
    or intermediate commit can split it from the business snapshot and markers.
    """
    path = Path(db_path)
    if initialize:
        ensure_relational_schema(path)
    with sqlite3.connect(path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        migrate_trip_table(connection)
        connection.execute('DROP TABLE IF EXISTS ideas')
        connection.execute(
            """
            INSERT INTO app_snapshots (key, payload, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET payload = excluded.payload, updated_at = CURRENT_TIMESTAMP
            """,
            (SNAPSHOT_KEY, json.dumps(snapshot, ensure_ascii=False)),
        )
        sync_relational_tables(connection, snapshot)
        from app.storage.task_repository import write_tasks
        write_tasks(connection, task_upserts, delete_trip_ids)
        if migration:
            for name in ([migration] if isinstance(migration, str) else migration):
                connection.execute('INSERT INTO storage_migrations(name) VALUES (?)', (name,))


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
        "agent_undo": deepcopy(getattr(store, "agent_undo", {})),
        "dispatchers": deepcopy(getattr(store, "dispatchers", {})),
        "display_jobs": deepcopy(store.display_jobs),
        "schema_version": CURRENT_SCHEMA_VERSION,
        "users": {user_id: asdict(user) for user_id, user in users.items()},
        "trips": {trip_id: asdict(trip) for trip_id, trip in store.trips.items()},
        "messages": {trip_id: [asdict(message) for message in messages] for trip_id, messages in store.messages.items()},
        "plans": {trip_id: asdict(plan) for trip_id, plan in store.plans.items()},
    }


def restore_state(store: Any, snapshot: dict[str, Any], users: dict[str, User]) -> int:
    schema_version = int(snapshot.get("schema_version", 1))
    from app.storage.model_migration import migrate_snapshot
    snapshot = migrate_snapshot(snapshot)
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
    from app.workflow.dispatcher import defaults
    store.dispatchers = {key: {**defaults(), **snapshot.get("dispatchers", {}).get(key, {})} for key in store.trips}
    store.agent_undo = {key: deepcopy(value) for key, value in snapshot.get("agent_undo", {}).items() if key in store.trips}
    # Legacy history is imported separately; normal restore never hydrates history.
    store.tasks = {}
    store.display_jobs = {jid: deepcopy(job) for jid, job in snapshot.get('display_jobs', {}).items()
                          if job.get('trip_id') in store.trips}

    return _next_numeric_id(snapshot)


def load_persisted_state(store, users):
    """Shared startup coordinator for both State and compatibility DemoStore."""
    snapshot = load_snapshot(store.db_path)
    next_id = None
    if snapshot:
        if int(snapshot.get('schema_version', 1)) < CURRENT_SCHEMA_VERSION:
            backup_database(store.db_path)
        next_id = restore_state(store, snapshot, users)
    initialize_task_history(store, snapshot, users)
    return next_id


def initialize_task_history(store, snapshot, users):
    """Import legacy history once; failure leaves the old snapshot authoritative."""
    from app.storage.task_repository import SQLiteTaskRepository
    ensure_relational_schema(store.db_path)
    store.task_repository = SQLiteTaskRepository(store.db_path)
    if snapshot is None:
        with sqlite3.connect(store.db_path) as connection:
            if connection.execute('SELECT 1 FROM execution_tasks LIMIT 1').fetchone():
                raise ValueError('任务历史存在但业务快照缺失，拒绝恢复孤儿任务')
    if snapshot and int(snapshot.get('schema_version', 1)) < TASK_HISTORY_SCHEMA_VERSION:
        imported = _restore_tasks(snapshot.get('tasks', {}), store.trips)
        # A mixed/partially migrated database is not silently overwritten.
        with sqlite3.connect(store.db_path) as connection:
            if connection.execute('SELECT 1 FROM storage_migrations WHERE name = ?', ('task_history_v7',)).fetchone():
                raise ValueError('任务历史迁移标记与旧快照矛盾')
        for task in imported.values():
            existing = store.task_repository.get(task['id'])
            by_request = store.task_repository.find_request(task['trip_id'], task['request_id'])
            if (existing is not None and existing != task) or (by_request is not None and by_request != task):
                raise ValueError('旧任务历史与数据库记录冲突')
        save_snapshot(store.db_path, serialize_state(store, users), initialize=False,
                      task_upserts=imported.values(), migration=['task_history_v7', 'trip_model_v9', 'requirements_text_v10', 'followup_v11'])
    elif snapshot and int(snapshot.get('schema_version', 1)) < CURRENT_SCHEMA_VERSION:
        version = int(snapshot.get('schema_version', 1))
        migrations = ([ 'trip_model_v9' ] if version < 9 else [])
        if version < 10:
            migrations.append('requirements_text_v10')
        migrations.append('followup_v11')
        save_snapshot(store.db_path, serialize_state(store, users), initialize=False, migration=migrations)
    store.tasks = {task['id']: task for task in store.task_repository.active()}


def _restore_tasks(raw_tasks: Any, trips: dict[str, Trip]) -> dict[str, dict[str, Any]]:
    """Restore execution records defensively; they never revive on process start."""
    if not isinstance(raw_tasks, dict):
        return {}
    restored: dict[str, dict[str, Any]] = {}
    for task_id, raw in raw_tasks.items():
        if not isinstance(task_id, str) or not isinstance(raw, dict):
            continue
        trip_id = raw.get('trip_id')
        if (trip_id not in trips or raw.get('id') != task_id
                or not isinstance(raw.get('request_id'), str) or not raw['request_id']):
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
    from app.storage.model_migration import migrate_trip
    raw = migrate_trip(raw)
    owner = raw.get("owner") or raw.get("initiator")
    if not isinstance(owner, dict):
        raise ValueError("Trip snapshot is missing its owner")
    return Trip(
        id=str(raw["id"]),
        destination=str(raw["destination"]),
        owner=_user(owner),
        status=str(raw["status"]),
        last_activity=str(raw["last_activity"]),
        requirements=(
            TripRequirements(**raw["requirements"]) if isinstance(raw.get("requirements"), dict)
            else TripRequirements()
        ),
        workflow_state=deepcopy(raw.get("workflow_state") or {}),
        mode=str(raw.get('mode') or 'city'),
        lodging=deepcopy(raw.get('lodging') or {}),
        legacy_metadata=deepcopy(raw.get('legacy_metadata') or {}),
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
        execution_progress=deepcopy(raw.get('execution_progress') or {}),
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
        display_style=str(raw.get("display_style") or ""),
        requirements_snapshot=deepcopy(raw.get("requirements_snapshot", {})),
        lodging_snapshot=deepcopy(raw["lodging_snapshot"]) if isinstance(raw.get("lodging_snapshot"), dict) else {},
        requirements_basis_complete=raw.get("requirements_basis_complete", True),
        requirements_legacy_stale=raw.get("requirements_legacy_stale", False),
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
