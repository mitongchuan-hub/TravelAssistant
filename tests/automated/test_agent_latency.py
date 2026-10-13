"""Verify latency attribution, failure evidence, caching and report safety offline."""
from copy import deepcopy
from datetime import date
import importlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'performance'))
from latency_timeline import Timeline, interval_union
from run_agent_latency import DATASET, resolve_dataset, run_sample, frontend_poll_intervals, validate_dataset
from latency_report import aggregate, write_reports

DATA = resolve_dataset(DATASET, date(2026, 10, 11))


def test_latency_nested_spans_account_for_wall_time_once():
    clock = [0.0]
    t = Timeline(clock=lambda: clock[0])
    with t.span('root', 'orchestration'):
        clock[0] = 1
        with t.span('logical request', 'model_overhead'):
            clock[0] = 2
            with t.span('SDK', 'model_planning'):
                clock[0] = 6
            clock[0] = 7
        clock[0] = 10
    result = t.finalize()
    assert result['accounting_valid'] and result['measured_seconds'] == 10
    assert result['exclusive_seconds_by_category'] == {'orchestration': 4, 'model_overhead': 2, 'model_planning': 4}
    assert [s['seconds'] for s in result['spans']] == [10, 6, 4]
    assert next(e for e in t.chrome_trace()['traceEvents'] if e['name']=='SDK')['dur'] == 4e6
    assert interval_union([(1, 5), (3, 8), (9, 10)]) == 8


def test_latency_offsets_start_at_request_not_hook_setup():
    clock = [0.0]
    t = Timeline(clock=lambda: clock[0])
    clock[0] = .5
    with t.span('request', 'orchestration'):
        t.event('accepted')
        clock[0] = 2
    result = t.finalize()
    assert result['measured_seconds'] == 1.5
    assert result['spans'][0]['start_seconds'] == 0
    assert result['events'][0]['offset_seconds'] == 0
    assert result['spans'][0]['end_seconds'] == 1.5


def test_latency_overlapping_lanes_do_not_double_count_user_wait():
    clock = [0.0]
    t = Timeline(clock=lambda: clock[0])
    with t.span('foreground request', 'orchestration'):
        clock[0] = 1
        with t.span('background work', 'model_display', lane='display'):
            clock[0] = 2
        clock[0] = 3
    result = t.finalize()
    assert result['concurrent'] and result['measured_seconds'] == 3
    assert result['lane_seconds'] == {'foreground': 3, 'display': 1}
    assert sum(result['exclusive_seconds_by_category'].values()) == 4
    assert result['accounting_valid'] and result['overlap_seconds'] == 1
    events = [e for e in t.chrome_trace()['traceEvents'] if e['ph']=='X']
    assert events[0]['tid'] != events[1]['tid']


def test_latency_failed_spans_keep_duration_and_close_parent():
    clock = [0.0]
    t = Timeline(clock=lambda: clock[0])
    with pytest.raises(TimeoutError):
        with t.span('root', 'orchestration'):
            with t.span('SDK', 'model_display'):
                clock[0] = 5
                raise TimeoutError('injected test')
    result = t.finalize()
    assert result['accounting_valid'] and result['measured_seconds'] == 5
    assert all(s['status'] == 'error' for s in result['spans'])
    assert result['spans'][1]['error_type'] == 'TimeoutError'


def test_latency_dataset_dates_and_profiles_are_valid():
    assert len(DATA['cases']) == 26
    assert DATA['fixtures']['one_day']['requirements']['start_date'] == '2026-10-14'
    assert DATA['fixtures']['seven_days']['requirements']['end_date'] == '2026-10-20'
    assert len(DATA['profiles']['smoke']) == 4
    assert frontend_poll_intervals() == {'app': 3, 'app_active': 1, 'tasks': 2.5, 'tasks_active': 1}
    bad = deepcopy(DATA)
    bad['cases'].append(bad['cases'][0])
    with pytest.raises(ValueError, match='Duplicate'):
        validate_dataset(bad)


@pytest.mark.parametrize('case', DATA['cases'], ids=lambda c: c['id'])
def test_latency_all_scenarios_record_complete_isolated_timeline(monkeypatch, tmp_path, case):
    monkeypatch.setenv('OPENAI_API_KEY', 'latency-fixture-key')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://latency.invalid/v1')
    monkeypatch.setenv('OPENAI_MODEL', 'fixture')
    monkeypatch.setenv('OPENAI_FALLBACK_MODEL', '')
    from app import agent_client
    original_request = agent_client._create_chat_completion
    original_create = agent_client.create_completion
    original_main_store = importlib.import_module('app.main').store
    rows = run_sample(DATA, case, tmp_path / 'run', simulate=True, tools_mode='controlled')
    assert len(rows) == len(case['steps'])
    assert agent_client._create_chat_completion is original_request
    assert agent_client.create_completion is original_create
    assert importlib.import_module('app.main').store is original_main_store
    for row in rows:
        assert row['error'] is None, row['error']
        assert row['expectation_checks_pass'], row['expectation_checks']
        assert row['timeline']['accounting_valid']
        for lane, values in row['timeline']['exclusive_seconds_by_lane'].items():
            assert sum(values.values()) == pytest.approx(row['timeline']['lane_seconds'][lane])
        if row['task']:
            assert row['metrics']['primary_seconds'] <= row['metrics']['seconds']
        assert row['model_mode'] == 'scripted' and row['tools_mode'] == 'controlled'
        assert row['metrics']['http_live_calls'] == 0
        assert row['readiness']['browser_visible_seconds'] is None
        assert row['readiness']['first_token_seconds'] is None
        if row['task']:
            assert row['readiness']['reply_store_ready_seconds'] is not None
            assert row['readiness']['chat_html_ready_seconds'] >= row['readiness']['reply_store_ready_seconds']
        if row['metrics']['sdk_requests']:
            assert row['metrics']['completion_tokens'] is None
    if case['id'] == 'LAT-025':
        assert rows[0]['metrics']['backend_calls'] == 2
        assert rows[0]['metrics']['cache_hits'] == 1
    if case['id'] == 'LAT-026':
        assert rows[0]['metrics']['backend_calls'] == 3
        assert rows[0]['metrics']['cache_hits'] == 1
    if case['id'] == 'LAT-024':
        assert rows[0]['after']['plan']['text']
        assert rows[0]['after']['plan']['display_status'] == 'fallback'
        assert rows[0]['metrics']['seconds'] >= .05
        assert rows[0]['fault_hits'] == ['display_timeout']


def test_latency_retries_have_individual_sdk_spans_and_shared_budget(monkeypatch):
    import httpx
    import openai
    from types import SimpleNamespace
    from app import agent_client
    from app.agent_runtime import TurnBudget
    from latency_instrumentation import Instrumentation, completion
    attempts = []
    def transport(client, params, api):
        attempts.append(dict(params))
        if len(attempts) == 1:
            response = httpx.Response(400, request=httpx.Request('POST', 'https://fixture.invalid/v1'))
            raise openai.BadRequestError('reasoning_effort unsupported', response=response,
                body={'error': {'param': 'reasoning_effort', 'message': 'reasoning_effort unsupported'}})
        return completion('ok')
    monkeypatch.setattr(agent_client, 'create_completion', transport)
    monkeypatch.setenv('OPENAI_WIRE_API', 'chat_completions')
    t = Timeline()
    budget = TurnBudget()
    case = {'id': 'retry-probe', 'faults': {}}
    with Instrumentation(t, case, {}, tools_mode='controlled', base_date=date(2026, 10, 11), trace_sink=lambda trace: None):
        with t.span('root', 'orchestration'):
            agent_client._create_chat_completion(SimpleNamespace(base_url='https://fixture.invalid/v1'), 'fixture',
                [{'role': 'user', 'content': 'hello'}], agent_role='display', reasoning_effort='low', budget=budget)
    result = t.finalize()
    requests = [s for s in result['spans'] if s['name'].startswith('SDK请求：')]
    assert len(requests) == 2 and requests[0]['status'] == 'error' and requests[1]['status'] == 'completed'
    assert len([s for s in result['spans'] if s['name'].startswith('模型逻辑调用：')]) == 1
    assert budget.model_calls == 2 and result['accounting_valid']
    assert requests[0]['metadata']['reasoning_sent'] == 'low'
    assert requests[1]['metadata']['reasoning_sent'] is None


def test_latency_report_preserves_failures_and_escapes_script_content(monkeypatch, tmp_path):
    monkeypatch.setenv('OPENAI_API_KEY', 'latency-fixture-key')
    monkeypatch.setenv('OPENAI_MODEL', 'fixture')
    case = next(c for c in DATA['cases'] if c['id'] == 'LAT-021')
    rows = run_sample(DATA, case, tmp_path/'sample', simulate=True, tools_mode='controlled')
    second = deepcopy(rows[0])
    second['attempt'] = 2
    second['outcome'] = 'failed'
    second['metrics']['seconds'] = 20
    rows[0]['input'] = '</script><script>window.injected=true</script>'
    rows.append(second)
    aggregate_rows = aggregate(rows)
    assert aggregate_rows[0]['all_wait_seconds']['max'] == 20
    assert aggregate_rows[0]['failed_wait_seconds'] == [20]
    out = tmp_path/'report'
    out.mkdir()
    summary = write_reports(out, {'model_mode': 'scripted', 'tools_mode': 'controlled', 'model': 'fixture',
        'base_date': '2026-10-11', 'business_db_unchanged': True}, rows, [])
    text = (out/'report.html').read_text(encoding='utf-8')
    assert '</script><script>window.injected' not in text
    assert '\\u003c/script\\u003e' in text
    assert '/*__LATENCY_DATA__*/' not in text
    assert (out/'spans.csv').read_bytes().startswith(b'\xef\xbb\xbf')
    assert summary['outcomes']['failed'] == 1
