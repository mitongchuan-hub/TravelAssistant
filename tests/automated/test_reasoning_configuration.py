"""Explicit reasoning settings must not be silently removed after rejection."""
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from openai import BadRequestError

from app.agents import model_client
from app.agents.trace import ConversationTrace
from app.agents.budget import AgentBudgetError, TurnBudget


def test_rejected_reasoning_setting_is_not_removed_or_retried(monkeypatch):
    requests = []
    rejection = BadRequestError(
        'reasoning_effort is not supported',
        response=httpx.Response(400, request=httpx.Request('POST', 'https://model.offline.invalid')),
        body={'error': {'param': 'reasoning_effort', 'code': 'unsupported_parameter'}},
    )

    def reject(client, params, api):
        requests.append(dict(params))
        raise rejection

    monkeypatch.setattr(model_client, 'create_completion', reject)
    budget = TurnBudget()
    for _ in range(2):
        with pytest.raises(BadRequestError) as error:
            model_client.request_completion(
                SimpleNamespace(base_url='https://model.offline.invalid'), 'test-model',
                [{'role': 'user', 'content': 'synthetic'}],
                reasoning_effort='medium', budget=budget, fallback_model='fallback',
            )
        assert error.value is rejection
    assert len(requests) == 2
    assert all(request['reasoning_effort'] == 'medium' for request in requests)
    assert budget.model_calls == 2


@pytest.mark.parametrize('response_format', [
    {'type': 'json_schema', 'json_schema': {'name': 'synthetic', 'schema': {'type': 'object'}}},
    {'type': 'json_object'},
])
def test_rejected_response_format_is_not_changed_or_retried(monkeypatch, response_format):
    requests = []
    rejection = BadRequestError(
        'response_format is not supported',
        response=httpx.Response(400, request=httpx.Request('POST', 'https://model.offline.invalid')),
        body={'error': {'param': 'response_format', 'code': 'unsupported_parameter'}},
    )

    def reject(client, params, api):
        requests.append(dict(params))
        raise rejection

    monkeypatch.setattr(model_client, 'create_completion', reject)
    budget = TurnBudget()
    trace = ConversationTrace('task-id', datetime.now().isoformat(), 'trip-id', 'synthetic')
    with pytest.raises(BadRequestError) as error:
        model_client.request_completion(
            None, 'test-model', [{'role': 'user', 'content': 'synthetic'}],
            response_format=response_format, trace=trace, budget=budget,
        )
    assert error.value is rejection
    assert len(requests) == 1
    assert requests[0]['response_format'] == response_format
    assert budget.model_calls == 1
    endings = [record['value'] for record in trace.records if record['title'] == '模型请求结束']
    assert len(endings) == 1
    assert endings[0]['status'] == 'exception'
    assert not any(record['title'] == '模型请求重试' for record in trace.records)


@pytest.mark.parametrize('with_budget', [True, False])
def test_each_model_call_has_unique_paired_trace_id(monkeypatch, with_budget):
    response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3),
                               choices=[SimpleNamespace(message=SimpleNamespace(content='synthetic'))])
    requests = []

    def complete(client, params, api):
        requests.append(dict(params))
        if len(requests) == 1:
            raise RuntimeError('synthetic failure')
        return response

    monkeypatch.setattr(model_client, 'create_completion', complete)
    trace = ConversationTrace('task-id', datetime.now().isoformat(), 'trip-id', 'synthetic')
    budget = TurnBudget() if with_budget else None
    for index in range(2):
        if index == 0:
            with pytest.raises(RuntimeError, match='synthetic failure'):
                model_client.request_completion(None, 'test-model', [], trace=trace, budget=budget)
        else:
            assert model_client.request_completion(None, 'test-model', [], trace=trace, budget=budget) is response
    starts = [record['value'] for record in trace.records if record['title'] == '模型请求开始']
    ends = [record['value'] for record in trace.records if record['title'] == '模型请求结束']
    assert len(requests) == len(starts) == len(ends) == 2
    ids = [record['model_request_id'] for record in starts]
    assert len(set(ids)) == 2
    assert all(UUID(value).hex == value for value in ids)
    assert [record['model_request_id'] for record in ends] == ids
    assert [record['status'] for record in ends] == ['exception', 'success']
    for record in trace.records:
        assert record['value']['request_id'] == 'task-id'
        assert record['value']['model_request_id'] in ids
    if budget:
        assert [record['request_index'] for record in starts] == [1, 2]
        assert budget.model_calls == 2
        assert budget.input_tokens == 2 and budget.output_tokens == 3


@pytest.mark.parametrize('api', ['chat_completions', 'responses'])
def test_fixed_parameters_pass_through_and_thinking_is_chat_only(monkeypatch, api):
    requests = []
    response = SimpleNamespace(usage=None)
    monkeypatch.setenv('OPENAI_WIRE_API', api)
    monkeypatch.setenv('OPENAI_MAIN_ENABLE_THINKING', 'false')

    def complete(client, params, wire):
        requests.append((params, wire))
        return response

    monkeypatch.setattr(model_client, 'create_completion', complete)
    messages = [{'role': 'user', 'content': 'synthetic'}]
    tools = [{'type': 'function', 'function': {'name': 'synthetic', 'parameters': {'type': 'object'}}}]
    response_format = {'type': 'json_object'}
    assert model_client.request_completion(
        None, 'test-model', messages, tools=tools, response_format=response_format,
        reasoning_effort='medium', agent_role='travel_agent', budget=TurnBudget(),
    ) is response
    assert len(requests) == 1
    params, wire = requests[0]
    assert wire == api
    assert params['messages'] is messages and params['tools'] is tools
    assert params['response_format'] is response_format
    assert params['reasoning_effort'] == 'medium'
    assert 0 < params['timeout'] <= 12
    if api == 'chat_completions':
        assert params['extra_body'] == {'chat_template_kwargs': {'enable_thinking': False}}
    else:
        assert 'extra_body' not in params


@pytest.mark.parametrize('max_models', [None, 16])
@pytest.mark.parametrize('remaining,expected', [(350.0, 300.0), (35.0, 35.0)])
def test_request_timeout_uses_configuration_and_remaining_time(monkeypatch, max_models, remaining, expected):
    requests = []
    monkeypatch.setenv('OPENAI_TIMEOUT_SECONDS', '300')
    budget = TurnBudget(max_models=max_models)
    monkeypatch.setattr(budget, 'remaining', lambda: remaining)

    def complete(client, params, api):
        requests.append(params)
        return SimpleNamespace(usage=None)

    monkeypatch.setattr(model_client, 'create_completion', complete)
    model_client.request_completion(None, 'test-model', [], budget=budget)
    assert requests[0]['timeout'] == expected
    assert budget.model_calls == 1


@pytest.mark.parametrize('cancelled', [False, True])
def test_budget_rejection_does_not_start_model_request(monkeypatch, cancelled):
    def forbidden(*args, **kwargs):
        pytest.fail('Budget rejection must not send a model request')

    monkeypatch.setattr(model_client, 'create_completion', forbidden)
    budget = TurnBudget(max_models=0 if not cancelled else 16, cancelled=lambda: cancelled)
    trace = ConversationTrace('task-id', datetime.now().isoformat(), 'trip-id', 'synthetic')
    with pytest.raises(AgentBudgetError):
        model_client.request_completion(None, 'test-model', [], trace=trace, budget=budget)
    assert budget.model_calls == 0
    assert len(trace.records) == 1
    assert trace.records[0]['title'] == '模型请求未开始'
    value = trace.records[0]['value']
    assert value['request_id'] == 'task-id'
    assert UUID(value['model_request_id']).hex == value['model_request_id']


@pytest.mark.parametrize('scope', ['current_model_request', 'display_model_request'])
def test_fallback_is_per_request_with_independent_ids_and_shared_budget(monkeypatch, scope):
    requests = []
    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3),
        choices=[SimpleNamespace(message=SimpleNamespace(content='synthetic', tool_calls=[]))],
    )

    class RateLimitError(RuntimeError):
        status_code = 429

    def complete(client, params, api):
        requests.append(dict(params))
        if params['model'] == 'primary':
            raise RateLimitError('private provider details')
        return response

    monkeypatch.setattr(model_client, 'create_completion', complete)
    budget = TurnBudget(max_models=4)
    trace = ConversationTrace('task-id', datetime.now().isoformat(), 'trip-id', 'synthetic')
    messages = [{'role': 'user', 'content': 'synthetic'}]
    tools = [{'type': 'function', 'function': {'name': 'synthetic'}}]
    response_format = {'type': 'json_object'}
    for _ in range(2):
        assert model_client.request_completion(
            None, 'primary', messages, trace=trace, budget=budget,
            tools=tools, response_format=response_format, reasoning_effort='medium',
            fallback_model='fallback', fallback_scope=scope,
        ) is response
    assert [item['model'] for item in requests] == ['primary', 'fallback'] * 2
    assert all(item['messages'] is messages and item['tools'] is tools for item in requests)
    assert all(item['response_format'] is response_format and item['reasoning_effort'] == 'medium'
               for item in requests)
    assert budget.model_calls == 4
    assert (budget.input_tokens, budget.output_tokens) == (4, 6)
    starts = [row['value'] for row in trace.records if row['title'] == '模型请求开始']
    ends = [row['value'] for row in trace.records if row['title'] == '模型请求结束']
    ids = [row['model_request_id'] for row in starts]
    assert len(set(ids)) == 4
    assert [row['model_request_id'] for row in ends] == ids
    assert [row['request_index'] for row in starts] == [1, 2, 3, 4]
    assert [row['status'] for row in ends] == ['exception', 'success'] * 2
    switches = [row['value'] for row in trace.records if row['title'] == '切换备用模型']
    detail = ({'error_type': 'RateLimitError', 'status_code': 429}
              if scope == 'display_model_request' else {'reason': 'private provider details'})
    assert switches == [{'from': 'primary', 'to': 'fallback', 'scope': scope, **detail}] * 2
    if scope == 'display_model_request':
        assert 'private provider details' not in str(trace.records)


@pytest.mark.parametrize('fallback', [None, '', 'primary', 'fallback'])
def test_rate_limit_does_not_repeat_fallback_and_respects_disabled_configuration(monkeypatch, fallback):
    requests = []

    class RateLimitError(RuntimeError):
        status_code = 429

    def reject(client, params, api):
        requests.append(params['model'])
        raise RateLimitError('synthetic')

    monkeypatch.setattr(model_client, 'create_completion', reject)
    with pytest.raises(RateLimitError):
        model_client.request_completion(None, 'primary', [], fallback_model=fallback, budget=TurnBudget())
    assert requests == (['primary', 'fallback'] if fallback == 'fallback' else ['primary'])


@pytest.mark.parametrize('cancel_on_failure', [False, True])
def test_fallback_cannot_bypass_exhausted_budget_or_cancellation(monkeypatch, cancel_on_failure):
    requests = []
    cancelled = False
    budget = TurnBudget(max_models=2 if cancel_on_failure else 1, cancelled=lambda: cancelled)
    trace = ConversationTrace('task-id', datetime.now().isoformat(), 'trip-id', 'synthetic')

    class RateLimitError(RuntimeError):
        status_code = 429

    def reject(client, params, api):
        nonlocal cancelled
        requests.append(params['model'])
        cancelled = cancel_on_failure
        raise RateLimitError('synthetic')

    monkeypatch.setattr(model_client, 'create_completion', reject)
    with pytest.raises(AgentBudgetError):
        model_client.request_completion(None, 'primary', [], fallback_model='fallback', trace=trace, budget=budget)
    assert requests == ['primary']
    assert budget.model_calls == 1
    blocked = trace.records[-1]
    assert blocked['title'] == '模型请求未开始'
    assert blocked['value']['model'] == 'fallback'
    start = next(row['value'] for row in trace.records if row['title'] == '模型请求开始')
    assert blocked['value']['model_request_id'] != start['model_request_id']
