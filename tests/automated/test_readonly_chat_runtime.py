"""Offline read-only chat loop checks; no Store, database, dotenv or network."""
from copy import deepcopy
import json
from types import SimpleNamespace as NS

import pytest

from app.workflow.execution import RequestContext
from app.services.itinerary.plan_basis import plan_basis
from app.agents.budget import AgentBudgetError, TurnBudget
from app.agents.task_runtime import TaskExecutionContext, TaskExecutionRuntime
from app.agents.model_transport import responses_input
from app.models import Trip, TripRequirements, User


def call(name, args, ident='call-1'):
    return NS(id=ident, function=NS(name=name, arguments=json.dumps(args)))


class Script:
    def __init__(self, *steps):
        self.steps = iter(steps)
        self.transcripts = []

    def __call__(self, client, model, messages, **kwargs):
        kwargs['budget'].model()
        self.transcripts.append(deepcopy(messages))
        step = next(self.steps)
        message = NS(content=step if isinstance(step, str) else None,
                     tool_calls=[] if isinstance(step, str) else step)
        if not isinstance(step, str):
            message._responses_output = [{'type': 'reasoning', 'id': 'reasoning-1'}, *[
                {'type': 'function_call', 'call_id': item.id, 'name': item.function.name,
                 'arguments': item.function.arguments} for item in step]]
        return NS(choices=[NS(message=message, finish_reason='stop')])


def runtime(script, *, entry='main', budget=None):
    trip = Trip('trip', '', '', '', '', '', User('owner', '测试', '测'), '', '',
                requirements=TripRequirements(city='杭州'))
    ctx = RequestContext('trip', 'owner', 'task', None, 'req', 'message', '解释地铁和Python',
                         entry, 'task_execution', 0, plan_basis(None), 0, 0, 0)
    execution = TaskExecutionContext(trip, [], [], None, {}, '2030-01-01', entry=entry)
    return TaskExecutionRuntime(None, 'offline', ctx, execution, script, budget=budget)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Unexpected network call')
    monkeypatch.setattr('app.agents.tools.registry.run_tool', forbidden)


def test_main_prompt_context_and_plain_finish():
    script = Script('Python是一种编程语言。')
    runner = runtime(script)
    result = runner.run()
    assert not result.failed and not result.changes and not result.changes
    assert result.answer == 'Python是一种编程语言。'
    assert len(runner.registry) == 6 and 'submit_changes' in runner.registry
    context = json.loads(script.transcripts[0][1]['content'])
    assert context['requirements']['missing_for_generation']
    assert context['current_date'] == '2030-01-01'
    assert 'environment' not in context
    assert 'travel_window' not in context
    assert context['requirements']['known']['dates'] is None
    assert '普通聊天可自由回答非旅行问题' in runner.config.prompt
    assert runner.config.max_steps == 16


def test_main_sequential_multiple_calls_and_responses_replay():
    calls = [call('read_context', {'section': 'requirements'}, 'a'),
             call('read_context', {'section': 'messages'}, 'b')]
    script = Script([calls[0]], [calls[1]], '完成')
    runner = runtime(script)
    assert not runner.run().failed
    messages = script.transcripts[2]
    assert [row['tool_call_id'] for row in messages if row['role'] == 'tool'] == ['a', 'b']
    assistant = messages[2]
    assert len(assistant['tool_calls']) == 1
    replay = responses_input(messages)
    assert len([row for row in replay if row.get('type') == 'function_call']) == 2
    assert [row['call_id'] for row in replay if row.get('type') == 'function_call_output'] == ['a', 'b']
    assert len([row for row in replay if row.get('type') == 'reasoning']) == 2


@pytest.mark.parametrize('ids', [('same', 'same'), ('ok', ''), ('ok', None)])
def test_invalid_batch_ids_execute_nothing(ids):
    runner = runtime(Script([call('read_context', {'section': 'messages'}, ident) for ident in ids]))
    executed = []
    runner._execute_tool = lambda *args: executed.append(args)
    assert runner.run().failed
    assert not executed


def test_ids_cannot_be_reused_across_rounds():
    runner = runtime(Script([call('read_context', {'section': 'messages'})],
                            [call('read_context', {'section': 'messages'})]))
    assert runner.run().failed
    assert runner.budget.tool_calls == 1


def test_main_allows_long_query_loop_but_stops_repeated_invalid_writes():
    steps = [[call('read_context', {'section': 'messages'}, f'call-{i}')] for i in range(20)]
    runner = runtime(Script(*steps, '未保存。'))
    result = runner.run()
    assert not result.failed and not result.changes
    assert runner.budget.model_calls == 21 and runner.budget.tool_calls == 20
    invalid = runtime(Script(*[[call('submit_changes', {'user_quote': 'ignored'}, f'bad-{i}')]
                               for i in range(2)]))
    assert invalid.run().failed


def test_schema_validation_still_rejects_extra_properties():
    script = Script([call('read_context', {'section': 'messages', 'evil': True})], '未读取')
    runner = runtime(script)
    assert not runner.run().failed
    assert json.loads(script.transcripts[1][-1]['content'])['status'] == 'invalid_arguments'


def test_main_does_not_replace_final_text_with_transit_guard():
    runner = runtime(Script('Python解释；公交线路未核实。'))
    runner.evidence = [{'tool': 'query_route', 'result': {
        'mode': 'transit', 'origin_poi_id': 'a', 'destination_poi_id': 'b',
        'transit_detail_status': 'missing'}}]
    assert runner.run().answer == 'Python解释；公交线路未核实。'


def test_main_cross_city_queries_cache_and_catalog_route_binding(monkeypatch):
    runner = runtime(Script('unused'))
    seen = []
    places = [{'poi_id': ident, 'name': ident, 'address': '', 'longitude': 116.4 + i,
               'latitude': 39.9, 'city': '北京', 'district': '', 'fetched_at': '2030-01-01', 'source': '高德地图 POI'}
              for i, ident in enumerate(['a', 'b'])]
    def query(name, args, **kwargs):
        seen.append((name, deepcopy(args), kwargs['route_cache']))
        return {'status': 'available', 'candidates': places} if name == 'search_places' else {'status': 'available'}
    monkeypatch.setattr('app.agents.tools.registry.run_tool', query)
    args = {'city': '北京', 'query': '景点'}
    # Runtime schemas are tested separately; fake query deliberately ignores adapter details.
    output = runner._query('search_places', args)
    runner._query('search_places', args)
    assert len(seen) == 1 and 'route_review' not in output
    assert set(runner.catalog) == {'a', 'b'}
    runner._query('query_route', {'origin_poi_id': 'a', 'destination_poi_id': 'b'})
    assert seen[-1][2] is runner.cache and runner.cache['_budget'] is runner.budget
    with pytest.raises(ValueError, match='真实POI'):
        runner._query('query_route', {'origin_poi_id': 'fake', 'destination_poi_id': 'b'})
    runner._query('search_food', {'city': '北京'})
    assert seen[-1][0] == 'search_food'


def test_budget_none_counts_but_still_cancels_and_times_out():
    budget = TurnBudget(max_models=None, max_tools=None, max_external=None)
    budget.model_calls = budget.tool_calls = budget.external_calls = 1000
    budget.model()
    budget.tools(1000)
    budget.external()
    budget.cancelled = lambda: True
    with pytest.raises(AgentBudgetError):
        budget.remaining()
    budget.cancelled = None
    budget.deadline = 0
    with pytest.raises(AgentBudgetError):
        budget.remaining()
    with pytest.raises(AgentBudgetError):
        TurnBudget(max_models=0).model()


def test_planning_keeps_serial_contract_budget_and_correction_limit():
    script = Script(*[[call('read_context', {'section': 'messages'}, f'{i}a'),
                       call('read_context', {'section': 'ideas'}, f'{i}b')] for i in range(2)])
    runner = runtime(script, entry='planning')
    assert runner.run().failed and runner.budget.tool_calls == 0
    assert runner.budget.max_models == 16 and 'submit_changes' in runner.registry
    assert runner.config.max_steps == 16
    assert 'generation_question' not in runner._initial_context()
    with pytest.raises(ValueError, match='同城'):
        runner._query('search_places', {'city': '北京'})


def test_route_adapter_cache_limit_only_for_bounded_budgets(monkeypatch):
    from app.agents.tools.routes import lookup_route
    calls = []
    monkeypatch.setattr('app.agents.tools.routes.query_route', lambda *args: calls.append(args) or {'status': 'available'})
    catalog = {ident: {'poi_id': ident, 'longitude': i, 'latitude': 1} for i, ident in enumerate(['a', 'b'])}
    cached = {tuple([i] * 7): {'status': 'available'} for i in range(32)}
    args = {'origin_poi_id': 'a', 'destination_poi_id': 'b'}
    assert lookup_route(args, catalog, dict(cached))['status'] == 'unavailable'
    cache = {**cached, '_budget': TurnBudget(max_external=None)}
    assert lookup_route(args, catalog, cache)['status'] == 'available'
    assert lookup_route(args, catalog, cache)['status'] == 'available'
    assert len(calls) == 1
