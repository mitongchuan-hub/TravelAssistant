"""Prose survives display conversion failures; business failures keep old plans."""
from copy import deepcopy

import pytest

from app import agent_client
from app.agent_runtime import TravelAgentRuntime
from app.store import DemoStore
from test_agent_roles import completion, scripted, ready_for_planning


def test_planning_reuses_explicit_tool_result_and_saves_prose(monkeypatch):
    text = '杭州一日行程\n上午游览西湖。\n天气与开放时间待核实。'
    requests = scripted(monkeypatch, [
        completion(calls=[('query_weather', {'city': '杭州'})]), completion(text)])
    queries = []
    monkeypatch.setattr(agent_client, 'run_tool', lambda *a, **kw: queries.append(a) or {'status': 'unavailable'})
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    ready_for_planning(local, trip.id)
    local.generate_plan(trip.id)
    assert local.plans[trip.id].display_status == 'pending'
    local.run_pending_display_jobs()
    assert len(queries) == 1 and len(requests) == 3
    assert [kw['agent_role'] for _, kw in requests] == ['planning', 'planning', 'display']
    assert all('response_format' not in kw for _, kw in requests if kw['agent_role']=='planning')
    assert any(m.get('tool_call_id') == 'call-0' for m in requests[1][0])
    assert local.plans[trip.id].text == text


def test_prose_with_markdown_and_braces_survives_display_failure(monkeypatch):
    source = '# 行程\n\n下午：西湖散步。\n备注原文：{请灵活安排}\n```text\n交通待确认\n```'
    calls = scripted(monkeypatch, [completion(source)])
    local = DemoStore(persistence_enabled=False)
    ready_for_planning(local, 'trip-hangzhou')
    local.generate_plan('trip-hangzhou')
    local.run_pending_display_jobs()
    assert len(calls) == 2 and local.plans['trip-hangzhou'].text == source
    assert not hasattr(local, 'plan_versions')


def test_empty_output_keeps_previous_plan_without_format_retry(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    ready_for_planning(local, 'trip-hangzhou')
    original = deepcopy(local.plans['trip-hangzhou'])
    calls = scripted(monkeypatch, [completion('')])
    local.generate_plan('trip-hangzhou')
    assert len(calls) == 1
    assert local.plans['trip-hangzhou'] == original
    assert not hasattr(local, 'plan_versions')
    assert local.messages['trip-hangzhou'][-1].status == 'failed'


def test_network_failure_does_not_trigger_output_recovery(monkeypatch):
    calls = scripted(monkeypatch, [TimeoutError('upstream timeout')])
    local = DemoStore(persistence_enabled=False)
    ready_for_planning(local, 'trip-hangzhou')
    runtime = TravelAgentRuntime(object(), 'test', {'user_body': '生成', 'trip': local.trips['trip-hangzhou']}, request=agent_client._create_chat_completion)
    with pytest.raises(TimeoutError):
        runtime.run('planning')
    assert len(calls) == 1
