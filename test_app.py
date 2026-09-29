from pathlib import Path

from fastapi.testclient import TestClient

from app.agent_client import _build_context
from app.main import app, invite_token_for
from app.store import DemoStore, USERS, store


client = TestClient(app)
client.post("/login", data={"nickname": "林夏"}, follow_redirects=False)


def test_sqlite_relational_schema_is_created(tmp_path):
    import sqlite3

    db_path = tmp_path / "travelassistant.sqlite3"
    first_store = DemoStore(db_path=str(db_path), persistence_enabled=True)
    first_store.create_trip("表结构测试", "1月1日", "人均 1000", "轻松", "")

    with sqlite3.connect(db_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    assert {"users", "trips", "trip_members", "messages", "ideas", "plans", "plan_versions"}.issubset(tables)
    assert connection.execute("SELECT COUNT(*) FROM trips WHERE destination = ?", ("表结构测试",)).fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] >= 1
    assert connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0] >= 1


def test_sqlite_snapshot_persists_created_trip_and_messages(tmp_path):
    db_path = tmp_path / "travelassistant.sqlite3"
    first_store = DemoStore(db_path=str(db_path), persistence_enabled=True)
    user = first_store.get_or_create_user("持久用户")
    trip = first_store.create_trip("持久旅行", "1月1日 - 1月3日", "人均 1000", "轻松", "")
    first_store.add_member(trip.id, user)
    first_store.add_user_message(trip.id, "这条消息需要保留", user.id)

    second_store = DemoStore(db_path=str(db_path), persistence_enabled=True)

    assert db_path.exists()
    assert second_store.trips[trip.id].destination == "持久旅行"
    assert second_store.messages[trip.id][-1].body == "这条消息需要保留"
    assert any(preference.member.name == "持久用户" for preference in second_store.preferences[trip.id])


def test_health_endpoint_reports_app_status():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["trip_count"] >= 1
    assert response.json()["message_count"] >= 1
    assert response.json()["persistence"]["status"] in {"ok", "disabled"}
    assert response.json()["static_version"]


def test_static_assets_use_cache_busting_version():
    response = client.get("/trips")

    assert response.status_code == 200
    assert "/static/styles.css?v=" in response.text
    assert "/static/app.js?v=" in response.text


def test_logged_in_root_redirects_to_trip_list_unless_switching_user():
    local_client = TestClient(app)
    local_client.post("/login", data={"nickname": "已登录用户"}, follow_redirects=False)

    response = local_client.get("/", follow_redirects=False)
    switch_response = local_client.get("/?switch=1", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/trips"
    assert switch_response.status_code == 200
    assert "你的昵称" in switch_response.text


def test_favicon_does_not_log_as_missing_route():
    response = client.get("/favicon.ico")

    assert response.status_code == 204


def test_login_page_renders_nickname_form():
    anonymous = TestClient(app)
    response = anonymous.get("/")

    assert response.status_code == 200
    assert 'action="/login"' in response.text
    assert 'name="nickname"' in response.text
    assert "进入旅行群" in response.text


def test_nickname_login_sets_cookie_and_shows_user():
    local_client = TestClient(app)
    response = local_client.post("/login", data={"nickname": "小白"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/trips"
    assert "travel_user_id" in response.headers["set-cookie"]

    follow = local_client.get("/trips")
    assert "小白" in follow.text


def test_invite_page_shows_copyable_share_link():
    trip = store.create_trip("分享邀请测试", "12月1日 - 12月3日", "人均 2000", "轻松", "")

    response = client.get(f"/invite/{trip.id}")

    assert response.status_code == 200
    assert "share-card" in response.text
    assert "复制这个链接发给朋友" in response.text
    assert "data-copy-source" in response.text
    assert "data-copy-button" in response.text
    assert "?token=" in response.text
    assert f"/invite/{trip.id}" in response.text


def test_unknown_trip_renders_friendly_error_page():
    response = client.get("/workspace/not-exist")

    assert response.status_code == 404
    assert "没有找到这个旅行群" in response.text
    assert "这个链接可能已经失效" in response.text
    assert "回到旅行群" in response.text


def test_invite_join_adds_current_user_to_trip():
    local_client = TestClient(app)
    local_client.post("/login", data={"nickname": "阿宁"}, follow_redirects=False)
    trip = store.create_trip("邀请加入测试", "12月1日 - 12月3日", "人均 2000", "轻松", "")

    response = local_client.post(
        f"/invite/{trip.id}/join", data={"token": invite_token_for(trip.id)}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/workspace/{trip.id}?tab=chat"
    assert "阿" in store.trips[trip.id].member_initials
    assert any(preference.member.name == "阿宁" for preference in store.preferences[trip.id])


def test_cookie_user_sends_chat_without_member_selector():
    local_client = TestClient(app)
    local_client.post("/login", data={"nickname": "小秋"}, follow_redirects=False)
    trip = store.create_trip("当前用户发言测试", "12月1日 - 12月3日", "人均 2000", "轻松", "")

    response = local_client.post(
        f"/workspace/{trip.id}/messages",
        data={"body": "我想住安静一点"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert store.messages[trip.id][-1].sender.name == "小秋"
    assert "小秋" in response.text
    assert "data-current-name" in response.text
    assert "data-member-select" not in response.text


def test_anonymous_routes_redirect_to_login():
    anonymous = TestClient(app)

    for path in ("/trips", "/trips/new", "/workspace/trip-hangzhou"):
        response = anonymous.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"


def test_empty_nickname_does_not_set_login_cookie():
    anonymous = TestClient(app)
    response = anonymous.post("/login", data={"nickname": "   "}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "travel_user_id" not in response.headers.get("set-cookie", "")


def test_unknown_trip_routes_return_404():
    checked_client = TestClient(app)
    checked_client.post("/login", data={"nickname": "查错用户"}, follow_redirects=False)

    assert checked_client.get("/workspace/not-exist").status_code == 404
    assert checked_client.get("/invite/not-exist").status_code == 404
    assert checked_client.post("/workspace/not-exist/messages", data={"body": "hi"}).status_code == 404
    assert checked_client.post("/workspace/not-exist/plan").status_code == 404


def test_invite_join_rejects_invalid_token():
    trip = store.create_trip("错误邀请测试", "12月1日 - 12月3日", "人均 2000", "轻松", "")

    response = client.post(f"/invite/{trip.id}/join", data={"token": "bad-token"})

    assert response.status_code == 403
    assert "这个邀请链接不可用" in response.text


def test_anonymous_invite_join_redirects_to_login_without_adding_member():
    trip = store.create_trip("匿名加入测试", "12月1日 - 12月3日", "人均 2000", "轻松", "")
    before = list(store.trips[trip.id].member_initials)
    anonymous = TestClient(app)

    response = anonymous.post(f"/invite/{trip.id}/join", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert store.trips[trip.id].member_initials == before


def test_anonymous_message_sender_id_redirects_without_writing():
    trip = store.create_trip("匿名伪造发言测试", "12月1日 - 12月3日", "人均 2000", "轻松", "")
    before = len(store.messages[trip.id])
    anonymous = TestClient(app)

    response = anonymous.post(
        f"/workspace/{trip.id}/messages",
        data={"body": "匿名伪造", "sender_id": "u1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert len(store.messages[trip.id]) == before


def test_trip_list_page_renders_seed_trip():
    response = client.get("/trips")

    assert response.status_code == 200
    assert "杭州周末旅行" in response.text
    assert "Agent 已生成初版行程" in response.text


def test_trip_list_uses_compact_meta_chips():
    response = client.get("/trips")

    assert response.status_code == 200
    assert "trip-meta-row" in response.text
    assert "人均 1500" in response.text
    assert "轻松、少排队、适合聊天" in response.text


def test_create_trip_page_has_lightweight_context_helper():
    response = client.get("/trips/new")

    assert response.status_code == 200
    assert "form-helper-card" in response.text
    assert "先不用填很完整" in response.text
    assert "不能接受" in response.text


def test_create_trip_redirects_to_invitation():
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
    assert response.headers["location"].startswith("/invite/trip-")


def test_agent_context_includes_all_workspace_tabs():
    trip = store.create_trip("全局视角测试", "12月1日 - 12月3日", "人均 2600", "轻松", "")
    store.add_idea(trip.id, "想去海边散步")
    store.generate_plan(trip.id)

    context = _build_context(
        store.trips[trip.id],
        store.messages[trip.id],
        "@旅行助手 整理一下",
        idea_cards=store.idea_cards[trip.id],
        preferences=store.preferences[trip.id],
        plan=store.plans[trip.id],
        plan_versions=store.plan_versions[trip.id],
        metrics=store.trip_metrics(trip.id),
    )

    assert "四个导航页的全局上下文" in context
    assert "想法页 / 想法墙" in context
    assert "聊天页 / 最近消息" in context
    assert "行程页 / 当前计划" in context
    assert "行程页 / 版本历史" in context
    assert "成员页 / 成员偏好" in context
    assert "想去海边散步" in context
    assert store.plans[trip.id].title in context


def test_message_json_api_sends_without_page_reload():
    trip = store.create_trip("异步消息测试", "12月1日 - 12月3日", "人均 2000", "轻松", "")

    response = client.post(f"/api/trips/{trip.id}/messages", json={"body": "异步发一条消息"})

    assert response.status_code == 200
    data = response.json()
    assert data["messages"][0]["body"] == "异步发一条消息"
    assert data["messages"][0]["sender"]["name"] == "林夏"
    assert store.messages[trip.id][-1].body == "异步发一条消息"


def test_message_json_api_queues_agent_thinking_message():
    trip = store.create_trip("异步 Agent 测试", "12月1日 - 12月3日", "人均 2000", "轻松", "")

    response = client.post(f"/api/trips/{trip.id}/messages", json={"body": "@旅行助手 整理一下"})

    assert response.status_code == 200
    statuses = [message["status"] for message in response.json()["messages"]]
    assert "thinking" in statuses
    assert any(message.sender_type == "agent" for message in store.messages[trip.id])


def test_finishing_agent_task_only_removes_matching_thinking_message():
    trip = store.create_trip("并发思考测试", "12月1日 - 12月3日", "人均 2000", "轻松", "")
    first = store.start_agent_task(trip.id, "@旅行助手 整理 A", USERS["u1"])
    second = store.start_agent_task(trip.id, "@旅行助手 整理 B", USERS["u2"])

    store.finish_agent_task(trip.id, "@旅行助手 整理 A", "u1", first.id)

    assert all(message.id != first.id for message in store.messages[trip.id])
    assert any(message.id == second.id and message.status == "thinking" for message in store.messages[trip.id])


def test_failed_agent_task_replaces_matching_thinking_message():
    trip = store.create_trip("失败兜底测试", "12月1日 - 12月3日", "人均 2000", "轻松", "")
    thinking = store.start_agent_task(trip.id, "@旅行助手 整理", USERS["u1"])

    store.fail_agent_task(trip.id, thinking.id)

    assert all(message.id != thinking.id for message in store.messages[trip.id])
    assert store.messages[trip.id][-1].sender_type == "agent"
    assert "整理失败" in store.messages[trip.id][-1].body


def test_anonymous_message_json_api_requires_login():
    anonymous = TestClient(app)
    response = anonymous.post("/api/trips/trip-hangzhou/messages", json={"body": "hi"})

    assert response.status_code == 401
    assert response.json()["error"] == "请先登录"


def test_chat_messages_partial_returns_message_fragment():
    response = client.get("/workspace/trip-hangzhou/messages/partial")

    assert response.status_code == 200
    assert "data-message-id" in response.text
    assert "chat-dock" not in response.text
    assert "bottom-nav" not in response.text


def test_anonymous_chat_messages_partial_redirects_to_login():
    anonymous = TestClient(app)
    response = anonymous.get("/workspace/trip-hangzhou/messages/partial", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_workspace_chat_accepts_message_and_agent_replies():
    response = client.post(
        "/workspace/trip-hangzhou/messages",
        data={"body": "预算不要超过人均 1500"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "预算不要超过人均 1500" in response.text
    assert "预算约束已更新" not in response.text


def test_workspace_chat_at_agent_triggers_agent_reply():
    response = client.post(
        "/workspace/trip-hangzhou/messages",
        data={"body": "@旅行助手 预算不要超过人均 1500"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "@旅行助手 预算不要超过人均 1500" in response.text
    assert "预算约束已更新" in response.text


def test_group_chat_supports_multiple_member_senders():
    trip = store.create_trip("多人发言测试", "10月1日 - 10月3日", "人均 3000", "轻松", "")

    response = client.post(
        f"/workspace/{trip.id}/messages",
        data={"body": "我想住得离地铁近一点", "sender_id": "u2"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert store.messages[trip.id][-1].sender.name == "周予"
    assert "周予" in response.text
    assert "我想住得离地铁近一点" in response.text


def test_agent_plan_command_generates_itinerary_from_chat():
    trip = store.create_trip("群聊生成行程测试", "10月1日 - 10月3日", "人均 2600", "轻松", "")
    store.add_idea(trip.id, "想去海边散步")

    response = client.post(
        f"/workspace/{trip.id}/messages",
        data={"body": "@旅行助手 生成第一版行程", "sender_id": "u3"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert store.plans[trip.id].days
    assert store.trips[trip.id].last_activity == "Agent 已生成行程草案"
    assert "行程草案" in response.text


def test_agent_trigger_adds_idea_card_from_chat():
    trip = store.create_trip("测试抽取想法", "10月1日 - 10月3日", "人均 3000", "轻松", "")

    response = client.post(
        f"/workspace/{trip.id}/messages",
        data={"body": "@旅行助手 预算不要超过人均 2000"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert any(card.kind == "预算" and "2000" in card.body for card in store.idea_cards[trip.id])


def test_agent_budget_idea_updates_instead_of_duplicate():
    trip = store.create_trip("测试预算更新", "10月1日 - 10月3日", "人均 3000", "轻松", "")

    client.post(
        f"/workspace/{trip.id}/messages",
        data={"body": "@旅行助手 预算不要超过人均 2000"},
        follow_redirects=True,
    )
    client.post(
        f"/workspace/{trip.id}/messages",
        data={"body": "@旅行助手 预算改成人均 2500"},
        follow_redirects=True,
    )

    budget_cards = [card for card in store.idea_cards[trip.id] if card.kind == "预算"]
    assert len(budget_cards) == 1
    assert "2500" in budget_cards[0].body


def test_agent_syncs_preferences_to_triggering_member():
    trip = store.create_trip("成员归属测试", "10月1日 - 10月3日", "人均 3000", "轻松", "")

    client.post(
        f"/workspace/{trip.id}/messages",
        data={"body": "@旅行助手 预算不要超过人均 1800", "sender_id": "u2"},
        follow_redirects=True,
    )

    preferences = {preference.member.name: preference for preference in store.preferences[trip.id]}
    assert "预算不要超过人均 1800" in preferences["周予"].known
    assert "预算" not in preferences["周予"].missing
    assert "预算不要超过人均 1800" not in preferences["林夏"].known


def test_agent_summary_command_splits_recent_chat_by_member():
    trip = store.create_trip("整理归属测试", "10月1日 - 10月3日", "人均 3000", "轻松", "")

    client.post(f"/workspace/{trip.id}/messages", data={"body": "预算人均 2000", "sender_id": "u1"}, follow_redirects=True)
    client.post(f"/workspace/{trip.id}/messages", data={"body": "想去海边散步", "sender_id": "u2"}, follow_redirects=True)
    response = client.post(
        f"/workspace/{trip.id}/messages",
        data={"body": "@旅行助手 整理一下大家的需求", "sender_id": "u3"},
        follow_redirects=True,
    )

    preferences = {preference.member.name: preference for preference in store.preferences[trip.id]}
    assert response.status_code == 200
    assert "按成员整理好了" in response.text
    assert "预算人均 2000" in preferences["林夏"].known
    assert "地点偏好" in preferences["周予"].known
    assert "林夏：预算人均 2000" in response.text
    assert "周予：地点偏好" in response.text


def test_plan_versions_record_specific_change_summary():
    trip = store.create_trip("版本摘要测试", "11月1日 - 11月3日", "人均 2200", "轻松", "")
    store.add_idea(trip.id, "想去古城散步")
    client.post(f"/workspace/{trip.id}/plan", follow_redirects=True)
    store.add_idea(trip.id, "想去洱海骑行")

    response = client.post(f"/workspace/{trip.id}/plan", follow_redirects=True)

    assert response.status_code == 200
    assert store.plan_versions[trip.id][-1].change_summary.startswith("第 2 版")
    assert "想去洱海骑行" in store.plan_versions[trip.id][-1].change_summary
    assert "根据想去古城散步、想去洱海骑行等新想法调整" in response.text


def test_agent_idea_syncs_to_member_preferences():
    trip = store.create_trip("测试成员偏好", "10月1日 - 10月3日", "人均 3000", "轻松", "")

    client.post(
        f"/workspace/{trip.id}/messages",
        data={"body": "@旅行助手 预算不要超过人均 2000"},
        follow_redirects=True,
    )
    response = client.get(f"/workspace/{trip.id}?tab=members")

    assert response.status_code == 200
    assert "预算不要超过人均 2000" in response.text
    assert "预算" not in store.preferences[trip.id][0].missing


def test_agent_conflict_idea_syncs_to_member_conflicts():
    trip = store.create_trip("测试成员冲突", "10月1日 - 10月3日", "人均 3000", "轻松", "")

    client.post(
        f"/workspace/{trip.id}/messages",
        data={"body": "@旅行助手 不要每天太累，不能连续换地方"},
        follow_redirects=True,
    )

    assert any("不要每天太累" in item for item in store.preferences[trip.id][0].conflicts)


def test_workspace_chat_renders_travel_assistant_status():
    response = client.get("/workspace/trip-hangzhou?tab=chat")

    assert response.status_code == 200
    assert "听大家说想法" not in response.text
    assert "整理偏好" not in response.text
    assert "安排行程" not in response.text
    assert "等大家确认" not in response.text
    assert "大家先自由聊；需要我介入时" not in response.text
    assert "Agent 工作台" not in response.text


def test_new_trip_agent_intro_explains_how_to_wake_agent():
    trip = store.create_trip("厦门家庭旅行", "10月1日 - 10月3日", "人均 3000", "轻松", "带老人")

    response = client.get(f"/workspace/{trip.id}?tab=chat")

    assert response.status_code == 200
    assert "大家可以先自由聊" in response.text
    assert "@旅行助手" in response.text
    assert "普通聊天不会自动触发 Agent" in response.text


def test_workspace_chat_stays_focused_on_plain_conversation():
    response = client.get("/workspace/trip-hangzhou?tab=chat")

    assert response.status_code == 200
    assert "chat-dock" in response.text
    assert "群聊记录" in response.text
    assert "chat-agent-card" in response.text
    assert "参考：聊天 / 想法 / 成员 / 行程" in response.text
    assert "data-chat-list" in response.text
    assert "messages/partial" in response.text
    assert "data-api-url" in response.text
    assert "点开看要点" in Path("app/static/styles.css").read_text(encoding="utf-8")
    assert "发消息，或输入 @旅行助手 叫醒 Agent" in response.text
    assert "quick-prompts" not in response.text
    assert "agent-command-strip" not in response.text
    assert "整理需求" not in response.text
    assert "找冲突" not in response.text
    assert "生成行程" not in response.text
    assert "chat-progress" not in response.text
    assert "data-current-name" in response.text
    assert "composer-user" not in response.text
    assert "data-member-select" not in response.text
    assert "思考中" in Path("app/static/app.js").read_text(encoding="utf-8")


def test_workspace_secondary_pages_share_mobile_bottom_navigation():
    for tab in ("chat", "itinerary", "members"):
        response = client.get(f"/workspace/trip-hangzhou?tab={tab}")

        assert response.status_code == 200
        assert 'aria-label="底部导航"' in response.text
        assert 'class="tabbar"' not in response.text
        assert 'class="trip-command"' not in response.text
        assert 'class="agent-rail"' not in response.text
        assert "Agent 工作台" not in response.text


def test_workspace_secondary_pages_mark_active_bottom_nav_item():
    expected = {
        "chat": '<a class="active" href="/workspace/trip-hangzhou?tab=chat">',
        "itinerary": '<a class="active" href="/workspace/trip-hangzhou?tab=itinerary">',
        "members": '<a class="active" href="/workspace/trip-hangzhou?tab=members">',
    }

    for tab, active_link in expected.items():
        response = client.get(f"/workspace/trip-hangzhou?tab={tab}")

        assert response.status_code == 200
        assert active_link in response.text


def test_workspace_defaults_to_idea_board():
    response = client.get("/workspace/trip-hangzhou")

    assert response.status_code == 200
    assert "workspace--board" in response.text
    assert "旅行想法白板" in response.text
    assert "Agent 已归类" not in response.text
    assert "wall-agent-summary" not in response.text
    assert "地点" in response.text
    assert "预算" in response.text
    assert "节奏" in response.text
    assert "禁忌" in response.text
    assert "生成计划" in response.text
    assert "听大家说想法" not in response.text
    assert "整理偏好" not in response.text
    assert "安排行程" not in response.text
    assert "底部导航" in response.text
    assert "board-switcher" not in response.text
    assert "data-route-board" in response.text
    assert "data-route-index" in response.text
    assert "idea-wall-legend" in response.text
    assert "写一个旅行想法" in response.text
    assert "M70 92" not in response.text
    assert "--x:" not in response.text
    assert "--y:" not in response.text


def test_idea_cards_expose_details_for_bottom_sheet():
    response = client.get("/workspace/trip-hangzhou")

    assert response.status_code == 200
    assert 'data-idea-detail-trigger' in response.text
    assert 'class="idea-detail-sheet"' in response.text
    assert 'aria-label="想法详情"' in response.text
    assert "提出人" in response.text
    assert "采纳状态" in response.text
    assert "idea-detail-statusbar" in response.text
    assert "第一天傍晚去断桥和白堤" in response.text


def test_idea_board_accepts_new_idea_and_stays_on_board():
    response = client.post(
        "/workspace/trip-hangzhou/ideas",
        data={"body": "第二天别太早起"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert response.url.path == "/workspace/trip-hangzhou"
    assert response.url.query == b"tab=board"
    assert "第二天别太早起" in response.text
    assert "待归类" in response.text


def test_empty_itinerary_page_offers_generate_plan_action():
    trip = store.create_trip("空行程入口测试", "11月1日 - 11月3日", "人均 2200", "轻松", "")

    response = client.get(f"/workspace/{trip.id}?tab=itinerary")

    assert response.status_code == 200
    assert "还没有行程" in response.text
    assert "让 Agent 生成第一版" in response.text
    assert f'action="/workspace/{trip.id}/plan"' in response.text
    assert "回到群聊补充需求" in response.text


def test_generate_plan_updates_itinerary_from_ideas():
    trip = store.create_trip("青岛海边旅行", "10月1日 - 10月3日", "人均 2500", "轻松", "")
    store.add_idea(trip.id, "想去栈桥看海")

    response = client.post(f"/workspace/{trip.id}/plan", follow_redirects=True)

    assert response.status_code == 200
    assert response.url.path == f"/workspace/{trip.id}"
    assert response.url.query == b"tab=itinerary"
    assert "青岛海边旅行行程草案" in response.text
    assert "想去栈桥看海" in response.text
    assert store.plans[trip.id].status == "草案"


def test_generate_plan_saves_version_history():
    trip = store.create_trip("大理慢旅行", "11月1日 - 11月3日", "人均 2200", "轻松", "")
    store.add_idea(trip.id, "想去洱海骑行")

    client.post(f"/workspace/{trip.id}/plan", follow_redirects=True)
    response = client.post(f"/workspace/{trip.id}/plan", follow_redirects=True)

    assert response.status_code == 200
    assert [version.label for version in store.plan_versions[trip.id]] == ["v1", "v2"]
    assert "当前版本 v2" in response.text
    assert "版本记录" in response.text
    assert "当前版本变化" in response.text
    assert "个安排" in response.text


def test_revision_feedback_generates_new_plan_version():
    local_client = TestClient(app)
    local_client.post("/login", data={"nickname": "周修改"}, follow_redirects=False)
    trip = store.create_trip("反馈改行程测试", "11月1日 - 11月3日", "人均 2200", "轻松", "")
    local_client.post(f"/invite/{trip.id}/join", data={"token": invite_token_for(trip.id)}, follow_redirects=False)
    store.add_idea(trip.id, "想去古城散步")
    client.post(f"/workspace/{trip.id}/plan", follow_redirects=True)
    item_id = store.plans[trip.id].days[0].items[0].id
    item_title = store.plans[trip.id].days[0].items[0].title

    response = local_client.post(
        f"/workspace/{trip.id}/revision",
        data={"item_id": item_id, "feedback": "这个太赶，换成室内并留休息时间"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert store.plans[trip.id].status == "草案"
    assert len(store.plan_versions[trip.id]) == 2
    assert store.plan_versions[trip.id][-1].change_summary.startswith("第 2 版")
    assert item_title in store.plan_versions[trip.id][-1].change_summary
    assert "这个太赶，换成室内并留休息时间" in response.text
    assert "当前版本变化" in response.text
    assert store.messages[trip.id][-1].agent_card.title == "已生成新草案"
    revision_cards = [card for card in store.idea_cards[trip.id] if card.status == "修改反馈"]
    assert revision_cards
    assert revision_cards[-1].author == "周修改"


def test_itinerary_page_has_revision_feedback_form():
    response = client.get("/workspace/trip-hangzhou?tab=itinerary")

    assert response.status_code == 200
    assert "revision-drawer" in response.text
    assert "revision-form" in response.text
    assert "让 Agent 调整这项" in response.text
    assert "提交修改意见" in response.text


def test_confirm_plan_marks_current_plan_confirmed():
    trip = store.create_trip("确认计划测试", "11月1日 - 11月3日", "人均 2200", "轻松", "")
    store.add_idea(trip.id, "想去古城散步")
    client.post(f"/workspace/{trip.id}/plan", follow_redirects=True)

    response = client.post(f"/workspace/{trip.id}/plan/confirm", follow_redirects=True)

    assert response.status_code == 200
    assert store.plans[trip.id].status == "已确认"
    assert store.trips[trip.id].status == "已确认"
    assert "已确认" in response.text
    assert store.plan_versions[trip.id][-1].change_summary == "确认当前行程"


def test_itinerary_version_history_is_collapsed_by_default():
    trip = store.create_trip("版本收起测试", "11月1日 - 11月3日", "人均 2200", "轻松", "")
    store.add_idea(trip.id, "想去古城散步")
    client.post(f"/workspace/{trip.id}/plan", follow_redirects=True)
    store.add_idea(trip.id, "想去洱海骑行")
    response = client.post(f"/workspace/{trip.id}/plan", follow_redirects=True)

    assert response.status_code == 200
    assert "version-history" in response.text
    assert "版本记录 · 2 个版本" in response.text
    assert '<details class="version-history"' in response.text


def test_restore_plan_version_creates_new_draft_from_old_version():
    trip = store.create_trip("回退计划测试", "11月1日 - 11月3日", "人均 2200", "轻松", "")
    store.add_idea(trip.id, "想去古城散步")
    client.post(f"/workspace/{trip.id}/plan", follow_redirects=True)
    first_version_id = store.plan_versions[trip.id][0].id
    store.add_idea(trip.id, "想去洱海骑行")
    client.post(f"/workspace/{trip.id}/plan", follow_redirects=True)

    response = client.post(
        f"/workspace/{trip.id}/plan/restore",
        data={"version_id": first_version_id},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert store.plans[trip.id].status == "草案"
    assert store.plan_versions[trip.id][-1].change_summary == "从 v1 恢复为新草案"
    assert "当前版本 v3" in response.text


def test_new_idea_is_added_to_route_tail_without_reusing_seed_positions():
    trip = store.create_trip("测试想法墙", "10月1日 - 10月3日", "人均 3000", "轻松", "")
    first = store.add_idea(trip.id, "喜欢拍照")
    second = store.add_idea(trip.id, "想吃烤鸟")

    assert [card.title for card in store.idea_cards[trip.id]] == ["先让大家说想法", "喜欢拍照", "想吃烤鸟"]
    assert first.rotation
    assert second.rotation


def test_idea_board_keeps_at_most_eight_cards():
    trip = store.create_trip("八张想法墙", "10月1日 - 10月3日", "人均 3000", "轻松", "")

    for index in range(10):
        store.add_idea(trip.id, f"新增想法 {index}")

    assert len(store.idea_cards[trip.id]) == 8
    assert store.idea_cards[trip.id][-1].title == "新增想法 9"


def test_idea_wall_css_keeps_category_color_details():
    css = Path("app/static/styles.css").read_text(encoding="utf-8")

    assert "--idea-place-bg" in css
    assert "--idea-budget-bg" in css
    assert "--idea-pace-bg" in css
    assert "--idea-taboo-bg" in css
    assert "--idea-pending-bg" in css
    assert ".floating-idea.kind-地点" in css
    assert ".floating-idea.kind-预算" in css
    assert ".floating-idea.kind-节奏" in css
    assert ".floating-idea.kind-禁忌" in css
    assert ".floating-idea.kind-待归类" in css
    assert "Idea wall polish" in css
    assert "idea-wall-legend" in css
    assert "idea-detail-statusbar" in css


def test_mobile_shell_uses_warm_background_behind_fixed_bottom_nav():
    css = Path("app/static/styles.css").read_text(encoding="utf-8")

    assert "background: var(--bg);" in css
    assert ".bottom-nav::before" in css


def test_frontend_polish_keeps_mobile_travel_assistant_details():
    css = Path("app/static/styles.css").read_text(encoding="utf-8")
    js = Path("app/static/app.js").read_text(encoding="utf-8")

    assert "route-draw" in css
    assert "prefers-reduced-motion" in css
    assert ".bottom-nav a:active" in css
    assert ".bottom-nav a span" in css
    assert ".travel-page-top > div > span" in css
    assert "backdrop-filter: blur(18px)" in css
    assert ".message.user .message-body > p" in css
    assert ".message.agent .message-body > p::before" in css
    assert ".timeline-item::before" in css
    assert ".preference-card:has(.conflict)" in css
    assert "Member page polish" in css
    assert "member-card-head" in css
    assert "member-preference-section" in css
    assert "requestAnimationFrame(drawIdeaRoutes)" in js
    assert "openIdeaDetail" in js
    assert "querySelectorAll(selector)" in js
    assert "copyInviteLink" in js
    assert "data-copy-button" in js
    assert "form.dataset.submitting" in js
    assert "submitComposerMessage" in js
    assert "markMessageFailed" in js
    assert "retryFailedMessage" in js
    assert "发送失败，点此重试" in js
    assert "data-api-url" in js or "dataset.apiUrl" in js
    assert "startChatPolling" in js
    assert "refreshChatMessages" in js
    assert "setInterval(() => refreshChatMessages" in js
    assert "aria-busy" in js
    assert js.count("button.textContent = originalText") == 1
    assert "composer-user" in css
    assert "selectedMember" in js
    assert "progress-strip" in css
    assert "version-summary-card" in css
    assert "revision-form" in css
    assert "revision-drawer" in css
    assert "Itinerary page polish" in css
    assert "setTimeout(() => form.submit()" not in js
    assert "submitAgentMention" not in js
    assert "appendAgentThinkingMessage" in js
    assert "appendUserMessage" in js
    assert "waitForNextPaint" in js
    assert "思考中" in js
    assert "我在看大家刚刚说的旅行想法" not in js
    assert "button.textContent = '处理中'" not in js
    assert "scrollChatToLatest" in js
    assert "Chat page should stay focused" in css
    assert ".thinking-message .message-body > p" in css
    assert "trip-meta-row" in css
    assert "form-helper-card" in css
    assert "--radius-card" in css
    assert "chat-agent-card" in css
    assert "Chat readability polish" in css
    assert "retry-message" in css
    assert "failed-message" in css
    assert "agent-card-sources" in css
    assert "data-agent-thinking-message" in js
    assert "|| (!force && chatList.querySelector('[data-agent-thinking-message]'))" not in js
    assert "share-card" in css
    assert "error-card" in css


def test_itinerary_page_renders_structured_plan():
    response = client.get("/workspace/trip-hangzhou?tab=itinerary")

    assert response.status_code == 200
    assert "杭州 3 天 2 晚轻松行程" in response.text
    assert "西湖傍晚散步" in response.text
    assert "满足需求" in response.text
    assert "林夏" in response.text


def test_members_page_renders_preferences():
    response = client.get("/workspace/trip-hangzhou?tab=members")

    assert response.status_code == 200
    assert "大家的想法" in response.text
    assert "偏好矩阵" not in response.text
    assert "member-card" in response.text
    assert "member-card-head" in response.text
    assert "member-preference-section" in response.text
    assert "已确认" in response.text
    assert "待确认" in response.text
    assert "需要协调" in response.text
    assert "旅行整理进度" in response.text
    assert "轻松节奏" in response.text
    assert "酒店预算" in response.text
