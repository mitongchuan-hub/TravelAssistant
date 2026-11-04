"""Offline contracts for the opt-in real-model skill ablation."""
import json
from types import SimpleNamespace as NS

import pytest

from tests import travel_skill_eval as ev


def reply(name=None, arguments=None, content='离线回复'):
    calls = [] if name is None else [NS(id='call-' + name, function=NS(
        name=name, arguments=json.dumps(arguments, ensure_ascii=False)))]
    return NS(choices=[NS(finish_reason='tool_calls' if calls else 'stop',
        message=NS(content=None if calls else content, tool_calls=calls))])


def test_offline_manifest_is_complete_and_never_opens_client(tmp_path, monkeypatch):
    monkeypatch.setattr(ev.resources, 'client_scope', lambda **kw: pytest.fail('network'))
    state = ev.evaluate(tmp_path, live=False)
    assert state['status'] == 'not_run'
    assert len(state['tasks']) == 6
    assert [x['condition'] for x in state['tasks']] == [
        'with_skill', 'without_skill', 'without_skill', 'with_skill', 'with_skill', 'without_skill']
    for task in state['tasks']:
        directory = tmp_path / 'iteration-1' / task['name'] / task['condition']
        for file in ['outputs/result.md', 'outputs/result.json', 'transcript.json',
                     'timing.json', 'eval_metadata.json']:
            assert (directory / file).exists()
        meta = json.loads((directory / 'eval_metadata.json').read_text('utf-8'))
        assert meta['budget'] == ev.LIMITS
        assert set(meta['skill_resources']) == {'main'}
        assert meta['skill_version'] == '2'
        assert meta['input_snapshot']['execution_context']['current_date'] == '2030-05-09'
    assert (tmp_path / 'run-state.json').exists()
    third = ev.build_inputs(ev.load_cases()[2])
    assert '2030-05-10' in third[2].text and '2030-05-11' in third[2].text
    assert '龙井村' in third[2].text and '午餐' in third[2].text


def test_ablation_removes_all_loading_instructions_but_keeps_contract():
    full, without, edits = ev.prompt_variants()
    assert '先通过 read_skill' in full
    assert 'read_skill' not in without
    assert '按已加载规划技能执行' not in without
    assert '正文组织按用户要求执行' in without
    assert 'save_trip_changes' in without and '不造坐标' in without
    assert len(edits) >= 4
    assert all(item['old'] in full for item in edits)


@pytest.mark.parametrize('condition', ['with_skill', 'without_skill'])
def test_real_terminal_candidate_and_local_reads_without_query(monkeypatch, condition):
    monkeypatch.setattr(ev.TaskExecutionRuntime, '_query', lambda *a: pytest.fail('real query'))
    seen = []
    def request(client, model, messages, **kw):
        assert kw['trace'].request_id
        assert kw['budget'].total_token_limit == 180000
        assert not kw.get('fallback_model')
        kw['budget'].model()
        kw['budget'].record_usage(100, 10)
        seen.append(messages)
        if len(seen) == 1:
            return reply('read_context', {'section': 'plan'})
        if len(seen) == 2:
            return reply('save_trip_changes', {'user_quote': ev.load_cases()[0]['prompt'],
                'itinerary': {'action': 'replace', 'text': '# 离线候选\n2030-05-10 与 2030-05-11'}})
        pytest.fail('terminal requested another model')
    monkeypatch.setattr(ev, 'request_completion', request)
    result, transcript, timing, meta = ev.run_case(ev.load_cases()[0], condition, None, 'offline')
    assert not result['runtime_failed'], result
    assert result['has_changes'] and result['unchanged']
    assert result['model_calls'] == 2 and result['total_tokens'] == 220
    assert result['answer'].startswith('# 离线候选')
    assert not result['external_boundary_violations']
    assert transcript['calls'][-1]['arguments_raw']


def test_all_external_handlers_replaced_and_unknown_registry_rejected(monkeypatch):
    context, execution, plan, fixtures = ev.build_inputs(ev.load_cases()[0])
    runtime = ev.TaskExecutionRuntime(None, 'offline', context, execution, request=lambda *a: None)
    ev.install_fixtures(runtime, fixtures)
    for name in ev.EXTERNAL:
        assert runtime.registry[name].handler is not None
    runtime.registry['surprise'] = runtime.registry['read_context']
    with pytest.raises(ValueError, match='registry'):
        ev.install_fixtures(runtime, fixtures)


def test_fixture_routes_are_directional_synthetic_and_unknown_is_not_invented():
    _, _, _, fixture = ev.build_inputs(ev.load_cases()[0])
    assert fixture['source'] == 'synthetic_fixture'
    assert fixture['routes']
    assert all(row['source'] == 'synthetic_fixture' for row in fixture['routes'])
    assert ev.fixture_response(fixture, 'maps_search_detail', {'id': 'not-known'})['status'] == 'unavailable'


def test_trace_strips_reasoning_opaque_and_exception_body():
    trace = ev.MemoryTrace('id')
    trace.add('Task Runtime异常', {'error_type': 'ValueError', 'reason': 'secret'})
    trace.add('模型响应', {'content': 'final', 'reasoning_content': 'secret',
        '_responses_output': [{'secret': 'opaque'}], 'tool_calls': []})
    text = json.dumps(trace.events)
    assert 'secret' not in text and 'opaque' not in text
    assert 'ValueError' in text and 'final' in text
    assert not hasattr(trace, 'write')


def test_model_exception_is_type_only_and_progress_persists(tmp_path, monkeypatch):
    def fail(*a, **kw):
        raise RuntimeError('sensitive provider body')
    monkeypatch.setattr(ev, 'request_completion', fail)
    result, transcript, _, _ = ev.run_case(ev.load_cases()[0], 'with_skill', None, 'offline')
    assert result['runtime_failed']
    assert 'sensitive provider body' not in json.dumps([result, transcript])
    assert 'RuntimeError' in json.dumps(transcript)


def test_token_limit_is_soft_cumulative_stop():
    budget = ev.new_budget()
    budget.model()
    with pytest.raises(ev.AgentBudgetError):
        budget.record_usage(180001, 0)
    with pytest.raises(ev.AgentBudgetError):
        budget.model()
    assert budget.model_calls == 1


@pytest.mark.parametrize('case', ev.load_cases(), ids=lambda c: c['name'])
def test_each_original_prompt_can_use_real_save_validation(monkeypatch, case):
    def request(*a, **kw):
        return reply('save_trip_changes', {'user_quote': case['prompt'],
            'itinerary': {'action': 'replace', 'text': ev.OLD_PLAN}})
    monkeypatch.setattr(ev, 'request_completion', request)
    result, transcript, _, _ = ev.run_case(case, 'without_skill', None, 'offline')
    # Freeze existing business blockers; never weaken the real save handler.
    assert result['has_changes'] == (case['id'] == 1), transcript
    if case['id'] != 1:
        assert any(e['value'].get('error_type') == 'ValueError' for e in transcript['tool_results'])
    assert result['unchanged']


@pytest.mark.parametrize('name,args', [
    ('maps_text_search', {'city': '杭州', 'keywords': '西湖'}),
    ('maps_around_search', {'keywords': '餐厅', 'location': '120.15,30.25'}),
    ('maps_search_detail', {'id': 'west'}),
    ('query_weather', {'city': '杭州', 'date': '2030-05-10'}),
    ('maps_direction_walking', {'origin': '120.15,30.25', 'destination': '120.14,30.24'}),
    ('maps_direction_driving', {'origin': '120.15,30.25', 'destination': '120.14,30.24'}),
    ('maps_direction_transit_integrated', {'origin': '120.15,30.25', 'destination': '120.14,30.24',
                                         'city': '杭州', 'cityd': '杭州'}),
])
def test_every_external_call_is_fixture_only(monkeypatch, name, args):
    monkeypatch.setattr(ev.TaskExecutionRuntime, '_query', lambda *a: pytest.fail('real query'))
    count = []
    def request(*a, **kw):
        count.append(1)
        if len(count) == 1:
            return reply(name, args)
        assert 'synthetic_fixture' in a[2][-1]['content']
        return reply()
    monkeypatch.setattr(ev, 'request_completion', request)
    result, transcript, _, _ = ev.run_case(ev.load_cases()[0], 'without_skill', None, 'offline')
    assert not result['runtime_failed']
    assert transcript['tool_results'][0]['value']['result']['source'] == 'synthetic_fixture'


def test_local_skill_read_and_without_registry_are_real(monkeypatch):
    def request(*a, **kw):
        tools = [t['function']['name'] for t in kw['tools']]
        if 'read_skill' not in tools:
            assert 'read_skill' not in a[2][0]['content']
            return reply()
        if a[2][-1]['role'] != 'tool':
            return reply('read_skill', {'name': 'travel-planning', 'resource': 'main'})
        assert 'content' in json.loads(a[2][-1]['content'])
        return reply()
    monkeypatch.setattr(ev, 'request_completion', request)
    result, _, _, _ = ev.run_case(ev.load_cases()[0], 'with_skill', None, 'offline')
    assert result['called_skill']
    result, _, _, _ = ev.run_case(ev.load_cases()[0], 'without_skill', None, 'offline')
    assert not result['called_skill']


def test_store_true_refuses_before_resources(tmp_path, monkeypatch):
    monkeypatch.setattr(ev.config, 'load_environment', lambda: None)
    monkeypatch.setattr(ev.config, 'validate_configuration', lambda **kw: None)
    monkeypatch.setattr(ev, 'response_storage_enabled', lambda: True)
    monkeypatch.setattr(ev.resources, 'client_scope', lambda **kw: pytest.fail('network'))
    with pytest.raises(ValueError, match='storage'):
        ev.evaluate(tmp_path, live=True)
