"""Opt-in six-task skill ablation; default only freezes offline artifacts.

Use the project's conda interpreter. No maps, weather, Store or commit workflow is
used. Synthetic handler replacement deliberately does NOT test production query
validation, endpoint registration or provider evidence provenance. Human review
of dates, preserved sections, tradeoffs and itinerary quality is required.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import signal
import sys
from time import monotonic
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app.agents.budget import AgentBudgetError, TurnBudget
from app.agents.inputs.execution import TaskExecutionContext
from app.agents.model_client import request_completion
from app.agents.model_transport import response_storage_enabled, wire_api
from app.agents.roles import role_reasoning_effort, travel_role
from app.agents.skills import read_skill_resource, skill_catalog, skill_catalog_prompt
from app.agents.travel_runtime import TaskExecutionRuntime
from app.application.contracts import RequestContext, serialize
from app.core import config, resources
from app.domain.itinerary.identity import plan_basis
from app.domain.models import Message, Trip, TripPlan, TripRequirements, User

EVAL_PATH = ROOT / 'backend/app/agents/skills/travel-planning/evals/evals.json'
LIMITS = dict(max_models=24, max_tools=24, max_external=0, seconds=360,
              total_token_limit=180000)
EXTERNAL = frozenset({'maps_text_search', 'maps_around_search', 'maps_search_detail',
    'maps_direction_walking', 'maps_direction_driving',
    'maps_direction_transit_integrated', 'query_weather'})
REGISTRY = EXTERNAL | {'read_context', 'read_skill', 'save_trip_changes'}
NOTE = ('Skill ablation of the CURRENT resident safety/output prompt, not the old '
        'monolithic no-skill baseline. All place, route and weather facts are synthetic. '
        'External ToolSpec handlers are replaced, bypassing runtime._query; production '
        'evidence provenance, coordinate/endpoint registration and real feasibility are '
        'NOT validated. save_trip_changes is real local candidate validation only; '
        'no Store, persistence, display generation, retries, fallback or LLM judge. '
        'Tokens are a soft cumulative allowance per task; one response can exceed it '
        'and is then discarded. Missing provider usage cannot establish token coverage.')

# Explicit synthetic coordinates and directional routes, never real map sources.
PLACES = [
    ('hotel', '合成湖畔住宿', '120.15,30.25'),
    ('west', '西湖合成游览点', '120.14,30.24'),
    ('lingyin', '灵隐寺合成游览点', '120.10,30.24'),
    ('longjing', '龙井村合成游览点', '120.10,30.21'),
    ('museum', '杭州合成博物馆', '120.16,30.26'),
    ('lunch', '合成杭帮菜午餐店', '120.14,30.25'),
    ('shopping', '合成购物街区', '120.16,30.24'),
]
# origin, destination, reference minutes, walking meters, reference fare per person.
LEGS = [('hotel', 'west', 25, 450, 3), ('west', 'lunch', 20, 350, 2),
    ('lunch', 'shopping', 25, 400, 3), ('shopping', 'hotel', 20, 300, 2),
    ('hotel', 'lingyin', 60, 900, 6), ('lingyin', 'lunch', 55, 800, 6),
    ('lunch', 'longjing', 65, 1100, 6), ('longjing', 'hotel', 70, 1200, 6),
    ('lunch', 'museum', 35, 600, 3), ('museum', 'hotel', 30, 500, 3),
    ('lingyin', 'longjing', 60, 1200, 6), ('longjing', 'museum', 80, 1400, 7),
    ('west', 'lingyin', 55, 900, 6)]
OLD_PLAN = """# 杭州两日合成行程（已保存完整正文）
共同要求：不吃辣。第二天午餐地点已确定。每天09:00从合成湖畔住宿出发，20:00前返回。
## 第一天 2030-05-10
09:00 合成湖畔住宿出发，公交参考25分钟、步行450米，09:25到西湖合成游览点。
09:25—11:30 西湖游览，10:30—10:50坐下休息。
11:30出发，公交参考20分钟、步行350米到合成杭帮菜午餐店。
12:00—13:00 午餐：杭帮菜，不吃辣；菜品、价格和营业待核实。
13:00—13:30 午间休息。13:30出发，公交参考25分钟、步行400米到合成购物街区。
14:00—16:00 逛街与休息；16:00返回住宿，公交参考20分钟、步行300米。
16:30—18:00 住宿休息，18:00—19:00住宿附近晚餐（具体店铺待核实），19:00回原住宿。
## 第二天 2030-05-11
09:00 合成湖畔住宿出发，公交参考60分钟、步行900米到灵隐寺合成游览点。
10:00—11:00 灵隐游览，门票与预约待核实。
11:00出发，公交参考55分钟、步行800米到合成杭帮菜午餐店。
12:00—13:00 午餐（已确定地点）：合成杭帮菜午餐店。不吃辣；不改此项。
13:00—13:30 原地休息。13:30出发，公交参考65分钟、步行1100米到龙井村合成游览点。
14:35—16:00 龙井村游览，15:15—15:35休息。
16:00返回合成湖畔住宿，公交参考70分钟、步行1200米，17:10回原住宿。
18:00—19:00住宿附近晚餐（具体店铺待核实），19:00回原住宿。
所有交通数值仅为合成方向性参考，不代表2030年班次。开放、票价、预约、晚餐及无辣可供应均待核实。
"""


def digest(value):
    return sha256((value if isinstance(value, bytes) else
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode())).hexdigest()


def load_cases():
    cases = json.loads(EVAL_PATH.read_text('utf-8'))['evals']
    if len(cases) != 3 or [c['id'] for c in cases] != [1, 2, 3]:
        raise ValueError('Expected exactly the existing three eval scenarios')
    return cases


def prompt_variants():
    full = travel_role().prompt
    base = (ROOT / 'backend/app/agents/prompts/travel.txt').read_text('utf-8')
    paragraphs = base.split('\n\n')
    loading = [p for p in paragraphs if p.startswith('生成、全局重排或实质性局部修改时，')
               or p.startswith('仅服务器白名单 read_skill 正常返回的本地内容')]
    if len(loading) != 2:
        raise ValueError('Skill guidance paragraphs changed; review ablation')
    edits = [{'old': p, 'new': ''} for p in loading]
    edits += [{'old': '\n\n' + skill_catalog_prompt(), 'new': ''},
              {'old': 'read_context、read_skill、query_weather', 'new': 'read_context、query_weather'},
              {'old': '；正文组织按已加载规划技能执行', 'new': '；正文组织按用户要求执行'}]
    without = full
    for edit in edits:
        if without.count(edit['old']) != 1:
            raise ValueError('Ablation text changed; manual review required')
        without = without.replace(edit['old'], edit['new'], 1)
    if 'read_skill' in without or '已加载规划技能' in without:
        raise ValueError('Residual skill loading instruction')
    return full, without, edits


def fixtures_for(case):
    details = []
    for poi_id, name, location in PLACES:
        details.append(dict(status='available', id=poi_id, name=name, city='杭州',
            address='合成杭州夹具地址-' + poi_id, location=location,
            source='synthetic_fixture', metadata={'provider': 'synthetic_fixture',
            'fetched_at': '2030-05-09T00:00:00+08:00'},
            note='仅合成事实；营业、门票、预约、菜单、价格及未来开放未知。'))
    places = {p['id']: p for p in details}
    routes = []
    for origin, destination, minutes, walking, fare in LEGS:
        routes.append(dict(status='available', origin_poi_id=origin, destination_poi_id=destination,
            origin_name=places[origin]['name'], destination_name=places[destination]['name'],
            origin=places[origin]['location'], destination=places[destination]['location'],
            mode='transit', duration_minutes=minutes, distance_meters=minutes * 250,
            walking_distance_meters=walking, fare_yuan=fare, source='synthetic_fixture',
            transit_detail_status='missing', transit_steps=[],
            note='合成方向性时间参考；没有线路明细，不代表未来班次，不保证适老或零步行。'))
    return dict(source='synthetic_fixture', case_id=case['id'], details=details, routes=routes,
        weather={'status': 'unavailable', 'source': 'synthetic_fixture',
                 'reason': '2030年游玩日期无可用预报，天气未知。'})


def saved_evidence(fixture):
    evidence = []
    for detail in fixture['details']:
        lon, lat = map(float, detail['location'].split(','))
        candidate = dict(poi_id=detail['id'], name=detail['name'], city='杭州',
            address=detail['address'], longitude=lon, latitude=lat,
            source='synthetic_fixture', fetched_at='2030-05-09T00:00:00+08:00')
        evidence.append(dict(tool='maps_search_detail', arguments={'id': detail['id']},
            result=deepcopy(detail), normalized={'status': 'available',
                'source': 'synthetic_fixture', 'candidates': [candidate]},
            queried_at='2030-05-09T00:00:00+08:00'))
    for route in fixture['routes']:
        evidence.append(dict(tool='maps_direction_transit_integrated',
            arguments={'origin': route['origin'], 'destination': route['destination'],
                       'city': '杭州', 'cityd': '杭州'},
            result={'status': 'available', 'source': 'synthetic_fixture',
                    'route_estimate': deepcopy(route)}, normalized=deepcopy(route),
            queried_at='2030-05-09T00:00:00+08:00'))
    return evidence


def build_inputs(case):
    req = case['context_requirements']
    fixture = fixtures_for(case)
    owner = User('synthetic-owner', 'Evaluator', 'E')
    trip = Trip('synthetic-trip', req['city'], owner, '规划中', '2030-05-09',
        requirements=TripRequirements(start_date=req['start_date'], end_date=req['end_date'],
            city=req['city'], day_count=req['day_count'], transport_mode=req['transport_mode'],
            lodging_address='合成杭州夹具地址-hotel', text=req['requirements_text']),
        lodging=dict(poi_id='hotel', name=PLACES[0][1], city='杭州',
            address='合成杭州夹具地址-hotel', longitude=120.15, latitude=30.25,
            source='synthetic_fixture', fetched_at='2030-05-09T00:00:00+08:00'))
    plan = TripPlan('synthetic-plan', '杭州两日合成计划', '草稿' if case['id'] == 3 else '未生成',
        text=OLD_PLAN if case['id'] == 3 else '', evidence=saved_evidence(fixture),
        requirements_revision=0, requirements_snapshot=asdict(trip.requirements),
        lodging_snapshot=deepcopy(trip.lodging))
    message = Message('synthetic-message', owner, 'user', case['prompt'], '2030-05-09')
    context = RequestContext(trip.id, owner.id, 'synthetic-execution', None,
        'synthetic-request', message.id, case['prompt'], 'main', 'task_execution',
        0, plan_basis(plan), 0, 0, 0)
    execution = TaskExecutionContext(trip, [message], plan, {}, '2030-05-09')
    return context, execution, plan, fixture


def same_coordinate(left, right):
    try:
        a, b = tuple(map(Decimal, left.split(','))), tuple(map(Decimal, right.split(',')))
        return len(a) == len(b) == 2 and all(v.is_finite() for v in a + b) and a == b
    except (AttributeError, InvalidOperation, ValueError):
        return False


def fixture_response(fixture, name, args):
    unavailable = dict(status='unavailable', source='synthetic_fixture',
        reason='没有此精确请求的固定合成事实；不得补造。')
    if name == 'query_weather':
        return deepcopy(fixture['weather'])
    if name in {'maps_text_search', 'maps_around_search'}:
        return dict(status='available', source='synthetic_fixture',
            note='固定合成候选集，不代表真实搜索排名、半径或营业。', pois=deepcopy(fixture['details']))
    if name == 'maps_search_detail':
        return deepcopy(next((p for p in fixture['details'] if p['id'] == args['id']), unavailable))
    if name.startswith('maps_direction_'):
        mode = {'maps_direction_walking': 'walking', 'maps_direction_driving': 'driving',
                'maps_direction_transit_integrated': 'transit'}[name]
        row = next((r for r in fixture['routes'] if same_coordinate(r['origin'], args.get('origin'))
            and same_coordinate(r['destination'], args.get('destination')) and r['mode'] == mode), None)
        if row is None or args.get('departure_at') is not None:
            return deepcopy(unavailable)
        return {'status': 'available', 'source': 'synthetic_fixture', 'route_estimate': deepcopy(row)}
    raise ValueError('External fixture name not allowed')


def install_fixtures(runtime, fixture):
    if set(runtime.registry) != REGISTRY or len(runtime.registry) != 10:
        raise ValueError('Unexpected registry; refusing external access')
    for name in EXTERNAL:
        def handler(args, name=name):
            output = fixture_response(fixture, name, args)
            runtime.evidence.append({'tool': name, 'arguments': deepcopy(args),
                'result': deepcopy(output), 'normalized': deepcopy(output.get('route_estimate', output)),
                'source': 'synthetic_fixture', 'queried_at': '2030-05-09T00:00:00+08:00'})
            return output
        runtime.registry[name] = replace(runtime.registry[name], handler=handler)


def new_budget():
    return TurnBudget(**LIMITS)


def safe(value):
    """Never persist opaque provider blocks, reasoning text or exception bodies."""
    if isinstance(value, dict):
        return {k: safe(v) for k, v in value.items() if k not in {
            '_responses_output', 'reasoning', 'reasoning_content', 'reasoning_text',
            'encrypted_content', 'analysis', 'reason', 'exception', 'error_message'}}
    if isinstance(value, (list, tuple)):
        return [safe(v) for v in value]
    return value


class MemoryTrace:
    def __init__(self, request_id, progress=None):
        self.request_id, self.events, self.progress = request_id, [], progress

    def add(self, name, value):
        if name == 'Task Runtime异常':
            value = {'error_type': value.get('error_type')}
        # Request params can contain provider replay blocks; only allow request metadata.
        if name == '模型请求':
            value = {k: v for k, v in value.items() if k != 'params'}
        self.events.append({'name': name, 'value': safe(deepcopy(value))})
        if self.progress:
            self.progress(self.events)


def metadata(case, condition, model):
    context, execution, _, fixture = build_inputs(case)
    full, without, edits = prompt_variants()
    role = travel_role()
    return dict(status='not_run', eval_id=case['id'], eval_name=case['name'],
        prompt_input=case['prompt'], assertions=[], name=case['name'], condition=condition, model=model,
        wire_api=wire_api(), thinking=os.getenv('OPENAI_MAIN_ENABLE_THINKING', '').strip().lower()
            or 'provider_default', reasoning_effort=role_reasoning_effort(role),
        prompt=full if condition == 'with_skill' else without,
        prompt_sha256=digest((full if condition == 'with_skill' else without).encode()),
        skill_version=read_skill_resource('travel-planning')['version'],
        skill_resources={r: read_skill_resource(item['name'], r)['hash']
            for item in skill_catalog() for r in item['resources']},
        scenario_sha256=digest(EVAL_PATH.read_bytes()), runtime_sha256=digest(
            (ROOT / 'backend/app/agents/travel_runtime.py').read_bytes()),
        budget=LIMITS, fixture_mode='synthetic_handler_replacement', fixture_sha256=digest(fixture),
        explanation=NOTE, ablation_edits=edits, scenario=case,
        input_snapshot={'request_context': serialize(context), 'execution_context': serialize(execution)},
        fixture=fixture, grading='objective plumbing only; semantic review by human',
        store=False, retries=0, fallback=None)


def run_case(case, condition, client, model, progress=None):
    meta = metadata(case, condition, model)
    context, execution, plan, fixture = build_inputs(case)
    before = serialize(execution)
    trace = MemoryTrace(context.request_id, progress)
    calls, errors = [], []
    def request(*args, **kwargs):
        try:
            result = request_completion(*args, **kwargs)
            for call in result.choices[0].message.tool_calls or []:
                calls.append({'name': call.function.name, 'arguments_raw': call.function.arguments})
            return result
        except Exception as exc:
            errors.append(type(exc).__name__)
            trace.add('eval_request_exception', {'error_type': type(exc).__name__})
            raise
    runtime = TaskExecutionRuntime(client, model, context, execution, request=request,
        trace=trace, budget=new_budget(), max_steps=24)
    install_fixtures(runtime, fixture)
    if condition not in {'with_skill', 'without_skill'}:
        raise ValueError('Unknown condition')
    if condition == 'without_skill':
        del runtime.registry['read_skill']
    runtime.config = replace(runtime.config, prompt=meta['prompt'], max_steps=24,
        tools=tuple(spec.model_spec() for spec in runtime.registry.values()))
    started = monotonic()
    result = None
    try:
        result = runtime.run()
    finally:
        if progress:
            progress(trace.events)
    answer = result.answer
    if result.changes:
        answer = result.changes.get('result_text', '') or answer
    skill_calls = [c for c in calls if c['name'] == 'read_skill']
    attempted = ''
    for call in calls:
        if call['name'] == 'save_trip_changes':
            try:
                attempted = json.loads(call['arguments_raw']).get('itinerary', {}).get('text', '')
            except (ValueError, AttributeError, TypeError):
                pass
    result_data = dict(status='completed', runtime_failed=result.failed, answer=answer,
        attempted_itinerary=attempted,
        failure_category=(('save_validation_rejected' if any(
            e['name'] == '工具执行' and e['value'].get('name') == 'save_trip_changes'
            and e['value'].get('error_type') for e in trace.events) else
            'request_exception' if errors else 'runtime_failed') if result.failed else None),
        has_changes=bool(result.changes), changes=serialize(result.changes),
        unchanged=serialize(execution) == before,
        called_skill=bool(skill_calls), read_skill_names=[c['arguments_raw'] for c in skill_calls],
        external_boundary_violations=runtime.usage['external_calls'] != 0,
        errors=errors + [e['value']['error_type'] for e in trace.events
            if e['name'] == 'Task Runtime异常'], **runtime.usage)
    # Failure reason may include exception text from the transport; never export it.
    elapsed = round(monotonic() - started, 3)
    timing = {'seconds': elapsed, 'total_duration_seconds': elapsed,
              'duration_ms': round(elapsed * 1000), **runtime.usage}
    transcript = {'calls': calls, 'messages': safe(runtime.messages), 'events': trace.events,
                  'tool_results': [e for e in trace.events if e['name'] == '工具执行']}
    meta['status'] = 'completed'
    return result_data, transcript, timing, meta


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


def save_task(directory, result, transcript, timing, meta):
    write_json(directory / 'outputs/result.json', result)
    body = result.get('answer', '')
    if not result.get('has_changes') and result.get('attempted_itinerary'):
        body += '\n\n# 未通过本地校验的模型候选，不是已保存或成功交付\n\n' + result['attempted_itinerary']
    (directory / 'outputs/result.md').write_text(body or
        ('Not run.\n' if result['status'] == 'not_run' else 'No final itinerary; inspect candidate/trace.\n'), encoding='utf-8')
    write_json(directory / 'transcript.json', transcript)
    write_json(directory / 'timing.json', timing)
    write_json(directory / 'eval_metadata.json', meta)


def evaluate(output, live=False):
    output = Path(output)
    if (output / 'run-state.json').exists():
        raise ValueError('Run output already exists; choose a new directory')
    if live:
        config.load_environment()
        config.validate_configuration(scope='model')
        if response_storage_enabled():
            raise ValueError('Provider response storage must be false; refusing live run')
        model = config.model_name()
    else:
        model = 'not_run_current_configuration_not_loaded'
    tasks = []
    for index, case in enumerate(load_cases()):
        order = ('with_skill', 'without_skill') if index % 2 == 0 else ('without_skill', 'with_skill')
        for condition in order:
            tasks.append({'name': case['name'], 'condition': condition, 'status': 'not_run'})
            directory = output / 'iteration-1' / case['name'] / condition
            save_task(directory, {'status': 'not_run', 'answer': ''},
                {'calls': [], 'events': [], 'messages': []}, {'status': 'not_run'},
                metadata(case, condition, model))
    state = dict(status='not_run', tasks=tasks, explanation=NOTE,
                 total_max_model_calls=144, budget_per_task=LIMITS)
    state_path = output / 'run-state.json'
    write_json(state_path, state)
    if not live:
        return state
    try:
        state['status'] = 'running'
        write_json(state_path, state)
        with resources.client_scope(), resources.borrow_client() as client:
            for task in tasks:
                case = next(c for c in load_cases() if c['name'] == task['name'])
                directory = output / 'iteration-1' / task['name'] / task['condition']
                task['status'] = 'running'
                def progress(events):
                    write_json(directory / 'transcript.json', {'status': 'running', 'events': events})
                    task['events'] = len(events)
                    ends = [e for e in events if e['name'] == '模型请求结束']
                    task['model_responses_or_errors'] = len(ends)
                    write_json(state_path, state)
                    if events and events[-1]['name'] == '模型请求结束':
                        print(json.dumps(task, ensure_ascii=False), flush=True)
                write_json(state_path, state)
                try:
                    values = run_case(case, task['condition'], client, model, progress)
                    save_task(directory, *values)
                    task.update({k: values[0][k] for k in ['status', 'runtime_failed',
                        'model_calls', 'tool_calls', 'total_tokens', 'read_skill_names', 'errors']})
                    task['seconds'] = values[2]['seconds']
                except KeyboardInterrupt:
                    task['status'] = 'interrupted'
                    raise
                except Exception as exc:
                    task.update(status='failed', error_type=type(exc).__name__)
                    write_json(directory / 'outputs/result.json', task)
                finally:
                    write_json(state_path, state)
                print(json.dumps(task, ensure_ascii=False), flush=True)
        state['status'] = 'completed'
    except KeyboardInterrupt:
        state['status'] = 'interrupted'
    except Exception as exc:
        state.update(status='failed', error_type=type(exc).__name__)
    finally:
        write_json(state_path, state)
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly allow configured real model requests')
    parser.add_argument('--output', type=Path, default=None)
    args = parser.parse_args(argv)
    output = args.output or ROOT / '.cache/travel_skill_eval' / uuid4().hex
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        state = evaluate(output, live=args.live)
        print(json.dumps({'output': str(output), 'status': state['status']}, ensure_ascii=False), flush=True)
        return 0 if state['status'] in {'not_run', 'completed'} else 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    raise SystemExit(main())
