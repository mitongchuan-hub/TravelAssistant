"""Single-agent terminal/error contracts replace the removed task/todo protocol."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.application.contracts import RequestContext
from app.domain.itinerary.identity import plan_basis
from app.agents.budget import TurnBudget
from app.agents.inputs.execution import TaskExecutionContext
from app.agents.travel_runtime import TaskExecutionRuntime
from app.domain.models import Message, Trip, TripPlan, User


def make_runtime(request, **kwargs):
    owner = User('user', 'User', 'U')
    trip = Trip('trip', '北京市', owner, '', '')
    plan = TripPlan('plan', '', '未生成')
    message = Message('message', owner, 'user', '记下晚上夜骑这个想法', 'now')
    context = RequestContext('trip', owner.id, 'execution', None, 'request', message.id,
        message.body, 'main', 'task_execution', 0, plan_basis(plan), 0, 0, 0)
    execution = TaskExecutionContext(trip, [message],  plan, {}, '2026-10-23')
    return TaskExecutionRuntime(None, 'test', context, execution, request=request, **kwargs)


def call(name='submit_changes', arguments=None, *, call_id='call'):
    return SimpleNamespace(id=call_id, type='function', function=SimpleNamespace(name=name,
        arguments=arguments if isinstance(arguments, str) else json.dumps(arguments or {})))


def response(content=None, calls=(), finish_reason='stop'):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason,
        message=SimpleNamespace(content=content, tool_calls=list(calls)))])


def changes():
    return {'user_quote': '记下晚上夜骑这个想法', 'requirements_text': '晚上夜骑'}


def test_terminal_submit_is_one_model_call_and_not_a_store_mutation():
    seen = []

    def request(client, model, messages, **kwargs):
        seen.append(kwargs['agent_role'])
        assert {tool['function']['name'] for tool in kwargs['tools']} == {
            'read_context', 'query_weather', 'maps_text_search', 'maps_search_detail',
            'maps_direction_walking', 'maps_direction_driving', 'maps_direction_transit_integrated', 'submit_changes'}
        return response('不能作为已保存答复透传', [call(arguments=changes())])

    runtime = make_runtime(request)
    before = deepcopy(runtime.execution_context)
    proposal = runtime.run()
    assert not proposal.failed
    assert len(seen) == 1
    assert proposal.changes['args'] == changes()
    assert '不能作为已保存答复透传' not in proposal.answer
    assert runtime.execution_context == before


@pytest.mark.parametrize('invalid', [call(arguments='{'), call(arguments=[]),
    call('manage_task', {}), call('manage_todos', {}), call('finish_turn', {'reply': 'Done'}),
    call(arguments={'user_quote': '记下晚上夜骑这个想法', 'ideas': [
        {'action': 'create', 'kind': '待归类', 'title': '夜骑', 'body': '晚上夜骑', 'authority': 'admin'}]})])
def test_invalid_call_can_be_corrected_without_partial_changes(invalid):
    seen = []

    def request(client, model, messages, **kwargs):
        seen.append(messages[-1])
        if len(seen) == 1:
            return response(calls=[invalid])
        error = json.loads(messages[-1]['content'])
        assert error['recoverable'] and error['status'] == 'invalid_arguments'
        return response('尚未保存，请补充。')

    runtime = make_runtime(request)
    before = deepcopy(runtime.execution_context)
    proposal = runtime.run()
    assert not proposal.failed
    assert not proposal.changes
    assert runtime.execution_context == before


def test_repeated_invalid_submission_fails_closed_without_success_claim():
    ids = iter(['invalid-1', 'invalid-2', 'invalid-3'])
    runtime = make_runtime(lambda *a, **kw: response(calls=[call(arguments={}, call_id=next(ids))]))
    before = deepcopy(runtime.execution_context)
    proposal = runtime.run()
    assert proposal.failed
    assert not proposal.changes
    assert runtime.execution_context == before
    assert runtime.usage['tool_calls'] <= 3


def test_parallel_submission_batch_is_never_partially_executed():
    responses = iter([response(calls=[call(arguments=changes()), call(arguments=changes())]),
                      response('未执行这批修改。')])
    runtime = make_runtime(lambda *a, **kw: next(responses))
    proposal = runtime.run()
    assert not proposal.failed
    assert not proposal.changes
    assert runtime.execution_context.trip.requirements.text == ''


@pytest.mark.parametrize('failure', ['timeout', 'truncated', 'cancelled', 'tool_budget', 'model_budget'])
def test_budget_transport_and_cancellation_never_publish_pending_changes(failure):
    requested = []

    def request(client, model, messages, **kwargs):
        requested.append(True)
        kwargs['budget'].model()
        if failure == 'timeout':
            raise TimeoutError('timeout')
        return response(calls=[call(arguments=changes())],
                        finish_reason='length' if failure == 'truncated' else 'tool_calls')

    budget = TurnBudget(max_tools=0 if failure == 'tool_budget' else 64,
                        max_models=0 if failure == 'model_budget' else 16,
                        cancelled=(lambda: True) if failure == 'cancelled' else None)
    runtime = make_runtime(request, budget=budget)
    before = deepcopy(runtime.execution_context)
    proposal = runtime.run()
    assert proposal.failed
    assert not proposal.changes
    assert runtime.execution_context == before
    if failure in {'model_budget', 'cancelled'}:
        assert not requested


def test_step_limit_does_not_convert_read_results_into_a_write():
    runtime = make_runtime(lambda *a, **kw: response(calls=[call('read_context', {'section': 'ideas'})]),
                           max_steps=1)
    proposal = runtime.run()
    assert proposal.failed
    assert not proposal.changes
    assert runtime.execution_context.trip.requirements.text == ''
