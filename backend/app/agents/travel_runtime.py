"""单旅行 Agent：一个上下文、原生地图与独立天气工具、一次终止提交。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
import json
from time import monotonic
from typing import Any, Mapping

from jsonschema import ValidationError

from app.application.contracts import RequestContext
from app.application.contracts import ExecutionResult
from app.agents.roles import travel_role, role_reasoning_effort
from app.agents.budget import AgentBudgetError, TurnBudget
from app.agents.inputs.context import project_main_context
from app.agents.inputs.execution import TaskExecutionContext
from app.agents.tools.read_context import read_context
from app.agents.tools.save_trip_changes import save_trip_changes
from app.agents.model_transport import IncompleteModelOutput
from app.domain.lodging import collect_places
from app.domain.itinerary.prerequisites import required_information
from app.services.maps.evidence import (ROUTE_TOOLS, PLACE_TOOLS, SEARCH_TOOLS,
    evidence_kind, evidence_result, place_messages, successful_detail_place)
from app.services.maps.places import normalize_pois
from app.services.maps.routes import normalize_route
from app.agents.tools import registry as tools
from app.workflow.counters import normalize_workflow


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
        # Keep only detail-derived center coordinates, separate from search catalog.
        self.detail_centers = {}
        self.endpoint_refs = {}
        for row in saved or []:
            self._register_endpoints(row)
            place = successful_detail_place(row)
            if place:
                self.detail_centers[place['poi_id']] = place
                self._merge_places({place['poi_id']: place})
                self.search_ids.add(place['poi_id'])
            if isinstance(row, dict) and row.get('tool') in SEARCH_TOOLS:
                self.search_ids.update(self._search_ids(row.get('result')))
        self.registry = tools.agent_tool_registry(
            read_context=partial(read_context, request_context=self.request_context,
                execution_context=self.execution_context, trip=self.trip, plan=self.plan, evidence=self.evidence),
            save_trip_changes=partial(save_trip_changes, request_context=self.request_context,
                execution_context=self.execution_context, evidence=self.evidence, budget=self.budget),
            query=self._query)
        self._map_handlers = {name: spec.handler for name, spec in self.registry.items()
                              if name in PLACE_TOOLS or name in ROUTE_TOOLS}
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
                return self.registry['save_trip_changes'].execute({'user_quote': self.request_context.body,
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
                used_ids = {row.get('tool_call_id') for row in self.messages if row['role'] == 'tool'}
                for call in calls:
                    call_id = getattr(call, 'id', None)
                    if not isinstance(call_id, str) or not call_id.strip() or call_id in used_ids:
                        return ExecutionResult.failure('模型工具调用标识缺失或重复，未执行本次工具。')
                    used_ids.add(call_id)
                self.messages.append(self._assistant_message(assistant, calls))
                result = self._execute_batch(calls)
                if result is not None:
                    return result
            return ExecutionResult.failure('本轮执行步数已达上限，未保存任何修改。')
        except (AgentBudgetError, TimeoutError, IncompleteModelOutput) as exc:
            return ExecutionResult.failure(str(exc))
        except Exception as exc:
            self.event('Task Runtime异常', {'error_type': type(exc).__name__, 'reason': str(exc)[:500]})
            return ExecutionResult.failure('本次执行未完成，当前需求和行程已保留。')
        finally:
            self.event('Task Runtime预算', self.usage)
            self.event('Task Runtime耗时', {'seconds': round(monotonic() - started, 3)})

    def _execute_batch(self, calls):
        """Only map I/O runs in workers; trusted state and transcript stay on this thread."""
        pending = []
        invalid_limit = False

        def receipt(call, output=None, error=None):
            nonlocal invalid_limit
            if error is not None:
                if isinstance(error, IncompleteModelOutput) or not isinstance(
                        error, (ValueError, TypeError, KeyError, ValidationError)):
                    raise error
                reason = error.message if isinstance(error, ValidationError) else str(error)
                invalid_limit = self._record_failure(call.function.name, reason) or invalid_limit
                output = {'status': 'invalid_arguments', 'reason': reason, 'recoverable': True}
            self.messages.append({'role': 'tool', 'tool_call_id': call.id,
                                  'content': json.dumps(output, ensure_ascii=False, default=str)})

        def flush():
            # Consume in model order, even when I/O completed out of order.
            rows, pending[:] = list(pending), []
            for call, spec, prepared, future, started in rows:
                output, error = None, None
                try:
                    output = self._finalize_query(prepared, future.result())
                except Exception as exc:
                    error = exc
                self.event('工具执行', {'role': self.config.name, 'name': spec.name,
                    'arguments': prepared['arguments'], 'seconds': round(monotonic() - started, 3),
                    'result': output, 'error_type': type(error).__name__ if error else None})
                receipt(call, output, error)

        with ThreadPoolExecutor(max_workers=3, thread_name_prefix='travel-map') as pool:
            for index, call in enumerate(calls):
                spec = self.registry.get(call.function.name)
                is_map = (spec is not None and spec.name in self._map_handlers
                          and spec.handler is self._map_handlers[spec.name])
                if not is_map:
                    flush()  # Local, weather and terminal tools are execution barriers.
                try:
                    self.budget.tools(1)
                    if spec is None:
                        raise ValueError('未知工具')
                    arguments = json.loads(call.function.arguments or '{}')
                    self.budget.remaining()
                    if is_map:
                        spec.validate(arguments)
                        try:
                            prepared = self._prepare_query(spec.name, arguments)
                        except (ValueError, TypeError, KeyError):
                            if not pending:
                                raise
                            # Earlier searches/details may authorize this call, never guess IDs/refs.
                            flush()
                            prepared = self._prepare_query(spec.name, arguments)
                        if any(row[2]['key'] == prepared['key'] for row in pending):
                            flush()
                            prepared = self._prepare_query(spec.name, arguments)
                        if 'cached' in prepared:
                            flush()
                            receipt(call, prepared['cached'])
                            continue
                        started = monotonic()
                        self.event('工具开始', {'role': self.config.name, 'name': spec.name, 'arguments': arguments})
                        future = pool.submit(copy_context().run, self._query_io, prepared)
                        pending.append((call, spec, prepared, future, started))
                        continue
                    if spec.terminal and invalid_limit:
                        return ExecutionResult.failure('无效工具调用重复或纠正次数已达上限，未保存任何修改。')
                    output = self._execute_tool(spec, arguments)
                    if spec.terminal:
                        for skipped in calls[index + 1:]:
                            self.event('工具跳过', {'name': skipped.function.name,
                                'tool_call_id': skipped.id, 'status': 'skipped_due_to_terminal'})
                        return output
                    self.budget.remaining()
                except (ValueError, TypeError, KeyError, ValidationError) as exc:
                    flush()
                    receipt(call, error=exc)
                else:
                    receipt(call, output)
            flush()
        if invalid_limit:
            return ExecutionResult.failure('无效工具调用重复或纠正次数已达上限，未保存任何修改。')
        return None

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

    @staticmethod
    def _search_ids(output):
        if not isinstance(output, dict) or output.get('status') not in (None, '1', 1, 'available') or output.get('error'):
            return set()
        pois = output.get('pois')
        return {poi['id'] for poi in pois if isinstance(poi, dict)
                and isinstance(poi.get('id'), str) and poi['id'].strip()} if isinstance(pois, list) else set()

    def _around_center(self, value):
        # Reuse coordinate matching, but never authorize a search-only catalog hit.
        self._known_coordinate(value)
        lon, lat = map(float, value.split(','))
        import math
        if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError('周边中心坐标无效')
        centers = list(self.detail_centers.values())
        if self.trip.lodging:
            centers.append(self.trip.lodging)
        matches = [p for p in centers if p.get('longitude') == lon and p.get('latitude') == lat]
        if not matches:
            raise ValueError('周边中心须来自固定住宿或成功查询同ID详情的真实中心坐标')
        if not self.chat_entry:
            city = self.trip.requirements.city.removesuffix('市')
            if not any(p.get('city', '').removesuffix('市') == city for p in matches):
                raise ValueError('同城周边查询须使用住宿所在城市的真实中心')

    def _known_coordinate(self, value):
        try:
            lon, lat = (float(part) for part in value.split(','))
        except (AttributeError, TypeError, ValueError):
            raise ValueError('路线坐标必须来自已查询的真实地点') from None
        matches = [place for place in self.catalog.values()
                   if place.get('longitude') == lon and place.get('latitude') == lat]
        if not matches:
            raise ValueError('路线坐标必须来自已查询的真实地点，请先搜索并查询详情')
        if len({place['poi_id'] for place in matches}) != 1:
            raise ValueError('相同坐标对应多个地点，请使用成功详情的center ref明确身份')
        return matches[0]

    def _register_endpoints(self, row):
        from app.services.maps.evidence import detail_endpoints
        place = successful_detail_place(row)
        for endpoint in detail_endpoints(row):
            self.endpoint_refs[endpoint['ref']] = {**place,
                'longitude': endpoint['longitude'], 'latitude': endpoint['latitude'],
                'endpoint_ref': endpoint['ref'], 'endpoint_kind': endpoint['kind'],
                'evidence_ref': endpoint['evidence_ref']}

    def _route_endpoint(self, args, side):
        ref, coords = args.get(side + '_ref'), args.get(side)
        if ref is None:
            return self._known_coordinate(coords)
        point = self.endpoint_refs.get(ref)
        if point is None:
            raise ValueError('路线端点ref无效或不属于当前可见详情证据')
        if (side == 'origin' and point['endpoint_kind'] == 'entrance'
                or side == 'destination' and point['endpoint_kind'] == 'exit'):
            raise ValueError('路线出入口用途冲突：出发使用出口，到达使用入口')
        if coords is not None:
            try:
                valid = tuple(map(float, coords.split(','))) == (point['longitude'], point['latitude'])
            except (ValueError, AttributeError):
                valid = False
            if not valid:
                raise ValueError('路线端点ref与坐标冲突')
        return deepcopy(point)

    def _query(self, name, args):
        prepared = self._prepare_query(name, args)
        if 'cached' in prepared:
            return prepared['cached']
        return self._finalize_query(prepared, self._query_io(prepared))

    def _prepare_query(self, name, args):
        self.budget.remaining()
        city = self.trip.requirements.city.removesuffix('市')
        if name not in ROUTE_TOOLS and name not in PLACE_TOOLS and name != 'query_weather':
            raise ValueError('未知查询工具')
        if not self.chat_entry and name in {'maps_text_search', 'query_weather'}:
            if not args.get('city') or args['city'].removesuffix('市') != city:
                raise ValueError('同城查询须使用当前住宿所在城市。')
        if name == 'maps_search_detail' and args.get('id') not in self.search_ids:
            raise ValueError('地点详情只接受本轮搜索或已保存地点返回的真实ID')
        if name == 'maps_around_search':
            self._around_center(args.get('location'))
        endpoints = None
        if name in ROUTE_TOOLS:
            endpoints = [self._route_endpoint(args, side) for side in ('origin', 'destination')]
            args = deepcopy(args)
            for side, point in zip(('origin', 'destination'), endpoints):
                args[side] = f"{point['longitude']},{point['latitude']}"
            if not self.chat_entry and any(point.get('city', '').removesuffix('市') != city for point in endpoints):
                raise ValueError('同城路线须使用住宿所在城市的真实地点')
            if ROUTE_TOOLS[name] == 'transit':
                for key, point in zip(('city', 'cityd'), endpoints):
                    if args.get(key, '').removesuffix('市') != point.get('city', '').removesuffix('市'):
                        raise ValueError('公交城市须与已查询地点所在城市一致')
        key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
        if key in self.external_results:
            self.event('查询缓存复用', {'tool': name, 'scope': 'current_turn'})
            return {'name': name, 'arguments': args, 'key': key,
                    'cached': deepcopy(self.external_results[key])}
        return {'name': name, 'arguments': deepcopy(args), 'key': key, 'endpoints': endpoints,
                'started': monotonic()}

    def _query_io(self, prepared):
        name, args = prepared['name'], prepared['arguments']
        from app.services.maps.amap_mcp import budget_scope
        from app.agents.tools.weather import budget_scope as weather_scope
        scope = weather_scope if name == 'query_weather' else budget_scope
        local_departure_rejection = (name == 'maps_direction_transit_integrated'
                                     and args.get('departure_at') is not None)
        if name != 'query_weather' and not local_departure_rejection:
            self.budget.external()
        with scope(self.budget):
            output = tools.run_tool(name, args, place_catalog=self.catalog, route_cache=self.cache)
        return output

    def _finalize_query(self, prepared, output):
        name, args, key = prepared['name'], prepared['arguments'], prepared['key']
        endpoints, started = prepared['endpoints'], prepared['started']
        self.budget.remaining()
        normalized = output
        if name in PLACE_TOOLS:
            if name in SEARCH_TOOLS:
                self.search_ids.update(self._search_ids(output))
            elif output.get('status') != 'unavailable' and output.get('id') != args['id']:
                raise ValueError('地图详情返回的ID与请求不一致')
            if name == 'maps_search_detail':
                place = successful_detail_place({'tool': name, 'arguments': args, 'result': output})
                if place:
                    self.detail_centers[place['poi_id']] = place
                    row = {'tool': name, 'arguments': args, 'result': output}
                    self._register_endpoints(row)
                    from app.services.maps.evidence import detail_endpoints
                    # Presentation fields stay outside the immutable raw result used for refs.
                    output = {**output, 'navigation_endpoints': detail_endpoints(row)}
            normalized = normalize_pois(output, include_unlocated=name == 'maps_around_search')
            self._merge_places(collect_places([{'role': 'tool', 'name': 'search_places',
                'content': json.dumps(normalized, ensure_ascii=False)}]))
        elif endpoints:
            # Decode only for business evidence. LLM receives the full native result.
            normalized = normalize_route(output if 'status' in output or 'route' in output else {'route': output},
                                         *endpoints, mode=ROUTE_TOOLS[name])
        if endpoints:
            output = {**output, 'route_estimate': deepcopy(normalized)}
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
