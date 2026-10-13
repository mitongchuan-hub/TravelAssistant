"""Reasoning configuration and provider fallbacks through the real SDK wire format."""
import json

import httpx
import openai
import pytest

from app import agent_client
from app.agent_client import _create_chat_completion, ConversationTrace
from app.agent_roles import roles, role_reasoning_effort
from app.agent_runtime import TurnBudget, AgentBudgetError


@pytest.fixture(autouse=True)
def reasoning_env(monkeypatch):
    monkeypatch.setenv('OPENAI_WIRE_API', 'chat_completions')
    for key in ['OPENAI_REASONING_EFFORT', *['OPENAI_REASONING_EFFORT_' + role.upper() for role in roles()]]:
        monkeypatch.delenv(key, raising=False)


def test_role_defaults_and_explicit_override_precedence(monkeypatch):
    configs = roles()
    assert {name: role_reasoning_effort(role) for name, role in configs.items()} == {
        'main': 'low', 'chat': 'low', 'display': 'low', 'research': 'low', 'planning': 'medium', 'revision': 'medium'}
    assert role_reasoning_effort(configs['revision'], 'explain') == 'low'
    monkeypatch.setenv('OPENAI_REASONING_EFFORT', 'high')
    assert role_reasoning_effort(configs['main']) == 'high'
    monkeypatch.setenv('OPENAI_REASONING_EFFORT_MAIN', ' default ')
    assert role_reasoning_effort(configs['main']) is None
    monkeypatch.setenv('OPENAI_REASONING_EFFORT_REVISION', 'low')
    assert role_reasoning_effort(configs['revision'], 'item') == 'low'
    monkeypatch.setenv('OPENAI_REASONING_EFFORT_REVISION', ' ')
    assert role_reasoning_effort(configs['revision'], 'explain') == 'high'


def test_bad_config_is_not_silently_changed(monkeypatch):
    monkeypatch.setenv('OPENAI_REASONING_EFFORT_PLANNING', 'turbo')
    with pytest.raises(ValueError, match='planning'):
        role_reasoning_effort(roles()['planning'])


def success():
    return httpx.Response(200, json={'id': 'test', 'model': 'kimi-k3',
        'choices': [{'message': {'role': 'assistant', 'content': '好的', 'reasoning_content': 'private-test-reasoning'}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 12, 'total_tokens': 22,
                  'completion_tokens_details': {'reasoning_tokens': 8}}})


def sdk(handler, url='https://provider.invalid/v1'):
    return openai.OpenAI(api_key='test-only', base_url=url, max_retries=0,
                         http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def rejection(param='reasoning_effort', message='Unsupported value for reasoning_effort: medium', code='unsupported_value'):
    return httpx.Response(400, json={'error': {'param': param, 'message': message, 'code': code}})


def test_sdk_sends_effort_with_tools_and_format_without_logging_reasoning_body():
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return success()
    trace = ConversationTrace('effort', 'now', 'synthetic', '你好')
    tools = [{'type': 'function', 'function': {'name': 'read_context', 'parameters': {'type': 'object', 'properties': {}}}}]
    with sdk(handler) as client:
        _create_chat_completion(client, 'kimi-k3', [{'role': 'user', 'content': '你好'}], tools=tools,
                                response_format={'type': 'json_object'}, reasoning_effort='medium',
                                agent_role='planning', trace=trace, budget=TurnBudget())
    assert requests[0]['reasoning_effort'] == 'medium'
    assert requests[0]['tools'] == tools and requests[0]['response_format'] == {'type': 'json_object'}
    assert 'enable_thinking' not in requests[0] and 'thinking_budget' not in requests[0]
    log = '\n'.join(trace.events)
    assert 'private-test-reasoning' not in log
    assert '"reasoning_tokens": 8' in log and '"seconds"' in log
    assert '"sent": "medium"' in log and '"agent_role": "planning"' in log


def test_default_omits_effort_on_wire():
    requests = []
    with sdk(lambda request: requests.append(json.loads(request.content)) or success()) as client:
        _create_chat_completion(client, 'kimi-k3', [], reasoning_effort=None)
    assert 'reasoning_effort' not in requests[0]


@pytest.mark.parametrize('message,code', [
    ('Parameter reasoning_effort is not supported.', 'unsupported_parameter'),
    ("'reasoning_effort' must be one of: 'low', 'high'", 'invalid_parameter_error'),
    ('Unsupported value: medium', 'unsupported_value'),
    ('Unknown parameter reasoning_effort', 'unknown_parameter'),
])
def test_rejection_falls_back_once_preserving_context_and_counts_budget(message, code):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return rejection(message=message, code=code) if len(requests) == 1 else success()
    budget = TurnBudget()
    trace = ConversationTrace('effort', 'now', 'synthetic', 'test')
    with sdk(handler) as client:
        _create_chat_completion(client, 'kimi-k3', [{'role': 'user', 'content': 'question'}],
                                response_format={'type': 'json_object'}, reasoning_effort='medium', trace=trace, budget=budget)
    assert budget.model_calls == 2 and len(requests) == 2
    assert requests[0].pop('reasoning_effort') == 'medium'
    assert requests[0] == requests[1]
    assert 'provider_default_after_rejection' in '\n'.join(trace.events)


def test_rejection_cache_is_scoped_to_provider_model_value_and_turn():
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if payload.get('reasoning_effort') == 'medium' and payload['model'] == 'unsupported':
            return rejection()
        return success()
    budget = TurnBudget()
    with sdk(handler) as client:
        _create_chat_completion(client, 'unsupported', [], reasoning_effort='medium', budget=budget)
        _create_chat_completion(client, 'unsupported', [], reasoning_effort='medium', budget=budget)
        assert len(requests) == 3 and 'reasoning_effort' not in requests[-1]
        _create_chat_completion(client, 'unsupported', [], reasoning_effort='low', budget=budget)
        assert requests[-1]['reasoning_effort'] == 'low'
        _create_chat_completion(client, 'fallback-model', [], reasoning_effort='medium', budget=budget)
        assert requests[-1]['reasoning_effort'] == 'medium'
        _create_chat_completion(client, 'unsupported', [], reasoning_effort='medium', budget=TurnBudget())
        assert requests[-2]['reasoning_effort'] == 'medium' and 'reasoning_effort' not in requests[-1]
    with sdk(handler, 'https://another.invalid/v1') as client:
        _create_chat_completion(client, 'unsupported', [], reasoning_effort='medium', budget=budget)
        assert requests[-2]['reasoning_effort'] == 'medium'


@pytest.mark.parametrize('status,param,message,error', [
    (400, 'messages', 'Invalid messages when using reasoning_effort', openai.BadRequestError),
    (401, 'reasoning_effort', 'unsupported reasoning_effort', openai.AuthenticationError),
    (429, 'reasoning_effort', 'unsupported reasoning_effort', openai.RateLimitError),
    (500, 'reasoning_effort', 'unsupported reasoning_effort', openai.InternalServerError),
])
def test_unrelated_auth_rate_limit_server_errors_do_not_retry(status, param, message, error):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={'error': {'param': param, 'message': message, 'code': 'invalid_parameter_error'}})
    with sdk(handler) as client, pytest.raises(error):
        _create_chat_completion(client, 'test', [], reasoning_effort='low')
    assert len(requests) == 1


def test_timeout_does_not_retry_or_strip_effort():
    requests = []
    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout('timeout', request=request)
    with sdk(handler) as client, pytest.raises(openai.APITimeoutError):
        _create_chat_completion(client, 'test', [], reasoning_effort='low')
    assert len(requests) == 1


def test_schema_fallback_keeps_effort_until_explicit_effort_rejection():
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if payload.get('response_format', {}).get('type') == 'json_schema':
            return rejection('response_format', 'response_format json_schema is unsupported', 'unsupported_parameter')
        if 'reasoning_effort' in payload:
            return rejection()
        return success()
    trace = ConversationTrace('effort', 'now', 'synthetic', 'test')
    budget = TurnBudget()
    with sdk(handler) as client:
        _create_chat_completion(client, 'test', [], response_format={'type': 'json_schema'}, reasoning_effort='medium', trace=trace, budget=budget)
    assert len(requests) == budget.model_calls == 3
    assert requests[1]['reasoning_effort'] == 'medium'
    assert requests[1]['response_format'] == requests[2]['response_format'] == {'type': 'json_object'}
    assert 'reasoning_effort' not in requests[2]
    assert sum('==================== 模型请求 ' in event for event in trace.events) == 3


def test_fallback_does_not_exceed_shared_model_budget():
    requests = []
    with sdk(lambda request: requests.append(request) or rejection()) as client:
        with pytest.raises(AgentBudgetError):
            _create_chat_completion(client, 'test', [], reasoning_effort='medium', budget=TurnBudget(max_models=1))
    assert len(requests) == 1


def test_workflow_passes_intent_agent_and_explanation_effort(monkeypatch):
    from test_agent_roles import completion, delegate, scripted, local_trip
    from app.agent_runtime import TravelAgentRuntime
    text = '生成一个行程'
    requests = scripted(monkeypatch, [delegate('planning', text), completion('需要补充：请确认其他要求。')])
    local, trip = local_trip()
    local.add_user_message(trip.id, text)
    assert [kw['reasoning_effort'] for _, kw in requests] == ['low', 'medium']
    assert [kw['agent_role'] for _, kw in requests] == ['main', 'planning']
    local.plans[trip.id] = local._seed_plan()
    requests = scripted(monkeypatch, [completion('这是原来的安排。')])
    TravelAgentRuntime(object(), 'test', {'trip': trip, 'plan': local.plans[trip.id], 'user_body': '解释'},
                       request=agent_client._create_chat_completion).run('revision', scope='explain')
    assert requests[0][1]['reasoning_effort'] == 'low'
