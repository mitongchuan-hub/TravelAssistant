"""Presentation changes are adopted separately and cannot rewrite a saved plan."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.services.itinerary.plan_basis import plan_basis
from app.agents.task_runtime import TaskExecutionContext, TaskExecutionRuntime
from app.services.itinerary.plan_display import text_hash
from app.storage.store import DemoStore


TRIP = 'trip-hangzhou'
HTML = '<!doctype html><html><head><title>攻略</title></head><body><p>09:00 西湖，费用待核实。</p></body></html>'


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv('TRAVEL_DISPLAY_ENABLED', '1')
    value = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    value._apply_text_plan(TRIP, '09:00 西湖，费用待核实。', [{'tool': 'saved'}])
    value.plans[TRIP].status = value.trips[TRIP].status = '已确认'
    return value


def runtime(store, request, *, body='把攻略改成简洁手账风'):
    context = store.capture_request_context(TRIP, body=body)
    execution = TaskExecutionContext(deepcopy(store.trips[TRIP]), deepcopy(store.messages[TRIP]),
        [], deepcopy(store.plans[TRIP]), store.trip_metrics(TRIP), '2026-10-27')
    return TaskExecutionRuntime(None, 'test', context, execution, request=request)


def response(content=None, **changes):
    calls = []
    if changes:
        calls = [SimpleNamespace(id='submit', type='function', function=SimpleNamespace(
            name='submit_changes', arguments=json.dumps(changes, ensure_ascii=False)))]
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))])


def test_style_request_without_submission_does_not_mutate(store):
    before = deepcopy(store.plans[TRIP])
    value = runtime(store, lambda *a, **kw: response('暂不修改'))
    proposal = value.run()
    store.commit_turn(value.request_context, proposal)
    assert store.plans[TRIP] == before
    assert not store.display_jobs


def test_submitted_style_keeps_business_content_and_confirmation(store):
    before = deepcopy(store.plans[TRIP])
    calls = []

    def request(*args, **kwargs):
        calls.append(kwargs['agent_role'])
        return response(user_quote='把攻略改成简洁手账风', presentation={'style_instruction': '简洁手账风'})

    value = runtime(store, request)
    proposal = value.run()
    assert not proposal.failed
    assert store.plans[TRIP] == before  # A validated terminal proposal is not a write.
    receipt = store.commit_turn(value.request_context, proposal)
    after = store.plans[TRIP]
    assert receipt.committed
    assert len(calls) == 1  # No delegation or reply-review model.
    assert after.id == before.id
    assert after.text == before.text and after.evidence == before.evidence
    assert after.status == '已确认'
    assert plan_basis(after) == plan_basis(before)
    assert after.display_style == '简洁手账风'
    assert after.display_status == 'pending'
    assert next(iter(store.display_jobs.values()))['style_instruction'] == '简洁手账风'


def test_stale_plan_cannot_receive_a_prepared_style(store):
    before = deepcopy(store.plans[TRIP])
    value = runtime(store, lambda *a, **kw: response(
        user_quote='把攻略改成简洁手账风', presentation={'style_instruction': '简洁手账风'}))
    proposal = value.run()
    assert not proposal.failed
    store._apply_text_plan(TRIP, '10:00 灵隐寺，费用待核实。')
    current = deepcopy(store.plans[TRIP])
    receipt = store.commit_turn(value.request_context, proposal)
    assert not receipt.committed
    assert store.plans[TRIP] == current
    assert store.plans[TRIP].display_style == before.display_style


def test_late_result_for_old_style_cannot_publish(store, monkeypatch):
    from app.agents import agent_client
    from app.services.itinerary.guide_html import build_guide_display

    source_hash = text_hash(store.plans[TRIP].text)
    store.retry_display(TRIP, store.plans[TRIP].id, source_hash, style_instruction='旧风格')
    job_id = store._next_display_job()

    def generate(*args, **kwargs):
        store.retry_display(TRIP, store.plans[TRIP].id, source_hash, style_instruction='新风格')
        return {'display': build_guide_display(HTML), 'display_status': 'ready',
                'display_source_hash': source_hash}

    monkeypatch.setattr(agent_client, 'generate_plan_display', generate)
    store.run_display_job(job_id)
    assert store.plans[TRIP].display_style == '新风格'
    assert store.plans[TRIP].display is None
    assert next(iter(store.display_jobs.values()))['status'] == 'queued'


def test_failed_restyle_keeps_previous_guide(store, monkeypatch):
    from app.agents import agent_client
    from app.services.itinerary.guide_html import build_guide_display

    plan = store.plans[TRIP]
    plan.display = build_guide_display(HTML)
    plan.display_source_hash = text_hash(plan.text)
    plan.display_status = 'ready'
    previous = deepcopy(plan.display)
    store.retry_display(TRIP, plan.id, text_hash(plan.text), style_instruction='简洁')
    monkeypatch.setattr(agent_client, 'generate_plan_display', lambda *a, **kw: {
        'display': None, 'display_status': 'fallback', 'display_source_hash': text_hash(plan.text)})
    store.run_pending_display_jobs()
    assert store.plans[TRIP].display == previous
    assert store.plans[TRIP].status == '已确认'
    assert store.plans[TRIP].display_status == 'fallback'


def test_style_persists_and_survives_content_revision(store):
    from app.storage.persistence import serialize_state, restore_state

    store.plans[TRIP].display_style = '手账风'
    from app.storage.store import USERS
    snapshot = serialize_state(store, USERS)
    restored = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    restore_state(restored, snapshot, USERS)
    assert restored.plans[TRIP].display_style == '手账风'
    restored._apply_text_plan(TRIP, '10:00 西湖，费用待核实。')
    assert restored.plans[TRIP].display_style == '手账风'


def test_busy_and_confirmation_changes_refresh_preview_actions(store):
    initial = store.get_display_state(TRIP)['version']
    with store._foreground_operation(TRIP):
        busy = store.get_display_state(TRIP)['version']
        assert busy != initial
    assert store.get_display_state(TRIP)['version'] == initial
    store.plans[TRIP].status = '草案'
    assert store.get_display_state(TRIP)['version'] != initial


def test_retry_cannot_rebind_stale_html_to_current_prose(store, monkeypatch):
    from app.agents import agent_client
    from app.services.itinerary.guide_html import build_guide_display

    plan = store.plans[TRIP]
    plan.display = build_guide_display(HTML)
    plan.display_source_hash = text_hash('旧方案正文')
    plan.display_status = 'ready'
    assert not store.get_display_state(TRIP)['guide_available']
    store.retry_display(TRIP, plan.id, text_hash(plan.text))
    assert not store.get_display_state(TRIP)['guide_available']
    monkeypatch.setattr(agent_client, 'generate_plan_display', lambda *a, **kw: {
        'display': None, 'display_status': 'fallback', 'display_source_hash': text_hash(plan.text)})
    store.run_pending_display_jobs()
    assert not store.get_display_state(TRIP)['guide_available']
