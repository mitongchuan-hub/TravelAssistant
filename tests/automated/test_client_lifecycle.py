"""Offline integration coverage for request leases and application bootstrap."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event, Thread, get_ident
from types import SimpleNamespace

import pytest

from app.core import config, resources
from app.agents import entrypoints, model_client, trace as trace_module
from app.agents.inputs.execution import TaskExecutionContext


HTML = ('<!doctype html><html><head><title>杭州攻略</title></head>'
        '<body><h1>杭州攻略</h1><p>2030-05-01 上午西湖。</p></body></html>')


class FakeClient:
    def __init__(self, **settings):
        self.settings = settings
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-lifecycle-key')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://lifecycle.offline.invalid/v1')
    monkeypatch.setenv('OPENAI_STORE_RESPONSES', 'false')
    monkeypatch.setenv('TRAVEL_DISPLAY_ENABLED', 'false')
    monkeypatch.setenv('TRAVEL_DISPLAY_TIMEOUT_SECONDS', '12')
    monkeypatch.setenv('TRAVEL_COOKIE_SECURE', 'false')
    monkeypatch.setenv('AMAP_MAPS_API_KEY', 'offline-lifecycle-map-key')
    monkeypatch.setenv('AMAP_MCP_URL', 'https://maps.offline.invalid/mcp')
    monkeypatch.setenv('AMAP_MCP_TIMEOUT_SECONDS', '5')
    monkeypatch.delenv('TRAVEL_AGENT_DISABLE_LLM', raising=False)
    monkeypatch.setenv('TRAVEL_PERSISTENCE', '0')
    monkeypatch.setenv('OPENAI_WIRE_API', 'chat_completions')
    monkeypatch.setenv('OPENAI_TIMEOUT_SECONDS', '12')
    monkeypatch.setenv('OPENAI_MODEL', 'offline-first')
    monkeypatch.setenv('OPENAI_FALLBACK_MODEL', '')
    runs = []
    monkeypatch.setattr(trace_module, 'record_agent_run',
                        lambda trace, *, error=None: runs.append((trace, error)))
    return runs


@pytest.fixture
def contexts():
    from app.storage.store import DemoStore

    store = DemoStore(persistence_enabled=False, display_worker_enabled=False)

    def capture(request_id):
        task, _ = store.submit_task('trip-hangzhou', 'chat', {'body': '你好'}, request_id)
        context = store.capture_request_context('trip-hangzhou', task['id'])
        execution = TaskExecutionContext(
            trip=deepcopy(store.trips['trip-hangzhou']),
            plan=deepcopy(store.plans['trip-hangzhou']),
            messages=deepcopy(store.messages['trip-hangzhou']),

            metrics={}, current_date='2030-05-01', entry=context.entry)
        return context, execution

    return capture


def response(content):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content, tool_calls=[]), finish_reason='stop')], usage=None)


def test_proposals_and_display_share_owner_not_turn_state(monkeypatch, contexts, offline):
    monkeypatch.setattr(config, 'llm_enabled', lambda: True)
    clients, requests = [], []

    def factory(**settings):
        clients.append(FakeClient(**settings))
        return clients[-1]

    # Keep real runtimes, transport adapter, budgets, traces and HTML validation.
    def completion(client, params, api):
        requests.append((client, params))
        assert client.close_calls == 0
        return response(HTML if len(requests) == 3 else '你好！')

    monkeypatch.setattr(model_client, 'create_completion', completion)
    budgets = []
    from app.agents.budget import TurnBudget
    real_model = TurnBudget.model

    def count_model(budget):
        budgets.append(budget)
        assert budget.model_calls == 0
        real_model(budget)

    monkeypatch.setattr(TurnBudget, 'model', count_model)
    with resources.client_scope(client_factory=factory):
        first = entrypoints.generate_task_result(*contexts('first'))
        monkeypatch.setenv('OPENAI_MODEL', 'offline-second')
        second = entrypoints.generate_task_result(*contexts('second'))
        display = entrypoints.generate_plan_display('上午西湖。', ['2030-05-01'], 'trip-hangzhou')
        assert not first.failed and not second.failed
        assert display['display_status'] == 'ready'
        assert len(clients) == 1 and clients[0].close_calls == 0
        assert all(client is clients[0] for client, _ in requests)
        assert [params['model'] for _, params in requests] == [
            'offline-first', 'offline-second', 'offline-second']
        assert clients[0].settings['timeout'] == 12
        assert 'tools' not in requests[-1][1]
        assert len({id(budget) for budget in budgets}) == 3
        assert all(budget.model_calls == 1 for budget in budgets)
        assert (budgets[-1].max_models, budgets[-1].max_tools, budgets[-1].max_external) == (2, 0, 0)
        assert len(offline) == 3
        assert len({id(trace) for trace, _ in offline}) == 3
        assert len({trace.request_id for trace, _ in offline}) == 3
        assert all(trace.written and error is None for trace, error in offline)
        assert all(any(row['title'] == '模型请求' for row in trace.records) for trace, _ in offline)
    assert clients[0].close_calls == 1


def test_display_primary_rate_limit_uses_fallback_once(monkeypatch, offline):
    monkeypatch.setattr(config, 'llm_enabled', lambda: True)
    monkeypatch.setenv('OPENAI_FALLBACK_MODEL', 'offline-fallback')
    calls = []

    class RateLimitError(RuntimeError):
        status_code = 429

    def completion(client, params, api):
        calls.append(params['model'])
        if len(calls) == 1:
            raise RateLimitError('private provider details')
        return response(HTML)

    monkeypatch.setattr(model_client, 'create_completion', completion)
    with resources.client_scope(client_factory=lambda **settings: FakeClient(**settings)):
        result = entrypoints.generate_plan_display('上午西湖。', ['2030-05-01'], 'trip-hangzhou')

    assert result['display_status'] == 'ready'
    assert calls == ['offline-first', 'offline-fallback']
    trace, error = offline[-1]
    assert error is None
    switch = [row['value'] for row in trace.records if row['title'] == '切换备用模型']
    assert switch == [{'from': 'offline-first', 'to': 'offline-fallback',
                       'scope': 'display_model_request', 'error_type': 'RateLimitError',
                       'status_code': 429}]


def test_display_fallback_failure_does_not_get_a_third_model_call(monkeypatch, offline):
    monkeypatch.setattr(config, 'llm_enabled', lambda: True)
    monkeypatch.setenv('OPENAI_FALLBACK_MODEL', 'offline-fallback')
    calls = []

    class RateLimitError(RuntimeError):
        status_code = 429

    def completion(client, params, api):
        calls.append(params['model'])
        raise RateLimitError('private provider details')

    monkeypatch.setattr(model_client, 'create_completion', completion)
    with resources.client_scope(client_factory=lambda **settings: FakeClient(**settings)):
        result = entrypoints.generate_plan_display('上午西湖。', ['2030-05-01'], 'trip-hangzhou')

    assert result['display_status'] == 'fallback'
    assert calls == ['offline-first', 'offline-fallback']
    trace, error = offline[-1]
    assert error is None
    switch = [row['value'] for row in trace.records if row['title'] == '切换备用模型']
    assert switch == [{'from': 'offline-first', 'to': 'offline-fallback',
                       'scope': 'display_model_request', 'error_type': 'RateLimitError',
                       'status_code': 429}]


def test_display_non_rate_limit_error_does_not_use_fallback(monkeypatch, offline):
    monkeypatch.setattr(config, 'llm_enabled', lambda: True)
    monkeypatch.setenv('OPENAI_FALLBACK_MODEL', 'offline-fallback')
    calls = []

    class BadRequestError(RuntimeError):
        status_code = 400

    def completion(client, params, api):
        calls.append(params['model'])
        raise BadRequestError('private provider details')

    monkeypatch.setattr(model_client, 'create_completion', completion)
    with resources.client_scope(client_factory=lambda **settings: FakeClient(**settings)):
        result = entrypoints.generate_plan_display('上午西湖。', ['2030-05-01'], 'trip-hangzhou')

    assert result['display_status'] == 'fallback'
    assert calls == ['offline-first']


def test_display_budget_boundary_does_not_attempt_correction_after_fallback(monkeypatch, offline):
    monkeypatch.setattr(config, 'llm_enabled', lambda: True)
    monkeypatch.setenv('OPENAI_FALLBACK_MODEL', 'offline-fallback')
    calls = []

    class RateLimitError(RuntimeError):
        status_code = 429

    def completion(client, params, api):
        calls.append(params['model'])
        if len(calls) == 1:
            raise RateLimitError('private provider details')
        return response('<h1>截断的页面')

    monkeypatch.setattr(model_client, 'create_completion', completion)
    with resources.client_scope(client_factory=lambda **settings: FakeClient(**settings)):
        result = entrypoints.generate_plan_display('上午西湖。', ['2030-05-01'], 'trip-hangzhou')

    assert result['display_status'] == 'fallback'
    assert calls == ['offline-first', 'offline-fallback']


@pytest.mark.parametrize('failure', ['model-error', 'cancelled'])
def test_error_or_cancel_releases_lease_without_closing_owner(monkeypatch, contexts, offline, failure):
    monkeypatch.setattr(config, 'llm_enabled', lambda: True)
    calls = []

    def completion(*args):
        calls.append(None)
        raise RuntimeError('offline model failure')

    monkeypatch.setattr(model_client, 'create_completion', completion)
    client = FakeClient()
    cancelled = (lambda: True) if failure == 'cancelled' else None
    with resources.client_scope(client_factory=lambda **settings: client):
        proposal = entrypoints.generate_task_result(*contexts(failure), cancelled=cancelled)
        display = entrypoints.generate_plan_display('上午西湖。', ['2030-05-01'],
                                                      'trip-hangzhou', cancelled=cancelled)
        assert proposal.failed
        assert display['display_status'] == 'fallback'
        assert client.close_calls == 0
        with resources.borrow_client() as borrowed:
            assert borrowed is client
        assert len(offline) == 2 and all(trace.written for trace, _ in offline)
        assert len(calls) == (0 if failure == 'cancelled' else 2)
    assert client.close_calls == 1


@pytest.mark.parametrize('enabled', [False, True])
def test_missing_owner_fails_closed_without_unmanaged_construction(monkeypatch, contexts, offline, enabled):
    import openai

    def forbidden(*args, **kwargs):
        pytest.fail('request constructed or called an unmanaged model client')

    monkeypatch.setattr(openai, 'OpenAI', forbidden)
    monkeypatch.setattr(model_client, 'create_completion', forbidden)
    monkeypatch.setattr(config, 'llm_enabled', lambda: enabled)
    assert resources._owner is None
    proposal = entrypoints.generate_task_result(*contexts('no-owner'))
    display = entrypoints.generate_plan_display('上午西湖。', ['2030-05-01'], 'trip-hangzhou')
    assert proposal.failed and not proposal.changes
    assert display['display'] is None and display['display_status'] == 'fallback'
    assert len(offline) == 2 and all(trace.written for trace, _ in offline)
    assert resources._owner is None


def test_default_offline_fixture_never_constructs_or_calls_model(monkeypatch, contexts, offline):
    import openai

    def forbidden(*args, **kwargs):
        pytest.fail('default pytest path reached the network client')

    monkeypatch.setattr(openai, 'OpenAI', forbidden)
    monkeypatch.setattr(model_client, 'create_completion', forbidden)
    assert not config.llm_enabled()
    with resources.client_scope():
        assert entrypoints.generate_task_result(*contexts('pytest-guard')).failed
        assert entrypoints.generate_plan_display('上午西湖。', [], 'trip-hangzhou')['display_status'] == 'fallback'
    assert len(offline) == 2


def test_offline_constructor_guard_survives_enabling_model_configuration(monkeypatch):
    import openai

    monkeypatch.setattr(config, 'llm_enabled', lambda: True)
    with pytest.raises(AssertionError, match='real OpenAI constructor'):
        openai.OpenAI(api_key='offline-test-key')
    with pytest.raises(AssertionError, match='real OpenAI constructor'):
        with resources.client_scope():
            pytest.fail('real constructor guard allowed scope startup')
    assert resources._owner is None


@pytest.mark.parametrize('operation', ['connect', 'connect_ex', 'create_connection'])
def test_offline_socket_guard_rejects_network(operation):
    import socket

    address = ('203.0.113.1', 443)
    with pytest.raises(AssertionError, match='real network access'):
        if operation == 'create_connection':
            socket.create_connection(address)
        else:
            with socket.socket() as connection:
                getattr(connection, operation)(address)


def test_offline_http_guard_preserves_fake_transport_and_async_loop():
    import httpx

    def respond(request):
        return httpx.Response(200, json={'offline': True})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert client.get('https://offline.invalid').json() == {'offline': True}
    with httpx.Client() as client:
        with pytest.raises(AssertionError, match='real network access'):
            client.get('https://offline.invalid')

    async def exercise():
        # Creating this loop also exercises Windows' local socketpair wakeup.
        await asyncio.sleep(0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert (await client.get('https://offline.invalid')).json() == {'offline': True}
        async with httpx.AsyncClient() as client:
            with pytest.raises(AssertionError, match='real network access'):
                await client.get('https://offline.invalid')

    asyncio.run(exercise())


def test_fastapi_lifespan_orders_worker_and_drains_off_event_loop(monkeypatch):
    monkeypatch.setattr(config, 'llm_enabled', lambda: True)
    from app import main
    from app.api import dependencies as deps

    events, clients, worker_errors = [], [], []
    loop_thread = get_ident()
    ready, stopped, release = Event(), Event(), Event()
    workers = []

    def worker():
        try:
            with resources.borrow_client() as client:
                assert client is clients[-1]
                events.append('borrow')
                ready.set()
                assert release.wait(5)
                assert client.close_calls == 0
                events.append('release')
        except BaseException as exc:
            worker_errors.append(exc)
            ready.set()

    class Store:
        def start_display_worker(self):
            assert get_ident() == loop_thread
            events.append('start')
            thread = Thread(target=worker)
            workers.append(thread)
            thread.start()
            assert ready.wait(5)
            assert not worker_errors

        def stop_display_worker(self):
            assert get_ident() != loop_thread
            events.append('stop')
            stopped.set()

    class Client(FakeClient):
        def close(self):
            assert get_ident() != loop_thread
            events.append('close')
            super().close()

    @contextmanager
    def scope():
        assert get_ident() != loop_thread
        client = Client()
        clients.append(client)
        with resources.client_scope(client_factory=lambda **settings: client):
            events.append('ready')
            yield
            events.append('drain')
        events.append('closed')

    monkeypatch.setattr(deps, 'store', Store())
    monkeypatch.setattr(main, 'client_scope', scope)

    async def exercise():
        for _ in range(2):
            ready.clear()
            stopped.clear()
            release.clear()
            lifespan = main.app.router.lifespan_context(main.app)
            await lifespan.__aenter__()
            closing = asyncio.create_task(lifespan.__aexit__(None, None, None))
            try:
                assert await asyncio.to_thread(stopped.wait, 5)
                # Waiting in the lifecycle's threadpool must not block this loop.
                def wait_for_drain():
                    with resources._condition:
                        return resources._condition.wait_for(
                            lambda: resources._owner is not None and resources._owner.state == 'closing', 5)
                assert await asyncio.to_thread(wait_for_drain)
                assert not closing.done() and clients[-1].close_calls == 0
                with pytest.raises(resources.ResourceUnavailable):
                    with resources.borrow_client():
                        pass
            finally:
                release.set()
                await asyncio.wait_for(closing, 5)
                await asyncio.to_thread(workers[-1].join, 5)
            assert not workers[-1].is_alive() and not worker_errors
            assert resources._owner is None

    asyncio.run(exercise())
    assert events == ['ready', 'start', 'borrow', 'stop', 'drain', 'release', 'close', 'closed'] * 2
    assert len(clients) == 2 and clients[0] is not clients[1]
    assert all(client.close_calls == 1 for client in clients)


def run_import_script(script):
    with tempfile.TemporaryDirectory(prefix='travel-bootstrap-') as directory:
        environment = {**os.environ,
            'OPENAI_API_KEY': 'offline-subprocess-key',
            'OPENAI_BASE_URL': 'https://model.offline.invalid/v1',
            'OPENAI_MODEL': 'offline-model', 'OPENAI_FALLBACK_MODEL': '',
            'OPENAI_TIMEOUT_SECONDS': '12', 'OPENAI_WIRE_API': 'chat_completions',
            'OPENAI_STORE_RESPONSES': 'false',
            'CITY_DB_PATH': str(Path(directory) / 'bootstrap.sqlite3'),
            'TRAVEL_PERSISTENCE': 'false', 'TRAVEL_DISPLAY_ENABLED': 'false',
            'TRAVEL_DISPLAY_TIMEOUT_SECONDS': '12', 'TRAVEL_COOKIE_SECURE': 'false',
            'TRAVEL_AGENT_DISABLE_LLM': '1',
            'AMAP_MAPS_API_KEY': 'offline-map-key', 'AMAP_MAP_KEY': '',
            'AMAP_MCP_URL': 'https://maps.offline.invalid/mcp',
            'AMAP_MCP_TIMEOUT_SECONDS': '5', 'MODEL_ID': '',
        }
        environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[2] / 'backend')
        # Stub before importing config/main: never read the developer's .env.
        prefix = 'import dotenv\ndotenv.load_dotenv = lambda *args, **kwargs: False\n'
        result = subprocess.run([sys.executable, '-c', prefix + script],
            cwd=Path(__file__).resolve().parents[2], env=environment,
            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('module', [
    'app.agents.tools', 'app.services.maps.amap_mcp', 'app.services.maps.places',
    'app.services.maps', 'app.services.maps.amap_mcp', 'app.services.maps.places',
])
def test_tool_imports_have_no_bootstrap_or_cross_layer_side_effects(module):
    run_import_script("""
import importlib
import logging
import sys
import dotenv
import openai

def forbidden(*args, **kwargs):
    raise AssertionError('tool import performed bootstrap')

dotenv.load_dotenv = forbidden
openai.OpenAI = forbidden
logging.basicConfig = forbidden
module = MODULE_NAME
importlib.import_module(module)
assert 'app.main' not in sys.modules
assert 'app.storage.store' not in sys.modules
assert 'app.api.dependencies' not in sys.modules
assert 'app.agents.entrypoints' not in sys.modules
assert 'app.agents.travel_runtime' not in sys.modules
assert 'app.agents.budget' not in sys.modules
assert 'app.agents.tools.registry' not in sys.modules
if module.startswith('app.api'):
    assert not any(name == 'app.agents' or name.startswith('app.agents.') for name in sys.modules)
else:
    assert not any(name == 'app.api' or name.startswith('app.api.') for name in sys.modules)
from app.core import resources
assert resources._owner is None
""".replace('MODULE_NAME', repr(module)))


def test_agent_and_amap_imports_have_no_bootstrap_side_effects():
    run_import_script("""
import sys
import logging
import dotenv
import openai

def forbidden(*args, **kwargs):
    raise AssertionError('import performed bootstrap')

dotenv.load_dotenv = forbidden
openai.OpenAI = forbidden
logging.basicConfig = forbidden
from app.agents import entrypoints
from app.services.maps import amap_mcp
from app.core import resources
assert 'app.main' not in sys.modules
assert 'app.storage.store' not in sys.modules
assert resources._owner is None
""")


def test_main_invalid_configuration_fails_before_store_import():
    run_import_script("""
import builtins
import os
import sys
from app.core import config
os.environ.pop('OPENAI_MODEL')
os.environ['OPENAI_API_KEY'] = 'private-key-must-not-leak'
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == 'app.auth' or name.startswith(('app.storage', 'app.api')):
        raise AssertionError('business resources imported before validation')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
try:
    from app import main
except config.ConfigurationError as exc:
    assert 'OPENAI_MODEL' in str(exc)
    assert 'private-key-must-not-leak' not in str(exc)
else:
    raise AssertionError('missing configuration accepted')
assert 'app.storage.store' not in sys.modules
from app.core import resources
assert resources._owner is None
""")


def test_main_loads_and_validates_environment_before_store_import():
    run_import_script("""
import builtins
import os
import sys
from types import ModuleType
from app.core import config

# Use the same persistence/model safety guard as the parent pytest process.
sys.modules['pytest'] = ModuleType('pytest')
events = []
def load_environment():
    events.append('environment')
    assert 'bootstrap.sqlite3' in os.environ['CITY_DB_PATH']
config.load_environment = load_environment
real_validate = config.validate_configuration
def validate(scope):
    assert events == ['environment']
    assert scope == 'web'
    real_validate(scope)
    events.append('validated')
config.validate_configuration = validate
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == 'app.storage.store':
        assert events[:2] == ['environment', 'validated']
        events.append('store')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
from app import main
assert events[:2] == ['environment', 'validated'] and 'store' in events
from app.api import dependencies as deps
assert deps.store.db_path == os.environ['CITY_DB_PATH']
assert not deps.store.persistence_enabled
from app.core import resources
assert resources._owner is None
""")
