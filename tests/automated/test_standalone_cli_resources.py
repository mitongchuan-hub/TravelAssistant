"""Standalone runners never bootstrap resources without explicit live opt-in."""
from contextlib import contextmanager
import sys

import pytest

from app.core import config, resources
from tests.benchmarks import agent_latency as benchmark
from tests import live_single_agent_smoke, scope_eval, scope_suite


@pytest.mark.parametrize('runner', [scope_eval, scope_suite])
def test_scope_default_is_offline(monkeypatch, capsys, runner):
    def forbidden(*args, **kwargs):
        pytest.fail('Offline CLI must not bootstrap, create, borrow, or request a client')

    monkeypatch.setattr(config, 'load_environment', forbidden)
    monkeypatch.setattr(resources, 'client_scope', forbidden)
    monkeypatch.setattr(resources, 'borrow_client', forbidden)
    monkeypatch.setattr(scope_eval, '_create_chat_completion', forbidden)
    monkeypatch.setattr(sys, 'argv', [runner.__name__])
    runner.main()
    assert capsys.readouterr().out


def test_smoke_gate_precedes_bootstrap_and_isolation(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Smoke must require explicit opt-in before initialization')

    monkeypatch.setattr(config, 'load_environment', forbidden)
    monkeypatch.setattr(resources, 'client_scope', forbidden)
    monkeypatch.setattr(live_single_agent_smoke.tempfile, 'mkdtemp', forbidden)
    monkeypatch.setattr(sys, 'argv', ['live_single_agent_smoke.py'])
    with pytest.raises(SystemExit) as error:
        live_single_agent_smoke.main()
    assert error.value.code == 2


def test_smoke_isolation_precedes_bootstrap_and_failure_releases_scope(monkeypatch, tmp_path):
    events = []
    (tmp_path / '.cache').mkdir()
    monkeypatch.setattr(live_single_agent_smoke, 'ROOT', tmp_path)
    monkeypatch.setattr(live_single_agent_smoke.logging, 'disable', lambda *args: None)
    monkeypatch.setattr(sys, 'argv', ['live_single_agent_smoke.py', '--run-live'])
    monkeypatch.delitem(sys.modules, 'pytest')
    # Keep the smoke's environment cleanup local to this test.
    monkeypatch.setenv('CITY_DB_PATH', 'formal-db-must-not-be-used.sqlite3')
    monkeypatch.setenv('TRAVEL_PERSISTENCE', '0')
    monkeypatch.setenv('TRAVEL_AGENT_DISABLE_LLM', '1')
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'offline smoke fixture')

    def bootstrap():
        import os
        assert str(tmp_path) in os.environ['CITY_DB_PATH']
        assert os.environ['TRAVEL_PERSISTENCE'] == '1'
        assert 'PYTEST_CURRENT_TEST' not in os.environ
        assert 'TRAVEL_AGENT_DISABLE_LLM' not in os.environ
        events.append('bootstrap')

    @contextmanager
    def owner():
        assert events == ['timer_started', 'bootstrap']
        events.append('owner')
        try:
            yield
        finally:
            events.append('closed')

    class Timer:
        def __init__(self, seconds, callback):
            assert seconds == 345

        def start(self):
            events.append('timer_started')

        def cancel(self):
            events.append('timer_cancelled')

    monkeypatch.setattr(config, 'load_environment', bootstrap)
    monkeypatch.setattr(resources, 'client_scope', owner)
    monkeypatch.setattr(live_single_agent_smoke.threading, 'Timer', Timer)
    monkeypatch.setattr(config, 'llm_enabled', lambda: False)
    assert live_single_agent_smoke.main() == 1
    assert events == ['timer_started', 'bootstrap', 'owner', 'closed', 'timer_cancelled']
    import json
    report = json.loads(next((tmp_path / '.cache').glob('*/report.json')).read_text(encoding='utf-8'))
    assert report['failure']['type'] == 'AssertionError'
    assert report['production_db_used'] is False
    assert not (tmp_path / 'formal-db-must-not-be-used.sqlite3').exists()


@pytest.mark.parametrize('runner,timeout', [(scope_eval, 45), (scope_suite, 60)])
def test_scope_live_owns_client_until_all_cases_finish(monkeypatch, tmp_path, runner, timeout):
    events = []
    client = object()
    state = {'owner': False, 'borrowed': False}
    root = scope_suite.DATASET.resolve().parents[2]
    prompt = root / 'backend/app/agents/prompts/main.txt'
    # Runner output goes only into this temporary fixture, not the project cache.
    isolated_prompt = tmp_path / 'backend/app/agents/prompts/main.txt'
    isolated_prompt.parent.mkdir(parents=True)
    isolated_prompt.write_text(prompt.read_text(encoding='utf-8'), encoding='utf-8')
    monkeypatch.setattr(runner, 'PROMPT_PATH', isolated_prompt)
    monkeypatch.setattr(scope_suite, 'DATASET', root / 'tests/datasets/scope_comprehensive.json')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, 'load_environment', lambda: events.append('bootstrap'))

    @contextmanager
    def owner(**kwargs):
        assert kwargs == {'timeout_seconds': timeout}
        assert events == ['bootstrap']
        state['owner'] = True
        try:
            yield
        finally:
            assert not state['borrowed']
            state['owner'] = False
            events.append('closed')

    @contextmanager
    def borrow():
        assert state['owner']
        state['borrowed'] = True
        try:
            yield client
        finally:
            state['borrowed'] = False

    def evaluate(case, actual_client, model, *args):
        assert state == {'owner': True, 'borrowed': True}
        assert actual_client is client
        events.append('case')
        return dict(id=case['id'] if isinstance(case, dict) else case[0],
                    failed=False, unchanged=True, selected=[], calls=[], submitted=[])

    monkeypatch.setattr(resources, 'client_scope', owner)
    monkeypatch.setattr(resources, 'borrow_client', borrow)
    monkeypatch.setattr(runner, 'run_case', evaluate)
    if runner is scope_eval:
        monkeypatch.setattr(runner, 'CASES', [('fixture', 'Hello', 'Greeting')])
    else:
        monkeypatch.setattr(runner, 'load_cases', lambda: [dict(id='fixture', category='scope',
            level='simple', input='Hello', rubric='Greeting')])
    monkeypatch.setattr(sys, 'argv', [runner.__name__, '--live'])
    runner.main()
    assert events == ['bootstrap', 'case', 'closed']
    assert not state['owner']


def test_benchmark_live_releases_owner_after_request_failures(monkeypatch, tmp_path):
    events = []
    client = object()
    monkeypatch.setattr(config, 'load_environment', lambda: events.append('bootstrap'))
    monkeypatch.setattr(benchmark, 'APP_DIR', tmp_path)
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-offline-key')

    @contextmanager
    def owner():
        events.append('owner')
        try:
            yield
        finally:
            events.append('closed')

    @contextmanager
    def borrow():
        events.append('borrow')
        try:
            yield client
        finally:
            events.append('returned')

    def fail_request(actual_client, *args, **kwargs):
        assert actual_client is client
        assert events[-1] == 'borrow'
        raise RuntimeError('synthetic transport failure')

    monkeypatch.setattr(resources, 'client_scope', owner)
    monkeypatch.setattr(resources, 'borrow_client', borrow)
    monkeypatch.setattr(benchmark, '_create_chat_completion', fail_request)
    monkeypatch.setattr(sys, 'argv', ['bench_agent_latency.py', '--live', '--runs', '1'])
    assert benchmark.main() == 0
    assert events == ['bootstrap', 'owner', 'borrow', 'returned', 'closed']
    import json
    report = json.loads(next((tmp_path / '.cache/bench_latency').glob('*.json')).read_text(encoding='utf-8'))
    assert len(report['rows']) == 4
    assert all(row['error'] for row in report['rows'])
    assert report['payload_kind'] == 'synthetic'
