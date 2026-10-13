"""Run one isolated live case; keep machine checks separate from human grades."""
import argparse
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, date, timedelta
import json
import os
from pathlib import Path
import sys
import uuid
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['TRAVEL_PERSISTENCE'] = '0'
from app import agent_client
from app.agent_runtime import TravelAgentRuntime
from app.plan_display import validate_display, text_hash
from travel_dataset_support import (
    DEFAULT_DATASET, EXTERNAL_TOOLS, load_dataset, resolve_fixture as resolve_spec,
    create_fixture as create_store, controlled_tool, business_fingerprint,
)

# Preserve the import API used by the frozen v1 controlled cases.
DATA = load_dataset(ROOT / 'tests/datasets/travel_agent_v1/cases.json')


def resolve_fixture(name):
    return resolve_spec(DATA, name)


def create_fixture(name):
    spec = resolve_fixture(name)
    r = spec.get('requirements', {})
    if r:
        spec['requirements'] = {
            'city': r['city'], 'start_date': '2026-11-14', 'end_date': '2026-11-15',
            'arrival_time': '10:00', 'departure_time': '18:00',
            'arrival_location': '杭州东站', 'departure_location': '杭州东站',
            'travelers': 2, 'budget_amount': '2000', 'budget_scope': 'total',
            'budget_coverage': '含住宿、餐饮、市内交通、门票，不含往返大交通',
            'must_visit': r['must_visit'], 'avoid': r['avoid'], 'dietary': r['diet'], 'pace': r['pace'],
        }
    return create_store({'fixtures': {'legacy': spec}}, 'legacy')


def machine_checks(step, before, after, calls, tools, injection):
    expected = step['expected_plan_changed']
    changed = before['plan'] != after['plan']
    checks = {'plan_change': expected is None or changed == expected}
    a = step.get('assertions', {})
    old, new = before['trip']['requirements'], after['trip']['requirements']
    for key, value in a.get('requirements_equal', {}).items():
        checks['requirements_equal.' + key] = new.get(key) == value
    for key in a.get('requirements_preserved', []):
        checks['requirements_preserved.' + key] = new.get(key) == old.get(key)
    if 'text_replace' in a:
        rule = a['text_replace']
        checks['exact_text_replace'] = after['plan']['text'] == before['plan']['text'].replace(
            rule['old'], rule['new'], rule.get('count', 1))
    if 'text_append' in a:
        checks['exact_text_append'] = after['plan']['text'] == before['plan']['text'] + a['text_append']
    if a.get('text_preserved'):
        checks['text_preserved'] = after['plan']['text'] == before['plan']['text']
    if 'plan_status_equal' in a:
        checks['plan_status'] = after['plan']['status'] == a['plan_status_equal']
    if 'external_calls_equal' in a:
        checks['external_calls'] = sum(t['name'] in EXTERNAL_TOOLS for t in tools) == a['external_calls_equal']
    for role in a.get('role_calls_absent', []):
        checks['role_absent.' + role] = all(c['role'] != role for c in calls)
    for tool in a.get('required_tools', []):
        checks['tool_called.' + tool] = any(t['name'] == tool for t in tools)
    if a.get('injection_triggered'):
        checks['injection_triggered'] = injection['after_requirements']
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', default=str(DEFAULT_DATASET))
    parser.add_argument('--case', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    data = load_dataset(args.dataset)
    case = next((c for c in data['cases'] if c['id'] == args.case), None)
    if case is None:
        parser.error('Unknown case ID')
    controlled_v1 = {f'TA-{i:03d}' for i in (30, 31, 32, 33, 34, 36)}
    if case.get('execution_mode') in {'controlled', 'spec_only'} or case['id'] in controlled_v1:
        parser.error('Use the mapped pytest nodes for controlled cases; target cases are specifications only')
    output = Path(args.output).resolve()
    if output == ROOT / '.cache' or ROOT / '.cache' in output.parents:
        parser.error('Output must be outside the business cache')
    if output.exists() and any(output.iterdir()):
        parser.error('Use an empty output directory to retain first-attempt evidence')
    output.mkdir(parents=True, exist_ok=True)
    os.environ['TRAVEL_AGENT_DISABLE_LLM'] = '0'
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    os.environ.pop('PYTEST_CURRENT_TEST', None)
    events, queries, steps, model_calls = [], [], [], []
    injection = {'after_requirements': False}
    business_before = business_fingerprint()
    secrets = sorted({v for k, v in os.environ.items() if v and len(v) > 12
                      and any(t in k for t in ('KEY', 'TOKEN', 'PASSWORD'))}, key=len, reverse=True)

    def redact(text):
        for value in secrets:
            text = text.replace(value, '[REDACTED]')
        return text

    def dump(path, value):
        path.write_text(redact(json.dumps(value, ensure_ascii=False, indent=2, default=str)), encoding='utf-8')

    started = monotonic()
    original_add = agent_client.ConversationTrace.add

    def observe(trace, title, value):
        value = json.loads(redact(json.dumps(value, ensure_ascii=False, default=str)))
        original_add(trace, title, value)
        if title == '模型请求':
            value = {'agent_role': value.get('agent_role'), 'has_response_format': 'response_format' in value.get('params', {}),
                     'reasoning': value.get('reasoning'),
                     'tool_names': [s['function']['name'] for s in value.get('params', {}).get('tools') or []]}
        events.append({'trace': trace.request_id, 'title': title, 'value': deepcopy(value),
                       'offset_seconds': round(monotonic() - started, 6)})

    def write(trace, error=None):
        if error:
            trace.add('调用异常', type(error).__name__ + ': ' + str(error))
        (output / (trace.request_id + '.txt')).write_text(redact(''.join(trace.events)), encoding='utf-8')

    agent_client.ConversationTrace.add = observe
    agent_client.ConversationTrace.write = write
    original_init = TravelAgentRuntime.__init__

    def initialize(runtime, client, model, snapshot, **kwargs):
        original_init(runtime, client, model, dict(snapshot, current_date=data['current_date']), **kwargs)

    TravelAgentRuntime.__init__ = initialize
    original_request = agent_client._create_chat_completion

    def request(*a, **kw):
        tick = monotonic()
        row = {'role': kw.get('agent_role'), 'started_offset': round(tick - started, 6)}
        try:
            fault = case.get('fault_injection') == 'after_requirements_timeout' or case['id'] == 'TA-029'
            if fault and kw.get('agent_role') in {'planning', 'revision'}:
                for message in a[2]:
                    if message.get('role') == 'tool' and message.get('name') == 'update_requirements':
                        result = json.loads(message['content'])
                        requirements = result.get('requirements', {}).get('requirements', {})
                        if result.get('status') == 'ready' and requirements.get('budget_amount') == '2500':
                            injection['after_requirements'] = True
                            raise TimeoutError('Injected timeout after successful requirement update')
            response = original_request(*a, **kw)
            usage = getattr(response, 'usage', None)
            details = getattr(usage, 'completion_tokens_details', None)
            row['usage'] = {'prompt_tokens': getattr(usage, 'prompt_tokens', None),
                            'completion_tokens': getattr(usage, 'completion_tokens', None),
                            'reasoning_tokens': getattr(details, 'reasoning_tokens', None)}
            row['output_characters'] = len(response.choices[0].message.content or '')
            return response
        except Exception as exc:
            row['error_type'] = type(exc).__name__
            raise
        finally:
            row['seconds'] = round(monotonic() - tick, 6)
            model_calls.append(row)

    agent_client._create_chat_completion = request

    def run_tool(name, arguments, **kwargs):
        result = controlled_tool(data, case['tool_fixture'], name, arguments, **kwargs)
        queries.append({'name': name, 'arguments': deepcopy(arguments), 'result': deepcopy(result)})
        return result

    agent_client.run_tool = run_tool
    local, tid = create_fixture(case['setup']) if data['dataset_id'] == 'travel_agent_v1' else create_store(data, case['setup'])

    def snapshot():
        return {'trip': asdict(local.trips[tid]), 'plan': asdict(local.plans[tid]),
                'messages': [asdict(m) for m in local.messages[tid]],
                'ideas': [asdict(i) for i in local.idea_cards[tid]]}

    result = {'case_id': case['id'], 'dataset_version': data['version'], 'current_date': data['current_date'],
              'acceptance': case.get('acceptance', 'current'), 'execution_mode': case.get('execution_mode', 'live_agent'),
              'workflow': 'four_intents_latest_only_sync_display', 'fixture_spec': resolve_spec(data, case['setup']),
              'model': os.getenv('OPENAI_MODEL'), 'wire_api': os.getenv('OPENAI_WIRE_API'),
              'tool_fixture': case['tool_fixture'], 'external_tools': 'controlled_synthetic',
              'started_at': datetime.now().isoformat(), 'status': 'executed_pending_review',
              'steps': steps, 'events': events, 'queries': queries, 'model_calls': model_calls, 'injection': injection}
    try:
        for step in case['steps']:
            before = snapshot()
            offsets = len(events), len(queries), len(model_calls)
            tick = monotonic()
            task = None
            converted = None
            action = step['action']
            if action == 'clear_chat':
                local.clear_chat(tid)
            elif action == 'convert_display':
                from openai import OpenAI
                spec = resolve_spec(data, case['setup'])
                source = spec['source_text']
                with OpenAI(api_key=os.getenv('OPENAI_API_KEY'), base_url=os.getenv('OPENAI_BASE_URL') or None,
                            timeout=float(os.getenv('OPENAI_TIMEOUT_SECONDS', '180')), max_retries=0) as client:
                    trace = agent_client.ConversationTrace(str(uuid.uuid4()), datetime.now().isoformat(), tid, source)
                    runtime = TravelAgentRuntime(client, os.getenv('OPENAI_MODEL'),
                        {'trip': local.trips[tid], 'plan': local.plans[tid], 'user_body': ''}, trace=trace, request=request)
                    reply = runtime.convert_display(agent_client.AgentReply(source, None, [], plan_text=source))
                    converted = {'source_text': source, 'source_characters': len(source), 'display': reply.display,
                                 'status': reply.display_status, 'source_hash': reply.display_source_hash,
                                 'source_preserved': reply.plan_text == source,
                                 'hash_valid': reply.display_source_hash == text_hash(source)}
                    trace.write()
            else:
                kind = {'chat': 'chat', 'plan': 'plan', 'revision': 'revision'}.get(action)
                if kind is None:
                    raise ValueError('Unsupported action: ' + action)
                payload = {'body': step['input']} if kind == 'chat' else {'feedback': step['input']} if kind == 'revision' else {}
                task, _ = local.submit_task(tid, kind, payload, f"{case['id']}-{len(steps)}")
                local.run_task(task['id'])
                primary_seconds = monotonic() - tick
                local.run_pending_display_jobs()
            after = snapshot()
            selected = deepcopy(events[offsets[0]:])
            calls = deepcopy(model_calls[offsets[2]:])
            tools = [e['value'] for e in selected if e['title'] == '工具执行']
            checks = machine_checks(step, before, after, calls, tools, injection)
            display_checks = []
            plan = after['plan']
            if before['plan'] != plan:
                check = {'status': plan['display_status'], 'present': bool(plan['display'])}
                if plan['display']:
                    try:
                        basis = plan['requirements_snapshot']
                        first, last = date.fromisoformat(basis['start_date']), date.fromisoformat(basis['end_date'])
                        dates = [(first + timedelta(days=i)).isoformat() for i in range((last-first).days+1)]
                        validate_display(plan['display'], dates)
                        check['valid'] = plan['display_source_hash'] == text_hash(plan['text'])
                    except Exception as exc:
                        check.update(valid=False, error=type(exc).__name__ + ': ' + str(exc))
                    checks['display_structure_and_hash'] = check['valid']
                display_checks.append(check)
            if converted is not None:
                checks.update(display_ready=converted['status'] == 'ready',
                              source_preserved=converted['source_preserved'], display_hash=converted['hash_valid'])
            if task:
                checks['task_succeeded'] = local.tasks[task['id']]['status'] == 'succeeded'
            by_role = {}
            for call in calls:
                role = call['role']
                by_role[role] = round(by_role.get(role, 0) + call['seconds'], 6)
            total = monotonic() - tick
            model_seconds = sum(c['seconds'] for c in calls)
            external_seconds = sum(t['seconds'] for t in tools if t['name'] in EXTERNAL_TOOLS)
            internal_seconds = sum(t['seconds'] for t in tools if t['name'] not in EXTERNAL_TOOLS)
            prose_events = [e for e in selected if e['title'] == '文字行程']
            row = {'input': step, 'seconds': round(total, 6), 'before': before, 'after': after,
                   'plan_changed': before['plan'] != plan, 'plan_change_pass': checks['plan_change'],
                   'machine_checks': checks, 'machine_checks_pass': all(checks.values()),
                   'display_checks': display_checks, 'display_conversion': converted,
                   'events': selected, 'queries': deepcopy(queries[offsets[1]:]), 'model_calls': calls,
                   'timing': {'primary_seconds': round(primary_seconds, 6) if task else None,
                              'model_seconds': round(model_seconds, 6), 'model_seconds_by_role': by_role,
                              'display_seconds': by_role.get('display', 0),
                              'external_seconds': round(external_seconds, 6), 'internal_tool_seconds': round(internal_seconds, 6),
                              'other_seconds': round(total-model_seconds-external_seconds-internal_seconds, 6),
                              'text_ready_seconds': round(prose_events[-1]['offset_seconds']-(tick-started), 6) if prose_events else None},
                   'tasks': deepcopy(list(local.tasks.values()))}
            steps.append(row)
            dump(output / 'result.json', result)
    except Exception as exc:
        result.update(status='execution_error', error=type(exc).__name__ + ': ' + str(exc))
    finally:
        transport_errors = [str(e['value']) for e in events if e['title'] == '调用异常'
            and (str(e['value']).startswith(('APIConnectionError:', 'APITimeoutError:')) or
                 str(e['value']).startswith('InternalServerError:') and any(
                     f'Error code: {s}' in str(e['value']) for s in (500, 502, 503, 504)))]
        if transport_errors:
            result.update(status='environment_blocked', transport_errors=transport_errors)
        result['seconds'] = round(monotonic() - started, 6)
        result['business_db_before'] = business_before
        result['business_db_after'] = business_fingerprint()
        result['business_db_unchanged'] = result['business_db_after'] == business_before
        result['machine_checks_pass'] = len(steps) == len(case['steps']) and all(s['machine_checks_pass'] for s in steps)
        result['isolation'] = {'persistence_enabled': local.persistence_enabled, 'business_http_used': False}
        dump(output / 'result.json', result)
        print(json.dumps({k: result[k] for k in ('case_id', 'status', 'seconds', 'machine_checks_pass', 'business_db_unchanged')}, ensure_ascii=False), flush=True)
    return 1 if result['status'] == 'execution_error' or not result['business_db_unchanged'] else 0


if __name__ == '__main__':
    sys.exit(main())
