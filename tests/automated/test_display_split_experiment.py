from copy import deepcopy
import json
from types import SimpleNamespace
import pytest
from app.plan_display import SCHEMA, display_format
from tests.performance.display_split_experiment import generate_split_display, stage_format, GLOBAL_FIELDS, DAY_FIELDS
from tests.performance.run_display_split_experiment import run
from test_agent_roles import scripted, completion, display_data

DATES = ['2026-11-14', '2026-11-15']
SOURCE = '第一天午后散步，第二天退房；微辣也不要。建议带外套；住宿预算非报价，房态待核实。'


def pieces():
    full = display_data(DATES)
    full.update(budget_note='住宿预算非报价', risks=['微辣也不要'])
    full['preparation'] = {'clothing': [{'title': '外套', 'body': '建议带外套'}],
                           'accommodation': [{'title': '住宿', 'body': '房态待核实'}]}
    full['pending_items'] = ['房态待核实']
    return ({key: deepcopy(full[key]) for key in GLOBAL_FIELDS},
            {key: deepcopy(full[key]) for key in DAY_FIELDS})


def test_stage_schemas_are_disjoint_and_preserve_dates_without_mutating_shared_schema():
    before = deepcopy(SCHEMA)
    global_schema = stage_format('global', DATES)['json_schema']['schema']
    day_schema = stage_format('days', DATES)['json_schema']['schema']
    assert not (set(global_schema['properties']) & set(day_schema['properties']))
    assert set(global_schema['properties']) | set(day_schema['properties']) == set(display_format()['json_schema']['schema']['properties'])
    assert day_schema['properties']['days']['minItems'] == day_schema['properties']['days']['maxItems'] == 2
    assert day_schema['properties']['days']['items']['properties']['date']['enum'] == DATES
    assert SCHEMA == before


def test_split_uses_same_original_twice_and_merges_without_fabricating_or_rewriting(monkeypatch):
    global_data, day_data = pieces()
    calls = scripted(monkeypatch, [completion(global_data), completion(day_data)])
    before = deepcopy((global_data, day_data))
    result = generate_split_display(SOURCE, DATES, 'isolated')
    assert result['display_status'] == 'ready' and result['sdk_requests'] == 2
    assert [json.loads(messages[1]['content'])['source_text'] for messages, _ in calls] == [SOURCE, SOURCE]
    assert all(kwargs['tools'] is None for _, kwargs in calls)
    expected = {**global_data, **day_data, 'weather': []}
    assert result['display'] == expected
    assert (global_data, day_data) == before
    assert calls[0][1]['budget'] is calls[1][1]['budget']
    assert calls[0][1]['budget'].max_models == 2 and calls[0][1]['budget'].seconds == 180


@pytest.mark.parametrize('failure', ['global_json', 'days_schema', 'transport', 'wrong_order'])
def test_failed_piece_never_publishes_partial_display_or_launches_extra_correction(monkeypatch, failure):
    global_data, day_data = pieces()
    if failure == 'days_schema': del day_data['title']
    if failure == 'wrong_order': day_data['days'].reverse()
    responses = [completion(global_data), completion(day_data)]
    if failure == 'global_json': responses = [completion('{broken')]
    if failure == 'transport': responses = [completion(global_data), TimeoutError('upstream timeout')]
    calls = scripted(monkeypatch, responses)
    result = generate_split_display(SOURCE, DATES, 'isolated')
    assert result['display_status'] == 'fallback' and result['display'] is None
    assert len(calls) == (1 if failure == 'global_json' else 2)
    assert result['sdk_requests'] == len(calls)
    assert result['stages'][0].get('raw_output')


def test_foreground_arrival_prevents_second_stage(monkeypatch):
    global_data, _ = pieces()
    allowed = [True]
    def first(messages, kwargs):
        allowed[0] = False
        return completion(global_data)
    calls = scripted(monkeypatch, [first])
    result = generate_split_display(SOURCE, DATES, 'isolated', retry_allowed=lambda: allowed[0])
    assert len(calls) == 1 and result['display'] is None
    assert result['error_type'] == 'AgentBudgetError'
    assert len(result['stages']) == 1


def test_compatibility_request_exhausts_budget_and_no_third_sdk_request_is_sent(monkeypatch):
    global_data, _ = pieces()
    def compatibility(messages, kwargs):
        kwargs['budget'].model()
        return completion(global_data)
    calls = scripted(monkeypatch, [compatibility])
    result = generate_split_display(SOURCE, DATES, 'isolated')
    assert result['sdk_requests'] == 2 and len(calls) == 1
    assert result['error_type'] == 'AgentBudgetError' and result['display'] is None


def test_deadline_is_shared_and_exhaustion_after_first_stage_stops_remaining_work(monkeypatch):
    global_data, _ = pieces()
    def first(messages, kwargs):
        from time import monotonic
        kwargs['budget'].deadline = monotonic() - 1
        return completion(global_data)
    calls = scripted(monkeypatch, [first])
    result = generate_split_display(SOURCE, DATES, 'isolated')
    assert len(calls) == 1 and result['display'] is None
    assert result['error_type'] == 'AgentBudgetError'


def test_empty_preparation_is_retained_instead_of_program_guessing_clothes(monkeypatch):
    global_data, day_data = pieces()
    global_data['preparation'] = {'clothing': [], 'accommodation': []}
    scripted(monkeypatch, [completion(global_data), completion(day_data)])
    result = generate_split_display('原文没有衣物建议。', DATES, 'isolated')
    assert result['display_status'] == 'ready'
    assert result['display']['preparation']['clothing'] == []


def test_runner_alternates_strategies_and_preserves_failures_on_stop(monkeypatch, tmp_path):
    from app import agent_client
    from tests.performance import display_split_experiment
    calls = []
    def failure(strategy):
        def invoke(*args, **kwargs):
            calls.append(strategy)
            return {'display': None, 'display_status': 'fallback', 'error_type': 'APIConnectionError'}
        return invoke
    monkeypatch.setattr(agent_client, 'generate_plan_display', failure('whole'))
    monkeypatch.setattr(display_split_experiment, 'generate_split_display', failure('split'))
    args = SimpleNamespace(output=tmp_path/'run', repeats=3, cases=['LAT-012'], strategies=['whole', 'split'], max_consecutive_failures=2)
    assert run(args) == 2
    assert calls == ['whole', 'split']
    rows = json.loads((args.output/'results.json').read_text(encoding='utf-8'))
    assert len(rows) == 2 and all(r['result']['error_type'] == 'APIConnectionError' for r in rows)
    template = json.loads((args.output/'review-template.json').read_text(encoding='utf-8'))
    assert all(r['quality_grade'] == 'not_evaluated' for r in template)
    with pytest.raises(ValueError, match='empty'):
        run(args)
