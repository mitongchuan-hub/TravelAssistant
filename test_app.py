import json
import sqlite3
import importlib
from dataclasses import asdict
import pytest
from pathlib import Path

from fastapi.testclient import TestClient

from app.agent_client import _build_context, AgentReply
from app.models import AgentCard
from app.auth import AuthStore, SESSION_COOKIE_NAME
from app.main import app, auth
from app.persistence import CURRENT_SCHEMA_VERSION, load_snapshot, serialize_state
from app.store import DemoStore, USERS, store


TEST_EMAIL = "linxia@example.com"
TEST_PASSWORD = "correct-horse-2026"
if not auth.email_exists(TEST_EMAIL):
    auth.create_account(TEST_EMAIL, "u1", TEST_PASSWORD)

client = TestClient(app)
client.post(
    "/login",
    data={"email": TEST_EMAIL, "password": TEST_PASSWORD},
    follow_redirects=False,
)


@pytest.fixture
def successful_model(monkeypatch):
    def respond(**kwargs):
        plan = None
        if kwargs['planning_mode']:
            current = kwargs['plan']
            plan = asdict(current if current.days else DemoStore(persistence_enabled=False)._seed_plan())
            plan['status'] = '草案'
            plan['title'] = kwargs['trip'].destination + '行程'
            for day in plan['days']:
                for item in day['items']:
                    item['reason'] = '根据反馈调整'
        return AgentReply('已处理', AgentCard('itinerary-draft', '已生成新草案', '已处理', [], []), [], plan,
                          'show_plan' if plan else 'generate_plan')
    monkeypatch.setattr(importlib.import_module('app.store'), 'generate_agent_reply', respond)


def register_client(local_client: TestClient, email: str, password: str = TEST_PASSWORD):
    return local_client.post(
        "/register",
        data={"email": email, "password": password},
        follow_redirects=False,
    )


def test_sqlite_schema_uses_trip_owner_without_membership_table(tmp_path):
    db_path = tmp_path / "travelassistant.sqlite3"
    first_store = DemoStore(db_path=str(db_path), persistence_enabled=True)
    first_store.create_trip("表结构测试", "1月1日", "人均 1000", "轻松", "")

    with sqlite3.connect(db_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        trip_columns = {row[1] for row in connection.execute("PRAGMA table_info(trips)")}
        trip_count = connection.execute(
            "SELECT COUNT(*) FROM trips WHERE destination = ?", ("表结构测试",)
        ).fetchone()[0]

    assert {
        "users",
        "auth_accounts",
        "auth_sessions",
        "trips",
        "messages",
        "ideas",
        "plans",
        "plan_versions",
    }.issubset(tables)
    assert "trip_members" not in tables
    assert "owner_id" in trip_columns
    assert "initiator_id" not in trip_columns
    assert trip_count == 1


def test_auth_accounts_and_sessions_are_hashed_and_persistent(tmp_path):
    db_path = tmp_path / "auth.sqlite3"
    first_auth = AuthStore(str(db_path), persistence_enabled=True)

    assert first_auth.create_account("User@Example.com", "user-1", TEST_PASSWORD) is True
    assert first_auth.create_account("user@example.com", "user-2", TEST_PASSWORD) is False
    assert first_auth.authenticate("USER@example.com", TEST_PASSWORD) == "user-1"
    assert first_auth.authenticate("user@example.com", "wrong-password") is None
    assert first_auth.authenticate("user@example.com", TEST_PASSWORD + "x" * 80) is None

    token = first_auth.create_session("user-1")
    second_auth = AuthStore(str(db_path), persistence_enabled=True)

    assert second_auth.user_id_for_session(token) == "user-1"
    with sqlite3.connect(db_path) as connection:
        password_hash = connection.execute(
            "SELECT password_hash FROM auth_accounts WHERE email = ?",
            ("user@example.com",),
        ).fetchone()[0]
        token_hash = connection.execute("SELECT token_hash FROM auth_sessions").fetchone()[0]
    assert password_hash != TEST_PASSWORD
    assert TEST_PASSWORD not in password_hash
    assert token_hash != token


def test_sqlite_snapshot_persists_personal_trip_and_conversation(tmp_path):
    db_path = tmp_path / "travelassistant.sqlite3"
    first_store = DemoStore(db_path=str(db_path), persistence_enabled=True)
    user = first_store.create_user("持久用户")
    trip = first_store.create_trip("持久旅行", "1月1日 - 1月3日", "人均 1000", "轻松", "", user)
    first_store.add_user_message(trip.id, "这条消息需要保留", user.id)

    second_store = DemoStore(db_path=str(db_path), persistence_enabled=True)

    assert second_store.trips[trip.id].owner.name == "持久用户"
    assert any(message.body == "这条消息需要保留" for message in second_store.messages[trip.id])
    snapshot = load_snapshot(db_path)
    assert snapshot is not None
    assert snapshot["schema_version"] == CURRENT_SCHEMA_VERSION
    assert "group_members" not in snapshot
    assert "preferences" not in snapshot


def test_legacy_group_snapshot_migrates_to_owner_only(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    legacy_store = DemoStore(persistence_enabled=False)
    snapshot = serialize_state(legacy_store, USERS)
    snapshot.pop("schema_version")
    raw_trip = snapshot["trips"]["trip-hangzhou"]
    raw_trip["initiator"] = raw_trip.pop("owner")
    raw_trip["member_initials"] = ["林", "周"]
    snapshot["group_members"] = {"trip-hangzhou": ["u1", "legacy-peer"]}
    snapshot["preferences"] = {"trip-hangzhou": []}
    snapshot["users"]["legacy-peer"] = {"id": "legacy-peer", "name": "旧同行者", "initials": "旧"}
    snapshot["messages"]["trip-hangzhou"][0]["body"] = "大家 8 月底去杭州吧，我想安排两晚，节奏别太赶。"
    snapshot["messages"]["trip-hangzhou"].append(
        {
            "id": "legacy-peer-message",
            "sender": snapshot["users"]["legacy-peer"],
            "sender_type": "user",
            "body": "这是一条其他成员的旧消息",
            "created_at": "20:00",
            "status": "sent",
            "agent_card": None,
        }
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE app_snapshots (key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT)"
        )
        connection.execute(
            "INSERT INTO app_snapshots (key, payload) VALUES (?, ?)",
            ("demo_store_v1", json.dumps(snapshot, ensure_ascii=False)),
        )

    migrated = DemoStore(db_path=str(db_path), persistence_enabled=True)
    migrated_snapshot = load_snapshot(db_path)

    assert migrated.trips["trip-hangzhou"].owner.id == "u1"
    assert migrated.messages["trip-hangzhou"][0].body == "我 8 月底去杭州，想安排两晚，节奏别太赶。"
    assert all(message.sender.id != "legacy-peer" for message in migrated.messages["trip-hangzhou"])
    assert migrated_snapshot is not None
    assert migrated_snapshot["schema_version"] == CURRENT_SCHEMA_VERSION
    assert "group_members" not in migrated_snapshot


def test_health_endpoint_reports_app_status():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["trip_count"] >= 1
    assert response.json()["message_count"] >= 1
    assert response.json()["persistence"]["status"] in {"ok", "disabled"}
    assert "idea-auth" in response.json()["static_version"]


def test_static_assets_use_cache_busting_version():
    response = client.get("/trips")

    assert response.status_code == 200
    assert "/static/styles.css?v=" in response.text
    assert "/static/app.js?v=" in response.text


def test_logged_in_root_redirects_to_trip_list_and_logout_returns_to_login():
    local_client = TestClient(app)
    register_client(local_client, "root-redirect@example.com")

    response = local_client.get("/", follow_redirects=False)
    logout_response = local_client.post("/logout", follow_redirects=False)
    login_page = local_client.get("/")

    assert response.status_code == 303
    assert response.headers["location"] == "/trips"
    assert logout_response.status_code == 303
    assert login_page.status_code == 200
    assert "欢迎回来" in login_page.text


def test_login_page_describes_private_agent_product():
    anonymous = TestClient(app)
    response = anonymous.get("/")
    register_response = anonymous.get("/?mode=register")

    assert response.status_code == 200
    assert 'action="/login"' in response.text
    assert 'name="email"' in response.text
    assert 'name="password"' in response.text
    assert 'autocomplete="current-password"' in response.text
    assert "登录你的账号" not in response.text
    assert "欢迎回来" in response.text
    assert "把下一次出发，慢慢聊清楚" in response.text
    assert "auth-idea-board" in response.text
    assert "auth-idea-note--place" in response.text
    assert "auth-mode-switch" in response.text
    assert "旅行群" not in response.text
    assert register_response.status_code == 200
    assert 'action="/register"' in register_response.text
    assert 'autocomplete="new-password"' in register_response.text


def test_email_password_login_sets_secure_session_cookie_and_shows_user():
    local_client = TestClient(app)
    register_client(local_client, "xiaobai@example.com")
    local_client.post("/logout", follow_redirects=False)

    response = local_client.post(
        "/login",
        data={"email": "XIAOBAI@example.com", "password": TEST_PASSWORD},
        follow_redirects=False,
    )

    set_cookie = response.headers["set-cookie"]
    assert response.status_code == 303
    assert response.headers["location"] == "/trips"
    assert f"{SESSION_COOKIE_NAME}=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "travel_user_id" not in set_cookie
    assert "xiaobai" in local_client.get("/trips").text


def test_invalid_login_does_not_create_session():
    anonymous = TestClient(app)
    missing = anonymous.post("/login", data={"email": "", "password": ""}, follow_redirects=False)
    wrong = anonymous.post(
        "/login",
        data={"email": TEST_EMAIL, "password": "wrong-password"},
        follow_redirects=False,
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert "邮箱或密码不正确" in wrong.text
    assert SESSION_COOKIE_NAME not in wrong.headers.get("set-cookie", "")


def test_registration_validates_email_password_and_duplicate_accounts():
    invalid_email = TestClient(app).post(
        "/register",
        data={"email": "not-an-email", "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    weak_password = TestClient(app).post(
        "/register",
        data={"email": "weak@example.com", "password": "short"},
        follow_redirects=False,
    )
    local_client = TestClient(app)
    created = register_client(local_client, "duplicate@example.com")
    local_client.post("/logout", follow_redirects=False)
    duplicate = register_client(local_client, "DUPLICATE@example.com")

    assert invalid_email.status_code == 400
    assert "有效的邮箱地址" in invalid_email.text
    assert weak_password.status_code == 400
    assert "至少需要 8 个字符" in weak_password.text
    assert created.status_code == 303
    assert duplicate.status_code == 400
    assert "该邮箱已注册" in duplicate.text


def test_logout_revokes_server_session():
    local_client = TestClient(app)
    register_client(local_client, "logout@example.com")
    token = local_client.cookies.get(SESSION_COOKIE_NAME)

    response = local_client.post("/logout", follow_redirects=False)

    assert response.status_code == 303
    assert auth.user_id_for_session(token) is None
    assert local_client.get("/trips", follow_redirects=False).headers["location"] == "/"


def test_anonymous_pages_redirect_to_login_and_legacy_cookie_cannot_be_forged():
    anonymous = TestClient(app)
    anonymous.cookies.set("travel_user_id", "u1")

    for path in ("/trips", "/trips/new", "/workspace/trip-hangzhou"):
        response = anonymous.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"


def test_unknown_trip_renders_personal_travel_error_page():
    response = client.get("/workspace/not-exist")

    assert response.status_code == 404
    assert "没有找到这次旅行" in response.text
    assert "回到我的旅行" in response.text


def test_invitation_routes_are_removed():
    response = client.get("/invite/trip-hangzhou")

    assert response.status_code == 404
    assert "没有找到这次旅行" in response.text
    assert "/invite/" not in Path("app/main.py").read_text(encoding="utf-8")
    assert not Path("app/templates/invite.html").exists()


def test_trip_list_only_shows_current_users_trips():
    local_client = TestClient(app)
    register_client(local_client, "personal-list@example.com")

    before = local_client.get("/trips")
    create = local_client.post(
        "/trips",
        data={"destination": "只属于我的旅行", "date_range": "3月1日", "budget": "1000", "style": "慢游"},
        follow_redirects=False,
    )
    after = local_client.get("/trips")

    assert "杭州周末旅行" not in before.text
    assert create.headers["location"].endswith("?tab=chat")
    assert "只属于我的旅行" in after.text
    assert "杭州周末旅行" not in after.text


def test_other_user_cannot_open_or_write_trip():
    local_client = TestClient(app)
    register_client(local_client, "isolation@example.com")

    assert local_client.get("/workspace/trip-hangzhou").status_code == 404
    assert local_client.get("/api/trips/trip-hangzhou/messages").status_code == 404
    assert local_client.post("/api/trips/trip-hangzhou/messages", json={"body": "越权消息"}).status_code == 404
    assert local_client.post("/workspace/trip-hangzhou/plan").status_code == 404


def test_trip_list_and_create_page_use_personal_copy():
    list_response = client.get("/trips")
    create_response = client.get("/trips/new")

    assert list_response.status_code == 200
    assert "我的旅行" in list_response.text
    assert "创建旅行群" not in list_response.text
    assert "创建旅行" in create_response.text
    assert "创建并开始对话" in create_response.text
    assert "私人对话" in create_response.text
    assert "邀请" not in create_response.text


def test_create_trip_redirects_directly_to_private_chat():
    response = client.post(
        "/trips",
        data={
            "destination": "成都美食旅行",
            "date_range": "9月1日 - 9月3日",
            "budget": "人均 2000",
            "style": "美食、轻松",
            "note": "不要太赶",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/workspace/trip-")
    assert response.headers["location"].endswith("?tab=chat")
    assert "/invite/" not in response.headers["location"]


def test_agent_context_uses_three_personal_workspace_tabs():
    trip = store.create_trip("全局视角测试", "12月1日 - 12月3日", "人均 2600", "轻松", "")
    store.add_idea(trip.id, "想去海边散步")
    store.generate_plan(trip.id)

    context = _build_context(
        store.trips[trip.id],
        store.messages[trip.id],
        "整理一下",
        idea_cards=store.idea_cards[trip.id],
        plan=store.plans[trip.id],
        plan_versions=store.plan_versions[trip.id],
        metrics=store.trip_metrics(trip.id),
    )

    assert "应用提供的旅行上下文数据" in context
    assert "任务规则和输出格式以 system message 与当前 Skill 为准" in context
    assert "一对一私人对话" in context
    assert "想法页 / 想法墙" in context
    assert "聊天页 / 最近对话" in context
    assert "行程页 / 当前计划" in context
    assert "成员页" not in context
    assert "想去海边散步" in context


def test_plain_json_message_always_queues_agent_reply():
    trip = store.create_trip("普通消息触发测试", "12月1日 - 12月3日", "人均 2000", "轻松", "")

    response = client.post(f"/api/trips/{trip.id}/messages", json={"body": "我想住安静一点"})

    assert response.status_code == 200
    payload = response.json()["messages"]
    assert payload[0]["body"] == "我想住安静一点"
    assert payload[0]["sender_type"] == "user"
    assert payload[1]["status"] == "thinking"
    assert any(message.sender_type == "agent" and message.status == "sent" for message in store.messages[trip.id])
    assert all(message.status != "thinking" for message in store.messages[trip.id])


def test_empty_json_message_is_rejected():
    trip = store.create_trip("空消息测试", "12月1日", "1000", "轻松", "")

    response = client.post(f"/api/trips/{trip.id}/messages", json={"body": "  "})

    assert response.status_code == 400
    assert response.json()["error"] == "消息不能为空"


def test_anonymous_message_api_requires_login():
    anonymous = TestClient(app)
    response = anonymous.post("/api/trips/trip-hangzhou/messages", json={"body": "hi"})

    assert response.status_code == 401
    assert response.json()["error"] == "请先登录"


def test_finishing_agent_task_only_removes_matching_thinking_message():
    trip = store.create_trip("并发思考测试", "12月1日", "2000", "轻松", "")
    first = store.start_agent_task(trip.id)
    second = store.start_agent_task(trip.id)

    store.finish_agent_task(trip.id, "整理第一条", trip.owner.id, first.id)

    assert all(message.id != first.id for message in store.messages[trip.id])
    assert any(message.id == second.id and message.status == "thinking" for message in store.messages[trip.id])


def test_failed_agent_task_replaces_matching_thinking_message():
    trip = store.create_trip("失败兜底测试", "12月1日", "2000", "轻松", "")
    thinking = store.start_agent_task(trip.id)

    store.fail_agent_task(trip.id, thinking.id)

    assert all(message.id != thinking.id for message in store.messages[trip.id])
    assert store.messages[trip.id][-1].sender_type == "agent"
    assert "没有完成回复" in store.messages[trip.id][-1].body
    assert "@旅行助手" not in store.messages[trip.id][-1].body


def test_chat_messages_partial_returns_only_message_fragment():
    response = client.get("/workspace/trip-hangzhou/messages/partial")

    assert response.status_code == 200
    assert "data-message-id" in response.text
    assert "chat-dock" not in response.text
    assert "bottom-nav" not in response.text


def test_plain_form_message_gets_agent_reply_without_mention():
    trip = store.create_trip("表单对话测试", "12月1日", "2000", "轻松", "")
    before = len(store.messages[trip.id])

    response = client.post(
        f"/workspace/{trip.id}/messages",
        data={"body": "预算不要超过人均 1500", "sender_id": "agent"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert len(store.messages[trip.id]) == before + 2
    assert store.messages[trip.id][-2].sender.id == trip.owner.id
    assert store.messages[trip.id][-1].sender_type == "agent"
    assert "模型服务不可用" in response.text


def test_plain_plan_command_generates_itinerary(successful_model):
    trip = store.create_trip("对话生成行程测试", "10月1日 - 10月3日", "人均 2600", "轻松", "")
    store.add_idea(trip.id, "想去海边散步")

    response = client.post(
        f"/workspace/{trip.id}/messages",
        data={"body": "生成第一版行程"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert store.plans[trip.id].days
    assert store.trips[trip.id].last_activity == "Agent 已生成行程草案"
    assert "行程草案" in response.text


def test_plain_budget_message_adds_and_updates_single_idea_card(monkeypatch):
    def budget_reply(**kwargs):
        return AgentReply('预算已记录', AgentCard('requirement-summary', '预算', '', [], []),
                          [{'kind': '预算', 'title': '预算约束', 'body': kwargs['user_body']}])
    monkeypatch.setattr(importlib.import_module('app.store'), 'generate_agent_reply', budget_reply)
    trip = store.create_trip("预算更新测试", "10月1日", "人均 3000", "轻松", "")

    client.post(f"/workspace/{trip.id}/messages", data={"body": "预算不要超过人均 2000"})
    client.post(f"/workspace/{trip.id}/messages", data={"body": "预算改成人均 2500"})

    budget_cards = [card for card in store.idea_cards[trip.id] if card.kind == "预算"]
    assert len(budget_cards) == 1
    assert "2500" in budget_cards[0].body
    assert budget_cards[0].author == "对话"


def test_summary_command_organizes_single_users_recent_messages(monkeypatch):
    def summary_reply(**kwargs):
        return AgentReply('旅行偏好整理好了', AgentCard('requirement-summary', '旅行偏好整理好了', '',
                          ['预算：预算人均 2000', '地点：想去海边散步'], []), [])
    monkeypatch.setattr(importlib.import_module('app.store'), 'generate_agent_reply', summary_reply)
    trip = store.create_trip("对话整理测试", "10月1日", "人均 3000", "轻松", "")
    store.add_user_message(trip.id, "预算人均 2000", trip.owner.id, process_agent=False)
    store.add_user_message(trip.id, "想去海边散步", trip.owner.id, process_agent=False)

    response = client.post(
        f"/workspace/{trip.id}/messages",
        data={"body": "整理一下我的需求"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "旅行偏好整理好了" in response.text
    assert "预算：预算人均 2000" in response.text
    assert "地点：想去海边散步" in response.text
    assert "按成员" not in response.text


def test_workspace_defaults_to_private_chat():
    response = client.get("/workspace/trip-hangzhou")

    assert response.status_code == 200
    assert "chat-screen" in response.text
    assert "<span>私人对话</span>" not in response.text
    assert "旅行规划 Agent" in response.text
    assert "与旅行规划 Agent 的对话" in response.text
    assert "告诉 Agent 你的旅行想法" in response.text
    assert "@旅行助手" not in response.text
    assert "群聊" not in response.text
    assert "参考：当前对话 / 想法 / 行程" in response.text


def test_workspace_uses_three_item_bottom_navigation():
    expected = {
        "chat": '<a class="active" href="/workspace/trip-hangzhou?tab=chat">',
        "board": '<a class="active" href="/workspace/trip-hangzhou?tab=board">',
        "itinerary": '<a class="active" href="/workspace/trip-hangzhou?tab=itinerary">',
    }

    for tab, active_link in expected.items():
        response = client.get(f"/workspace/trip-hangzhou?tab={tab}")
        assert response.status_code == 200
        assert active_link in response.text
        assert 'aria-label="底部导航"' in response.text
        assert "tab=members" not in response.text


def test_removed_member_tab_falls_back_to_chat():
    response = client.get("/workspace/trip-hangzhou?tab=members")

    assert response.status_code == 200
    assert "chat-screen" in response.text
    assert "成员偏好" not in response.text
    assert "member-card" not in response.text


def test_idea_board_keeps_visual_workspace_and_source_details():
    response = client.get("/workspace/trip-hangzhou?tab=board")

    assert response.status_code == 200
    assert "workspace--board" in response.text
    assert "旅行想法白板" in response.text
    assert "data-route-board" in response.text
    assert "idea-wall-legend" not in response.text
    assert "data-kind=" not in response.text
    assert "写一个旅行想法" in response.text
    assert 'class="idea-detail-sheet"' in response.text
    assert "来源" in response.text
    assert "提出人" not in response.text


def test_agent_idea_matches_manual_card_across_types():
    local_store = DemoStore(persistence_enabled=False)
    trip = local_store.create_trip("济南", "待定", "待定", "轻松", "")
    manual = local_store.add_idea(trip.id, "爬山")
    local_store._add_agent_idea_cards(trip.id, [
        {"kind": "地点", "title": "爬山", "body": "想安排一次爬山", "status": "已整理"}
    ])
    cards = local_store.idea_cards[trip.id]
    assert len(cards) == 1
    assert cards[0].id == manual.id
    assert cards[0].author == "你"
    assert cards[0].body == "想安排一次爬山"


def test_duplicate_idea_migration_preserves_details(tmp_path):
    db_path = str(tmp_path / "ideas.sqlite3")
    local_store = DemoStore(db_path=db_path, persistence_enabled=True)
    trip = local_store.create_trip("济南", "待定", "待定", "轻松", "")
    local_store.add_idea(trip.id, "爬山")
    local_store.add_idea(trip.id, "爬山")
    restored = DemoStore(db_path=db_path, persistence_enabled=True)
    assert len(restored.idea_cards[trip.id]) == 1
    assert restored.idea_cards[trip.id][0].body == "爬山"
    reopened = DemoStore(db_path=db_path, persistence_enabled=True)
    assert len(reopened.idea_cards[trip.id]) == 1


def test_idea_board_accepts_new_idea_and_stays_on_board():
    trip = store.create_trip("想法墙测试", "10月1日", "3000", "轻松", "")

    response = client.post(
        f"/workspace/{trip.id}/ideas",
        data={"body": "第二天别太早起"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert response.url.query == b"tab=board"
    assert "第二天别太早起" in response.text
    assert store.idea_cards[trip.id][-1].author == "你"


def test_empty_itinerary_offers_chat_and_generate_actions():
    trip = store.create_trip("空行程入口测试", "11月1日", "人均 2200", "轻松", "")

    response = client.get(f"/workspace/{trip.id}?tab=itinerary")

    assert response.status_code == 200
    assert "还没有行程" in response.text
    assert "让 Agent 生成第一版" in response.text
    assert "回到对话补充需求" in response.text
    assert "成员" not in response.text


def test_generate_plan_updates_itinerary_and_saves_versions(successful_model):
    trip = store.create_trip("青岛海边旅行", "10月1日 - 10月3日", "人均 2500", "轻松", "")
    store.add_idea(trip.id, "想去栈桥看海")

    first = client.post(f"/workspace/{trip.id}/plan", follow_redirects=True)
    store.add_idea(trip.id, "想逛老城")
    second = client.post(f"/workspace/{trip.id}/plan", follow_redirects=True)

    assert first.status_code == 200
    assert second.status_code == 200
    assert store.plans[trip.id].status == "草案"
    assert [version.label for version in store.plan_versions[trip.id]] == ["v1", "v2"]
    assert "济南 6 天 5 晚情侣泉景美食之旅" not in second.text
    assert "行程概览" in second.text
    assert "天已安排" in second.text
    assert "个行程安排" in second.text
    assert "本版变化" not in second.text
    assert "版本记录 · 2 个版本" in second.text
    assert "版本记录" in second.text
    assert "想逛老城" in store.plan_versions[trip.id][-1].change_summary


def test_revision_feedback_generates_new_plan_version(successful_model):
    trip = store.create_trip("反馈改行程测试", "11月1日 - 11月3日", "人均 2200", "轻松", "")
    store.add_idea(trip.id, "想去古城散步")
    client.post(f"/workspace/{trip.id}/plan")
    item_id = store.plans[trip.id].days[0].items[0].id

    response = client.post(
        f"/workspace/{trip.id}/revision",
        data={"item_id": item_id, "feedback": "这个太赶，换成室内并留休息时间"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert len(store.plan_versions[trip.id]) == 2
    assert store.messages[trip.id][-1].agent_card.title == "已生成新草案"
    revision_cards = [card for card in store.idea_cards[trip.id] if card.status == "修改反馈"]
    assert revision_cards[-1].author == "你的反馈"


def test_confirm_and_restore_plan_versions(successful_model):
    trip = store.create_trip("确认与恢复测试", "11月1日", "2200", "轻松", "")
    store.add_idea(trip.id, "想去古城散步")
    client.post(f"/workspace/{trip.id}/plan")
    first_version_id = store.plan_versions[trip.id][0].id
    store.add_idea(trip.id, "想去洱海骑行")
    client.post(f"/workspace/{trip.id}/plan")

    confirm = client.post(f"/workspace/{trip.id}/plan/confirm", follow_redirects=True)
    confirmed_activity = store.trips[trip.id].last_activity
    confirmed_status = store.plans[trip.id].status
    restore = client.post(
        f"/workspace/{trip.id}/plan/restore",
        data={"version_id": first_version_id},
        follow_redirects=True,
    )

    assert confirm.status_code == 200
    assert confirmed_activity == "你已确认当前行程"
    assert confirmed_status == "已确认"
    assert restore.status_code == 200
    assert store.plans[trip.id].status == "草案"
    assert store.plan_versions[trip.id][-1].change_summary == "从 v1 恢复为新草案"


def test_idea_board_keeps_at_most_eight_cards():
    trip = store.create_trip("八张想法墙", "10月1日", "3000", "轻松", "")

    for index in range(10):
        store.add_idea(trip.id, f"新增想法 {index}")

    assert len(store.idea_cards[trip.id]) == 8
    assert store.idea_cards[trip.id][-1].title == "新增想法 9"


def test_frontend_assets_reflect_solo_chat_flow():
    css = Path("app/static/styles.css").read_text(encoding="utf-8")
    js = Path("app/static/app.js").read_text(encoding="utf-8")

    assert ".bottom-nav" in css
    assert "grid-template-columns: repeat(3, 1fr)" in css
    assert "appendAgentThinkingMessage" in js
    assert "await waitForNextPaint();\n  const thinkingMessage = appendAgentThinkingMessage();" in js
    assert "selectedMember" not in js
    assert "isAgentMentionValue" not in js
    assert "copyInviteLink" not in js
    assert "data-copy-button" not in js
    assert "发送失败，点此重试" in js
    assert "startChatPolling" in js
    assert "requestAnimationFrame(drawIdeaRoutes)" in js


def test_itinerary_page_renders_requirement_labels_not_member_names():
    response = client.get("/workspace/trip-hangzhou?tab=itinerary")

    assert response.status_code == 200
    assert "杭州 3 天 2 晚轻松行程" not in response.text
    assert "西湖傍晚散步" in response.text
    assert "对应偏好" in response.text
    assert "茶园体验" in response.text
    assert "满足需求" not in response.text


# --- search_food tool (Amap) ---

from urllib.error import URLError

from app.tools import food
from app.tools.registry import run_tool


FAKE_AMAP_PAYLOAD = {
    "status": "1",
    "infocode": "10000",
    "pois": [
        {
            "name": "评分最高店",
            "address": "某路 1 号",
            "adname": "武侯区",
            "business_area": "紫荆",
            "atag": "牛肉,毛肚",
            "tel": "028-12345678",
            "biz_ext": {"rating": "4.9", "cost": "82.00", "opentime2": "周一至周日 11:00-23:00"},
        },
        {
            "name": "评分次高店",
            "address": "某路 2 号",
            "adname": "锦江区",
            "business_area": "海椒市",
            "atag": "兔腰",
            "tel": "",
            "biz_ext": {"rating": "4.8", "cost": "99.00", "opentime2": ""},
        },
        {
            "name": "无评分店",
            "address": "某路 3 号",
            "biz_ext": {},
        },
    ],
}


def test_search_food_sorts_by_rating_and_skips_unrated(monkeypatch):
    monkeypatch.setenv("AMAP_MAP_KEY", "test-key")
    monkeypatch.setattr(food, "_fetch_json", lambda params: FAKE_AMAP_PAYLOAD)

    result = run_tool("search_food", {"city": "成都", "keywords": "火锅", "max_results": 2})

    assert result["status"] == "available"
    assert [item["name"] for item in result["recommendations"]] == ["评分最高店", "评分次高店"]
    assert result["recommendations"][0]["rating"] == "4.9"
    assert result["recommendations"][0]["cost_per_person_cny"] == 82.0
    assert result["recommendations"][1]["tel"] is None
    assert result["total_matched"] == 3


def test_search_food_clamps_max_results(monkeypatch):
    monkeypatch.setenv("AMAP_MAP_KEY", "test-key")
    monkeypatch.setattr(food, "_fetch_json", lambda params: FAKE_AMAP_PAYLOAD)

    result = run_tool("search_food", {"city": "成都", "max_results": 999})

    assert result["status"] == "available"
    assert len(result["recommendations"]) <= 10


def test_search_food_requires_city(monkeypatch):
    monkeypatch.setenv("AMAP_MAP_KEY", "test-key")

    result = run_tool("search_food", {"city": "  "})

    assert result["status"] == "unavailable"
    assert "城市" in result["reason"]


def test_search_food_without_amap_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("AMAP_MAP_KEY", raising=False)

    result = run_tool("search_food", {"city": "成都"})

    assert result["status"] == "unavailable"
    assert "AMAP_MAP_KEY" in result["reason"]


def test_search_food_handles_network_error(monkeypatch):
    monkeypatch.setenv("AMAP_MAP_KEY", "test-key")

    def boom(params):
        raise URLError("connection refused")

    monkeypatch.setattr(food, "_fetch_json", boom)

    result = run_tool("search_food", {"city": "成都", "keywords": "火锅"})

    assert result["status"] == "unavailable"
    assert result["city"] == "成都"
    assert "再次确认" in result["reason"]


def test_search_food_reports_amap_failure(monkeypatch):
    monkeypatch.setenv("AMAP_MAP_KEY", "test-key")
    monkeypatch.setattr(food, "_fetch_json", lambda params: {"status": "0", "info": "INVALID_USER_KEY"})

    result = run_tool("search_food", {"city": "成都"})

    assert result["status"] == "unavailable"
    assert "INVALID_USER_KEY" in result["reason"]
