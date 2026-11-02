"""单旅行 Agent：一个上下文、原生地图与独立天气工具、一次终止提交。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
import json
from time import monotonic
from typing import Any, Mapping

from jsonschema import ValidationError

from app.workflow.execution import RequestContext
from app.workflow.execution import ExecutionResult
from app.agents.agent_roles import travel_role, role_reasoning_effort
from app.agents.budget import AgentBudgetError, TurnBudget
from app.agents.main_input import project_main_context
from app.agents.tools.read_context import read_context
from app.agents.tools.submit_changes import submit_changes
from app.agents.model_transport import IncompleteModelOutput
from app.services.places import collect_places
from app.services.itinerary.planning import required_information
from app.services.maps.evidence import ROUTE_TOOLS, PLACE_TOOLS, evidence_kind, evidence_result, place_messages
from app.services.maps.places import normalize_pois
from app.services.maps.routes import normalize_route
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
        if self.entry not in {'main', 'confirm'}:
            raise ValueError('未知任务执行入口')
        if self.scope not in {'', 'item', 'all'}:
            raise ValueError('任务执行范围无效')
        if self.display_target is not None and self.entry != 'main':
            raise ValueError('展示目标只能用于聊天入口')


_ROUTE_DETAIL_HINTS = ('公交', '地铁', '公共交通', '线路', '换乘', '乘车', '车站', '站点', '怎么去')


def transit_detail_guard(task, evidence):
    """缺线路时只交代耗时与缺项，不把摘要当完整公交路线。"""
    if not isinstance(task, str) or not any(word in task for word in _ROUTE_DETAIL_HINTS):
        return None
    for row in evidence:
        result = evidence_result(row)
        if (evidence_kind(row) == 'query_route' and result.get('mode') == 'transit'
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

    # 初始化脱离存储的快照、共享预算和本轮工具注册表。
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
        self._merge_places(collect_places(place_messages(saved)))
        self.search_ids = set(self.catalog)
        self.registry = tools.agent_tool_registry(
            read_context=partial(read_context, request_context=self.request_context,
                execution_context=self.execution_context, trip=self.trip, plan=self.plan, evidence=self.evidence),
            submit_changes=partial(submit_changes, request_context=self.request_context,
                execution_context=self.execution_context, evidence=self.evidence, budget=self.budget),
            query=self._query)
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
                return self.registry['submit_changes'].execute({'user_quote': self.request_context.body,
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

    def _known_coordinate(self, value):
        try:
            lon, lat = (float(part) for part in value.split(','))
        except (AttributeError, TypeError, ValueError):
            raise ValueError('路线坐标必须来自已查询的真实地点') from None
        matches = [place for place in self.catalog.values()
                   if place.get('longitude') == lon and place.get('latitude') == lat]
        if not matches:
            raise ValueError('路线坐标必须来自已查询的真实地点，请先搜索并查询详情')
        return matches[0]

    def _query(self, name, args):
        self.budget.remaining()
        city = self.trip.requirements.city.removesuffix('市')
        if name not in ROUTE_TOOLS and name not in PLACE_TOOLS and name != 'query_weather':
            raise ValueError('未知查询工具')
        if not self.chat_entry and name in {'maps_text_search', 'query_weather'}:
            if not args.get('city') or args['city'].removesuffix('市') != city:
                raise ValueError('同城查询须使用当前住宿所在城市。')
        if name == 'maps_search_detail' and args.get('id') not in self.search_ids:
            raise ValueError('地点详情只接受本轮搜索或已保存地点返回的真实ID')
        endpoints = None
        if name in ROUTE_TOOLS:
            endpoints = [self._known_coordinate(args.get(key)) for key in ('origin', 'destination')]
            if not self.chat_entry and any(point.get('city', '').removesuffix('市') != city for point in endpoints):
                raise ValueError('同城路线须使用住宿所在城市的真实地点')
            if ROUTE_TOOLS[name] == 'transit':
                for key, point in zip(('city', 'cityd'), endpoints):
                    if args.get(key, '').removesuffix('市') != point.get('city', '').removesuffix('市'):
                        raise ValueError('公交城市须与已查询地点所在城市一致')
        key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
        if key in self.external_results:
            self.event('查询缓存复用', {'tool': name, 'scope': 'current_turn'})
            return deepcopy(self.external_results[key])
        from app.services.maps.amap_mcp import budget_scope
        from app.agents.tools.weather import budget_scope as weather_scope
        scope = weather_scope if name == 'query_weather' else budget_scope
        started = monotonic()
        if name != 'query_weather':
            self.budget.external()
        with scope(self.budget):
            output = tools.run_tool(name, args, place_catalog=self.catalog, route_cache=self.cache)
        self.budget.remaining()
        normalized = output
        if name in PLACE_TOOLS:
            if name == 'maps_text_search':
                self.search_ids.update(poi['id'] for poi in output.get('pois', [])
                                       if isinstance(poi, dict) and isinstance(poi.get('id'), str))
            elif output.get('status') != 'unavailable' and output.get('id') != args['id']:
                raise ValueError('地图详情返回的ID与请求不一致')
            normalized = normalize_pois(output)
            self._merge_places(collect_places([{'role': 'tool', 'name': 'search_places',
                'content': json.dumps(normalized, ensure_ascii=False)}]))
        elif endpoints:
            # Decode only for business evidence. LLM receives the full native result.
            normalized = normalize_route({'route': output.get('route', output)},
                                         *endpoints, mode=ROUTE_TOOLS[name])
        if output.get('status') != 'unavailable':
            self.external_results[key] = deepcopy(output)
        self.evidence.append({'tool': name, 'arguments': deepcopy(args), 'result': deepcopy(output),
                              'normalized': deepcopy(normalized),
                              'queried_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                              'seconds': round(monotonic() - started, 3)})
        return output

    def _route_unavailable_answer(self, warning):
        lines = [warning]
        for row in self.evidence:
            result = evidence_result(row)
            if evidence_kind(row) != 'query_route' or result.get('status') != 'available':
                continue
            details = []
            if result.get('duration_minutes') is not None:
                details.append(f"约{result['duration_minutes']}分钟")
            if result.get('distance_meters') is not None:
                details.append(f"{result['distance_meters']}米")
            if details:
                lines.append(f"{result.get('origin_name', '起点')}→{result.get('destination_name', '终点')}：{'，'.join(details)}。")
        return '\n'.join(lines)
