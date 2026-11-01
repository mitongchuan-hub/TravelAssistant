"""Strict configuration contracts; all values are synthetic and offline."""
import pytest

from app.core import config


pytestmark = pytest.mark.usefixtures('restore_model_configuration')


@pytest.mark.parametrize('marker', [None, 'synthetic-test (call)'])
def test_model_enablement_depends_on_configuration_not_pytest(monkeypatch, marker):
    if marker is None:
        monkeypatch.delenv('PYTEST_CURRENT_TEST', raising=False)
    else:
        monkeypatch.setenv('PYTEST_CURRENT_TEST', marker)
    assert config.llm_enabled() is True
    monkeypatch.setenv('TRAVEL_AGENT_DISABLE_LLM', 'true')
    assert config.llm_enabled() is False


def test_missing_key_disables_model_access(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY')
    assert config.llm_enabled() is False


MODEL_KEYS = ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_MODEL',
              'OPENAI_TIMEOUT_SECONDS', 'OPENAI_WIRE_API', 'OPENAI_STORE_RESPONSES')
SYSTEM_KEYS = ('CITY_DB_PATH', 'TRAVEL_PERSISTENCE', 'TRAVEL_DISPLAY_ENABLED',
               'AMAP_MAPS_API_KEY', 'AMAP_MCP_URL', 'AMAP_MCP_TIMEOUT_SECONDS')


@pytest.mark.parametrize('scope', ['model', 'system', 'web'])
def test_explicit_test_configuration_is_valid(scope):
    assert config.validate_configuration(scope) is None


@pytest.mark.parametrize('value', [None, '', '  ', 'your_api_key', 'YOUR_OPENAI_COMPATIBLE_KEY',
                                 'your_custom_placeholder', '你的模型服务密钥'])
def test_required_string_rejects_missing_blank_and_placeholders(monkeypatch, value):
    if value is None:
        monkeypatch.delenv('STRICT_TEST_VALUE', raising=False)
    else:
        monkeypatch.setenv('STRICT_TEST_VALUE', value)
    with pytest.raises(config.ConfigurationError, match='STRICT_TEST_VALUE'):
        config.required_string('STRICT_TEST_VALUE')


def test_required_string_trims_without_changing_case(monkeypatch):
    monkeypatch.setenv('STRICT_TEST_VALUE', '  Mixed-Case  ')
    assert config.required_string('STRICT_TEST_VALUE') == 'Mixed-Case'


@pytest.mark.parametrize('value,expected', [('1', True), ('TRUE', True), (' Yes ', True),
    ('on', True), ('0', False), ('false', False), (' NO ', False), ('off', False)])
def test_boolean_accepts_only_documented_values(monkeypatch, value, expected):
    monkeypatch.setenv('STRICT_TEST_VALUE', value)
    assert config.boolean('STRICT_TEST_VALUE') is expected


@pytest.mark.parametrize('value', ['', 'maybe', '2', '-1', 'truthy'])
def test_boolean_does_not_guess(monkeypatch, value):
    monkeypatch.setenv('STRICT_TEST_VALUE', value)
    with pytest.raises(config.ConfigurationError, match='STRICT_TEST_VALUE'):
        config.boolean('STRICT_TEST_VALUE')


@pytest.mark.parametrize('value', ['', 'invalid', '0', '-1', 'nan', 'inf', '-inf', '1e999'])
@pytest.mark.parametrize('name,getter', [('OPENAI_TIMEOUT_SECONDS', config.model_timeout),
    ('TRAVEL_DISPLAY_TIMEOUT_SECONDS', config.display_timeout),
    ('AMAP_MCP_TIMEOUT_SECONDS', config.amap_timeout)])
def test_timeout_rejects_nonfinite_nonpositive_and_invalid(monkeypatch, value, name, getter):
    monkeypatch.setenv(name, value)
    with pytest.raises(config.ConfigurationError, match=name):
        getter()


def test_timeout_bounds_and_fractional_values(monkeypatch):
    monkeypatch.setenv('OPENAI_TIMEOUT_SECONDS', ' 0.25 ')
    assert config.model_timeout() == 0.25
    monkeypatch.setenv('AMAP_MCP_TIMEOUT_SECONDS', '120')
    assert config.amap_timeout() == 120
    monkeypatch.setenv('AMAP_MCP_TIMEOUT_SECONDS', '120.001')
    with pytest.raises(config.ConfigurationError, match='AMAP_MCP_TIMEOUT_SECONDS'):
        config.amap_timeout()


@pytest.mark.parametrize('value', ['ftp://host.invalid', 'missing-scheme.invalid',
    'https://', 'https://user:private-password@host.invalid/v1',
    'https://host.invalid/#fragment', 'https://host.invalid:bad/v1',
    'https://host.invalid:99999/v1', 'https://host.invalid/a b', 'https://[invalid'])
@pytest.mark.parametrize('name,getter', [('OPENAI_BASE_URL', config.model_base_url),
                                        ('AMAP_MCP_URL', config.amap_url)])
def test_urls_are_validated_without_echoing_input(monkeypatch, value, name, getter):
    monkeypatch.setenv(name, value)
    with pytest.raises(config.ConfigurationError, match=name) as error:
        getter()
    assert value not in str(error.value)
    assert 'private-password' not in str(error.value)


def test_model_can_use_http_but_amap_requires_https(monkeypatch):
    monkeypatch.setenv('OPENAI_BASE_URL', 'http://localhost:8001/v1')
    assert config.model_base_url() == 'http://localhost:8001/v1'
    monkeypatch.setenv('AMAP_MCP_URL', 'http://maps.offline.invalid/mcp')
    with pytest.raises(config.ConfigurationError, match='AMAP_MCP_URL'):
        config.amap_url()


@pytest.mark.parametrize('name', MODEL_KEYS)
def test_model_required_settings_have_no_defaults(monkeypatch, name):
    monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('MODEL_ID', 'legacy-alias-is-not-a-model-default')
    with pytest.raises(config.ConfigurationError, match=name):
        config.validate_configuration('model')


@pytest.mark.parametrize('name', SYSTEM_KEYS + ('TRAVEL_COOKIE_SECURE',))
def test_web_required_settings_have_no_defaults(monkeypatch, name):
    monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv('AMAP_MAP_KEY', raising=False)
    with pytest.raises(config.ConfigurationError, match=name):
        config.validate_configuration('web')


def test_validation_aggregates_errors_without_leaking_secrets(monkeypatch):
    for name in MODEL_KEYS + SYSTEM_KEYS + ('TRAVEL_COOKIE_SECURE',):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv('AMAP_MAP_KEY', raising=False)
    secrets = {'OPENAI_BASE_URL': 'https://private-user:private-secret@offline.invalid',
               'OPENAI_WIRE_API': 'private-wire-secret',
               'AMAP_MCP_TIMEOUT_SECONDS': 'private-map-secret'}
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(config.ConfigurationError) as error:
        config.validate_configuration('web')
    message = str(error.value)
    for name in MODEL_KEYS + SYSTEM_KEYS + ('TRAVEL_COOKIE_SECURE',):
        assert name in message
    for value in (*secrets.values(), 'private-secret', 'private-map-secret'):
        assert value not in message
    assert message.count('\n- ') >= len(MODEL_KEYS + SYSTEM_KEYS)


@pytest.mark.parametrize('scope', ['model', 'system'])
def test_validation_scopes_do_not_require_unrelated_settings(monkeypatch, scope):
    monkeypatch.delenv('TRAVEL_COOKIE_SECURE', raising=False)
    if scope == 'model':
        for name in SYSTEM_KEYS + ('AMAP_MAP_KEY', 'TRAVEL_DISPLAY_TIMEOUT_SECONDS'):
            monkeypatch.delenv(name, raising=False)
    assert config.validate_configuration(scope) is None


@pytest.mark.parametrize('scope', ['', 'invalid', 'MODEL', None])
def test_unknown_validation_scope_is_rejected(scope):
    with pytest.raises(ValueError, match='scope'):
        config.validate_configuration(scope)


@pytest.mark.parametrize('guard', ['TRAVEL_AGENT_DISABLE_LLM', 'PYTEST_CURRENT_TEST'])
def test_test_or_offline_guards_do_not_bypass_validation(monkeypatch, guard):
    monkeypatch.setenv(guard, '1')
    monkeypatch.delenv('OPENAI_API_KEY')
    with pytest.raises(config.ConfigurationError, match='OPENAI_API_KEY'):
        config.validate_configuration('model')


@pytest.mark.parametrize('value', [None, '', ' '])
def test_fallback_model_is_explicitly_optional(monkeypatch, value):
    if value is None:
        monkeypatch.delenv('OPENAI_FALLBACK_MODEL', raising=False)
    else:
        monkeypatch.setenv('OPENAI_FALLBACK_MODEL', value)
    assert config.fallback_model_name() == ''
    assert config.validate_configuration('model') is None


def test_fallback_model_is_trimmed_and_placeholders_are_not_silenced(monkeypatch):
    monkeypatch.setenv('OPENAI_FALLBACK_MODEL', ' fallback-name ')
    assert config.fallback_model_name() == 'fallback-name'
    monkeypatch.setenv('OPENAI_FALLBACK_MODEL', 'your_model')
    with pytest.raises(config.ConfigurationError, match='OPENAI_FALLBACK_MODEL'):
        config.validate_configuration('model')


@pytest.mark.parametrize('primary', [None, '', ' '])
def test_map_key_legacy_alias_is_supported(monkeypatch, primary):
    if primary is None:
        monkeypatch.delenv('AMAP_MAPS_API_KEY', raising=False)
    else:
        monkeypatch.setenv('AMAP_MAPS_API_KEY', primary)
    monkeypatch.setenv('AMAP_MAP_KEY', ' legacy-offline-key ')
    assert config.amap_api_key() == 'legacy-offline-key'
    assert config.validate_configuration('system') is None


def test_map_key_getter_raises_instead_of_returning_empty_string(monkeypatch):
    monkeypatch.delenv('AMAP_MAPS_API_KEY', raising=False)
    monkeypatch.delenv('AMAP_MAP_KEY', raising=False)
    with pytest.raises(config.ConfigurationError, match='AMAP_MAPS_API_KEY / AMAP_MAP_KEY'):
        config.amap_api_key()


def test_validation_has_no_bootstrap_or_resource_side_effects(monkeypatch, tmp_path):
    import builtins
    import sqlite3
    from app.core import resources

    path = tmp_path / 'must-not-create' / 'database.sqlite3'
    monkeypatch.setenv('CITY_DB_PATH', str(path))
    monkeypatch.setenv('TRAVEL_PERSISTENCE', 'true')

    def forbidden(*args, **kwargs):
        pytest.fail('configuration validation performed I/O or constructed resources')

    monkeypatch.setattr(config, 'load_environment', forbidden)
    monkeypatch.setattr(config, 'load_dotenv', forbidden)
    monkeypatch.setattr(builtins, 'open', forbidden)
    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    monkeypatch.setattr(resources, 'client_scope', forbidden)
    assert config.validate_configuration('web') is None
    assert not path.parent.exists()
    assert resources._owner is None


def test_map_primary_key_takes_precedence_and_invalid_primary_is_not_hidden(monkeypatch):
    monkeypatch.setenv('AMAP_MAPS_API_KEY', ' primary-offline-key ')
    monkeypatch.setenv('AMAP_MAP_KEY', 'legacy-offline-key')
    assert config.amap_api_key() == 'primary-offline-key'
    monkeypatch.setenv('AMAP_MAPS_API_KEY', 'your_amap_web_service_key')
    with pytest.raises(config.ConfigurationError, match='AMAP_MAPS_API_KEY / AMAP_MAP_KEY'):
        config.amap_api_key()


@pytest.mark.parametrize('enabled', ['false', '0', 'off', 'no'])
def test_disabled_display_does_not_require_timeout(monkeypatch, enabled):
    monkeypatch.setenv('TRAVEL_DISPLAY_ENABLED', enabled)
    monkeypatch.delenv('TRAVEL_DISPLAY_TIMEOUT_SECONDS', raising=False)
    assert config.validate_configuration('system') is None


@pytest.mark.parametrize('enabled', ['true', '1', ' ON ', 'yes'])
def test_enabled_display_requires_valid_timeout(monkeypatch, enabled):
    monkeypatch.setenv('TRAVEL_DISPLAY_ENABLED', enabled)
    monkeypatch.delenv('TRAVEL_DISPLAY_TIMEOUT_SECONDS', raising=False)
    with pytest.raises(config.ConfigurationError, match='TRAVEL_DISPLAY_TIMEOUT_SECONDS'):
        config.validate_configuration('system')
    monkeypatch.setenv('TRAVEL_DISPLAY_TIMEOUT_SECONDS', '0')
    with pytest.raises(config.ConfigurationError, match='TRAVEL_DISPLAY_TIMEOUT_SECONDS'):
        config.validate_configuration('web')


@pytest.mark.parametrize('value', ['responses', ' CHAT_COMPLETIONS '])
def test_wire_api_accepts_explicit_supported_protocols(monkeypatch, value):
    monkeypatch.setenv('OPENAI_WIRE_API', value)
    assert config.wire_api() == value.strip().lower()


@pytest.mark.parametrize('value', ['auto', 'chat', 'invalid'])
def test_wire_api_rejects_guessing(monkeypatch, value):
    monkeypatch.setenv('OPENAI_WIRE_API', value)
    with pytest.raises(config.ConfigurationError, match='OPENAI_WIRE_API'):
        config.wire_api()


@pytest.mark.parametrize('value,expected', [('true', True), (' FALSE ', False)])
def test_response_storage_uses_explicit_boolean(monkeypatch, value, expected):
    monkeypatch.setenv('OPENAI_STORE_RESPONSES', value)
    assert config.store_responses() is expected


@pytest.mark.parametrize('value', ['1', '0', 'yes', 'off', 'auto'])
def test_response_storage_does_not_use_general_boolean_aliases(monkeypatch, value):
    monkeypatch.setenv('OPENAI_STORE_RESPONSES', value)
    with pytest.raises(config.ConfigurationError, match='OPENAI_STORE_RESPONSES'):
        config.store_responses()
