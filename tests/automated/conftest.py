"""Hermetic configuration installed before test-module collection imports Store.

These are explicit test settings, not application defaults. Reset only before
each test: deleting a required setting inside a test must remain observable.
"""
import os
from pathlib import Path
import tempfile

import dotenv
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
}
os.environ.update(TEST_ENVIRONMENT)
# Never inherit real aliases or a real-process model-disable setting. Pytest's
# model guards remain active; individual tests install fakes explicitly.
for _name in ('MODEL_ID', 'AMAP_MAP_KEY', 'TRAVEL_AGENT_DISABLE_LLM'):
    os.environ.pop(_name, None)

_original_load_dotenv = dotenv.load_dotenv


def _no_project_dotenv(*args, **kwargs):
    """Collection/bootstrap may not read a developer's real .env file."""
    return False


dotenv.load_dotenv = _no_project_dotenv


@pytest.fixture(autouse=True)
def hermetic_configuration(monkeypatch):
    # This runs before local fixtures, not at getter/validation time. Deliberate
    # deletions in tests therefore stay deleted until monkeypatch teardown.
    for name, value in TEST_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    for name in ('MODEL_ID', 'AMAP_MAP_KEY', 'TRAVEL_AGENT_DISABLE_LLM'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(dotenv, 'load_dotenv', _no_project_dotenv)


def pytest_unconfigure(config):
    dotenv.load_dotenv = _original_load_dotenv
    _test_directory.cleanup()
