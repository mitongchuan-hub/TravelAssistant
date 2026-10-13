"""A bounded second review repairs omissions without guessing or publishing early."""
from copy import deepcopy
import json
from app.store import DemoStore
from test_agent_roles import route, completion, scripted, update_call

USER = '2026年11月14日10:00杭州东站到，15日18:00同站走，两人合计2000元，含住宿餐饮市内交通不含往返票，请规划。'
FIELDS = {'start_date': '2026-11-14', 'end_date': '2026-11-15', 'arrival_time': '10:00',
          'departure_time': '18:00', 'arrival_location': '杭州东站', 'departure_location': '杭州东站',
          'travelers': 2, 'budget_amount': '2000', 'budget_scope': 'total',
          'budget_coverage': '含住宿餐饮市内交通不含往返票'}


def test_partial_extraction_gets_one_review_and_can_complete_provided_fields(monkeypatch):
    local = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    calls = scripted(monkeypatch, [route('planning', USER, 'generate'), update_call(USER, {'city': '杭州'}),
                                  update_call(USER, FIELDS), completion('完整文字草案，未知费用保持待核实。')])
    local.add_user_message(trip.id, USER)
    feedback = json.loads(calls[2][0][-1]['content'])
    assert feedback['status'] == 'review_needed'
    assert local.trips[trip.id].requirements.start_date == FIELDS['start_date']
    assert local.trips[trip.id].requirements.budget_amount == '2000'
    assert local.plans[trip.id].text == '完整文字草案，未知费用保持待核实。'
    assert len(calls) == 4


def test_unresolved_requirements_cannot_publish_a_draft_after_review(monkeypatch):
    local = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    user = '杭州两天，请规划。'
    before = deepcopy(local.plans[trip.id])
    scripted(monkeypatch, [route('planning', user, 'generate'), update_call(user, {'city': '杭州'}), completion('不该保存的行程')])
    local.add_user_message(trip.id, user)
    assert local.plans[trip.id] == before
    assert local.messages[trip.id][-1].body.startswith('需要补充')
    assert local.trips[trip.id].workflow_state['awaiting_generation']


def test_page_requirement_question_is_successful_processing_not_task_failure(monkeypatch):
    local = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    scripted(monkeypatch, [])
    before = deepcopy(local.plans[trip.id])
    task, _ = local.submit_task(trip.id, 'plan', {}, 'missing-input')
    local.run_task(task['id'])
    result = local.tasks[task['id']]
    assert result['status'] == 'succeeded' and result['needs_input']
    assert result['message'].startswith('需要补充')
    assert result['result_url'].endswith('tab=chat') and local.plans[trip.id] == before
