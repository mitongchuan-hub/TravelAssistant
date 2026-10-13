"""Temporary instrumentation for isolated latency runs; restores every patched hook."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import date, timedelta
from functools import wraps
import importlib
import json
import re
from time import sleep
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

EXTERNAL = {'query_weather', 'search_places', 'search_food', 'query_route'}


def completion(content='', calls=()):
    message = SimpleNamespace(content=content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
        tool_calls=[SimpleNamespace(id=f'latency-call-{i}', function=SimpleNamespace(name=name,
            arguments=json.dumps(args, ensure_ascii=False))) for i, (name, args) in enumerate(calls)])
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason='stop')], usage=None)


def resolve_tool_arguments(arguments, messages):
    catalog = {}
    for message in messages:
        if message.get('role') == 'tool':
            try:
                result = json.loads(message['content'])
            except (ValueError, TypeError):
                continue
            for item in result.get('candidates', []):
                catalog[item['name']] = item['poi_id']
    names = {'$station': '杭州东站', '$lake': '西湖湖滨'}
    return {k: catalog.get(names[v], 'unmatched-' + v[1:]) if isinstance(v, str) and v in names else v
            for k, v in arguments.items()}


class ScriptedModel:
    """Exercise the production loop offline; never a measure of real model speed/quality."""
    def __init__(self, case, step):
        self.case, self.step = case, step
        self.counts = {}

    def __call__(self, params, role):
        messages = params['messages']
        payload = json.loads(messages[1]['content'])
        user = payload.get('user_message', '')
        if role == 'main':
            return completion({'scope': '', 'question': None, **self.step['simulation_route'], 'user_quote': user})
        if role == 'chat':
            return completion('我可以整理旅行需求、查询资料、规划和修改行程。')
        if role == 'display':
            source, dates = payload['source_text'], payload['source_dates']
            return completion({'schema_version': '1.1', 'weather': [], 'title': source.splitlines()[0], 'overview': None,
                'budget_note': None, 'days': [{'date': d, 'label': f'第{i+1}天', 'theme': None,
                    'items': [{'time_text': '午后', 'title': '原文安排', 'category': 'activity', 'location': None,
                        'duration_text': None, 'description': source, 'cost_text': None, 'notes': []}]}
                    for i, d in enumerate(dates)], 'preparation': {'clothing': [], 'accommodation': []},
                'pending_items': [], 'risks': []})
        index = self.counts.get(role, 0)
        self.counts[role] = index + 1
        changes = self.step.get('simulation_changes')
        if changes and index == 0:
            return completion(calls=[('update_requirements', {'user_quote': user, 'changes': changes})])
        prefix = 1 if changes else 0
        if role == 'revision' and index == prefix:
            calls = [('read_context', {'section': 'plan'})]
            if payload.get('action') == 'modify':
                calls.append(('check_requirements', {'user_quote': user}))
            return completion(calls=calls)
        if role == 'planning' and not payload.get('requirements_checked') and index == prefix:
            return completion(calls=[('check_requirements', {'user_quote': user})])
        prefix += int(role == 'revision' or role == 'planning' and not payload.get('requirements_checked'))
        # Once a tool marks requirements checked, the initial payload remains the same throughout the loop.
        specs = self.case.get('simulation_tools', [])
        tool_index = index - prefix
        if 0 <= tool_index < len(specs):
            spec = specs[tool_index]
            return completion(calls=[(spec['name'], resolve_tool_arguments(spec['arguments'], messages))])
        if role == 'research':
            return completion('查询结果已整理；资料来自本次工具，未提供的开放、价格与过敏安全仍待核实。')
        if role == 'planning' and payload.get('action') == 'update_requirements':
            return completion('已记录本轮需求，当前行程未修改。')
        if role == 'revision':
            current = ''
            for message in messages:
                if message.get('role') == 'tool' and message.get('name') == 'read_context':
                    current = json.loads(message['content']).get('plan', {}).get('text', '')
            if payload.get('action') == 'explain':
                return completion('午后保留休息与轻松节奏一致；原文未记录的决策依据未知。')
            append = self.step.get('expect', {}).get('text_append')
            if append is not None:
                return completion(current + append)
            if self.case['id'] == 'LAT-018':
                return completion(current.replace('午后西湖湖滨散步', '午后原地休息'))
            return completion(current + '\n节奏调整：午后增加原地休息，所有硬约束保留。')
        dates = payload['travel_window']['dates']
        text = '杭州旅行草案\n' + '\n'.join(f'{d}：上午西湖湖滨散步，中午用餐不吃辣，午后原地休息。接驳、开放与价格待核实。' for d in dates)
        return completion(text + '\n预算为分配额度，交通缓冲为估算。')


def synthetic_payload(service, args, base_date):
    if service == 'weather':
        if 'geocoding' in args[0]:
            return {'results': [{'name': '杭州', 'country': '合成测试资料', 'latitude': 30.25, 'longitude': 120.15}]}
        days = [(base_date + timedelta(days=i)).isoformat() for i in range(16)]
        return {'timezone': 'Asia/Shanghai', 'daily': {'time': days, 'weather_code': [1]*16,
            'temperature_2m_max': [22]*16, 'temperature_2m_min': [16]*16, 'precipitation_probability_max': [20]*16}}
    if service == 'routes':
        return {'status': '1', 'infocode': '10000', 'route': {'paths': [{'duration': '2280', 'distance': '12000'}],
                                                          'transits': [{'duration': '2280', 'distance': '12000'}]}}
    if service == 'food':
        return {'status': '1', 'infocode': '10000', 'pois': [{'id': 'lat-food', 'name': '合成杭帮菜候选',
            'address': '合成地址', 'biz_ext': {'rating': '4.5', 'cost': '80'}}]}
    keyword = args[0].get('keywords', '')
    names = [('杭州东站', 'lat-station', '120.212,30.291'), ('西湖湖滨', 'lat-lake', '120.153,30.249')]
    return {'status': '1', 'infocode': '10000', 'pois': [{'id': pid, 'name': name, 'location': loc,
        'address': '合成地址', 'cityname': '杭州', 'adname': '合成区域'}
        for name, pid, loc in names if name in keyword or keyword in name]}


def mark_synthetic(value):
    if isinstance(value, dict):
        # The current catalog contract requires this provider identifier on POIs.
        # Keep it on synthetic candidates, and label the whole response as synthetic.
        poi = 'poi_id' in value and 'longitude' in value
        return {k: (v if poi else '受控合成资料（不是真实旅行事实）') if k == 'source'
                else mark_synthetic(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mark_synthetic(v) for v in value]
    return value


class Instrumentation:
    def __init__(self, timeline, case, step, *, tools_mode, base_date, trace_sink, simulate=False):
        self.t = timeline
        self.case, self.step = case, step
        self.tools_mode, self.base_date = tools_mode, base_date
        self.trace_sink = trace_sink
        self.simulator = ScriptedModel(case, step) if simulate else None
        self.patches = ExitStack()
        self.fault_hits = []

    def set(self, target, name, value):
        self.patches.enter_context(patch.object(target, name, value))

    def wrap(self, target, method, category, label=None, details=None, after=None):
        original = getattr(target, method)
        @wraps(original)
        def wrapped(*args, **kwargs):
            metadata = details(*args, **kwargs) if details else {}
            with self.t.span(label or method, category, **metadata) as span:
                result = original(*args, **kwargs)
                if after:
                    after(span, result, args, kwargs)
                return result
        self.set(target, method, wrapped)

    def __enter__(self):
        from app import agent_client, agent_runtime, plan_display
        from app.store import DemoStore
        store_module = importlib.import_module('app.store')
        original_init = agent_runtime.TravelAgentRuntime.__init__
        def init(runtime, client, model, snapshot, **kwargs):
            with self.t.span('构建工作流快照与角色', 'context'):
                original_init(runtime, client, model, dict(snapshot, current_date=self.base_date.isoformat()), **kwargs)
        self.set(agent_runtime.TravelAgentRuntime, '__init__', init)
        original_add = agent_client.ConversationTrace.add
        def add(trace, title, value):
            with self.t.span('组织日志：' + title, 'logging', cache_hit=title == '查询缓存复用'):
                original_add(trace, title, value)
            if title in {'意图识别结果', '文字行程', '展示转换', '共享调用预算', 'Workflow 返回结果',
                         '思考参数降级', '模型请求重试', '调用异常', 'Agent Loop 状态'}:
                self.t.event(title, value=deepcopy(value))
        self.set(agent_client.ConversationTrace, 'add', add)
        def write(trace, error=None):
            if error:
                trace.add('调用异常', type(error).__name__ + ': ' + str(error))
            self.trace_sink(trace)
        self.set(agent_client.ConversationTrace, 'write', write)
        request = agent_client._create_chat_completion
        def timed_request(client, model, messages, **kwargs):
            role = kwargs.get('agent_role')
            budget = kwargs.get('budget')
            token = self.t.role.set(role)
            try:
                with self.t.span(f'模型逻辑调用：{role}', 'model_overhead', role=role, model=model,
                    message_count=len(messages), input_characters=sum(len(str(m.get('content') or '')) for m in messages),
                    tools=[s['function']['name'] for s in kwargs.get('tools') or []],
                    reasoning_requested=kwargs.get('reasoning_effort'), budget_before=max(0, budget.deadline-agent_runtime.monotonic()) if budget else None) as row:
                    try:
                        return request(client, model, messages, **kwargs)
                    finally:
                        row['metadata']['budget_after'] = max(0, budget.deadline-agent_runtime.monotonic()) if budget else None
            finally:
                self.t.role.reset(token)
        self.set(agent_client, '_create_chat_completion', timed_request)
        transport = agent_client.create_completion
        def timed_transport(client, params, api):
            role = self.t.role.get()
            with self.t.span(f'SDK请求：{role}', 'model_' + str(role), role=role, wire_api=api,
                timeout_seconds=params.get('timeout'), reasoning_sent=params.get('reasoning_effort'),
                response_format_type=(params.get('response_format') or {}).get('type'),
                input_characters=sum(len(str(m.get('content') or '')) for m in params['messages']),
                schema_characters=len(json.dumps(params.get('response_format') or {}, ensure_ascii=False)),
                source='scripted' if self.simulator else 'live_model') as row:
                if role == 'display' and self.case.get('faults', {}).get('display_timeout'):
                    self.fault_hits.append('display_timeout')
                    row['metadata']['source'] = 'injected_fault'
                    with self.t.span('受控展示超时等待', 'injected_wait'):
                        sleep(self.case['faults']['delay_seconds'])
                    raise TimeoutError('Injected display timeout; not an upstream timeout')
                result = self.simulator(params, role) if self.simulator else transport(client, params, api)
                message = result.choices[0].message
                usage = getattr(result, 'usage', None)
                details = getattr(usage, 'completion_tokens_details', None)
                row['metadata'].update(output_characters=len(message.content or ''),
                    prompt_tokens=getattr(usage, 'prompt_tokens', None), completion_tokens=getattr(usage, 'completion_tokens', None),
                    reasoning_tokens=getattr(details, 'reasoning_tokens', None),
                    finish_reason=getattr(result.choices[0], 'finish_reason', None),
                    proposed_tools=[{'name': call.function.name, 'arguments': call.function.arguments} for call in message.tool_calls or []],
                    provider_timings=getattr(result, 'timings', None))
                return result
        self.set(agent_client, 'create_completion', timed_transport)
        runtime = agent_runtime.TravelAgentRuntime
        for name, category, label in [('classify', 'orchestration', '意图识别与结构校验'),
            ('run_role', 'orchestration', '业务Agent Loop'), ('convert_display', 'display_overhead', '展示转换与校验'),
            ('read_context', 'context', '读取上下文'), ('_initial_messages', 'context', '准备业务角色输入')]:
            self.wrap(runtime, name, category, label,
                details=(lambda runtime, role, *a, **kw: {'role': role}) if name == 'run_role' else None)
        tool = runtime.execute_tool
        def execute(runtime, role, name, arguments):
            with self.t.span('工具：' + name, 'external_tool_overhead' if name in EXTERNAL else 'internal_tool',
                             role=role, tool=name, arguments=deepcopy(arguments)) as row:
                result = tool(runtime, role, name, arguments)
                output = result[0]
                row['metadata'].update(result_status=output.get('status') if isinstance(output, dict) else None,
                    result_characters=len(json.dumps(output, ensure_ascii=False, default=str)))
                return result
        self.set(runtime, 'execute_tool', execute)
        external = agent_client.run_tool
        def dispatch(name, arguments, **kwargs):
            cache = kwargs.get('route_cache') or {}
            before_keys = set(cache)
            network_before = sum(s['category'] in {'external_io', 'controlled_io'} for s in self.t.spans)
            with self.t.span('外部工具分发：' + name, 'external_tool_overhead', tool=name,
                             backend_mode=self.tools_mode) as row:
                if self.case.get('faults', {}).get('external_unavailable'):
                    self.fault_hits.append('external_unavailable')
                    with self.t.span('受控外部不可用等待', 'injected_wait', tool=name):
                        sleep(self.case['faults']['delay_seconds'])
                    result = {'status': 'unavailable', 'source': '受控故障', 'reason': '注入外部不可用，无旅行事实。'}
                else:
                    result = external(name, arguments, **kwargs)
                    if self.tools_mode == 'controlled':
                        result = mark_synthetic(result)
                        result['benchmark_note'] = '此响应为合成资料；候选POI的source字段只保留应用识别协议，不是实际查询证据。'
                network_count = sum(s['category'] in {'external_io', 'controlled_io'} for s in self.t.spans) - network_before
                cache_hit = False
                if name == 'query_weather':
                    cache_hit = ('weather_forecast', str(arguments.get('city', '')).strip().casefold()) in before_keys and network_count == 0
                elif name == 'query_route':
                    catalog = kwargs.get('place_catalog', {})
                    origin, destination = catalog.get(arguments.get('origin_poi_id')), catalog.get(arguments.get('destination_poi_id'))
                    if origin and destination:
                        key = (origin['poi_id'], destination['poi_id'], origin['longitude'], origin['latitude'],
                               destination['longitude'], destination['latitude'], arguments.get('mode', 'transit'))
                        cache_hit = key in before_keys and network_count == 0
                row['metadata'].update(result_status=result.get('status'), result_reason=result.get('reason'),
                    backend_calls=network_count, cache_hit=cache_hit,
                    no_backend_call=network_count == 0, injected=bool(self.case.get('faults', {}).get('external_unavailable')))
                return result
        self.set(agent_client, 'run_tool', dispatch)
        for service in ('weather', 'places', 'food', 'routes'):
            module = importlib.import_module('app.tools.' + service)
            original = module._fetch_json
            def fetch(*args, _original=original, _service=service, **kwargs):
                endpoint = urlsplit(args[0]).netloc + urlsplit(args[0]).path if _service == 'weather' else 'mcp.amap.com/mcp'
                category = 'controlled_io' if self.tools_mode == 'controlled' else 'external_io' if _service == 'weather' else 'mcp_adapter'
                with self.t.span('服务获取与解析：' + _service, category,
                                 service=_service, endpoint=endpoint, source=self.tools_mode) as row:
                    result = synthetic_payload(_service, args, self.base_date) if self.tools_mode == 'controlled' else _original(*args, **kwargs)
                    row['metadata']['payload_characters'] = len(json.dumps(result, ensure_ascii=False))
                    return result
            self.set(module, '_fetch_json', fetch)
        if self.tools_mode != 'controlled':
            from app.tools.amap_mcp import MCPTools
            self.wrap(MCPTools, 'call', 'external_io', '高德MCP工具调用',
                details=lambda client, name, arguments: {'mcp_tool': name, 'endpoint': 'mcp.amap.com/mcp'},
                after=lambda row, result, args, kwargs: row['metadata'].update(payload_characters=len(json.dumps(result, ensure_ascii=False))))
        if self.tools_mode == 'controlled':
            self.patches.enter_context(patch.dict('os.environ', {'AMAP_MAP_KEY': 'latency-synthetic-only', 'AMAP_MAPS_API_KEY': ''}))
        for target, name, category, label in [
            (store_module, 'generate_agent_reply', 'context', '准备模型客户端、快照与清理'),
            (agent_client, '_build_chat_context', 'context', '聊天上下文序列化'),
            (DemoStore, '_generate_reply', 'context', '读取Store快照并校验模型交付'),
            (agent_runtime, 'planning_brief', 'validation', '需求完整性与日期窗口'),
            (agent_runtime, 'chat_changes', 'validation', '聊天需求候选校验'),
            (agent_runtime, 'update_requirements', 'validation', '工作流需求更新校验'),
            (agent_runtime, 'validate_display', 'validation', '展示Schema与日期校验'),
            (plan_display, 'validate_display', 'validation', '保存前展示Schema与日期校验'),
            (DemoStore, '_apply_text_plan', 'commit', '替换当前原文、展示与依据'),
            (DemoStore, 'run_display_job', 'orchestration', '后台展示任务'),
            (store_module, 'serialize_state', 'serialization', 'Store快照序列化'),
            (store_module, 'save_snapshot', 'persistence', 'SQLite事务写入'),
        ]:
            self.wrap(target, name, category, label)
        def committed(row, result, args, kwargs):
            reply = args[2]
            self.t.event('commit_finished', failed=result.failed, needs_input=result.needs_input,
                         has_plan_text=bool(reply.plan_text), plan_id=args[0].plans[args[1]].id)
        self.wrap(DemoStore, '_commit_agent_result', 'commit', '原子保存边界与快照冲突检查', after=committed)
        def persisted(row, result, args, kwargs):
            row['metadata']['effective_write'] = args[0].persistence_enabled and not getattr(args[0], '_defer_persist', 0)
            if row['metadata']['effective_write']:
                local = args[0]
                self.t.event('sqlite_snapshot_saved', plan_ids={tid: plan.id for tid, plan in local.plans.items()})
        self.wrap(DemoStore, '_persist', 'persistence', 'SQLite全量快照保存', after=persisted)
        def published(row, result, args, kwargs):
            job = args[1]
            if result and job['status'] in {'succeeded', 'failed'}:
                self.t.event('cards_published', plan_id=job['plan_id'], job_id=job['id'], status=job['status'])
        self.wrap(DemoStore, '_publish_display', 'commit', '发布当前原文的卡片', after=published)
        for name in ('finish_agent_task', 'request_revision'):
            self.wrap(DemoStore, name, 'orchestration', '任务处理：' + name,
                after=lambda row, result, args, kwargs: self.t.event('reply_store_ready',
                    message_id=result.id if result else None, message_status=result.status if result else None))
        return self

    def __exit__(self, *exc):
        return self.patches.__exit__(*exc)
