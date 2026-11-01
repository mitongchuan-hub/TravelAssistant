"""单旅行 Agent：一个上下文、六个工具、一次终止提交。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from time import monotonic
from typing import Any, Mapping

from jsonschema import ValidationError

from app.workflow.execution import RequestContext
from app.workflow.execution import ExecutionResult
from app.agents.agent_roles import travel_role, role_reasoning_effort
from app.agents.budget import AgentBudgetError, TurnBudget
from app.agents.main_input import project_main_context, project_main_read_context
from app.agents.model_transport import IncompleteModelOutput
from app.services.places import collect_places
from app.services.itinerary.planning import required_information
from app.services.itinerary.planning_evidence import critical_route_review
from app.agents.tools import registry as tools
from app.workflow.task_context import normalize_workflow


@dataclass(frozen=True)
class TaskExecutionContext:
    """Web 层传入的独立可信快照；不授予模型存储权限。"""
    trip: Any
    messages: list[Any] | tuple[Any, ...]
    idea_cards: list[Any] | tuple[Any, ...]
    plan: Any
    metrics: Mapping[str, Any]
    current_date: str
    entry: str = 'main'
    scope: str = ''
    target_item_id: str | None = None
    display_target: Mapping[str, Any] | None = None

    def __post_init__(self):
        if self.entry not in {'main', 'planning', 'revision', 'confirm'}:
            raise ValueError('未知任务执行入口')
        if self.scope not in {'', 'item', 'all'}:
            raise ValueError('任务执行范围无效')
        if self.display_target is not None and self.entry != 'revision':
            raise ValueError('展示目标只能用于修改入口')


_ROUTE_DETAIL_HINTS = ('公交', '地铁', '公共交通', '线路', '换乘', '乘车', '车站', '站点', '怎么去')


def transit_detail_guard(task, evidence):
    """缺线路时只交代耗时与缺项，不把摘要当完整公交路线。"""
    if not isinstance(task, str) or not any(word in task for word in _ROUTE_DETAIL_HINTS):
        return None
    for row in evidence:
        result = row.get('result', {})
        if (row.get('tool') == 'query_route' and result.get('mode') == 'transit'
                and result.get('origin_poi_id') != result.get('destination_poi_id')
                and (result.get('transit_detail_status') == 'missing'
                     or (result.get('transit_detail_status') not in {'available', 'partial'}
                         and not result.get('transit_steps')))):
            return '公交线路明细未返回；仅有已查询的耗时和距离，具体线路、上下车站及换乘仍待核实。'
    return None


@dataclass
class TaskExecutionRuntime:
    client: Any
    model: str
    request_context: RequestContext
    execution_context: TaskExecutionContext
    request: Any
    trace: Any = None
    budget: TurnBudget | None = None
    cache: dict = field(default_factory=dict)
    max_steps: int = 16
    max_corrections: int = 3

    # 初始化脱离存储的快照、共享预算和绑定当前实例的六工具注册表。
    def __post_init__(self):
        if not isinstance(self.request_context, RequestContext):
            raise ValueError('request_context必须由服务器创建')
        if self.request_context.strategy != 'task_execution':
            raise ValueError('TaskExecutionRuntime只接受task_execution策略')
        if not isinstance(self.execution_context, TaskExecutionContext):
            raise ValueError('execution_context类型不正确')
        if self.request_context.entry != self.execution_context.entry:
            raise ValueError('执行入口与可信请求不一致')
        self.chat_entry = self.execution_context.entry == 'main'
        self.budget = self.budget or (
            TurnBudget(max_models=None, max_tools=None, max_external=None)
            if self.chat_entry else TurnBudget())
        self.cache['_budget'] = self.budget
        self.config = travel_role()
        self.trip = deepcopy(self.execution_context.trip)
        self.plan = deepcopy(self.execution_context.plan)
        message_ids = [row.id for row in self.execution_context.messages if getattr(row, 'id', None)]
        self.workflow = normalize_workflow(
            getattr(self.trip, 'workflow_state', {}), messages=message_ids,
            plan_basis=self.request_context.plan_basis,
            requirements_revision=self.request_context.requirements_revision)
        self.catalog = {}
        self.evidence = []
        self.external_results = {}
        saved = getattr(self.plan, 'evidence', []) if self.plan else []
        self._merge_places(collect_places([
            {'role': 'tool', 'name': 'search_places', 'content': json.dumps(row['result'], ensure_ascii=False)}
            for row in saved if isinstance(row, dict) and row.get('tool') == 'search_places'
            and isinstance(row.get('result'), dict)]))
        self.registry = tools.agent_tool_registry(read_context=self._read_context,
                                                 submit_changes=self._submit, query=self._query)
        self._corrections = 0
        self._failures = {}
        self.messages = []

    def event(self, name, value):
        if self.trace:
            self.trace.add(name, value)

    @property
    def usage(self):
        return self.budget.usage()

    def _merge_places(self, places):
        self.catalog.update(deepcopy(places))
        if not self.chat_entry:
            city = self.trip.requirements.city.removesuffix('市')
            for poi_id, place in list(self.catalog.items()):
                if not isinstance(place, dict) or place.get('city', '').removesuffix('市') != city:
                    del self.catalog[poi_id]
        if self.trip.lodging:
            self.catalog[self.trip.lodging['poi_id']] = deepcopy(self.trip.lodging)

    def _initial_context(self):
        return project_main_context(
            request_context=self.request_context, execution_context=self.execution_context,
            workflow=self.workflow, recent_chat=self.execution_context.messages,
            plan=self.plan, missing_requirements=required_information(self.trip))

    # 全入口复用唯一转录；终止提交后不再请求模型，异常直接闭合失败。
    def run(self) -> ExecutionResult:
        started = monotonic()
        try:
            self.budget.remaining()
            if self.execution_context.entry == 'confirm':
                return self._submit({'user_quote': self.request_context.body,
                                     'itinerary': {'action': 'confirm'}})
            self.messages = [
                {'role': 'system', 'content': self.config.prompt},
                {'role': 'user', 'content': json.dumps(self._initial_context(), ensure_ascii=False, default=str)},
            ]
            steps = 0
            while self.chat_entry or steps < min(self.max_steps, self.config.max_steps):
                steps += 1
                self.budget.reserve(models=1)
                response = self.request(
                    self.client, self.model, self.messages,
                    tools=[item.model_spec() for item in self.registry.values()],
                    trace=self.trace, budget=self.budget,
                    reasoning_effort=role_reasoning_effort(self.config), agent_role=self.config.name)
                self.budget.remaining()
                choices = list(getattr(response, 'choices', ()) or ())
                if len(choices) != 1 or getattr(choices[0], 'finish_reason', None) not in {
                        None, 'stop', 'tool_calls', 'function_call'}:
                    raise IncompleteModelOutput('模型响应不完整，未执行任何工具。')
                assistant = choices[0].message
                calls = list(getattr(assistant, 'tool_calls', ()) or ())
                if not calls:
                    answer = (getattr(assistant, 'content', '') or '').strip()
                    if not answer:
                        return ExecutionResult.failure('模型未返回有效回复，未保存任何修改。')
                    detail_warning = (None if self.chat_entry else
                                      transit_detail_guard(self.request_context.body, self.evidence))
                    if detail_warning:
                        # 没有线路依据时不透传可能捏造的线路说明，也不另调模型复核。
                        answer = self._route_unavailable_answer(detail_warning)
                    return ExecutionResult(answer=answer)
                if len(calls) != 1:
                    if self._record_failure('serial', '一次响应只能调用一个工具，本批未执行。'):
                        return ExecutionResult.failure('模型重复违反串行工具合同。')
                    # 不把无效并行调用写入正式工具转录，避免生成多条工具响应。
                    self.messages.append({'role': 'user', 'content': '刚才的多个工具调用均未执行。每次仅调用一个工具。'})
                    continue
                used_ids = {row.get('tool_call_id') for row in self.messages if row['role'] == 'tool'}
                for call in calls:
                    call_id = getattr(call, 'id', None)
                    if not isinstance(call_id, str) or not call_id.strip() or call_id in used_ids:
                        return ExecutionResult.failure('模型工具调用标识缺失或重复，未执行本次工具。')
                    used_ids.add(call_id)
                self.messages.append(self._assistant_message(assistant, calls))
                for call in calls:
                    self.budget.tools(1)
                    try:
                        spec = self.registry.get(call.function.name)
                        if spec is None:
                            raise ValueError('未知工具')
                        arguments = json.loads(call.function.arguments or '{}')
                        self.budget.remaining()
                        output = self._execute_tool(spec, arguments)
                        if spec.terminal:
                            return output
                        self.budget.remaining()
                    except (ValueError, TypeError, KeyError, ValidationError) as exc:
                        if isinstance(exc, IncompleteModelOutput):
                            raise
                        reason = exc.message if isinstance(exc, ValidationError) else str(exc)
                        if self._record_failure(call.function.name, reason):
                            return ExecutionResult.failure('无效工具调用重复或纠正次数已达上限，未保存任何修改。')
                        output = {'status': 'invalid_arguments', 'reason': reason, 'recoverable': True}
                    self.messages.append({'role': 'tool', 'tool_call_id': call.id,
                                          'content': json.dumps(output, ensure_ascii=False, default=str)})
            return ExecutionResult.failure('本轮执行步数已达上限，未保存任何修改。')
        except (AgentBudgetError, TimeoutError, IncompleteModelOutput) as exc:
            return ExecutionResult.failure(str(exc))
        except Exception as exc:
            self.event('Task Runtime异常', {'error_type': type(exc).__name__, 'reason': str(exc)[:500]})
            return ExecutionResult.failure('本次执行未完成，当前需求和行程已保留。')
        finally:
            self.event('Task Runtime预算', self.usage)
            self.event('Task Runtime耗时', {'seconds': round(monotonic() - started, 3)})

    def _record_failure(self, name, reason):
        # 按错误类别与原因计数，不能靠更换无效参数绕开重复上限。
        key = (name, reason)
        self._failures[key] = self._failures.get(key, 0) + 1
        self._corrections += 1
        return self._failures[key] >= 2 or self._corrections > min(self.max_corrections, 3)

    def _execute_tool(self, spec, arguments):
        started, output, error = monotonic(), None, None
        self.event('工具开始', {'role': self.config.name, 'name': spec.name, 'arguments': arguments})
        try:
            output = spec.execute(arguments)
            return output
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            self.event('工具执行', {'role': self.config.name, 'name': spec.name,
                'arguments': arguments, 'seconds': round(monotonic() - started, 3),
                'result': None if spec.terminal else output, 'error_type': error})

    @staticmethod
    def _assistant_message(assistant, calls):
        message = {'role': 'assistant', 'content': getattr(assistant, 'content', None),
                   'tool_calls': [{'id': call.id, 'type': 'function', 'function': {
                       'name': call.function.name, 'arguments': call.function.arguments}} for call in calls]}
        replay = getattr(assistant, '_responses_output', None)
        if replay is not None:
            message['_responses_output'] = deepcopy(replay)
        return message

    def _read_context(self, args):
        section = args['section']
        raw = {'requirements': {'items': self.execution_context.idea_cards},
               'messages': self.execution_context.messages,
               'ideas': self.execution_context.idea_cards,
               'evidence': list(getattr(self.plan, 'evidence', []) or []) + self.evidence}.get(section, {})
        return project_main_read_context(
            section, raw, trip=self.trip, plan=self.plan,
            expected_basis=self.request_context.plan_basis,
            current_message_id=self.request_context.message_id, request_args=args)

    def _submit(self, args):
        from app.services.agent_crud import prepare_changes
        self.budget.remaining()
        # 证据仅为服务器实际执行记录，不接受模型提交的事实或引用。
        payload = prepare_changes(args, self.request_context, self.execution_context, deepcopy(self.evidence))
        self.budget.remaining()
        return ExecutionResult(changes=payload)

    def _route_review(self):
        return critical_route_review(self.trip, self.catalog, self.evidence,
                                     self.budget.max_external - self.budget.external_calls)

    # 普通查询复用原地图/天气适配器，同城、真实POI、缓存和外部预算均在服务端控制。
    def _query(self, name, args):
        self.budget.remaining()
        city = self.trip.requirements.city.removesuffix('市')
        if not self.chat_entry and name in {'search_places', 'search_food', 'query_weather'}:
            requested = args.get('city') or self.trip.requirements.city
            if not requested or requested.removesuffix('市') != city:
                raise ValueError('同城查询须使用当前住宿所在城市。')
            args = dict(args, city=requested)
        if name == 'query_route':
            endpoints = [self.catalog.get(args.get(key)) for key in ('origin_poi_id', 'destination_poi_id')]
            if any(not point or (not self.chat_entry and point.get('city', '').removesuffix('市') != city)
                   for point in endpoints):
                raise ValueError('路线仅接受目录中的真实POI，请先查询地点。')
        planning = self.execution_context.entry in {'planning', 'revision'}
        if planning and name == 'search_food':
            pending = [row for row in self._route_review() if row['status'] == 'not_attempted']
            if pending:
                return {'status': 'prerequisite_needed', 'route_review': pending,
                        'instruction': '先查询关键住宿接驳，再查具体餐厅。'}
        key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
        if name != 'query_route' and key in self.external_results:
            self.event('查询缓存复用', {'tool': name, 'scope': 'current_turn'})
            return deepcopy(self.external_results[key])
        if name != 'query_route':
            required_calls = 6 if name in {'search_places', 'search_food'} else 2
            self.budget.reserve(external=required_calls)
            if name != 'query_weather':
                self.budget.external()
        from app.agents.tools.amap_mcp import budget_scope
        from app.agents.tools.weather import budget_scope as weather_scope
        started = monotonic()
        scope = weather_scope if name == 'query_weather' else budget_scope
        with scope(self.budget):
            output = tools.run_tool(name, args, place_catalog=self.catalog, route_cache=self.cache)
        self.budget.remaining()
        self._merge_places(collect_places([{'role': 'tool', 'name': name,
                                           'content': json.dumps(output, ensure_ascii=False)}]))
        if name != 'query_route' and output.get('status') == 'available':
            self.external_results[key] = deepcopy(output)
        self.evidence.append({'tool': name, 'arguments': deepcopy(args), 'result': deepcopy(output),
                              'queried_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                              'seconds': round(monotonic() - started, 3)})
        if name == 'search_places' and not self.chat_entry:
            # 聊天生成与页面入口共享路线提示；只读查询不强制补查整条行程。
            return dict(output, route_review=self._route_review())
        return output

    def _route_unavailable_answer(self, warning):
        lines = [warning]
        for row in self.evidence:
            result = row.get('result', {})
            if row.get('tool') != 'query_route' or result.get('status') != 'available':
                continue
            details = []
            if result.get('duration_minutes') is not None:
                details.append(f"约{result['duration_minutes']}分钟")
            if result.get('distance_meters') is not None:
                details.append(f"{result['distance_meters']}米")
            if details:
                lines.append(f"{result.get('origin_name', '起点')}→{result.get('destination_name', '终点')}：{'，'.join(details)}。")
        return '\n'.join(lines)
