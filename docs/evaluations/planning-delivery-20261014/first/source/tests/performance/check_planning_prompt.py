"""Real model, controlled facts and an isolated Store; review prose separately."""
import argparse
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from time import monotonic
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
os.environ['TRAVEL_PERSISTENCE'] = '0'
from app import agent_client
from app.agent_runtime import TravelAgentRuntime
from run_travel_dataset import machine_checks
from travel_dataset_support import business_fingerprint, controlled_tool, create_fixture, load_dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=ROOT / 'tests/datasets/planning_prompt_v1/cases.json')
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    data = load_dataset(args.dataset)
    cases = data['cases']
    if args.cases:
        known = {case['id']: case for case in cases}
        if set(args.cases) - known.keys():
            parser.error('Unknown case ID')
        cases = [known[case_id] for case_id in dict.fromkeys(args.cases)]
    output = args.output.resolve()
    if output == ROOT / '.cache' or ROOT / '.cache' in output.parents:
        parser.error('Use an output outside the business cache')
    if output.exists() and any(output.iterdir()):
        parser.error('Use a new empty directory; preserve earlier attempts')
    output.mkdir(parents=True, exist_ok=True)
    os.environ['TRAVEL_AGENT_DISABLE_LLM'] = '0'
    os.environ.pop('PYTEST_CURRENT_TEST', None)
    secrets = sorted({value for key, value in os.environ.items() if value and len(value) > 8
                      and any(word in key for word in ('KEY', 'TOKEN', 'PASSWORD'))}, key=len, reverse=True)

    def redact(text):
        for secret in secrets:
            text = text.replace(secret, '[REDACTED]')
        return text

    def dump(path, value):
        path.write_text(redact(json.dumps(value, ensure_ascii=False, indent=2, default=str)), encoding='utf-8')

    hashes = {}
    for relative in ('app/prompts/agents/planning.txt', 'app/prompts/agents/travel_rules.txt',
                     'app/agent_roles.py', 'app/agent_runtime.py', 'app/planning.py',
                     'app/planning_evidence.py', 'app/chat_presentation.py',
                     'app/prompts/agents/revision.txt', 'app/templates/partials/chat_messages.html',
                     'tests/performance/check_planning_prompt.py'):
        source = ROOT / relative
        target = output / 'source' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        hashes[relative] = hashlib.sha256(source.read_bytes()).hexdigest()
    dump(output / 'dataset.json', data)
    before = business_fingerprint()
    manifest = {'started_at': datetime.now().isoformat(), 'dataset': data['dataset_id'],
                'model': os.getenv('OPENAI_MODEL'), 'wire_api': os.getenv('OPENAI_WIRE_API'),
                'reasoning': {key: value for key, value in os.environ.items() if key.startswith('OPENAI_REASONING_EFFORT')},
                'scope': 'live_model_isolated_memory_store_planning_text_only',
                'tool_modes': {case['id']: 'live_external' if case['tool_fixture'] == 'live_external' else 'controlled'
                               for case in cases},
                'display_conversion': 'not_run', 'business_http_used': False,
                'source_hashes': hashes, 'cases': [case['id'] for case in cases], 'results': [],
                'quality_grade': 'pending_manual_review', 'business_db_before': before}
    dump(output / 'manifest.json', manifest)
    consecutive_transport_failures = 0
    original_request = agent_client._create_chat_completion
    original_run_tool = agent_client.run_tool
    original_init = TravelAgentRuntime.__init__
    for case in cases:
        directory = output / case['id']
        directory.mkdir()
        calls, events, tools = [], [], []
        local, tid = create_fixture(data, case['setup'])

        def snapshot():
            return {'trip': asdict(local.trips[tid]), 'plan': asdict(local.plans[tid]),
                    'messages': [asdict(message) for message in local.messages[tid]],
                    'ideas': [asdict(idea) for idea in local.idea_cards[tid]]}

        def init(runtime, client, model, source, **kwargs):
            original_init(runtime, client, model, dict(source, current_date=data['current_date']), **kwargs)

        def trace_write(trace, error=None):
            if error:
                trace.add('调用异常', type(error).__name__ + ': ' + str(error))
            (directory / (trace.request_id + '.txt')).write_text(redact(''.join(trace.events)), encoding='utf-8')

        def trace_add(trace, title, value):
            original_add(trace, title, value)
            if title in {'工具执行', '意图识别结果', 'Agent Loop 状态', '共享调用预算', '调用异常', '关键接驳复核'}:
                events.append({'title': title, 'value': deepcopy(value)})

        def request(*positional, **kwargs):
            started = monotonic()
            row = {'role': kwargs.get('agent_role')}
            try:
                response = original_request(*positional, **kwargs)
                usage = getattr(response, 'usage', None)
                row['usage'] = {name: getattr(usage, name, None) for name in ('prompt_tokens', 'completion_tokens')}
                row['proposed_tools'] = [call.function.name for call in response.choices[0].message.tool_calls or []]
                return response
            except Exception as exc:
                row['error_type'] = type(exc).__name__
                raise
            finally:
                row['seconds'] = round(monotonic() - started, 6)
                calls.append(row)

        def run_tool(name, arguments, **kwargs):
            override = next((item for item in data.get('tool_overrides', {}).get(case['tool_fixture'], [])
                             if item['name'] == name and item['arguments'] == arguments), None)
            if case['tool_fixture'] == 'live_external':
                result = original_run_tool(name, arguments, **kwargs)
            elif override is not None:
                result = deepcopy(override['result'])
            else:
                result = controlled_tool(data, case['tool_fixture'], name, arguments, **kwargs)
            tools.append({'name': name, 'arguments': deepcopy(arguments), 'result': deepcopy(result)})
            return result

        original_add = agent_client.ConversationTrace.add
        rows = []
        print(json.dumps({'event': 'case_start', 'case': case['id']}, ensure_ascii=False), flush=True)
        with patch.object(TravelAgentRuntime, '__init__', init), patch.object(agent_client, '_create_chat_completion', request), \
             patch.object(agent_client, 'run_tool', run_tool), patch.object(agent_client.ConversationTrace, 'write', trace_write), \
             patch.object(agent_client.ConversationTrace, 'add', trace_add):
            for index, step in enumerate(case['steps'], 1):
                offsets = len(calls), len(events), len(tools)
                state_before = snapshot()
                started = monotonic()
                kind = step['action']
                payload = {'body': step['input']} if kind == 'chat' else {}
                task, _ = local.submit_task(tid, kind, payload, f"{case['id']}-{index}")
                local.run_task(task['id'])
                state_after = snapshot()
                selected_events = events[offsets[1]:]
                executed_tools = [event['value'] for event in selected_events if event['title'] == '工具执行']
                checks = machine_checks(step, state_before, state_after, calls[offsets[0]:], executed_tools, {})
                checks['task_succeeded'] = local.tasks[task['id']]['status'] == 'succeeded'
                rows.append({'input': step, 'before': state_before, 'after': state_after,
                             'seconds': round(monotonic() - started, 6), 'machine_checks': checks,
                             'machine_checks_pass': all(checks.values()), 'task': deepcopy(local.tasks[task['id']]),
                             'model_calls': deepcopy(calls[offsets[0]:]), 'queries': deepcopy(tools[offsets[2]:])})
                result = {'case_id': case['id'], 'title': case['title'], 'steps': rows,
                          'tool_mode': 'live_external' if case['tool_fixture'] == 'live_external' else 'controlled',
                          'events': events, 'manual_review_checklist': case['review'],
                          'quality_grade': 'pending_manual_review', 'display_conversion': 'not_run'}
                dump(directory / 'result.json', result)
                (directory / f'step-{index}-plan.txt').write_text(redact(state_after['plan']['text']), encoding='utf-8')
        passed = all(row['machine_checks_pass'] for row in rows)
        transport_failed = any(call.get('error_type') in {'APIConnectionError', 'APITimeoutError'} for call in calls)
        consecutive_transport_failures = consecutive_transport_failures + 1 if transport_failed else 0
        manifest['results'].append({'case': case['id'], 'machine_checks_pass': passed,
                                    'model_calls': len(calls), 'transport_failed': transport_failed})
        dump(output / 'manifest.json', manifest)
        print(json.dumps(manifest['results'][-1], ensure_ascii=False), flush=True)
        if consecutive_transport_failures >= 2:
            manifest['stop_reason'] = 'Two consecutive transport failures; earlier evidence retained'
            break
    manifest.update(finished_at=datetime.now().isoformat(), business_db_after=business_fingerprint())
    manifest['business_db_unchanged'] = manifest['business_db_before'] == manifest['business_db_after']
    dump(output / 'manifest.json', manifest)
    (output / 'done').write_text('complete', encoding='utf-8')
    return int(len(manifest['results']) != len(cases) or not manifest['business_db_unchanged']
               or any(not row['machine_checks_pass'] for row in manifest['results']))


if __name__ == '__main__':
    raise SystemExit(main())
