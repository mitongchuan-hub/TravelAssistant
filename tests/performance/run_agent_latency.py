"""Scenario latency benchmark: serial isolated samples, nested spans and UX readiness."""
import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, timedelta
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from time import perf_counter
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
os.environ['TRAVEL_PERSISTENCE'] = '0'
from app.tools.amap_mcp import configured as amap_configured
from travel_dataset_support import resolve_fixture, create_fixture, business_fingerprint
from latency_timeline import Timeline
from latency_instrumentation import Instrumentation, resolve_tool_arguments, EXTERNAL

DATASET = ROOT / 'tests/datasets/agent_latency_v1/cases.json'


def resolve_dataset(path, base_date):
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    values = {'start': (base_date+timedelta(days=3)).isoformat(), 'end3': (base_date+timedelta(days=5)).isoformat(),
              'end7': (base_date+timedelta(days=9)).isoformat()}
    def expand(value):
        if isinstance(value, str):
            for key, replacement in values.items():
                value = value.replace('{{' + key + '}}', replacement)
            return value
        if isinstance(value, list):
            return [expand(v) for v in value]
        if isinstance(value, dict):
            return {k: expand(v) for k, v in value.items()}
        return value
    data = expand(raw)
    data['resolved_base_date'] = raw.get('resolved_base_date', base_date.isoformat())
    validate_dataset(data)
    return data


def validate_dataset(data):
    ids = [c['id'] for c in data['cases']]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate latency case IDs')
    for case in data['cases']:
        spec = resolve_fixture(data, case['setup'])
        for step in case['steps']:
            if step['kind'] not in {'chat', 'plan', 'revision', 'display', 'tool_probe'}:
                raise ValueError('Unsupported latency entry')
            if step['kind'] == 'display' and not spec.get('source_text'):
                raise ValueError('Missing display source')
        if case.get('faults', {}).get('delay_seconds', 0) > 1:
            raise ValueError('Injected delay must be <= 1 second; actual provider waits are unchanged')
    for name, members in data['profiles'].items():
        if not members or len(members) != len(set(members)) or set(members)-set(ids):
            raise ValueError('Invalid latency profile: ' + name)
    return data


def secret_redactor():
    secrets = sorted({v for k, v in os.environ.items() if v and len(v) > 8
        and any(word in k for word in ('KEY', 'TOKEN', 'PASSWORD'))}, key=len, reverse=True)
    def redact(text):
        for value in secrets:
            text = text.replace(value, '[REDACTED]')
        return text
    return redact


def dump(path, value, redact):
    path.write_text(redact(json.dumps(value, ensure_ascii=False, indent=2, default=str)), encoding='utf-8')


def frontend_poll_intervals():
    result = {}
    for name, prefix in (('app', 'CHAT'), ('tasks', 'TASK')):
        text = (ROOT / f'app/static/{name}.js').read_text(encoding='utf-8')
        for suffix, field in (('IDLE', name), ('ACTIVE', name+'_active')):
            match = re.search(prefix+r'_POLL_'+suffix+r'_MS\s*=\s*(\d+)', text)
            result[field] = int(match.group(1))/1000 if match else None
    return result


def readiness(timeline, tid, before_plan_id, task_status):
    events = timeline['events']
    def first(name, predicate=lambda e: True):
        return next((e['offset_seconds'] for e in events if e['name'] == name and predicate(e)), None)
    prose = first('文字行程')
    saved = first('sqlite_snapshot_saved', lambda e: e['plan_ids'].get(tid) != before_plan_id)
    reply = first('reply_store_ready', lambda e: e.get('message_id') is not None)
    result = {
        'request_accepted_seconds': first('request_accepted'),
        'processing_started_seconds': first('processing_started'),
        'prose_generated_seconds': prose,
        'display_conversion_finished_seconds': first('展示转换'),
        'new_plan_persisted_seconds': saved,
        'reply_store_ready_seconds': reply,
        'task_terminal_seconds': first('task_terminal'),
        'reply_json_ready_seconds': first('reply_json_ready'),
        'chat_html_ready_seconds': first('chat_html_ready'),
        'itinerary_html_ready_seconds': first('itinerary_html_ready'),
        'cards_published_seconds': first('cards_published'),
        'cards_html_ready_seconds': first('cards_html_ready'),
        'first_token_seconds': None,
        'browser_visible_seconds': None,
        'prose_to_reply_store_seconds': reply-prose if prose is not None and reply is not None else None,
        'display_queue_and_execution_seconds': first('cards_published')-reply if first('cards_published') is not None and reply is not None else None,
        'task_status': task_status,
        'scope': 'Local Store + isolated ASGI response. Browser transport/render/poll wait not measured.',
        'poll_intervals_seconds': frontend_poll_intervals(),
    }
    return result


def coverage_checks(step, before, after, spans, display):
    expected = step.get('expect', {})
    checks = {}
    if 'plan_changed' in expected:
        checks['plan_changed'] = (before['plan'] != after['plan']) == expected['plan_changed']
    for key, value in expected.get('requirements_equal', {}).items():
        checks['requirements.' + key] = after['trip']['requirements'].get(key) == value
    if 'text_append' in expected:
        checks['exact_text_append'] = after['plan']['text'] == before['plan']['text'] + expected['text_append']
    for tool in expected.get('tools_required', []):
        checks['tool_called.' + tool] = any(s['metadata'].get('tool') == tool for s in spans if s['name'].startswith('工具：'))
    if 'display_status' in expected:
        checks['display_status'] = (display or after['plan'])['display_status'] == expected['display_status']
    return checks


def run_sample(data, case, output, *, tools_mode='controlled', simulate=False, attempt=1):
    """One fresh Store/SQLite per case attempt. Multi-turn steps share only that Store."""
    from app import agent_client
    from app.agent_runtime import TravelAgentRuntime
    from app.auth import AuthStore
    from app.plan_display import text_hash
    from fastapi.testclient import TestClient
    from openai import OpenAI
    main_module = importlib.import_module('app.main')
    base_date = date.fromisoformat(data['resolved_base_date'])
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    redact = secret_redactor()
    local, tid = create_fixture(data, case['setup'])
    local.db_path = str(output / 'isolated.sqlite3')
    local.persistence_enabled = True
    local._persist()
    auth = AuthStore('latency-unused.sqlite3', False)
    password = 'synthetic-latency-account'
    auth.create_account('latency@example.com', local.trips[tid].owner.id, password)
    results = []
    environment = {'TRAVEL_AGENT_DISABLE_LLM': '0', 'PYTEST_CURRENT_TEST': '', 'PYTHONDONTWRITEBYTECODE': '1'}
    with patch.dict(os.environ, environment), patch.object(main_module, 'store', local), patch.object(main_module, 'auth', auth), TestClient(main_module.app) as web:
        web.post('/login', data={'email': 'latency@example.com', 'password': password})
        for step_index, step in enumerate(case['steps'], 1):
            directory = output / f'step-{step_index}'
            directory.mkdir(exist_ok=True)
            def snapshot():
                return {'trip': asdict(local.trips[tid]), 'plan': asdict(local.plans[tid]),
                        'messages': [asdict(m) for m in local.messages[tid]]}
            before = snapshot()
            def progress(span):
                if span['name'].startswith(('SDK请求：', '工具：', '服务获取')):
                    print(json.dumps({'event': 'span_start', 'case': case['id'], 'attempt': attempt,
                        'step': step_index, 'stage': span['name'], 'offset_seconds': round(span['start_seconds'], 3)}, ensure_ascii=False), flush=True)
            t = Timeline(on_start=progress)
            def trace_sink(trace):
                with t.span('诊断日志写入隔离目录', 'logging'):
                    (directory / (trace.request_id + '.txt')).write_text(redact(''.join(trace.events)), encoding='utf-8')
            task, display_result, error = None, None, None
            try:
                with Instrumentation(t, case, step, tools_mode=tools_mode, base_date=base_date,
                                     trace_sink=trace_sink, simulate=simulate) as hooks:
                    with t.span('后台计时hook安装', 'observer_wait', lane='observer'):
                        local.start_display_worker(force=True)
                    with t.span('场景处理与服务器首次响应就绪', 'orchestration', case_id=case['id'], step=step_index):
                        if step['kind'] in {'display', 'tool_probe'}:
                            with OpenAI(api_key=os.getenv('OPENAI_API_KEY') or 'latency-synthetic-only',
                                        base_url=os.getenv('OPENAI_BASE_URL') or None, max_retries=0) as client:
                                trace = agent_client.ConversationTrace(uuid.uuid4().hex, datetime.now().isoformat(), tid, step.get('input') or '')
                                runtime = TravelAgentRuntime(client, os.getenv('OPENAI_MODEL') or 'synthetic',
                                    {'trip': local.trips[tid], 'plan': local.plans[tid], 'user_body': ''},
                                    trace=trace, request=agent_client._create_chat_completion)
                                if step['kind'] == 'display':
                                    source = resolve_fixture(data, case['setup'])['source_text']
                                    reply = runtime.convert_display(agent_client.AgentReply(source, None, [], plan_text=source))
                                    display_result = {'source_text': source, 'display': reply.display,
                                        'display_status': reply.display_status, 'source_hash': reply.display_source_hash,
                                        'hash_valid': reply.display_source_hash == text_hash(source), 'source_preserved': reply.plan_text == source}
                                else:
                                    messages = []
                                    for spec in case['simulation_tools']:
                                        arguments = resolve_tool_arguments(spec['arguments'], messages)
                                        result, _ = runtime.execute_tool('research', spec['name'], arguments)
                                        messages.append({'role': 'tool', 'name': spec['name'], 'content': json.dumps(result, ensure_ascii=False)})
                                trace.write()
                        else:
                            kind = step['kind']
                            payload = {'body': step['input']} if kind == 'chat' else {'feedback': step['input']} if kind == 'revision' else {}
                            with t.span('请求接受与入队', 'submission'):
                                task, _ = local.submit_task(tid, kind, payload, f"latency-{case['id']}-{attempt}-{step_index}")
                            t.event('request_accepted', task_id=task['id'])
                            t.event('processing_started', task_id=task['id'])
                            with t.span('执行任务直到终态', 'orchestration'):
                                local.run_task(task['id'])
                            task = deepcopy(local.tasks[task['id']])
                            t.event('task_terminal', status=task['status'])
                            with t.span('任务状态JSON响应', 'server_response') as span:
                                response = web.get(f'/api/trips/{tid}/tasks')
                                span['metadata'].update(http_status=response.status_code, bytes=len(response.content))
                            with t.span('消息JSON响应', 'server_response') as span:
                                response = web.get(f'/api/trips/{tid}/messages')
                                span['metadata'].update(http_status=response.status_code, bytes=len(response.content))
                            t.event('reply_json_ready')
                            with t.span('聊天片段HTML渲染', 'render') as span:
                                response = web.get(f'/workspace/{tid}/messages/partial')
                                span['metadata'].update(http_status=response.status_code, bytes=len(response.content))
                            t.event('chat_html_ready')
                            with t.span('行程页HTML渲染', 'render') as span:
                                response = web.get(f'/workspace/{tid}?tab=itinerary')
                                span['metadata'].update(http_status=response.status_code, bytes=len(response.content),
                                                       current_plan_id=local.plans[tid].id)
                            t.event('itinerary_html_ready')
                    if step['kind'] not in {'display', 'tool_probe'}:
                        with t.span('观测后台任务完成（非用户等待）', 'observer_wait', lane='observer'):
                            if not local.wait_for_display_jobs(tid, 190):
                                raise TimeoutError('Display observation deadline exceeded')
                        with t.span('卡片完成后的HTML响应', 'render', lane='http_after_display') as span:
                            response = web.get(f'/workspace/{tid}?tab=itinerary')
                            span['metadata'].update(http_status=response.status_code, bytes=len(response.content))
                        t.event('cards_html_ready')
                fault_hits = hooks.fault_hits
            except Exception as exc:
                error = {'type': type(exc).__name__, 'message': str(exc)[:500]}
                fault_hits = hooks.fault_hits if 'hooks' in locals() else []
                with t.span('后台计时收尾', 'observer_wait', lane='observer'):
                    local.wait_for_display_jobs(tid, 2)
            timeline = t.finalize()
            after = snapshot()
            workflow = [e for e in t.events if e['name'] == 'Workflow 返回结果']
            needs_input = any(e['value'].get('needs_input') for e in workflow)
            if error:
                outcome = 'execution_error'
            elif needs_input:
                outcome = 'needs_input'
            elif task:
                outcome = task['status']
            elif display_result:
                outcome = display_result['display_status']
            else:
                outcome = 'diagnostic_completed'
            calls = [s for s in t.spans if s['name'].startswith('SDK请求：')]
            model_requests = [s for s in t.spans if s['name'].startswith('模型逻辑调用：')]
            tool_calls = [s for s in t.spans if s['name'].startswith('工具：')]
            checks = coverage_checks(step, before, after, t.spans, display_result)
            if case.get('faults'):
                checks['fault_injection_hit'] = bool(fault_hits)
            total_model = sum(s['seconds'] for s in calls)
            total_tools = sum(s['seconds'] for s in tool_calls)
            row = {'case_id': case['id'], 'title': case['title'], 'group': case['group'], 'attempt': attempt,
                'step': step_index, 'entry': step['kind'], 'input': step.get('input'), 'focus': case['focus'],
                'model_mode': 'scripted' if simulate else 'live', 'tools_mode': tools_mode,
                'faults': case['faults'], 'fault_hits': fault_hits, 'outcome': outcome,
                'quality_grade': 'not_evaluated', 'expectation_checks': checks,
                'expectation_checks_pass': all(checks.values()), 'before': before, 'after': after, 'task': task,
                'display_conversion': display_result, 'error': error, 'timeline': timeline,
                'readiness': readiness(timeline, tid, before['plan']['id'], task['status'] if task else None),
                'metrics': {'seconds': timeline['measured_seconds'],
                    'primary_seconds': next((e['offset_seconds'] for e in t.events if e['name']=='itinerary_html_ready'), None),
                    'sdk_requests': len(calls),
                    'logical_model_calls': len(model_requests), 'model_retries': max(0, len(calls)-len(model_requests)),
                    'model_seconds': total_model, 'display_seconds': sum(s['seconds'] for s in calls if s['metadata']['role'] == 'display'),
                    'tool_calls': len(tool_calls), 'tool_seconds': total_tools,
                    'backend_calls': sum(s['category'] in {'external_io', 'controlled_io'} for s in t.spans),
                    'http_live_calls': sum(s['category'] == 'external_io' for s in t.spans),
                    'cache_hits': sum(s['metadata'].get('cache_hit', False) for s in t.spans),
                    'prompt_tokens': sum(s['metadata']['prompt_tokens'] for s in calls if isinstance(s['metadata'].get('prompt_tokens'), int)) if any(isinstance(s['metadata'].get('prompt_tokens'), int) for s in calls) else None,
                    'completion_tokens': sum(s['metadata']['completion_tokens'] for s in calls if isinstance(s['metadata'].get('completion_tokens'), int)) if any(isinstance(s['metadata'].get('completion_tokens'), int) for s in calls) else None},
                'store_size': {'trips': len(local.trips), 'messages': sum(len(v) for v in local.messages.values()),
                               'sqlite_bytes': Path(local.db_path).stat().st_size},
                'limitations': ['SDK duration includes network/provider queue/prefill/decoding; no reliable internal split.',
                                'No streaming/first-token measurement.', 'ASGI readiness is not actual browser visibility.',
                                'Setup, login and user think time excluded. Serial fresh-process attempts include client initialization.']}
            dump(directory / 'result.json', row, redact)
            dump(directory / 'chrome-trace.json', t.chrome_trace(), redact)
            results.append(row)
            dump(output / 'result.json', {'case_id': case['id'], 'attempt': attempt, 'steps': results}, redact)
            print(json.dumps({'event': 'step_finished', 'case': case['id'], 'attempt': attempt, 'step': step_index,
                'outcome': outcome, 'seconds': round(row['metrics']['seconds'], 3), 'accounting_valid': timeline['accounting_valid'],
                'expectation_checks_pass': row['expectation_checks_pass']}, ensure_ascii=False), flush=True)
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', default=str(DATASET))
    parser.add_argument('--profile', default='smoke')
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--base-date', default=date.today().isoformat())
    parser.add_argument('--tools', choices=('live', 'controlled'), default=None)
    parser.add_argument('--simulate', action='store_true', help='Scripted model plus controlled tools; instrumentation verification only')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--output')
    parser.add_argument('--sample-case', help=argparse.SUPPRESS)
    parser.add_argument('--sample-attempt', type=int, default=1, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        base_date = date.fromisoformat(args.base_date)
        data = resolve_dataset(args.dataset, base_date)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    tools_mode = args.tools or ('controlled' if args.simulate else 'live')
    if args.simulate and tools_mode != 'controlled':
        parser.error('Scripted runs require controlled tools')
    if args.repeats < 1:
        parser.error('repeats must be positive')
    if args.profile not in data['profiles']:
        parser.error('Unknown profile')
    ids = args.cases or data['profiles'][args.profile]
    known = {c['id']: c for c in data['cases']}
    if set(ids)-set(known):
        parser.error('Unknown case ID')
    if args.list:
        print(json.dumps({'dataset': data['dataset_id'], 'version': data['version'], 'base_date': data['resolved_base_date'],
            'case_count': len(ids), 'cases': [known[i] for i in ids]}, ensure_ascii=False, indent=2))
        return 0
    if not args.output:
        parser.error('--output is required')
    output = Path(args.output).resolve()
    if output == ROOT / '.cache' or ROOT / '.cache' in output.parents:
        parser.error('Use a fresh output outside the business cache')
    if output.exists() and any(output.iterdir()):
        parser.error('Use a new output directory; do not overwrite earlier attempts')
    output.mkdir(parents=True, exist_ok=True)
    # agent_client loads .env, even when only direct tool probes are selected.
    from app import agent_client
    os.environ['TRAVEL_AGENT_DISABLE_LLM'] = '0'
    os.environ.pop('PYTEST_CURRENT_TEST', None)
    redact = secret_redactor()
    if args.sample_case:
        rows = run_sample(data, known[args.sample_case], output, tools_mode=tools_mode,
                          simulate=args.simulate, attempt=args.sample_attempt)
        return int(any(not r['timeline']['accounting_valid'] or r['error'] for r in rows))
    from run_travel_dataset_batch import snapshot_source
    metadata = {'dataset': data['dataset_id'], 'dataset_version': data['version'], 'started_at': datetime.now().isoformat(),
        'base_date': data['resolved_base_date'], 'dataset_sha256': hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest(),
        'profile': args.profile, 'cases': list(dict.fromkeys(ids)),
        'repeats': args.repeats, 'execution': 'serial_fresh_subprocess', 'delivery': 'prose_then_background_cards',
        'model_mode': 'scripted' if args.simulate else 'live',
        'tools_mode': tools_mode, 'model': os.getenv('OPENAI_MODEL'), 'base_url': os.getenv('OPENAI_BASE_URL'),
        'wire_api': os.getenv('OPENAI_WIRE_API'), 'model_timeout_seconds': os.getenv('OPENAI_TIMEOUT_SECONDS', '180'),
        'reasoning_config': {k: v for k, v in os.environ.items() if k.startswith('OPENAI_REASONING_EFFORT')},
        'amap_configured': amap_configured(),
        'amap_transport': 'streamable_http_mcp', 'amap_endpoint': 'mcp.amap.com/mcp',
        'frontend_poll_intervals_seconds': frontend_poll_intervals(),
        'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'working_tree': subprocess.check_output(['git', 'status', '--short'], cwd=ROOT, text=True),
        'persistence': 'one isolated SQLite per case attempt', 'browser_visibility_measured': False}
    before = business_fingerprint()
    dump(output / 'metadata.json', metadata, redact)
    dump(output / 'dataset-resolved.json', data, redact)
    snapshot_source(output)
    records, failures = [], []
    for case_id in metadata['cases']:
        for attempt in range(1, args.repeats+1):
            case = known[case_id]
            target = output / case_id / f'attempt-{attempt}'
            target.mkdir(parents=True)
            command = [sys.executable, str(Path(__file__).resolve()), '--dataset', str(output/'dataset-resolved.json'),
                '--base-date', metadata['base_date'], '--sample-case', case_id, '--sample-attempt', str(attempt),
                '--output', str(target/'evidence'), '--tools', tools_mode]
            if args.simulate:
                command.append('--simulate')
            print(json.dumps({'event': 'case_start', 'case': case_id, 'attempt': attempt}, ensure_ascii=False), flush=True)
            process_started = perf_counter()
            with (target/'execution.log').open('w', encoding='utf-8') as log:
                try:
                    process = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                        timeout=300*len(case['steps'])+60, env=dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1'))
                    code = process.returncode
                except subprocess.TimeoutExpired:
                    code = -1
            result_file = target/'evidence/result.json'
            if result_file.exists():
                rows = json.loads(result_file.read_text(encoding='utf-8'))['steps']
                for row in rows:
                    row['evidence_path'] = str(result_file.relative_to(output))
                records.extend(rows)
            if code != 0 or not result_file.exists():
                failures.append({'case_id': case_id, 'attempt': attempt, 'returncode': code,
                    'process_wait_seconds': perf_counter()-process_started,
                    'log': str((target/'execution.log').relative_to(output)), 'reason': 'Process error or deadline; retain any completed steps'})
            dump(output/'results.json', records, redact)
            dump(output/'execution-errors.json', failures, redact)
            print(json.dumps({'event': 'case_finished', 'case': case_id, 'attempt': attempt,
                'returncode': code, 'outcomes': [r['outcome'] for r in records if r['case_id']==case_id and r['attempt']==attempt]}, ensure_ascii=False), flush=True)
    metadata['business_db_before'] = before
    metadata['business_db_after'] = business_fingerprint()
    metadata['business_db_unchanged'] = metadata['business_db_after'] == before
    metadata['finished_at'] = datetime.now().isoformat()
    dump(output/'metadata.json', metadata, redact)
    from latency_report import write_reports
    write_reports(output, json.loads((output/'metadata.json').read_text(encoding='utf-8')), records, failures)
    (output/'done').write_text('complete', encoding='utf-8')
    print(json.dumps({'finished': True, 'samples': len(records), 'errors': len(failures),
        'business_db_unchanged': metadata['business_db_unchanged'], 'report': str(output/'report.html')}, ensure_ascii=False), flush=True)
    return int(bool(failures) or not metadata['business_db_unchanged'])


if __name__ == '__main__':
    sys.exit(main())
