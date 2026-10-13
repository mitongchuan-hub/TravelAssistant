import json
from copy import deepcopy
import pytest
from app import agent_client
from app.plan_display import display_format, SCHEMA
from test_agent_roles import scripted, completion, display_data

DATES = ['2026-11-14', '2026-11-15', '2026-11-16']
SOURCE = '第一天14:00预约，不能改；不吃辣。第二天预算为估算，交通待核实。第三天未提供安排。'


def test_dynamic_schema_bounds_dates_without_mutating_shared_schema():
    before = deepcopy(SCHEMA)
    days = display_format(DATES)['json_schema']['schema']['properties']['days']
    assert days['minItems'] == days['maxItems'] == 3
    assert days['items']['properties']['date']['enum'] == DATES
    assert SCHEMA == before
    assert 'maxItems' not in display_format()['json_schema']['schema']['properties']['days']


@pytest.mark.parametrize('failure', ['count', 'order', 'json', 'schema'])
def test_invalid_display_corrects_once_with_original_source(monkeypatch, failure):
    invalid = display_data(DATES)
    if failure == 'count': invalid['days'] = invalid['days'][:1]
    if failure == 'order': invalid['days'].reverse()
    if failure == 'schema': del invalid['title']
    calls = scripted(monkeypatch, [completion('invalid JSON' if failure == 'json' else invalid), completion(display_data(DATES))])
    result = agent_client.generate_plan_display(SOURCE, DATES, 'isolated')
    assert result['display_status'] == 'ready' and len(calls) == 2
    correction = json.loads(calls[1][0][1]['content'])
    assert correction['source_text'] == SOURCE and correction['source_dates'] == DATES
    assert correction['validation_error'] and '不得' in correction['correction_instruction']
    assert calls[1][1]['budget'].model_calls == 2


@pytest.mark.parametrize('reason', ['foreground', 'remaining', 'transport', 'twice_invalid', 'compatibility_budget'])
def test_correction_is_bounded_and_never_retries_transport(monkeypatch, reason):
    invalid = display_data(DATES[:1])
    responses = [TimeoutError('upstream timeout')] if reason == 'transport' else [completion(invalid), completion(invalid)]
    if reason == 'compatibility_budget':
        def compatibility_response(messages, kwargs):
            kwargs['budget'].model()  # A protocol fallback consumes the second SDK request.
            return completion(invalid)
        responses = [compatibility_response]
    calls = scripted(monkeypatch, responses)
    if reason == 'remaining': monkeypatch.setenv('TRAVEL_DISPLAY_TIMEOUT_SECONDS', '59')
    result = agent_client.generate_plan_display(SOURCE, DATES, 'isolated', retry_allowed=lambda: reason != 'foreground')
    assert result['display_status'] == 'fallback'
    assert len(calls) == (2 if reason == 'twice_invalid' else 1)
    assert result['display'] is None
