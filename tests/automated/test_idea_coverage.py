"""A submitted full itinerary accounts for every saved wish without mutating it."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.workflow.execution import RequestContext
from app.services.itinerary.plan_basis import plan_basis
from app.agents.task_runtime import TaskExecutionContext
from app.models import IdeaCard, Trip, TripPlan, TripRequirements, User
from app.services.agent_crud import prepare_changes


def snapshot():
    owner = User('user', 'User', 'U')
    trip = Trip('trip', '北京市', '', '', '', '', owner, '', '', mode='city',
        requirements=TripRequirements(city='北京市', lodging_address='测试住宿',
            start_date='2030-05-01', end_date='2030-05-01', day_count=1),
        lodging={'poi_id': 'hotel', 'name': '测试住宿', 'city': '北京市'})
    plan = TripPlan('plan', '', '未生成')
    ideas = [IdeaCard(key, '待归类', title, title, '你', '待整理')
             for key, title in [('night', 'Night ride'), ('breakfast', 'Buns')]]
    request = RequestContext('trip', owner.id, 'execution', None, 'request', 'message',
        '请生成一天的同城行程', 'main', 'task_execution', 0, plan_basis(plan), 0, 0, 0)
    return request, TaskExecutionContext(trip, [], ideas, plan, {}, '2030-04-30')


def outcome(ref, status='scheduled', reason='Night ride at 19:30'):
    return {'idea_ref': ref, 'status': status, 'reason': reason}


def prepare(outcomes, text='Night ride at 19:30; Buns', *, empty_wall=False):
    request, execution = snapshot()
    if empty_wall:
        execution = SimpleNamespace(trip=execution.trip, plan=execution.plan, idea_cards=[])
    before = deepcopy(execution)
    result = prepare_changes({'user_quote': request.body, 'itinerary': {
        'action': 'replace', 'text': text, 'idea_outcomes': outcomes}}, request, execution)
    assert execution == before
    return result


@pytest.mark.parametrize('outcomes', [[], [outcome('night')],
    [outcome('night'), outcome('unknown')],
    [outcome('night'), outcome('night'), outcome('breakfast', reason='Buns')]])
def test_missing_unknown_or_duplicate_ideas_cannot_pass_coverage(outcomes):
    with pytest.raises(ValueError):
        prepare(outcomes)


def test_scheduled_outcome_must_point_to_actual_itinerary_text():
    with pytest.raises(ValueError):
        prepare([outcome('night'), outcome('breakfast', reason='Buns')], text='Buns')


def test_conflicts_cancelled_and_waiting_wishes_remain_explicit():
    for status in ('conflict', 'excluded', 'needs_input'):
        outcomes = [outcome('night', status, 'Return deadline conflicts with ride'),
                    outcome('breakfast', 'excluded', 'User cancelled breakfast')]
        payload = prepare(outcomes, text='Day plan')
        assert payload['args']['itinerary']['idea_outcomes'] == outcomes
        assert payload['idea_ids'] == ['breakfast', 'night']
    with pytest.raises(ValueError):
        prepare([outcome('night', 'conflict', ' '), outcome('breakfast', reason='Buns')])


def test_success_preserves_coverage_and_binds_all_saved_ideas():
    outcomes = [outcome('night'), outcome('breakfast', 'conflict', 'Breakfast window conflicts')]
    payload = prepare(outcomes, text='Night ride at 19:30')
    assert payload['args']['itinerary']['idea_outcomes'] == outcomes
    assert set(payload['expected_idea_hashes']) == {'night', 'breakfast'}
    assert payload['result_text'] == 'Night ride at 19:30'


def test_empty_wall_requires_no_invented_outcomes():
    assert prepare([], text='Day plan', empty_wall=True)['idea_ids'] == []
    with pytest.raises(ValueError):
        prepare([outcome('invented')], empty_wall=True)
