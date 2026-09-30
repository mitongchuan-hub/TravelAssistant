"""Regressions for chat tasks and persisted travel data (no real API calls)."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from types import SimpleNamespace
import importlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import agent_client
from app.agent_client import AgentReply
from app.models import AgentCard
from app.persistence import load_snapshot, save_snapshot, serialize_state
from app.store import DemoStore, USERS

store_module = importlib.import_module("app.store")


def reply(body="你好，有什么旅行问题想聊？"):
    return AgentReply(body, AgentCard("missing-info", "继续聊聊", "还需要一些信息", [], []), [])


def test_slow_reply_keeps_placeholder_and_does_not_block_other_writes(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    trip_id = "trip-hangzhou"
    thinking = local.start_agent_task(trip_id)
    entered, release = Event(), Event()

    def slow_model(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return reply()

    monkeypatch.setattr(store_module, "generate_agent_reply", slow_model)
    with ThreadPoolExecutor(max_workers=2) as pool:
        task = pool.submit(local.finish_agent_task, trip_id, "hi", "u1", thinking.id)
        try:
            assert entered.wait(2)
            assert thinking in local.messages[trip_id]
            write = pool.submit(local.add_user_message, trip_id, "第二条", "u1", False)
            assert write.result(timeout=1).body == "第二条"
        finally:
            release.set()
        task.result(timeout=2)
    messages = local.messages[trip_id]
    assert messages[-2].body == reply().body
    assert messages[-1].body == "第二条"
    assert not any(m.status == "thinking" for m in messages)


def test_task_failure_keeps_placeholder_until_failure_is_recorded(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    thinking = local.start_agent_task("trip-hangzhou")

    def broken_model(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(store_module, "generate_agent_reply", broken_model)
    with pytest.raises(RuntimeError):
        local.finish_agent_task("trip-hangzhou", "hi", "u1", thinking.id)
    assert thinking in local.messages["trip-hangzhou"]
    local.fail_agent_task("trip-hangzhou", thinking.id)
    assert not any(m.status == "thinking" for m in local.messages["trip-hangzhou"])


def test_restart_resolves_interrupted_tasks_without_losing_user_messages(tmp_path):
    path = str(tmp_path / "restart.sqlite3")
    first = DemoStore(db_path=path, persistence_enabled=True)
    user_message = first.add_user_message("trip-hangzhou", "hi", process_agent=False)
    first.start_agent_task("trip-hangzhou")
    restarted = DemoStore(db_path=path, persistence_enabled=True)
    assert any(m.id == user_message.id for m in restarted.messages["trip-hangzhou"])
    assert not any(m.status == "thinking" for m in restarted.messages["trip-hangzhou"])
    assert "没有完成回复" in restarted.messages["trip-hangzhou"][-1].body
    assert not any(m["status"] == "thinking" for m in load_snapshot(path)["messages"]["trip-hangzhou"])


def test_failed_first_plan_uses_local_plan_fallback(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    failure = AgentReply(
        "这次模型回复没有完成",
        AgentCard("missing-info", "模型调用暂时失败", "稍后重试", [], []),
        [],
        None,
    )
    monkeypatch.setattr(store_module, "generate_agent_reply", lambda *args, **kwargs: failure)
    local.generate_plan("trip-hangzhou")
    plan = local.plans["trip-hangzhou"]
    assert plan.days
    assert plan.preparation.clothing
    assert any(item.category == "meal" for day in plan.days for item in day.items)
    assert local.messages["trip-hangzhou"][-1].agent_card.title == "第一版行程已生成"



    local = DemoStore(persistence_enabled=False)
    local.confirm_plan("trip-hangzhou")
    plan = deepcopy(local.plans["trip-hangzhou"])
    versions = deepcopy(local.plan_versions["trip-hangzhou"])
    monkeypatch.setattr(store_module, "generate_agent_reply", lambda *a, **kw: reply("请先补充出发时间"))
    local.generate_plan("trip-hangzhou")
    assert local.plans["trip-hangzhou"] == plan
    assert local.plan_versions["trip-hangzhou"] == versions
    assert local.trips["trip-hangzhou"].status == "已确认"


def test_snapshot_and_relational_rows_rollback_together(tmp_path):
    path = tmp_path / "atomic.sqlite3"
    local = DemoStore(persistence_enabled=False)
    original = serialize_state(local, USERS)
    save_snapshot(path, original)
    broken = deepcopy(original)
    broken["trips"]["trip-hangzhou"]["destination"] = "should roll back"
    broken["messages"]["trip-hangzhou"].append(broken["messages"]["trip-hangzhou"][0])
    with pytest.raises(sqlite3.IntegrityError):
        save_snapshot(path, broken)
    assert load_snapshot(path) == original
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 4
        assert con.execute("SELECT destination FROM trips").fetchone()[0] == "杭州周末旅行"


def test_auth_only_database_can_be_loaded(tmp_path):
    from app.auth import AuthStore
    path = tmp_path / "auth-only.sqlite3"
    AuthStore(str(path), persistence_enabled=True)
    assert load_snapshot(path) is None
    assert DemoStore(str(path), persistence_enabled=True).trips


@pytest.mark.parametrize("value", ["0", "false", "off", "no"])
def test_false_disable_flag_does_not_disable_llm(monkeypatch, value):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("TRAVEL_AGENT_DISABLE_LLM", value)
    assert agent_client.is_llm_enabled()


def test_empty_model_output_is_reported_as_failure(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    monkeypatch.setattr(agent_client, "is_llm_enabled", lambda: True)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setattr(agent_client, "_create_chat_completion", lambda *a: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=""))]))
    result = agent_client.generate_agent_reply(local.trips["trip-hangzhou"], [], "hi")
    assert result.card.title == "模型调用暂时失败"


@pytest.mark.parametrize("body", [[], None, 42, {"body": []}, {"body": {"x": 1}}])
def test_message_api_rejects_non_text_payloads(body):
    from app.main import app, auth
    token = auth.create_session("u1")
    with TestClient(app) as client:
        client.cookies.set("travel_session", token)
        response = client.post("/api/trips/trip-hangzhou/messages", content=json.dumps(body),
                               headers={"Content-Type": "application/json"})
    assert response.status_code == 400


def test_temperature_retries_only_for_explicit_parameter_rejection():
    import httpx
    from openai import BadRequestError, AuthenticationError
    from unittest.mock import Mock

    response = httpx.Response(400, request=httpx.Request("POST", "https://model.invalid/chat/completions"))
    unsupported = BadRequestError("Parameter 'temperature'=0.4 is not supported", response=response, body=None)
    create = Mock(side_effect=[unsupported, "completed"])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert agent_client._create_chat_completion(client, "kimi-k3", []) == "completed"
    assert create.call_args_list[0].kwargs["temperature"] == 0.4
    assert "temperature" not in create.call_args_list[1].kwargs
    auth_error = AuthenticationError("rejected", response=response, body=None)
    create.reset_mock(side_effect=True)
    create.side_effect = auth_error
    with pytest.raises(AuthenticationError):
        agent_client._create_chat_completion(client, "kimi-k3", [])
    assert create.call_count == 1


@pytest.mark.parametrize("content,failed", [("", True), ("{}", True), ('{"body":', True), ("你好！", False)])
def test_model_response_validation_with_real_sdk(monkeypatch, content, failed):
    import httpx
    import openai

    monkeypatch.setattr(agent_client, "is_llm_enabled", lambda: True)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://model.invalid/v1")
    original_client = openai.OpenAI
    created_clients = []

    def factory(**kwargs):
        client = original_client(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"choices": [{"message": {"content": content}}]}))))
        created_clients.append(client)
        return client

    monkeypatch.setattr(openai, "OpenAI", factory)
    local = DemoStore(persistence_enabled=False)
    try:
        result = agent_client.generate_agent_reply(local.trips["trip-hangzhou"], [], "hi")
        assert (result.card.title == "模型调用暂时失败") is failed
        if not failed:
            assert result.body == content
    finally:
        for client in created_clients:
            client.close()


def test_model_timeout_does_not_silently_retry_for_minutes(monkeypatch):
    import httpx
    import openai

    monkeypatch.setattr(agent_client, "is_llm_enabled", lambda: True)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://model.invalid/v1")
    original_client = openai.OpenAI
    attempts = []
    clients = []

    def timed_out(request):
        attempts.append(request)
        raise httpx.ReadTimeout("simulated timeout", request=request)

    def factory(**kwargs):
        assert kwargs["timeout"] <= 180
        assert kwargs["max_retries"] == 0
        client = original_client(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(timed_out)))
        clients.append(client)
        return client

    monkeypatch.setattr(openai, "OpenAI", factory)
    local = DemoStore(persistence_enabled=False)
    try:
        result = agent_client.generate_agent_reply(local.trips["trip-hangzhou"], [], "hi")
        assert len(attempts) == 1
        assert result.card.title == "模型响应超时"
    finally:
        for client in clients:
            client.close()


def test_plan_keeps_preparation_and_timeline_categories():
    raw = {
        "body": "已整理旅行方案",
        "card": {
            "kind": "itinerary-draft",
            "title": "行程草案",
            "summary": "已按景点路线补齐准备事项",
            "bullets": ["安排用餐", "补充住宿", "标注天气装备"],
            "action_target": "itinerary",
        },
        "idea_cards": [],
        "plan": {
            "title": "杭州轻松行",
            "status": "草案",
            "preparation": {
                "clothing": [{"title": "薄外套", "body": "早晚温度较低时携带", "status": "建议"}],
                "accommodation": [{"title": "西湖东侧住宿", "body": "方便连接两天景点", "status": "待确认"}],
            },
            "days": [{
                "label": "Day 1",
                "items": [{
                    "category": "meal",
                    "time": "08:30",
                    "title": "早餐",
                    "location": "住宿附近",
                    "duration": "40 分钟",
                    "reason": "出发前补充体力",
                    "notes": "具体店铺待确认",
                    "meal": {
                        "meal_type": "早餐",
                        "recommendation": "片儿川面",
                        "cuisine": "杭州小吃",
                        "budget": "人均 30 元",
                        "reservation": "无需预约",
                    },
                    "satisfies": [],
                }, {
                    "category": "transport",
                    "time": "09:30",
                    "title": "前往西湖",
                    "location": "住宿 → 西湖",
                    "duration": "30 分钟",
                    "reason": "衔接上午游览",
                    "notes": "预留步行时间",
                    "satisfies": ["少绕路"],
                }],
            }],
        },
    }
    parsed = agent_client._parse_reply(json.dumps(raw, ensure_ascii=False))
    local = DemoStore(persistence_enabled=False)
    local._apply_agent_plan("trip-hangzhou", parsed.plan)
    plan = local.plans["trip-hangzhou"]
    assert plan.preparation.clothing[0].title == "薄外套"
    assert plan.preparation.accommodation[0].status == "待确认"
    assert [item.category for item in plan.days[0].items] == ["meal", "transport"]
    assert plan.days[0].items[0].meal.recommendation == "片儿川面"


def test_rate_limit_is_reported_separately_from_configuration_failure(monkeypatch):
    import httpx
    from openai import RateLimitError

    response = httpx.Response(429, request=httpx.Request("POST", "https://model.invalid/chat/completions"))
    monkeypatch.setattr(agent_client, "is_llm_enabled", lambda: True)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setattr(
        agent_client,
        "_create_chat_completion",
        lambda *args, **kwargs: (_ for _ in ()).throw(RateLimitError("too many requests", response=response, body=None)),
    )
    local = DemoStore(persistence_enabled=False)
    result = agent_client.generate_agent_reply(local.trips["trip-hangzhou"], [], "hi")
    assert result.card.title == "模型服务暂时限流"
    assert "繁忙" in result.body


def test_queued_replies_use_matching_history_and_do_not_duplicate(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    trip_id = "trip-hangzhou"
    _, first = local.queue_user_message(trip_id, "第一条", "u1")
    _, second = local.queue_user_message(trip_id, "第二条", "u1")
    histories = []

    def fake_model(**context):
        histories.append([m.body for m in context["messages"]])
        return reply("回复：" + context["user_body"])

    monkeypatch.setattr(store_module, "generate_agent_reply", fake_model)
    local.finish_agent_task(trip_id, "第一条", "u1", first.id)
    local.finish_agent_task(trip_id, "第二条", "u1", second.id)
    local.finish_agent_task(trip_id, "第一条", "u1", first.id)
    assert [m.body for m in local.messages[trip_id][-4:]] == ["第一条", "回复：第一条", "第二条", "回复：第二条"]
    assert "第二条" not in histories[0]
    assert "回复：第一条" in histories[1]
    assert len(histories) == 2
    assert not any("思考中" in body for history in histories for body in history)


def test_weather_tool_returns_requested_day_without_external_network(monkeypatch):
    from app.tools import weather

    def fake_fetch(url, params):
        if "geocoding" in url:
            return {"results": [{"name": "杭州", "latitude": 30.27, "longitude": 120.15, "country": "中国"}]}
        return {
            "timezone": "Asia/Shanghai",
            "daily": {
                "time": ["2026-08-24"],
                "weather_code": [1],
                "temperature_2m_max": [32],
                "temperature_2m_min": [25],
                "precipitation_probability_max": [20],
            },
        }

    monkeypatch.setattr(weather, "_fetch_json", fake_fetch)
    result = weather.query_weather({"city": "杭州", "date": "2026-08-24"})
    assert result["status"] == "available"
    assert result["condition"] == "大致晴"
    assert result["temperature_max_c"] == 32
    assert "轻薄衣物" in result["clothing_advice"]
    preview = weather.query_weather({"city": "杭州"})
    assert preview["forecast_preview"][0]["temperature_min_c"] == 25
    assert "穿衣" in preview["reason"]


def test_planning_tool_loop_preserves_assistant_tool_call_and_result(monkeypatch):
    call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="query_weather", arguments='{"city":"杭州"}'),
    )
    responses = [
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[call]))]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="已完成", tool_calls=[]))]),
    ]
    monkeypatch.setattr(agent_client, "_create_chat_completion", lambda *args, **kwargs: responses.pop(0))
    monkeypatch.setattr(agent_client, "run_tool", lambda name, arguments: {"status": "available", "city": arguments["city"]})
    messages = agent_client._collect_tool_messages(object(), "test-model", "context")
    assert messages[0]["role"] == "assistant"
    assert messages[0]["tool_calls"][0]["function"]["name"] == "query_weather"
    assert messages[1]["role"] == "tool"
    assert '"city": "杭州"' in messages[1]["content"]


def test_schema_retry_drops_only_unsupported_response_format():
    import httpx
    from openai import BadRequestError
    from unittest.mock import Mock

    response = httpx.Response(400, request=httpx.Request("POST", "https://model.invalid/chat/completions"))
    unsupported = BadRequestError("response_format json_schema is not supported", response=response, body=None)
    create = Mock(side_effect=[unsupported, "completed"])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result = agent_client._create_chat_completion(client, "kimi-k3", [], response_format={"type": "json_schema"})
    assert result == "completed"
    assert "response_format" in create.call_args_list[0].kwargs
    assert create.call_args_list[1].kwargs["response_format"] == {"type": "json_object"}
