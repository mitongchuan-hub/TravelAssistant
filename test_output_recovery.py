import json
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from app import agent_client
from app.agent_roles import PLAN_FORMAT
from app.store import DemoStore

MALFORMED = json.dumps({'body': '已完成', 'card': {':title': '新版'},
                        'plan': {':title': '新版', ':preparation': {':days': [7, 0, 1, 2]}}})


def completion(content, calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls or []))])


def valid_content():
    local = DemoStore(persistence_enabled=False)
    return json.dumps({'body': '已生成', 'plan': asdict(local._seed_plan())})


def test_recovery_reuses_tool_results_and_records_both_outputs(monkeypatch):
    calls = [SimpleNamespace(id='weather', function=SimpleNamespace(name='query_weather', arguments='{"city":"杭州"}'))]
    responses = [completion('', calls), completion(MALFORMED), completion(valid_content())]
    requests, queries = [], []
    def create(client, model, messages, **kwargs):
        requests.append(deepcopy(messages))
        if len(requests) == 3:
            assert kwargs['response_format'] == PLAN_FORMAT
        else:
            assert 'response_format' not in kwargs
        return responses.pop(0)
    monkeypatch.setattr(agent_client, '_create_chat_completion', create)
    monkeypatch.setattr(agent_client, 'run_tool', lambda *a, **kw: queries.append(a) or {'status': 'unavailable'})
    trace = agent_client.ConversationTrace('test', 'test', 'test', '重新规划')
    cache = {}
    result = agent_client._request_model(object(), 'test', '旅行上下文', True, trace=trace, route_cache=cache)
    assert agent_client._parse_reply(result.choices[0].message.content, strict_plan=True).plan
    assert len(queries) == 1 and len(requests) == 3
    assert requests[2][:len(requests[1])] == requests[1]
    assert requests[2][-2]['content'] == MALFORMED
    assert '未通过结构或行程安排校验' in requests[2][-1]['content']
    assert any('attempt' in event and ':title' in event for event in trace.events)
    assert any(m.get('tool_call_id') == 'weather' for m in requests[2])
    assert cache['final_reply'].plan


def test_valid_plan_does_not_retry(monkeypatch):
    calls = []
    monkeypatch.setattr(agent_client, '_create_chat_completion', lambda *a, **kw: calls.append(True) or completion(valid_content()))
    agent_client._request_model(object(), 'test', 'context', True)
    assert len(calls) == 1


def test_repeated_bad_output_is_reported_without_overwriting_plan(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    original = deepcopy(local.plans['trip-hangzhou'])
    versions = deepcopy(local.plan_versions['trip-hangzhou'])
    monkeypatch.setattr(agent_client, 'is_llm_enabled', lambda: True)
    monkeypatch.setattr('openai.OpenAI', lambda **kw: object())
    monkeypatch.setattr(agent_client.ConversationTrace, 'write', lambda *a, **kw: None)
    calls = []
    monkeypatch.setattr(agent_client, '_create_chat_completion', lambda *a, **kw: calls.append(True) or completion(MALFORMED))
    local.generate_plan('trip-hangzhou')
    assert len(calls) == 2
    assert local.plans['trip-hangzhou'] == original
    assert local.plan_versions['trip-hangzhou'] == versions
    assert local.messages['trip-hangzhou'][-1].agent_card.title == '行程格式校验失败'


def test_network_failure_does_not_trigger_format_recovery(monkeypatch):
    calls = []
    def create(*args, **kwargs):
        calls.append(True)
        raise TimeoutError('upstream timeout')
    monkeypatch.setattr(agent_client, '_create_chat_completion', create)
    with pytest.raises(TimeoutError):
        agent_client._request_model(object(), 'test', 'context', True)
    assert len(calls) == 1
