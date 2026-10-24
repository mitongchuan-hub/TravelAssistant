"""A current-turn checklist prevents silently dropping pending user requests."""
import json
from types import SimpleNamespace

from app.agents.agent_protocol import RequestContext, plan_basis
from app.agents.task_runtime import TaskExecutionContext, TaskExecutionRuntime
from app.models import Message, Trip, TripPlan, User


def make_runtime(request=None):
    owner = User('user', 'User', 'U')
    trip = Trip('trip', 'Beijing', '2026-10-24', '', '', '', owner, '', '')
    plan = TripPlan('plan', '', '')
    message = Message('message', owner, 'user', 'Check weather and route', 'now')
    context = RequestContext('trip', owner.id, 'execution', None, 'request', message.id,
        message.body, 'main', 'task_execution', 0, plan_basis(plan), 0, 0, 0)
    execution = TaskExecutionContext(trip, [message], [], plan, {}, '2026-10-23')
    return TaskExecutionRuntime(None, 'test', context, execution, request=request)


def call(name, **args):
    return SimpleNamespace(id='call', type='function', function=SimpleNamespace(
        name=name, arguments=json.dumps(args)))


def add(runtime):
    output, terminal = runtime._execute_call(call('manage_todos', operation='add', items=[
        {'id': None, 'title': 'Weather', 'status': 'pending', 'reason': ''},
        {'id': None, 'title': 'Route', 'status': 'pending', 'reason': ''},
    ]))
    assert terminal is None
    assert output['status'] == 'ok'
    return output['todos']


def test_pending_items_block_both_terminal_tools():
    runtime = make_runtime()
    add(runtime)
    for terminal in ('finish_turn', 'ask_user'):
        args = {'reply': 'Done'} if terminal == 'finish_turn' else {
            'question': 'Where?', 'answer_target': {'namespace': 'route', 'field': 'origin'}}
        output, proposal = runtime._execute_call(call(terminal, **args))
        assert proposal is None
        assert output['status'] == 'unavailable'
        assert 'todo' in output['reason'].lower()
    assert not runtime._can_finish_prose()


def test_updates_are_atomic_and_cannot_erase_other_items():
    runtime = make_runtime()
    todos = add(runtime)
    output, _ = runtime._execute_call(call('manage_todos', operation='update', items=[
        {**todos[0], 'status': 'done', 'reason': 'Weather result returned'},
        {**todos[1], 'id': 'unknown', 'status': 'unresolved', 'reason': 'Unavailable'},
    ]))
    assert output['status'] == 'unavailable'
    assert runtime.todos == todos
    output, _ = runtime._execute_call(call('manage_todos', operation='update', items=[
        {**todos[0], 'status': 'done', 'reason': 'Weather result returned'},
        {**todos[1], 'status': 'unresolved', 'reason': 'Route service unavailable'},
    ]))
    assert output['status'] == 'ok'
    _, proposal = runtime._execute_call(call('finish_turn', reply='Weather checked; route unavailable'))
    assert proposal is not None and not proposal.failed


def test_resolved_items_need_explanation_and_waiting_items_need_question():
    runtime = make_runtime()
    todos = add(runtime)
    output, _ = runtime._execute_call(call('manage_todos', operation='update', items=[
        {**todos[0], 'status': 'done', 'reason': ''}]))
    assert output['status'] == 'unavailable'
    runtime._execute_call(call('manage_todos', operation='update', items=[
        {**row, 'status': 'needs_input', 'reason': 'Need origin and date'} for row in todos]))
    output, proposal = runtime._execute_call(call('finish_turn', reply='Need information'))
    assert proposal is None and output['status'] == 'unavailable'
    _, proposal = runtime._execute_call(call('ask_user', question='Where and when?',
        answer_target={'namespace': 'route', 'field': 'origin'}))
    assert proposal is not None and not proposal.failed


def test_checklist_is_visible_each_decision_and_is_not_cross_turn_state():
    seen = []

    def request(client, model, messages, **kwargs):
        seen.append(json.loads(messages[-1]['content'])['turn_todos'])
        if len(seen) == 1:
            tool = call('manage_todos', operation='add', items=[
                {'id': None, 'title': 'Weather', 'status': 'pending', 'reason': ''}])
        elif len(seen) == 2:
            tool = call('manage_todos', operation='update', items=[
                {**seen[-1][0], 'status': 'unresolved', 'reason': 'Service unavailable'}])
        else:
            tool = call('finish_turn', reply='Weather service unavailable')
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=None, tool_calls=[tool]))])

    runtime = make_runtime(request)
    proposal = runtime.run()
    assert not proposal.failed
    assert seen[0] == []
    assert seen[1][0]['status'] == 'pending'
    assert seen[2][0]['status'] == 'unresolved'
    assert make_runtime().todos == []
    assert runtime.execution_context.trip.workflow_state == {}


def test_plain_reply_cannot_bypass_pending_checklist():
    runtime = make_runtime(lambda *args, **kwargs: SimpleNamespace(choices=[
        SimpleNamespace(message=SimpleNamespace(content='All done', tool_calls=[]))]))
    add(runtime)
    proposal = runtime.run()
    assert proposal.failed
    assert proposal.answer != 'All done'


def test_last_decision_records_unfinished_items_and_preserves_partial_reply():
    def request(client, model, messages, **kwargs):
        state = json.loads(messages[-1]['content'])['turn_todos']
        assert all(row['status'] == 'unresolved' for row in state)
        assert {tool['function']['name'] for tool in kwargs['tools']} == {'ask_user', 'finish_turn'}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=None, tool_calls=[call('finish_turn', reply='Partial results')]))])

    runtime = make_runtime(request)
    add(runtime)
    runtime.max_steps = 1
    proposal = runtime.run()
    assert not proposal.failed
    assert proposal.answer.startswith('Partial results')
    assert 'Weather' in proposal.answer and 'Route' in proposal.answer
    assert all(row['status'] == 'unresolved' for row in runtime.todos)


def test_todo_creation_is_bounded_and_does_not_allow_duplicate_or_resolved_adds():
    runtime = make_runtime()
    todos = add(runtime)
    for item in ({**todos[0], 'id': None},
                 {'id': None, 'title': 'New', 'status': 'done', 'reason': 'No result'}):
        output, _ = runtime._execute_call(call('manage_todos', operation='add', items=[item]))
        assert output['status'] == 'unavailable'
        assert runtime.todos == todos
    output, _ = runtime._execute_call(call('manage_todos', operation='add', items=[
        {'id': None, 'title': f'Item {n}', 'status': 'pending', 'reason': ''} for n in range(19)]))
    assert output['status'] == 'unavailable'
    assert runtime.todos == todos
