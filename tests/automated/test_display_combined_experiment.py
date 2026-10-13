from copy import deepcopy
import pytest
from jsonschema import ValidationError
from app.plan_display import SCHEMA, validate_display
from tests.performance.display_combined_experiment import combined_format, validate_combined, generate_combined_display, VERSION
from test_agent_roles import completion, display_data, scripted

DATES = ['2026-10-14', '2026-10-15']
SOURCE = '10月14日前后阴，约18–26°C。建议带外套。第一天午后散步，第二天退房；微辣也不要。房态待确认。'


def output():
    value = display_data(DATES)
    value['schema_version'] = VERSION
    value['preparation']['clothing'] = [{'title': '外套', 'body': '建议带外套'}]
    value['weather'] = [{'date_text': '10月14日前后', 'summary': '阴', 'temperature_text': '约18–26°C',
        'precipitation_text': None, 'queried_at_text': None, 'notes': []}]
    value['risks'] = ['微辣也不要']
    return value


def test_experimental_weather_schema_does_not_change_production_contract_or_fill_missing_dates():
    before = deepcopy(SCHEMA)
    data = output()
    result = validate_combined(data, DATES)
    assert result == data and SCHEMA == before
    assert len(result['weather']) == 1 and result['weather'][0]['date_text'] == '10月14日前后'
    with pytest.raises(ValidationError):
        validate_display(data, DATES)  # This candidate cannot silently enter the production protocol.


def test_all_fields_are_extracted_in_exactly_one_request_from_original_source(monkeypatch):
    calls = scripted(monkeypatch, [completion(output())])
    result = generate_combined_display(SOURCE, DATES, 'isolated')
    assert result['display_status'] == 'ready' and result['sdk_requests'] == 1 and len(calls) == 1
    assert SOURCE in calls[0][0][1]['content'] and calls[0][1]['tools'] is None
    assert calls[0][1]['budget'].max_models == 1
    assert result['display']['weather'][0]['temperature_text'] == '约18–26°C'
    assert result['display']['preparation']['clothing'] == output()['preparation']['clothing']


@pytest.mark.parametrize('failure', ['json', 'missing_weather', 'date_order', 'transport'])
def test_invalid_or_failed_response_is_preserved_without_second_request(monkeypatch, failure):
    data = output()
    if failure == 'missing_weather': del data['weather']
    if failure == 'date_order': data['days'].reverse()
    responses = [TimeoutError('upstream')] if failure == 'transport' else [completion('broken' if failure == 'json' else data)]
    calls = scripted(monkeypatch, responses)
    result = generate_combined_display(SOURCE, DATES, 'isolated')
    assert result['display'] is None and result['display_status'] == 'fallback'
    assert len(calls) == result['sdk_requests'] == 1


def test_no_forecast_or_preparation_in_source_can_stay_empty_without_program_filling(monkeypatch):
    data = output()
    data['preparation'] = {'clothing': [], 'accommodation': []}
    data['weather'] = []
    scripted(monkeypatch, [completion(data)])
    result = generate_combined_display('原文只写两天行程，天气待核实。', DATES, 'isolated')
    assert result['display_status'] == 'ready'
    assert result['display']['weather'] == [] and result['display']['preparation']['clothing'] == []


def test_foreground_or_cancellation_prevents_request(monkeypatch):
    calls = scripted(monkeypatch, [])
    result = generate_combined_display(SOURCE, DATES, 'isolated', retry_allowed=lambda: False)
    assert result['display'] is None and result['sdk_requests'] == 0 and not calls
    result = generate_combined_display(SOURCE, DATES, 'isolated', cancelled=lambda: True)
    assert result['sdk_requests'] == 0 and not calls
