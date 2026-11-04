"""Latency benchmark plans are current and offline unless explicitly opted in."""
import pytest

from tests.benchmarks import agent_latency as benchmark


def test_current_agents_and_default_offline(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError('Offline benchmark must not create a client or send requests')

    monkeypatch.setattr(benchmark.config, 'load_environment', forbidden)
    monkeypatch.setattr(benchmark.resources, 'client_scope', forbidden)
    monkeypatch.setattr(benchmark.resources, 'borrow_client', forbidden)
    monkeypatch.setattr(benchmark, 'request_completion', forbidden)
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    agents = benchmark.build_agents()
    assert [agent['name'] for agent in agents] == ['travel_agent', 'display']
    travel, display = agents
    assert len(travel['tools']) == 10
    assert {tool['function']['name'] for tool in travel['tools']} == {
        'read_context', 'read_skill', 'save_trip_changes', 'query_weather', 'maps_around_search', 'maps_text_search', 'maps_search_detail',
        'maps_direction_walking', 'maps_direction_driving', 'maps_direction_transit_integrated',
    }
    assert travel['user']['message']['text'] == benchmark.USER_MSG
    assert 'known' in travel['user']['requirements']
    assert 'travel_window' not in travel['user']
    assert 'dates' in travel['user']['requirements']['known']
    assert 'generation_question' not in travel['user']
    assert display['tools'] is None
    assert display['response_format'] is None
    assert display['user']['output_format'] == 'html'
    assert display['user']['source_dates'] == ['2026-11-01', '2026-11-02']
    assert 'output_schema' not in display['user']

    monkeypatch.setattr(benchmark.sys, 'argv', ['bench_agent_latency.py'])
    assert benchmark.main() == 0
    output = capsys.readouterr().out
    assert 'Offline plan only' in output
    assert '--live required' in output
    for invalid in ('0', '-1'):
        monkeypatch.setattr(benchmark.sys, 'argv', ['bench_agent_latency.py', '--runs', invalid])
        with pytest.raises(SystemExit) as error:
            benchmark.main()
        assert error.value.code == 2
