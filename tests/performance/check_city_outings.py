"""Three real-model city-outing samples, isolated from both business databases."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from time import monotonic
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ['TRAVEL_PERSISTENCE'] = '0'
os.environ['TRAVEL_AGENT_DISABLE_LLM'] = '0'
from app import agent_client
from app.city_outings import city_requirements
from app.store import DemoStore
from app.tools.places import search_places


def fingerprint():
    result = {}
    for name in ('travelassistant.sqlite3', 'cityassistant.sqlite3'):
        path = ROOT / '.cache' / name
        if not path.exists():
            result[name] = None
            continue
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True) as connection:
            result[name] = {}
            for table in ('users', 'trips', 'messages', 'ideas', 'plans'):
                rows = connection.execute(f'SELECT * FROM {table}').fetchall()
                result[name][table] = {'count': len(rows), 'sha256': hashlib.sha256(
                    repr(sorted(map(repr, rows))).encode()).hexdigest()}
    return result


def main():
    output = Path(sys.argv[1]).resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit('Use a new empty output directory')
    output.mkdir(parents=True, exist_ok=True)
    secrets = [v for k, v in os.environ.items() if v and len(v) > 8
               and any(w in k.upper() for w in ('KEY', 'TOKEN', 'PASSWORD'))]
    def redact(text):
        for secret in secrets: text = text.replace(secret, '[redacted]')
        return text
    def dump(path, value):
        path.write_text(redact(json.dumps(value, ensure_ascii=False, indent=2, default=str)), encoding='utf-8')
    sources = ['app/models.py', 'app/city_outings.py', 'app/requirements.py', 'app/planning.py',
        'app/planning_evidence.py', 'app/agent_roles.py', 'app/agent_runtime.py', 'app/tools/places.py',
        'app/tools/amap_mcp.py', 'app/tools/routes.py', 'app/store.py', 'app/main.py',
        'app/prompts/agents/city_planning.txt', 'app/prompts/agents/city_rules.txt',
        'app/prompts/agents/city_examples.txt', 'app/prompts/agents/city_revision.txt',
        'tests/performance/check_city_outings.py']
    manifest = {'started_at': datetime.now().isoformat(), 'model': os.getenv('OPENAI_MODEL'),
        'wire_api': os.getenv('OPENAI_WIRE_API'), 'business_before': fingerprint(),
        'sources': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources},
        'scope': 'isolated_memory_store_real_model_no_display', 'results': []}
    dump(output/'manifest.json', manifest)
    hotel = {'poi_id': 'synthetic-hotel', 'name': '湖滨住宿（合成）', 'address': '湖滨路合成地址',
        'longitude': 120.15, 'latitude': 30.25, 'city': '杭州市', 'district': '上城区',
        'source': '高德地图 POI', 'fetched_at': '2026-10-15T00:00:00+00:00'}
    park = {**hotel, 'poi_id': 'synthetic-park', 'name': '湖滨公园（合成）', 'address': '公园路合成地址',
            'place_type': '风景名胜', 'longitude': 120.16}
    museum = {**hotel, 'poi_id': 'synthetic-museum', 'name': '城市展览馆（合成）',
              'address': '展馆路合成地址', 'place_type': '科教文化服务', 'longitude': 120.17}
    cases = [
        {'id': 'CITY-001', 'title': '只有住宿，日期人数预算未知', 'lodging': hotel, 'values': {},
         'tool_mode': 'synthetic', 'action': 'chat', 'message': '帮我生成一天的同城游玩路线'},
        {'id': 'CITY-002', 'title': '两天人均预算，人数未知，接驳失败', 'lodging': hotel,
         'values': {'start_date': '2026-10-25', 'day_count': '2', 'budget_amount': '500',
                    'budget_scope': 'per_person', 'must_visit': museum['name'], 'dietary': '不吃辣',
                    'outing_start_time': '10:00', 'outing_end_time': '19:00'},
         'tool_mode': 'synthetic_routes_failed', 'action': 'plan', 'message': ''},
        {'id': 'CITY-003', 'title': '杭州住宿地址，真实高德和路线',
         'address': '杭州西湖国宾馆', 'values': {}, 'tool_mode': 'live', 'action': 'plan', 'message': ''},
    ]
    if len(sys.argv) > 2:
        requested = set(sys.argv[2:])
        if requested - {case['id'] for case in cases}:
            raise SystemExit('Unknown city sample ID')
        cases = [case for case in cases if case['id'] in requested]
    original_tool = agent_client.run_tool
    original_request = agent_client._create_chat_completion
    transport_failures = 0
    for case in cases:
        directory = output/case['id']; directory.mkdir()
        tools, calls = [], []
        print(json.dumps({'event': 'case_start', 'case': case['id']}, ensure_ascii=False), flush=True)
        selected = case.get('lodging')
        if case['tool_mode'] == 'live':
            result = search_places({'keywords': case['address']})
            dump(directory/'address-lookup.json', result)
            candidates = result.get('candidates', [])
            exact = [p for p in candidates if p['name'] == case['address']]
            selected = exact[0] if len(exact) == 1 else (candidates[0] if len(candidates) == 1 else None)
            if not selected:
                manifest['results'].append({'case': case['id'], 'status': 'address_unresolved',
                    'reason': 'No unique exact main POI; no guessed coordinates'})
                dump(output/'manifest.json', manifest)
                continue
        local = DemoStore(persistence_enabled=False, display_worker_enabled=False)
        values = {'lodging_address': case.get('address') or selected['address'], **case['values']}
        trip = local.create_trip(selected['city']+'同城游玩', '', '', '', '', mode='city', lodging=selected,
                                 requirements=city_requirements(values, selected['city']))
        source = asdict(trip)
        def request(*args, **kwargs):
            started = monotonic(); row = {'role': kwargs.get('agent_role')}
            try:
                response = original_request(*args, **kwargs)
                usage = getattr(response, 'usage', None)
                row['prompt_tokens'] = getattr(usage, 'prompt_tokens', None)
                row['completion_tokens'] = getattr(usage, 'completion_tokens', None)
                return response
            except Exception as exc:
                row['error_type'] = type(exc).__name__
                raise
            finally:
                row['seconds'] = monotonic()-started; calls.append(row)
        def tool(name, args, **kwargs):
            if case['tool_mode'] == 'live':
                result = original_tool(name, args, **kwargs)
            elif name == 'search_places':
                result = {'status': 'available', 'candidates': [park, museum],
                    'note': '合成测试数据，票价、指定日期开放和预约均未知；不可推断免费或可避雨。'}
            elif name == 'query_route':
                failed = case['tool_mode'] == 'synthetic_routes_failed'
                catalog = {p['poi_id']: p for p in (hotel, park, museum)}
                origin, destination = catalog.get(args['origin_poi_id']), catalog.get(args['destination_poi_id'])
                result = {'status': 'unavailable' if failed or not origin or not destination else 'available',
                    'origin_poi_id': args['origin_poi_id'], 'destination_poi_id': args['destination_poi_id'],
                    'origin_name': origin['name'] if origin else '', 'destination_name': destination['name'] if destination else '',
                    'mode': args['mode'], 'duration_minutes': None if failed else (20 if args['mode']=='transit' else 30),
                    'source': '合成测试', 'reason': '双向失败' if failed else '具体线路、票价及候车组成未知'}
            else:
                result = {'status': 'unavailable', 'reason': '本合成场景未提供此项资料'}
            tools.append({'name': name, 'arguments': deepcopy(args), 'result': deepcopy(result)})
            return result
        def trace_write(trace, error=None):
            if error: trace.add('调用异常', type(error).__name__+': '+str(error))
            (directory/(trace.request_id+'.txt')).write_text(redact(''.join(trace.events)), encoding='utf-8')
        started = monotonic()
        with patch.object(agent_client, 'run_tool', tool), patch.object(agent_client, '_create_chat_completion', request), \
             patch.object(agent_client.ConversationTrace, 'write', trace_write):
            payload = {'body': case['message']} if case['action']=='chat' else {}
            task, _ = local.submit_task(trip.id, case['action'], payload, case['id'])
            local.run_task(task['id'])
        plan = local.plans[trip.id]
        row = {'case': case['id'], 'title': case['title'], 'tool_mode': case['tool_mode'],
            'input': source, 'task': local.tasks[task['id']], 'model_calls': calls, 'tools': tools,
            'seconds': monotonic()-started, 'plan': asdict(plan),
            'last_message': local.messages[trip.id][-1].body,
            'checks': {'task_succeeded': local.tasks[task['id']]['status']=='succeeded',
                'text_saved': bool(plan.text), 'no_review_role': all(c['role']!='review' for c in calls),
                'unknown_travelers_preserved': local.trips[trip.id].requirements.travelers is None,
                'lodging_preserved': local.trips[trip.id].lodging==selected},
            'content_quality': 'pending_manual_review'}
        dump(directory/'result.json', row)
        (directory/'plan.txt').write_text(redact(plan.text), encoding='utf-8')
        manifest['results'].append({k: row[k] for k in ('case', 'seconds', 'checks', 'content_quality')})
        dump(output/'manifest.json', manifest)
        print(json.dumps(manifest['results'][-1], ensure_ascii=False), flush=True)
        failed = any(c.get('error_type') in {'APIConnectionError', 'APITimeoutError'} for c in calls)
        transport_failures = transport_failures+1 if failed else 0
        if transport_failures>=2:
            manifest['stop_reason'] = 'Two consecutive transport failures'; break
    manifest.update(finished_at=datetime.now().isoformat(), business_after=fingerprint())
    manifest['business_unchanged'] = manifest['business_before']==manifest['business_after']
    dump(output/'manifest.json', manifest)
    (output/'done').write_text('complete', encoding='utf-8')


if __name__=='__main__': main()
