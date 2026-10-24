"""Idea-wall wishes reach planning without granting system/tool authority."""
import json
from types import SimpleNamespace

import pytest

from app.agents.agent_protocol import plan_basis
from app.agents.agent_runtime import TravelAgentRuntime
from app.agents.main_input import project_idea_wall, project_main_context
from app.models import Trip, TripRequirements, User
from app.workflow.task_context import empty_workflow


def cards(count=10):
    return [
        {
            "id": f"idea-{index}",
            "kind": "待归类",
            "title": f"想法{index}" + "标" * 200,
            "body": f"正文{index}" + "文" * 600,
            "status": "待整理",
            "author": "你",
            "rotation": "1deg",
        }
        for index in range(count)
    ]


def test_projection_keeps_latest_cards_and_bounds_text():
    wall = project_idea_wall(cards(), include_body=True)
    assert wall["total"] == 10
    assert wall["truncated"] is True
    assert [item["ref"] for item in wall["items"]] == [f"idea-{index}" for index in range(2, 10)]
    first = wall["items"][0]
    assert first["status"] == "待整理"
    assert first["kind"] == "待归类"
    assert first["title"].startswith("想法2")
    assert len(first["title"]) == 120
    assert len(first["body"]) == 500
    assert first["author"] == "你"
    assert "rotation" not in first


def test_summary_does_not_present_card_organization_as_unfinished_travel_work():
    wall = project_idea_wall(cards(2))
    assert wall["truncated"] is False
    assert all("body" not in item for item in wall["items"])
    assert all("author" not in item for item in wall["items"])
    assert all("status" not in item and "kind" not in item for item in wall["items"])


def test_empty_and_invalid_cards_are_safe():
    assert project_idea_wall(None) == {"items": [], "total": 0, "truncated": False}
    wall = project_idea_wall([{"id": ""}, {"id": "idea-ok", "title": "可读"}])
    assert [item["ref"] for item in wall["items"]] == ["idea-ok"]


def trip():
    return Trip(
        id="trip-1",
        destination="北京市同城游玩",
        date_range="2026-10-24",
        budget="预算待定",
        style="风格待定",
        note="",
        owner=User(id="u1", name="你", initials="你"),
        status="草案",
        last_activity="",
        requirements=TripRequirements(
            city="北京市",
            start_date="2026-10-24",
            end_date="2026-10-24",
            day_count=1,
            lodging_address="北京工业大学",
        ),
        mode="city",
        lodging={
            "poi_id": "p1",
            "name": "北京工业大学",
            "address": "北京工业大学",
            "city": "北京市",
            "district": "",
            "longitude": 116.4,
            "latitude": 39.8,
            "source": "test",
            "fetched_at": "2026-10-21",
        },
    )


def test_main_context_includes_idea_wall_summary():
    execution = SimpleNamespace(
        trip=trip(),
        messages=[],
        idea_cards=cards(3),
        current_date="2026-10-21",
    )
    request = SimpleNamespace(
        message_id="m1",
        body="白天你看着安排",
        entry="main",
        plan_basis=plan_basis(None),
        requirements_revision=0,
    )
    context = project_main_context(
        request_context=request,
        execution_context=execution,
        workflow=empty_workflow(),
        recent_chat=[],
        plan=None,
    )
    wall = context["idea_wall"]
    assert wall["total"] == 3
    assert wall["items"][0]["ref"] == "idea-0"
    assert "body" not in wall["items"][0]
    assert context["requirements"]["known"]["city"] == "北京市"


class StopAfterCapture(Exception):
    pass


def runtime(captured, *, idea_cards=None):
    def request(client, model, messages, **kwargs):
        captured.update(json.loads(messages[1]["content"]))
        raise StopAfterCapture()

    return TravelAgentRuntime(
        None,
        "test-model",
        {
            "trip": trip(),
            "plan": None,
            "idea_cards": cards(2) if idea_cards is None else idea_cards,
            "messages": [],
            "current_date": "2026-10-21",
            "user_body": "页面操作：生成当前行程",
            "cancelled": False,
        },
        request=request,
    )


def workspace():
    return SimpleNamespace(context=SimpleNamespace(
        message_id="m1",
        conversation_task_id=None,
        plan_basis=plan_basis(None),
    ))


def test_draft_payload_contains_full_idea_wall():
    captured = {}
    with pytest.raises(StopAfterCapture):
        runtime(captured).run_capability("draft_itinerary", "生成", workspace=workspace())
    wall = captured["idea_wall"]
    assert wall["total"] == 2
    assert wall["items"][0]["body"].startswith("正文0")
    assert wall["items"][0]["author"] == "你"
    assert captured["capability_request"] == "生成"
    assert captured['idea_requirement_refs'] == ['idea-0', 'idea-1']


def test_truncated_payload_still_lists_every_required_idea_reference():
    captured = {}
    with pytest.raises(StopAfterCapture):
        runtime(captured, idea_cards=cards(10)).run_capability('draft_itinerary', 'Generate', workspace=workspace())
    assert captured['idea_wall']['truncated'] is True
    assert len(captured['idea_wall']['items']) == 8
    assert captured['idea_requirement_refs'] == [f'idea-{index}' for index in range(10)]


def test_non_planning_capability_does_not_receive_idea_wall():
    captured = {}
    with pytest.raises(StopAfterCapture):
        runtime(captured).run_capability("research", "查询", workspace=workspace())
    assert "idea_wall" not in captured
