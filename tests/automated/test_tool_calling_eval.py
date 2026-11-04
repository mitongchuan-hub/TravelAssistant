"""Single-response eval contracts; never spend model or map quota."""
from copy import deepcopy
import json
from types import SimpleNamespace as NS

import pytest

from tests import tool_calling_eval as ev


def response(name='query_weather', arguments='{"city":"杭州","date":"2026-11-04"}', content=None):
    return NS(choices=[NS(finish_reason='tool_calls', message=NS(content=content, tool_calls=[
        NS(id='call-1', type='function', function=NS(name=name, arguments=arguments))]))],
        usage=NS(prompt_tokens=100, completion_tokens=20))


def weather():
    return {'id': 'example', 'input': '查杭州明天天气', 'context': {}, 'prior_calls': [],
            'expected': {'tool': 'query_weather', 'arguments': {'city': '杭州', 'date': '2026-11-04'},
                         'alternatives': {'city': ['杭州市']}, 'optional': {}}}


def test_correct_response_and_raw_arguments_are_preserved():
    raw = '{"city": "杭州市", "date": "2026-11-04"}'
    row = ev.grade_response(weather(), response(arguments=raw))
    assert row['passed'] is True
    assert (row['selection_ok'], row['parameters_ok'], row['format_ok']) == (True, True, True)
    assert row['calls'][0]['arguments'] == raw
    assert row['usage'] == {'prompt_tokens': 100, 'completion_tokens': 20, 'reasoning_tokens': None}


@pytest.mark.parametrize('raw', ['{', '[]', 'null', '"text"', '{"city":"杭州","city":"北京"}',
    '{"city":NaN}', '{"city":Infinity}', '```json\n{}\n```', '{} trailing'])
def test_strict_json_cannot_be_repaired_into_a_pass(raw):
    row = ev.grade_response(weather(), response(arguments=raw))
    assert not row['passed'] and row['format_ok'] is False
    assert row['calls'][0]['arguments'] == raw


@pytest.mark.parametrize('args', [
    {'city': '杭州'}, {'city': 1, 'date': '2026-11-04'},
    {'city': '杭州', 'date': '明天'}, {'city': '杭州', 'date': '2026-11-05'},
    {'city': '杭州', 'date': '2026-02-30'},
    {'city': '杭州', 'date': '2026-11-04', 'units': 'c'},
])
def test_schema_and_semantic_parameter_errors(args):
    row = ev.grade_response(weather(), response(arguments=json.dumps(args)))
    assert row['selection_ok'] and row['format_ok']
    assert not row['parameters_ok'] and not row['passed']


def test_wrong_tool_is_distinct_from_format_failure():
    row = ev.grade_response(weather(), response('maps_text_search', '{"keywords":"杭州"}'))
    assert row['selection_ok'] is False and row['format_ok'] is True
    assert not row['passed']


@pytest.mark.parametrize('kind', ['none', 'multiple', 'unknown', 'prose', 'not_function'])
def test_single_structured_call_without_prose_required(kind):
    value = response()
    message = value.choices[0].message
    if kind == 'none':
        message.content = 'query_weather(city="杭州")'
        message.tool_calls = []
    elif kind == 'multiple':
        message.tool_calls *= 2
    elif kind == 'unknown':
        message.tool_calls[0].function.name = 'invented'
    elif kind == 'not_function':
        message.tool_calls[0].type = 'custom'
    else:
        message.content = '我先查一下。'
    assert not ev.grade_response(weather(), value)['passed']


def test_coordinates_allow_numeric_equivalence_but_not_reversal_or_extra_fields():
    case = {'expected': {'tool': 'maps_direction_walking', 'arguments': {
        'origin': '120.1,30.2', 'destination': '120.3,30.4'}, 'optional': {}, 'alternatives': {}}}
    assert ev.grade_response(case, response('maps_direction_walking',
        '{"origin":"120.100, 30.200","destination":"120.300,30.400"}'))['passed']
    for origin in ['30.2,120.1', '120.3,30.4', 'NaN,30.2', '181,30.2', '120.1,91']:
        row = ev.grade_response(case, response('maps_direction_walking', json.dumps({
            'origin': origin, 'destination': '120.3,30.4'})))
        assert not row['parameters_ok']
    assert not ev.grade_response(case, response('maps_direction_walking',
        '{"origin":"120.1,30.2","destination":"120.3,30.4","mode":"walking"}'))['passed']


def test_optional_defaults_and_read_pagination_boundaries():
    case = {'expected': {'tool': 'read_context', 'arguments': {'section': 'messages'},
                        'alternatives': {}, 'optional': {'offset': 0, 'limit': 20, 'query': ''}}}
    for args in [{'section': 'messages'}, {'section': 'messages', 'offset': 0, 'limit': 20}]:
        assert ev.grade_response(case, response('read_context', json.dumps(args)))['passed']
    for args in [{'section': 'messages', 'limit': 101}, {'section': 'messages', 'offset': -1},
                 {'section': 'messages', 'limit': True}, {'section': 'messages', 'limit': 0}]:
        assert not ev.grade_response(case, response('read_context', json.dumps(args)))['passed']


def test_submit_quote_full_replacement_and_no_unauthorized_operation():
    case = {'expected': {'tool': 'save_trip_changes', 'arguments': {
        'user_quote': '请记录：不吃辣。\n只记录。', 'requirements_text': '不吃辣。'},
        'alternatives': {}, 'optional': {}}}
    good = deepcopy(case['expected']['arguments'])
    assert ev.grade_response(case, response('save_trip_changes', json.dumps(good)))['passed']
    for patch in [{'user_quote': '请记录：不吃辣。'}, {'requirements_text': '吃辣。'},
                  {'itinerary': {'action': 'clear'}}, {'next_question': '要重排吗？'}]:
        assert not ev.grade_response(case, response('save_trip_changes', json.dumps({**good, **patch})))['passed']


def test_dataset_all_tools_and_gold_pass_without_exposing_gold():
    data = ev.load_dataset()
    assert len(data['cases']) == 100
    assert len({c['id'] for c in data['cases']}) == 100
    assert set(c['expected']['tool'] for c in data['cases']) == set(ev.registry()) - {'read_skill'}
    for case in data['cases']:
        gold = case['expected']
        row = ev.grade_response(case, response(gold['tool'], json.dumps(gold['arguments'])))
        assert row['passed'], (case['id'], row)
        messages = ev.build_messages(data, case)
        assert messages[0]['content'] == ev.travel_role().prompt
        assert json.loads(messages[1]['content'])['message']['text'] == case['input']
        assert 'expected' not in json.loads(messages[1]['content'])
        assert 'alternatives' not in json.dumps(messages, ensure_ascii=False)


@pytest.mark.parametrize('mutation', ['duplicate', 'unknown_tool', 'bad_gold', 'bad_rule', 'missing_context'])
def test_dataset_rejects_drift_and_invalid_rules(tmp_path, mutation):
    data = ev.load_dataset()
    case = data['cases'][0]
    if mutation == 'duplicate':
        data['cases'][1]['id'] = case['id']
    elif mutation == 'unknown_tool':
        case['expected']['tool'] = 'removed_tool'
    elif mutation == 'bad_gold':
        case['expected']['arguments']['unexpected'] = 1
    elif mutation == 'missing_context':
        del case['context']
    else:
        case['expected']['alternatives']['invented'] = ['x']
    path = tmp_path / 'cases.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError):
        ev.load_dataset(path)


def test_summary_uses_all_attempts_and_unrounded_threshold():
    rows = [{'id': str(i), 'tool': 'query_weather', 'passed': i < 94,
             'selection_ok': True, 'parameters_ok': i < 94, 'format_ok': True,
             'errors': []} for i in range(100)]
    assert ev.summarize(rows, 100)['meets_target'] is False
    rows[94].update(passed=True, parameters_ok=True)
    assert ev.summarize(rows, 100)['meets_target'] is True
    rows[-1].update(passed=False, selection_ok=None, parameters_ok=None, format_ok=None,
                    errors=['request_error'])
    summary = ev.summarize(rows, 100)
    assert summary['passed'] == 95 and summary['total'] == 100
    assert summary['metrics']['selection_ok']['unavailable'] == 1
    assert ev.summarize(rows[:95], 100)['meets_target'] is None


def test_optional_rules_are_validated_before_live_run(tmp_path):
    data = ev.load_dataset()
    data['cases'][0]['expected']['optional'] = {'invented': 1}
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError):
        ev.load_dataset(path)


def test_gold_quote_cannot_diverge_from_user_input(tmp_path):
    data = ev.load_dataset()
    case = next(c for c in data['cases'] if c['expected']['tool'] == 'save_trip_changes')
    case['expected']['arguments']['user_quote'] = 'different message'
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError):
        ev.load_dataset(path)


def test_subset_report_cannot_claim_full_suite_acceptance(tmp_path):
    row = ev.grade_response(weather(), response())
    report = {'rows': [row], 'summary': ev.summarize([row], 1), 'metadata': {'subset': True}}
    path = tmp_path / 'subset.json'
    ev.write_report(path, report)
    text = path.with_suffix('.md').read_text(encoding='utf-8')
    assert 'SUBSET' in text
    assert '全量验收：未执行' in text


def test_live_rejects_provider_storage_before_creating_client(monkeypatch):
    monkeypatch.setattr(ev.config, 'load_environment', lambda: None)
    monkeypatch.setenv('OPENAI_STORE_RESPONSES', 'true')
    monkeypatch.setattr(ev.resources, 'client_scope', lambda: pytest.fail('created client'))
    with pytest.raises(SystemExit) as exc:
        ev.main(['--live'])
    assert exc.value.code == 2


def test_offline_cli_does_not_create_client_or_claim_accuracy(monkeypatch, capsys):
    monkeypatch.setattr(ev.resources, 'client_scope', lambda: pytest.fail('offline created client'))
    assert ev.main([]) == 0
    output = capsys.readouterr().out
    assert '100' in output and '10 tools' in output
    assert 'read_skill' in output and 'not covered' in output
    assert '--live' in output
    assert 'passed=' not in output


def test_run_cases_one_request_no_tool_execution_and_error_in_denominator(monkeypatch):
    data = ev.load_dataset()
    selected = [weather(), {**weather(), 'id': 'failure'}]
    calls = []
    def request(client, model, messages, **kwargs):
        calls.append(kwargs)
        assert len(kwargs['tools']) == 10
        assert not kwargs.get('fallback_model') and not kwargs.get('trace')
        if len(calls) == 2:
            raise TimeoutError('key=private-secret')
        return response()
    monkeypatch.setattr(ev, 'request_completion', request)
    monkeypatch.setattr(ev, 'forbidden_handler', lambda *a, **kw: pytest.fail('tool executed'))
    rows = list(ev.run_cases(data, selected, object(), 'fake'))
    assert len(calls) == 2 and len(rows) == 2
    assert rows[0]['passed'] and not rows[1]['passed']
    assert rows[1]['format_ok'] is None and rows[1]['calls'] == []
    assert 'private-secret' not in json.dumps(rows)
    assert ev.summarize(rows, 2)['rate'] == 0.5


def test_plan_snapshot_is_real_and_gold_never_supplies_missing_plan():
    data = ev.load_dataset()
    case = next(c for c in data['cases'] if c['expected']['tool'] == 'save_trip_changes'
                and c['expected']['arguments'].get('itinerary', {}).get('action') == 'confirm')
    payload = json.loads(ev.build_messages(data, case)[1]['content'])
    assert payload['current_plan']['text'] == '上午西湖散步。\n午餐清淡。\n下午喝茶。'
    assert payload['current_plan']['status'] == 'draft'


def test_valid_optional_edit_context_is_not_a_false_negative():
    case = {'expected': {'tool': 'save_trip_changes', 'arguments': {'user_quote': '改字',
        'itinerary': {'action': 'edit_text', 'operation': 'replace', 'old_text': '甲', 'new_text': '乙'}},
        'alternatives': {}, 'optional': {}}}
    args = deepcopy(case['expected']['arguments'])
    args['itinerary']['context'] = None
    assert ev.grade_response(case, response('save_trip_changes', json.dumps(args)))['passed']


def test_exact_edit_valid_context_passes_but_wrong_context_fails():
    data = ev.load_dataset()
    case = next(c for c in data['cases'] if c['id'] == 'submit-13')
    args = deepcopy(case['expected']['arguments'])
    args['itinerary']['context'] = '上午西湖散步。'
    assert ev.grade_response(case, response('save_trip_changes', json.dumps(args)))['passed']
    args['itinerary']['context'] = '下午喝茶。'
    assert not ev.grade_response(case, response('save_trip_changes', json.dumps(args)))['passed']


@pytest.mark.parametrize('wire', ['chat_completions', 'responses'])
def test_real_transport_boundary_only_requests_once_and_keeps_arguments(monkeypatch, wire):
    monkeypatch.setenv('OPENAI_WIRE_API', wire)
    sent = []
    def create(**kwargs):
        sent.append(kwargs)
        if wire == 'chat_completions':
            return response()
        item = NS(model_dump=lambda **kw: {'type': 'function_call', 'call_id': 'c1',
            'name': 'query_weather', 'arguments': '{"city":"杭州","date":"2026-11-04"}'})
        return NS(status='completed', output=[item], usage=NS(input_tokens=100, output_tokens=20))
    client = NS(chat=NS(completions=NS(create=create)), responses=NS(create=create))
    rows = list(ev.run_cases(ev.load_dataset(), [weather()], client, 'fake'))
    assert rows[0]['passed'] and rows[0]['usage']['prompt_tokens'] == 100
    assert len(sent) == 1 and len(sent[0]['tools']) == 10
    assert sent[0]['store'] is False


@pytest.mark.parametrize('kind', ['rate_limit', 'incomplete'])
def test_real_transport_failures_do_not_retry(monkeypatch, kind):
    from app.agents.model_transport import IncompleteModelOutput
    class RateLimitError(Exception):
        status_code = 429
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        if kind == 'rate_limit':
            raise RateLimitError('private-provider-body')
        value = response()
        value.choices[0].finish_reason = 'length'
        return value
    client = NS(chat=NS(completions=NS(create=create)))
    rows = list(ev.run_cases(ev.load_dataset(), [weather()], client, 'fake'))
    assert len(calls) == 1 and not rows[0]['passed']
    assert rows[0]['format_ok'] is None
    assert rows[0]['exception_type'] == ('RateLimitError' if kind == 'rate_limit' else IncompleteModelOutput.__name__)
    assert 'private-provider-body' not in json.dumps(rows)


def test_report_discloses_skill_profile_and_uncovered_tool(tmp_path):
    report = {'rows': [], 'summary': ev.summarize([], 100),
              'metadata': {'profile': ev.PROFILE, 'uncovered_tools': ['read_skill']}}
    path = tmp_path / 'report.json'
    ev.write_report(path, report)
    text = path.with_suffix('.md').read_text(encoding='utf-8')
    assert ev.PROFILE in text and 'read_skill' in text and '未覆盖' in text


def test_report_preserves_raw_calls_and_marks_incomplete(tmp_path):
    row = ev.grade_response(weather(), response(arguments='{'))
    report = {'rows': [row], 'summary': ev.summarize([row], 2), 'metadata': {}}
    path = tmp_path / 'report.json'
    ev.write_report(path, report)
    assert json.loads(path.read_text(encoding='utf-8'))['rows'][0]['calls'][0]['arguments'] == '{'
    assert 'INCOMPLETE' in path.with_suffix('.md').read_text(encoding='utf-8')
    assert not list(tmp_path.glob('*.tmp'))
