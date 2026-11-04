"""Offline read-only chat loop checks; no Store, database, dotenv or network."""
from copy import deepcopy
from pathlib import Path
import json
from types import SimpleNamespace as NS

import pytest

from app.application.contracts import RequestContext
from app.domain.itinerary.identity import plan_basis
from app.agents.budget import AgentBudgetError, TurnBudget
from app.agents.inputs.execution import TaskExecutionContext
from app.agents.travel_runtime import TaskExecutionRuntime
from app.agents.model_transport import responses_input
from app.domain.models import Trip, TripRequirements, User


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


def runtime(script, *, entry='main', budget=None, body='解释地铁和Python'):
    trip = Trip('trip', '', User('owner', '测试', '测'), '', '',
                requirements=TripRequirements(city='杭州'))
    ctx = RequestContext('trip', 'owner', 'task', None, 'req', 'message', body,
                         entry, 'task_execution', 0, plan_basis(None), 0, 0, 0)
    execution = TaskExecutionContext(trip, [],  None, {}, '2030-01-01', entry=entry)
    return TaskExecutionRuntime(None, 'offline', ctx, execution, script, budget=budget)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Unexpected network call')
    monkeypatch.setattr('app.agents.tools.registry.run_tool', forbidden)


def test_main_prompt_context_and_plain_finish():
    script = Script('可以帮你查询同城交通。')
    runner = runtime(script, body='能查询同城交通吗？')
    result = runner.run()
    assert not result.failed and not result.changes and not result.changes
    assert result.answer == '可以帮你查询同城交通。'
    assert len(runner.registry) == 8 and 'submit_changes' in runner.registry
    context = json.loads(script.transcripts[0][1]['content'])
    assert context['requirements']['missing_for_generation']
    assert context['current_date'] == '2030-01-01'
    assert 'environment' not in context
    assert 'travel_window' not in context
    assert context['requirements']['known']['dates'] is None
    assert '不提供非旅行任务的实质回答' in runner.config.prompt
    assert '普通聊天可自由回答非旅行问题' not in runner.config.prompt
    assert runner.config.max_steps == 16


@pytest.mark.parametrize('body', [
    '教我做番茄炒蛋，给出配料比例和完整步骤。',
    '杭州西湖附近哪家餐厅能吃到番茄炒蛋？',
    '番茄炒蛋',
    '这次旅行想吃番茄炒蛋，记下来；再给我完整菜谱。',
    '旅行餐费每人80元，3人一共多少？',
    '在外国餐馆用餐，帮我翻译菜单上的 tomato omelette。',
])
def test_scope_policy_is_injected_for_food_and_travel_auxiliary_requests(body):
    """A scripted model proves prompt delivery only, NOT semantic compliance."""
    script = Script('离线占位回复，不代表模型的范围判断。')
    runner = runtime(script, body=body)
    assert not runner.run().failed
    prompt = script.transcripts[0][0]['content']
    assert prompt == runner.config.prompt
    assert '按请求意图和上下文判断，不按菜名或关键词封禁' in prompt
    assert '番茄炒蛋怎么做' in prompt
    assert '番茄炒蛋' in prompt and '先结合上下文澄清用途' in prompt
    assert '旅行费用计算、外国餐馆菜单翻译' in prompt
    assert '混合请求只处理旅行部分' in prompt
    assert '普通文字、工具查询与转述、submit_changes 的全部字段' in prompt
    assert '不把纯菜谱或其他越界内容保存为需求正文' in prompt
    assert json.loads(script.transcripts[0][1]['content'])['message']['text'] == body


def test_retained_main_prompt_matches_active_travel_prompt():
    prompts = Path(__file__).resolve().parents[2] / 'backend/app/agents/prompts'
    assert (prompts / 'main.txt').read_text(encoding='utf-8') == (
        prompts / 'travel.txt').read_text(encoding='utf-8')


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
    places = [{'id': ident, 'name': ident, 'address': '', 'location': f'{116.4 + i},39.9',
               'city': '北京'} for i, ident in enumerate(['a', 'b'])]
    def query(name, args, **kwargs):
        seen.append((name, deepcopy(args)))
        if name == 'maps_text_search':
            return {'pois': [{'id': row['id'], 'name': row['name']} for row in places]}
        if name == 'maps_search_detail':
            return next(row for row in places if row['id'] == args['id'])
        return {'paths': [{'duration': '600', 'distance': '500'}]}
    monkeypatch.setattr('app.agents.tools.registry.run_tool', query)
    args = {'city': '北京', 'keywords': '景点'}
    output = runner._query('maps_text_search', args)
    runner._query('maps_text_search', args)
    assert len(seen) == 1 and 'pois' in output
    assert not runner.catalog  # Search summaries do not invent coordinates.
    runner._query('maps_search_detail', {'id': 'a'})
    runner._query('maps_search_detail', {'id': 'b'})
    assert set(runner.catalog) == {'a', 'b'}
    raw = runner._query('maps_direction_walking', {'origin': '116.4,39.9', 'destination': '117.4,39.9'})
    assert raw['paths'][0]['duration'] == '600'
    assert runner.evidence[-1]['normalized']['duration_minutes'] == 10
    with pytest.raises(ValueError, match='真实地点'):
        runner._query('maps_direction_walking', {'origin': '0,0', 'destination': '117.4,39.9'})


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


def test_chat_keeps_serial_contract_and_correction_limit():
    script = Script(*[[call('read_context', {'section': 'messages'}, f'{i}a'),
                       call('read_context', {'section': 'ideas'}, f'{i}b')] for i in range(2)])
    runner = runtime(script, entry='main')
    assert runner.run().failed and runner.budget.tool_calls == 0
    assert runner.budget.max_models is None and 'submit_changes' in runner.registry
    assert runner.config.max_steps == 16
    assert 'generation_question' not in runner._initial_context()
    for entry in ('planning', 'revision'):
        with pytest.raises(ValueError, match='未知任务执行入口'):
            runtime(Script('unused'), entry=entry)


def test_native_route_cache_avoids_duplicate_external_calls(monkeypatch):
    runner = runtime(Script('unused'))
    runner.catalog = {ident: {'poi_id': ident, 'name': ident, 'longitude': i, 'latitude': 1,
                             'city': '杭州'} for i, ident in enumerate(['a', 'b'])}
    calls = []
    monkeypatch.setattr('app.agents.tools.registry.run_tool',
        lambda *args, **kwargs: calls.append(args) or {'paths': [{'duration': '60'}]})
    args = {'origin': '0,1', 'destination': '1,1'}
    runner._query('maps_direction_walking', args)
    runner._query('maps_direction_walking', args)
    assert len(calls) == 1 and runner.budget.external_calls == 1
