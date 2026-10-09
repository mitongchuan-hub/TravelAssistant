"""Tool latency is logged for internal reads, external calls and exceptions."""
import pytest
from app import agent_client
from app.agent_runtime import TravelAgentRuntime
from app.store import DemoStore


@pytest.mark.parametrize('name,raises', [('read_context', False), ('query_weather', False), ('query_weather', True)])
def test_each_tool_records_elapsed_seconds_and_failure(monkeypatch, name, raises):
    local = DemoStore(persistence_enabled=False)
    runtime = TravelAgentRuntime(object(), 'test', {'trip': local.trips['trip-hangzhou']})
    events = []
    runtime.event = lambda title, value: events.append((title, value))
    clock = [10.0]
    monkeypatch.setattr('app.agent_runtime.monotonic', lambda: clock[0])
    runtime.budget.deadline = 100
    if name == 'read_context':
        original = runtime.read_context
        def read(args):
            clock[0] += 0.125
            return original(args)
        monkeypatch.setattr(runtime, 'read_context', read)
        args = {'section': 'requirements'}
    else:
        def query(*a, **kw):
            clock[0] += 1.25
            if raises:
                raise TimeoutError('upstream failed')
            return {'status': 'unavailable', 'reason': 'not available'}
        monkeypatch.setattr(agent_client, 'run_tool', query)
        args = {'city': '杭州'}
    if raises:
        with pytest.raises(TimeoutError):
            runtime.execute_tool('research', name, args)
    else:
        runtime.execute_tool('research', name, args)
    records = [value for title, value in events if title == '工具执行']
    assert len(records) == 1
    record = records[0]
    assert record['name'] == name and record['role'] == 'research'
    assert record['seconds'] == (0.125 if name == 'read_context' else 1.25)
    assert record['error_type'] == ('TimeoutError' if raises else None)
    assert (record['result'] is None) == raises
