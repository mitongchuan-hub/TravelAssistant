"""Complete prose replaces card coverage without claiming semantic proof."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.application.contracts import RequestContext
from app.domain.itinerary.identity import plan_basis
from app.domain.models import Trip, TripPlan, TripRequirements, User
from app.domain.changes import prepare_changes


def snapshot():
    owner = User('user', 'User', 'U')
    trip = Trip('trip', '北京市', owner, '', '', mode='city',
        requirements=TripRequirements(city='北京市', lodging_address='测试住宿',
            transport_mode='transit', text='Night ride\nBuns',
            start_date='2030-05-01', end_date='2030-05-01', day_count=1),
        lodging={'poi_id': 'hotel', 'name': '测试住宿', 'city': '北京市'})
    plan = TripPlan('plan', '', '未生成')
    request = RequestContext('trip', owner.id, 'execution', None, 'request', 'message',
        '请生成一天的同城行程', 'main', 'task_execution', 0, plan_basis(plan), 0, 0, 0)
    return request, SimpleNamespace(trip=trip, plan=plan)


@pytest.mark.parametrize('legacy_field', ['idea_outcomes', 'idea_ids', 'expected_idea_hashes'])
def test_legacy_coverage_protocol_rejected(legacy_field):
    request, execution = snapshot()
    with pytest.raises(ValueError):
        prepare_changes({'user_quote': request.body, 'itinerary': {
            'action': 'replace', 'text': 'Day plan', legacy_field: []}}, request, execution)


def test_new_plan_does_not_require_or_invent_card_outcomes():
    request, execution = snapshot()
    before = deepcopy(execution)
    payload = prepare_changes({'user_quote': request.body, 'itinerary': {
        'action': 'replace', 'text': 'Night ride at 19:30; Buns'}}, request, execution)
    assert execution == before
    assert payload['result_text'] == 'Night ride at 19:30; Buns'
    assert 'idea_ids' not in payload and 'expected_idea_hashes' not in payload


def test_generation_explicitly_rejects_oversized_migrated_text():
    request, execution = snapshot()
    execution.trip.requirements.text = '文' * 20001
    with pytest.raises(ValueError, match='整理'):
        prepare_changes({'user_quote': request.body, 'itinerary': {
            'action': 'replace', 'text': 'Day plan'}}, request, execution)
