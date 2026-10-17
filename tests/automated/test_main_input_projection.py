"""Deterministic tests for the task-execution main decision projection."""
from copy import deepcopy
from types import SimpleNamespace

from app.agent_protocol import PlanBasis, RequestContext, plan_basis
from app.main_input import (
    project_conversation,
    project_evidence,
    project_ideas,
    project_main_context,
    project_main_read_context,
    project_messages,
    project_plan,
    project_requirements,
    project_tasks,
    _valid_display,
)
from app.models import Trip, TripDay, TripPlan, TripRequirements, User
from app.task_context import add_task, empty_workflow, set_active_question

OWNER = User("projection-owner", "测试用户", "测")


def _trip(workflow=None):
    return Trip("projection-trip", "杭州旅行", "", "", "", "保留用户限定：只查，不保存", OWNER,
        "草案", "", requirements=TripRequirements(
            city="杭州", travelers=0, budget_amount="", budget_scope="unspecified",
            must_visit=["西湖", "灵隐寺"], avoid=["排队"], dietary=["不吃辣"], pace="轻松", revision=4,
        ), workflow_state=workflow or empty_workflow(), mode="city", lodging={})


def _context(trip, body="酒店每晚500，只查询"):
    plan = TripPlan("plan-projection", "杭州行程", "草案", text="完整正文")
    return RequestContext(
        trip_id=trip.id, owner_id=trip.owner.id, execution_task_id="exec-projection",
        conversation_task_id=None, request_id="request-projection", message_id="message-current",
        body=body, entry="main", strategy="task_execution", requirements_revision=trip.requirements.revision,
        plan_basis=plan_basis(plan), workflow_revision=trip.workflow_state["revision"],
        sequence=trip.workflow_state["sequence"], epoch=trip.workflow_state["epoch"],
    ), plan


def _execution(trip, plan, messages=()):
    return SimpleNamespace(trip=trip, messages=list(messages), current_date="2026-10-16", plan=plan,
                           idea_cards=[], metrics={})


def test_conversation_deduplicates_by_id_not_body():
    messages = [
        SimpleNamespace(id="same", status="sent", sender_type="user", body="相同"),
        SimpleNamespace(id="one", status="sent", sender_type="user", body="相同"),
        SimpleNamespace(id="same", status="sent", sender_type="user", body="更新后的相同"),
        SimpleNamespace(id="current", status="sent", sender_type="user", body="当前"),
    ]
    result = project_conversation(messages, "current")
    assert [row["ref"] for row in result] == ["same", "one"]
    assert result[0]["text"] == "更新后的相同"


def test_requirements_preserves_zero_false_like_lists_and_unique_semantics():
    trip = _trip()
    result = project_requirements(trip, ["住宿地址"])
    assert result["known"]["travelers"] == 0
    assert result["known"]["must_visit"] == ["西湖", "灵隐寺"]
    assert result["known"]["note"] == "保留用户限定：只查，不保存"
    assert result["missing_for_generation"] == ["住宿地址"]


def test_main_projection_has_six_blocks_and_hides_server_metadata_without_mutation():
    trip = _trip()
    context, plan = _context(trip)
    messages = [SimpleNamespace(id="old", status="sent", sender_type="assistant", body="旧问题")]
    execution = _execution(trip, plan, messages)
    before = deepcopy((trip, plan, trip.workflow_state, messages))
    result = project_main_context(request_context=context, execution_context=execution,
        workflow=trip.workflow_state, recent_chat=messages, plan=plan, missing_requirements=[])
    assert set(result) == {"message", "environment", "requirements", "current_plan", "conversation", "task_context"}
    assert result["message"] == {"ref": "message-current", "text": "酒店每晚500，只查询"}
    assert result["environment"] == {"current_date": "2026-10-16", "mode": "city", "entry": "main"}
    raw = repr(result)
    assert "content_hash" not in raw and "sequence" not in raw and "remaining_model_calls" not in raw
    assert (trip, plan, trip.workflow_state, messages) == before


def test_current_message_is_not_repeated_but_same_body_other_id_is_kept():
    trip = _trip()
    context, plan = _context(trip, body="500")
    messages = [
        SimpleNamespace(id="message-current", status="sent", sender_type="user", body="500"),
        SimpleNamespace(id="previous", status="sent", sender_type="user", body="500"),
    ]
    result = project_main_context(request_context=context, execution_context=_execution(trip, plan, messages),
        workflow=trip.workflow_state, recent_chat=messages, plan=plan)
    assert [row["ref"] for row in result["conversation"]] == ["previous"]


def test_active_question_and_open_tasks_are_projected_without_terminal_tasks():
    workflow, task_id = add_task(empty_workflow(), goal="查询酒店，不保存旅行需求", effect_kind="read",
        source_message_ids=["old-question"], authorization_refs=["old-question"])
    workflow, question = set_active_question(workflow, task_id=task_id, text="酒店每晚预算？",
        answer_target={"namespace": "hotel.search", "field": "nightly_budget"},
        assistant_message_id="assistant-question", source_message_ids=["old-question"])
    trip = _trip(workflow)
    result = project_tasks(workflow, message_ids=["old-question", "assistant-question"],
        plan_basis_value=plan_basis(TripPlan("plan", "", "未生成")), requirements_revision=4)
    assert result["focus_task_ref"] == task_id
    assert result["open_tasks"][0]["effect_kind"] == "read"
    assert result["active_question"]["answer_target"]["field"] == "nightly_budget"
    assert result["active_question"]["task_ref"] == task_id


def test_stale_basis_is_reconfirmation_not_active_authorization():
    workflow, _ = add_task(empty_workflow(), goal="继续生成", effect_kind="itinerary",
        source_message_ids=["old"], authorization_refs=["old"], basis={"requirements_revision": 1})
    result = project_tasks(workflow, message_ids=["old"],
        plan_basis_value=plan_basis(TripPlan("plan", "", "未生成")), requirements_revision=4)
    assert result["open_tasks"][0]["status"] == "suspended"
    assert result["open_tasks"][0]["requires_reconfirmation"]


def test_plan_outline_falls_back_without_reliable_days_or_uses_structured_days():
    empty = TripPlan("plan-empty", "标题", "草案", text="只有正文，没有可靠目录")
    assert project_plan(empty, plan_basis(empty))["outline"] is None
    day = TripDay("day-1", "第一天", [], "", "西湖周边")
    structured = TripPlan("plan-days", "标题", "草案", days=[day], text="正文")
    outline = project_plan(structured, plan_basis(structured))["outline"]
    assert outline == [{"ref": "day-1", "label": "第一天", "date": None, "theme": "西湖周边"}]


def test_display_wrong_hash_is_never_used_as_outline():
    plan = TripPlan("plan-display", "标题", "草案", text="正文", display_status="ready",
        display_source_hash="wrong", display={"days": [{"label": "旧", "items": []}]})
    assert project_plan(plan, plan_basis(plan))["outline"] is None


def test_main_messages_are_compact_paginated_and_detached():
    messages = [
        SimpleNamespace(id="one", status="sent", sender_type="user", body="杭州", sender=OWNER,
                        created_at="secret", agent_card={}),
        SimpleNamespace(id="hidden", status="failed", sender_type="assistant", body="不要给main"),
        SimpleNamespace(id="two", status="sent", sender_type="assistant", body="杭州建议"),
        SimpleNamespace(id="one", status="sent", sender_type="user", body="最新杭州"),
    ]
    result = project_messages(messages, current_message_id="current", query="杭州", limit=1)
    assert result == {"items": [{"ref": "one", "role": "user", "text": "最新杭州"}],
                      "total": 2, "next_offset": 1, "query": "杭州"}
    assert "created_at" not in repr(result)


def test_main_ideas_allowlist_author_and_pagination():
    ideas = [SimpleNamespace(id="idea-1", kind="constraint", title="忌口", body="不吃辣",
                             author="你", status="active", rotation="7deg", internal="secret"),
             SimpleNamespace(id="idea-2", kind="suggestion", title="餐厅", body="西湖边",
                             author="Agent", status="new", rotation="-2deg")]
    result = project_ideas(ideas, query="西湖", limit=10)
    assert result["items"] == [{"ref": "idea-2", "kind": "suggestion", "title": "餐厅",
                                 "body": "西湖边", "status": "new", "author": "Agent"}]
    assert "rotation" not in repr(result)


def test_main_plan_read_only_returns_safe_outline_without_internal_metadata():
    day = TripDay("day-1", "第一天", [], "", "西湖")
    plan = TripPlan("plan-safe", "杭州", "草案", days=[day], text="完整正文",
                    requirements_snapshot={"start_date": "2026-10-16", "end_date": "2026-10-16"},
                    display_source_hash="hash", display_status="ready")
    result = project_main_read_context("plan", {"section": "plan"}, plan=plan,
                                       expected_basis=plan_basis(plan))
    assert result["outline"][0]["theme"] == "西湖"
    assert "完整正文" not in repr(result)
    assert "content_hash" not in repr(result)


def test_main_evidence_allowlists_weather_facts_and_hides_provider_data():
    raw = [{"tool": "query_weather", "arguments": {"city": "杭州", "token": "secret"},
            "queried_at": "secret-time", "seconds": 9.2,
            "result": {"status": "available", "city": "杭州", "requested_date": "2026-10-16",
                       "precipitation_probability_max": 70, "condition": "小雨",
                       "temperature_max_c": 22, "longitude": 120.1, "source": "Open-Meteo"}}]
    result = project_evidence(raw, limit=10)
    assert result["items"][0]["facts"] == {
        "city": "杭州", "requested_date": "2026-10-16", "condition": "小雨",
        "temperature_max_c": 22, "precipitation_probability_max": 70,
    }
    assert "token" not in repr(result) and "queried_at" not in repr(result)
    assert "longitude" not in repr(result)


def test_malformed_display_structured_day_falls_back_safely():
    plan = TripPlan("plan-malformed", "标题", "草案", text="正文", display_status="ready",
                    display_source_hash="", display={"days": [{"label": "旧", "items": []}]})
    plan.days = [{"id": "day-1", "items": []}]
    assert _valid_display(plan, None) is None
