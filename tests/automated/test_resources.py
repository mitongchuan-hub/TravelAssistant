"""Offline contracts for explicit configuration and shared client ownership."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Event

import pytest

from app.core import config, resources


class FakeClient:
    def __init__(self, **settings):
        self.settings = settings
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch, restore_model_configuration):
    monkeypatch.delenv('MODEL_ID', raising=False)
    monkeypatch.delenv('TRAVEL_AGENT_DISABLE_LLM', raising=False)
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://offline.invalid/v1')
    monkeypatch.setenv('OPENAI_TIMEOUT_SECONDS', '12')


def test_imports_have_no_bootstrap_side_effects():
    script = """
import sys
import dotenv
import openai

def forbidden(*args, **kwargs):
    raise AssertionError('import performed bootstrap')

dotenv.load_dotenv = forbidden
openai.OpenAI = forbidden
from app.core import config, resources
assert 'app.main' not in sys.modules
assert 'app.storage.store' not in sys.modules
assert resources._owner is None
"""
    result = subprocess.run(
        [sys.executable, '-c', script],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'backend')},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_environment_load_is_explicit_once_and_thread_safe(monkeypatch, tmp_path):
    calls = []
    env_path = tmp_path / '.env'
    env_path.write_text('RESOURCE_EXISTING=from-file\nRESOURCE_NEW=from-file\n', encoding='utf-8')
    monkeypatch.setenv('RESOURCE_EXISTING', 'from-process')
    monkeypatch.delenv('RESOURCE_NEW', raising=False)
    from dotenv.main import load_dotenv as real_loader

    def loader(path, *, override):
        calls.append((path, override))
        return real_loader(env_path, override=override)

    monkeypatch.setattr(config, 'load_dotenv', loader)
    monkeypatch.setattr(config, '_environment_loaded', False)
    barrier = Barrier(8)

    def load():
        barrier.wait(timeout=5)
        config.load_environment()

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(load) for _ in range(8)]
            for future in futures:
                future.result(timeout=10)
        config.load_environment()
        assert config.PROJECT_ROOT == Path(__file__).resolve().parents[2]
        assert calls == [(config.PROJECT_ROOT / '.env', False)]
        assert os.environ['RESOURCE_EXISTING'] == 'from-process'
        assert os.environ['RESOURCE_NEW'] == 'from-file'
    finally:
        os.environ.pop('RESOURCE_NEW', None)


def test_failed_environment_load_can_retry(monkeypatch):
    attempts = []

    def loader(*args, **kwargs):
        attempts.append(None)
        if len(attempts) == 1:
            raise OSError('offline load failure')

    monkeypatch.setattr(config, 'load_dotenv', loader)
    monkeypatch.setattr(config, '_environment_loaded', False)
    with pytest.raises(OSError):
        config.load_environment()
    config.load_environment()
    assert len(attempts) == 2


def test_settings_are_dynamic_and_preserve_existing_values(monkeypatch):
    for name, getter in [('OPENAI_MODEL', config.model_name),
                         ('OPENAI_TIMEOUT_SECONDS', config.model_timeout),
                         ('CITY_DB_PATH', config.database_path),
                         ('OPENAI_BASE_URL', config.model_base_url)]:
        with monkeypatch.context() as missing:
            missing.delenv(name, raising=False)
            with pytest.raises(config.ConfigurationError, match=name):
                getter()
    monkeypatch.delenv('OPENAI_FALLBACK_MODEL', raising=False)
    assert config.fallback_model_name() == ''
    monkeypatch.delenv('OPENAI_MODEL', raising=False)
    monkeypatch.setenv('MODEL_ID', 'legacy-model')
    with pytest.raises(config.ConfigurationError, match='OPENAI_MODEL'):
        config.model_name()
    monkeypatch.setenv('OPENAI_MODEL', ' primary-model ')
    monkeypatch.setenv('OPENAI_FALLBACK_MODEL', ' fallback-model ')
    monkeypatch.setenv('OPENAI_TIMEOUT_SECONDS', '12.5')
    monkeypatch.setenv('CITY_DB_PATH', 'relative/custom.sqlite3')
    assert config.model_name() == 'primary-model'
    assert config.fallback_model_name() == 'fallback-model'
    assert config.model_timeout() == 12.5
    assert Path(config.database_path()) == config.PROJECT_ROOT / 'relative/custom.sqlite3'
    monkeypatch.setenv('OPENAI_MODEL', '')
    monkeypatch.setenv('OPENAI_FALLBACK_MODEL', ' ')
    with pytest.raises(config.ConfigurationError, match='OPENAI_MODEL'):
        config.model_name()
    assert config.fallback_model_name() == ''
    monkeypatch.setenv('OPENAI_TIMEOUT_SECONDS', 'invalid')
    with pytest.raises(ValueError):
        config.model_timeout()


def test_borrow_requires_explicit_owner():
    with pytest.raises(resources.ResourceUnavailable):
        with resources.borrow_client():
            pytest.fail('missing owner allowed a borrow')


def test_scope_shares_client_and_captures_settings_once(monkeypatch):
    clients = []

    def factory(**kwargs):
        clients.append(FakeClient(**kwargs))
        return clients[-1]

    monkeypatch.setenv('OPENAI_BASE_URL', 'https://offline.invalid/v1')
    monkeypatch.setenv('OPENAI_TIMEOUT_SECONDS', '7.5')
    with resources.client_scope(client_factory=factory) as value:
        assert value is None
        monkeypatch.setenv('OPENAI_API_KEY', 'changed-offline-key')
        monkeypatch.setenv('OPENAI_TIMEOUT_SECONDS', '99')
        with resources.borrow_client() as first, resources.borrow_client() as second:
            assert first is second is clients[0]
            assert first.settings == {
                'api_key': 'offline-test-key', 'base_url': 'https://offline.invalid/v1',
                'timeout': 7.5, 'max_retries': 0,
            }
        assert first.close_calls == 0
    assert first.close_calls == 1
    assert resources._owner is None
    with resources.client_scope(timeout_seconds=4.0, client_factory=factory):
        with resources.borrow_client() as next_client:
            assert next_client is not first
            assert next_client.settings['timeout'] == 4.0
            assert next_client.settings['api_key'] == 'changed-offline-key'
    assert next_client.close_calls == 1


def test_explicit_timeout_does_not_read_unused_default(monkeypatch):
    monkeypatch.setenv('OPENAI_TIMEOUT_SECONDS', 'invalid')
    with resources.client_scope(timeout_seconds=2, client_factory=FakeClient):
        with resources.borrow_client() as client:
            assert client.settings['timeout'] == 2


def test_default_factory_uses_shared_fake_client_when_enabled(monkeypatch):
    import openai

    clients = []

    def factory(**kwargs):
        clients.append(FakeClient(**kwargs))
        return clients[-1]

    monkeypatch.setattr(config, 'llm_enabled', lambda: True)
    monkeypatch.setattr(openai, 'OpenAI', factory)
    with resources.client_scope():
        with resources.borrow_client() as client:
            assert client is clients[0]
            assert client.settings['base_url'] == 'https://offline.invalid/v1'
            assert client.settings['max_retries'] == 0
    assert client.close_calls == 1


@pytest.mark.parametrize('explicit_factory', [False, True])
def test_disabled_model_switch_never_constructs_any_factory(monkeypatch, explicit_factory):
    import openai

    def forbidden(**kwargs):
        pytest.fail('disabled model switch allowed client construction')

    monkeypatch.setattr(config, 'llm_enabled', lambda: False)
    monkeypatch.setattr(openai, 'OpenAI', forbidden)
    kwargs = {'client_factory': forbidden} if explicit_factory else {}
    with resources.client_scope(**kwargs):
        with pytest.raises(resources.ResourceUnavailable):
            with resources.borrow_client():
                pass
    assert resources._owner is None


@pytest.mark.parametrize('disabled', ['1', ' TRUE ', 'yes', 'on'])
def test_disabled_scope_never_calls_even_explicit_factory(monkeypatch, disabled):
    monkeypatch.setenv('TRAVEL_AGENT_DISABLE_LLM', disabled)

    def forbidden(**kwargs):
        pytest.fail('disabled client was constructed')

    with resources.client_scope(client_factory=forbidden):
        with pytest.raises(resources.ResourceUnavailable):
            with resources.borrow_client():
                pass
        with pytest.raises(resources.ResourceUnavailable):
            with resources.client_scope(client_factory=forbidden):
                pass
    assert resources._owner is None


@pytest.mark.parametrize('factory', [None, FakeClient])
@pytest.mark.parametrize('name', ['OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_TIMEOUT_SECONDS'])
def test_missing_required_client_setting_fails_before_construction(monkeypatch, factory, name):
    monkeypatch.delenv(name, raising=False)
    with pytest.raises(config.ConfigurationError, match=name):
        with resources.client_scope(client_factory=factory):
            pytest.fail('invalid configuration entered client scope')
    assert resources._owner is None


def test_scope_closes_after_body_and_borrow_errors():
    client = FakeClient()
    with pytest.raises(ValueError, match='request failed'):
        with resources.client_scope(client_factory=lambda **kwargs: client):
            with resources.borrow_client():
                raise ValueError('request failed')
    assert client.close_calls == 1
    assert resources._owner is None


def test_startup_and_close_errors_clear_owner():
    def broken_factory(**kwargs):
        raise ValueError('startup failed')

    with pytest.raises(ValueError, match='startup failed'):
        with resources.client_scope(client_factory=broken_factory):
            pass
    assert resources._owner is None

    class BrokenClose(FakeClient):
        def close(self):
            super().close()
            raise ValueError('close failed')

    client = BrokenClose()
    with pytest.raises(ValueError, match='close failed'):
        with resources.client_scope(client_factory=lambda **kwargs: client):
            pass
    assert client.close_calls == 1
    assert resources._owner is None
    with resources.client_scope(client_factory=FakeClient):
        with resources.borrow_client():
            pass


def test_concurrent_borrowers_share_client_without_serializing_requests():
    barrier = Barrier(5)
    client = FakeClient()

    def request():
        with resources.borrow_client() as borrowed:
            barrier.wait(timeout=5)
            return borrowed

    with resources.client_scope(client_factory=lambda **kwargs: client):
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(request) for _ in range(5)]
            assert all(future.result(timeout=10) is client for future in futures)
    assert client.close_calls == 1


def test_startup_reservation_rejects_overlap_and_borrow():
    constructing = Event()
    continue_startup = Event()

    def factory(**kwargs):
        constructing.set()
        assert continue_startup.wait(timeout=5)
        return FakeClient(**kwargs)

    def scope():
        with resources.client_scope(client_factory=factory):
            with resources.borrow_client() as client:
                return client

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(scope)
        try:
            assert constructing.wait(timeout=5)
            with pytest.raises(resources.ResourceUnavailable):
                with resources.client_scope(client_factory=FakeClient):
                    pass
            with pytest.raises(resources.ResourceUnavailable):
                with resources.borrow_client():
                    pass
        finally:
            continue_startup.set()
        assert future.result(timeout=10).close_calls == 1


def test_shutdown_drains_borrows_rejects_admission_and_closes_outside_lock():
    closing = Event()
    allow_close = Event()

    class BlockingClose(FakeClient):
        def close(self):
            super().close()
            closing.set()
            assert allow_close.wait(timeout=5)

    client = BlockingClose()
    scope = resources.client_scope(client_factory=lambda **kwargs: client)
    scope.__enter__()
    lease = resources.borrow_client()
    assert lease.__enter__() is client
    with ThreadPoolExecutor(max_workers=1) as pool:
        shutdown = pool.submit(scope.__exit__, None, None, None)
        try:
            # Wait for shutdown's state transition, not a scheduling-dependent sleep.
            with resources._condition:
                assert resources._condition.wait_for(
                    lambda: resources._owner.state == 'closing', timeout=5,
                )
            assert not closing.is_set()
            assert not shutdown.done()
            with pytest.raises(resources.ResourceUnavailable):
                with resources.borrow_client():
                    pass
            lease.__exit__(None, None, None)
            assert closing.wait(timeout=5)
            # These operations must complete while close itself is blocked.
            with pytest.raises(resources.ResourceUnavailable):
                with resources.borrow_client():
                    pass
            with pytest.raises(resources.ResourceUnavailable):
                with resources.client_scope(client_factory=FakeClient):
                    pass
        finally:
            if resources._owner is not None and resources._owner.borrowers:
                lease.__exit__(None, None, None)
            allow_close.set()
        shutdown.result(timeout=10)
    assert client.close_calls == 1
    assert resources._owner is None
