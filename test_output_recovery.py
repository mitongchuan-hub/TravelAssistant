import json
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from app import agent_client
from app.store import DemoStore


# Regression for the provider output seen on 2026-10-01: prefixed keys and number arrays.
MALFORMED = json.dumps({
    'body': '已完成', 'card': {':title': '新版'},
    'plan': {':title': '新版', ':preparation': {':days': [7, 0, 1, 2]}},
})


def completion(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def valid_content():
    local = DemoStore(persistence_enabled=False)
    return json.dumps({'body': '已生成', 'plan': asdict(local._seed_plan())})


def test_recovery_reuses_tool_results_and_records_both_outputs(monkeypatch):
    tool_calls = []
    tool_messages = [
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'weather', 'type': 'function',
          'function': {'name': 'query_weather', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'weather', 'content': '{"status":"unavailable"}'},
    ]
    def collect(*args, **kwargs):
        tool_calls.append(True)
        return tool_messages
    monkeypatch.setattr(agent_client, '_collect_tool_messages', collect)
    requests = []
    outputs = [MALFORMED, valid_content()]
    def create(client, model, messages, **kwargs):
        requests.append(deepcopy(messages))
        assert kwargs['response_format'] == {'type': 'json_object'}
        return completion(outputs.pop(0))
    monkeypatch.setattr(agent_client, '_create_chat_completion', create)
    trace = agent_client.ConversationTrace('test', 'test', 'test', '重新规划')
    result = agent_client._request_model(object(), 'test', '旅行上下文', True, trace=trace)
    assert agent_client._parse_reply(result.choices[0].message.content, strict_plan=True).plan
    assert len(tool_calls) == 1
    assert len(requests) == 2
    assert requests[1][:len(requests[0])] == requests[0]
    assert requests[1][-2]['content'] == MALFORMED
    assert '未通过结构校验' in requests[1][-1]['content']
    assert any('attempt' in event and ':title' in event for event in trace.events)
    assert 'plan 必须为 null' not in requests[0][0]['content']
    assert 'card.next_action' not in requests[0][0]['content']
    assert 'card.next_action' in agent_client.SYSTEM_PROMPT


def test_valid_plan_does_not_retry(monkeypatch):
    monkeypatch.setattr(agent_client, '_collect_tool_messages', lambda *a, **kw: [])
    calls = []
    def create(*args, **kwargs):
        calls.append(True)
        return completion(valid_content())
    monkeypatch.setattr(agent_client, '_create_chat_completion', create)
    agent_client._request_model(object(), 'test', 'context', True)
    assert len(calls) == 1


def test_repeated_bad_output_is_reported_without_overwriting_plan(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    original = deepcopy(local.plans['trip-hangzhou'])
    versions = deepcopy(local.plan_versions['trip-hangzhou'])
    monkeypatch.setattr(agent_client, 'is_llm_enabled', lambda: True)
    monkeypatch.setattr('openai.OpenAI', lambda **kw: object())
    monkeypatch.setattr(agent_client.ConversationTrace, 'write', lambda *a, **kw: None)
    monkeypatch.setattr(agent_client, '_collect_tool_messages', lambda *a, **kw: [])
    calls = []
    def create(*args, **kwargs):
        calls.append(True)
        return completion(MALFORMED)
    monkeypatch.setattr(agent_client, '_create_chat_completion', create)
    local.generate_plan('trip-hangzhou')
    assert len(calls) == 2
    assert local.plans['trip-hangzhou'] == original
    assert local.plan_versions['trip-hangzhou'] == versions
    message = local.messages['trip-hangzhou'][-1]
    assert message.agent_card.title == '行程格式校验失败'
    assert '检查 API Key' not in message.agent_card.summary


def test_network_failure_does_not_trigger_format_recovery(monkeypatch):
    monkeypatch.setattr(agent_client, '_collect_tool_messages', lambda *a, **kw: [])
    calls = []
    def create(*args, **kwargs):
        calls.append(True)
        raise TimeoutError('upstream timeout')
    monkeypatch.setattr(agent_client, '_create_chat_completion', create)
    with pytest.raises(TimeoutError):
        agent_client._request_model(object(), 'test', 'context', True)
    assert len(calls) == 1
