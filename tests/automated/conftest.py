"""Hermetic configuration installed before test-module collection imports Store.

These are explicit test settings, not application defaults. Reset only before
each test: deleting a required setting inside a test must remain observable.
"""
import os
from pathlib import Path
import socket
import tempfile
from threading import local

import dotenv
import httpx
import openai
import pytest


_test_directory = tempfile.TemporaryDirectory(prefix='travelassistant-automated-')
TEST_ENVIRONMENT = {
    'OPENAI_API_KEY': 'offline-test-key',
    'OPENAI_BASE_URL': 'https://model.offline.invalid/v1',
    'OPENAI_MODEL': 'offline-test-model',
    'OPENAI_FALLBACK_MODEL': '',
    'OPENAI_TIMEOUT_SECONDS': '12',
    'OPENAI_WIRE_API': 'chat_completions',
    'OPENAI_STORE_RESPONSES': 'false',
    'CITY_DB_PATH': str(Path(_test_directory.name) / 'test.sqlite3'),
    'TRAVEL_PERSISTENCE': 'false',
    'TRAVEL_DISPLAY_ENABLED': 'false',
    'TRAVEL_DISPLAY_TIMEOUT_SECONDS': '12',
    'TRAVEL_COOKIE_SECURE': 'false',
    'AMAP_MAPS_API_KEY': 'offline-map-key',
    'AMAP_MCP_URL': 'https://maps.offline.invalid/mcp',
    'AMAP_MCP_TIMEOUT_SECONDS': '5',
    'AMAP_REST_TIMEOUT_SECONDS': '5',
}
os.environ.update(TEST_ENVIRONMENT)
# Never inherit real aliases or a real-process model-disable setting. Test-side
# model guards remain active; individual tests install fakes explicitly.
for _name in ('MODEL_ID', 'AMAP_MAP_KEY', 'TRAVEL_AGENT_DISABLE_LLM'):
    os.environ.pop(_name, None)

_original_load_dotenv = dotenv.load_dotenv


def _no_project_dotenv(*args, **kwargs):
    """Collection/bootstrap may not read a developer's real .env file."""
    return False


dotenv.load_dotenv = _no_project_dotenv

# Install before collection imports app.main or any application lifespan. Keep
# deployment semantics in config; tests, not production, own offline protection.
from app.core import config as app_config

_original_llm_enabled = app_config.llm_enabled
_original_socket_connect = socket.socket.connect
_original_socket_connect_ex = socket.socket.connect_ex
_original_socketpair = socket.socketpair
_socketpair_state = local()


def _model_disabled():
    return False


def _forbidden_model_constructor(*args, **kwargs):
    raise AssertionError('Offline tests forbid the real OpenAI constructor; install a fake factory')


def _forbidden_network(*args, **kwargs):
    raise AssertionError('Offline tests forbid real network access; use a fake transport')


async def _forbidden_async_network(*args, **kwargs):
    _forbidden_network()


def _guarded_connect(sock, address):
    # Windows implements socketpair with an internal loopback connection. It is
    # asyncio's local wakeup channel, not an application network request.
    if getattr(_socketpair_state, 'active', False):
        return _original_socket_connect(sock, address)
    _forbidden_network()


def _guarded_connect_ex(sock, address):
    if getattr(_socketpair_state, 'active', False):
        return _original_socket_connect_ex(sock, address)
    _forbidden_network()


def _local_socketpair(*args, **kwargs):
    previous = getattr(_socketpair_state, 'active', False)
    _socketpair_state.active = True
    try:
        return _original_socketpair(*args, **kwargs)
    finally:
        _socketpair_state.active = previous


def _install_offline_guards(patch):
    patch.setattr(app_config, 'llm_enabled', _model_disabled)
    patch.setattr(openai, 'OpenAI', _forbidden_model_constructor)
    patch.setattr(socket.socket, 'connect', _guarded_connect)
    patch.setattr(socket.socket, 'connect_ex', _guarded_connect_ex)
    patch.setattr(socket, 'create_connection', _forbidden_network)
    patch.setattr(socket, 'socketpair', _local_socketpair)
    # Block only real HTTP transports: MockTransport and ASGI/TestClient remain
    # usable, including async transports on a genuine event loop.
    patch.setattr(httpx.HTTPTransport, 'handle_request', _forbidden_network)
    patch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', _forbidden_async_network)


_collection_guards = pytest.MonkeyPatch()
_install_offline_guards(_collection_guards)


@pytest.fixture
def restore_model_configuration(hermetic_configuration, monkeypatch):
    """Restore real config semantics, without permitting real clients/network."""
    monkeypatch.setattr(app_config, 'llm_enabled', _original_llm_enabled)
    return _original_llm_enabled


@pytest.fixture(autouse=True)
def hermetic_configuration(monkeypatch):
    # This runs before local fixtures, not at getter/validation time. Deliberate
    # deletions in tests therefore stay deleted until monkeypatch teardown.
    for name, value in TEST_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    for name in ('MODEL_ID', 'AMAP_MAP_KEY', 'TRAVEL_AGENT_DISABLE_LLM'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(dotenv, 'load_dotenv', _no_project_dotenv)
    _install_offline_guards(monkeypatch)


def pytest_unconfigure(config):
    _collection_guards.undo()
    dotenv.load_dotenv = _original_load_dotenv
    _test_directory.cleanup()
