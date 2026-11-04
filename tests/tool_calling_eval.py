"""Fixed single-tool eval. Offline by default; --live sends synthetic inputs to the configured model."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from time import monotonic
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from jsonschema import ValidationError
from app.agents.inputs.context import project_main_context
from app.agents.inputs.execution import TaskExecutionContext
from app.agents.model_client import request_completion
from app.agents.roles import travel_role, role_reasoning_effort
from app.agents.tools.registry import agent_tool_registry
from app.application.contracts import RequestContext
from app.core import config, resources
from app.domain.itinerary.identity import plan_basis
from app.domain.models import TripRequirements

DATASET = ROOT / 'tests/datasets/tool_calling_single.json'
COUNTS = {'read_context': 16, 'submit_changes': 24, 'query_weather': 12,
          'maps_text_search': 16, 'maps_search_detail': 8, 'maps_direction_walking': 8,
          'maps_direction_driving': 8, 'maps_direction_transit_integrated': 8}


def forbidden_handler(*args, **kwargs):
    raise AssertionError('Evaluation must never execute tools')


def registry():
    return agent_tool_registry(read_context=forbidden_handler, submit_changes=forbidden_handler,
                               query=forbidden_handler)


def strict_object(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def constant(value):
        raise ValueError('non-finite JSON constant')
    if not isinstance(raw, str):
        raise ValueError('arguments must be a JSON string')
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(value, dict):
        raise ValueError('arguments must be an object')
    return value


def coordinates(value):
    if not isinstance(value, str) or len(value.split(',')) != 2:
        raise ValueError('invalid coordinate string')
    try:
        lon, lat = (Decimal(part.strip()) for part in value.split(','))
    except InvalidOperation as exc:
        raise ValueError('invalid coordinate number') from exc
    if not lon.is_finite() or not lat.is_finite() or not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError('coordinate out of range')
    return lon, lat


def same_value(key, actual, expected):
    if key in {'origin', 'destination'}:
        return coordinates(actual) == coordinates(expected)
    if key == 'itinerary' and isinstance(actual, dict) and isinstance(expected, dict):
        actual, expected = deepcopy(actual), deepcopy(expected)
        if actual.get('action') == expected.get('action') == 'edit_text':
            # Omitted optional context and explicit null mean the same thing.
            if actual.get('context') is None:
                actual.pop('context', None)
            if expected.get('context') is None:
                expected.pop('context', None)
    return type(actual) is type(expected) and actual == expected


def parameter_errors(expected, args, *, plan_text=None):
    args = deepcopy(args)
    operation = args.get('itinerary')
    if (isinstance(operation, dict) and operation.get('action') == 'edit_text'
            and operation.get('operation') == 'replace' and isinstance(plan_text, str)):
        context, old = operation.get('context'), operation.get('old_text')
        if (isinstance(context, str) and isinstance(old, str) and old
                and plan_text.count(context) == 1 and context.count(old) == 1
                and 'context' not in expected['arguments'].get('itinerary', {})):
            # A valid unique locator changes neither the authorized target nor the edit.
            operation.pop('context')
    tools = registry()
    tool = tools[expected['tool']]
    errors = []
    try:
        tool.validate(args)
    except ValidationError:
        errors.append('schema')
    if set(args) - set(tool.parameters['properties']):
        errors.append('unknown_parameter')
    required = expected['arguments']
    optional = expected.get('optional', {})
    alternatives = expected.get('alternatives', {})
    if set(args) - set(required) - set(optional):
        errors.append('unauthorized_parameter')
    for key, value in required.items():
        choices = [value, *alternatives.get(key, [])]
        try:
            matched = key in args and any(same_value(key, args[key], item) for item in choices)
        except (ValueError, TypeError):
            matched = False
        if not matched:
            errors.append(f'value:{key}')
    for key, value in optional.items():
        if key in args and not same_value(key, args[key], value):
            errors.append(f'optional:{key}')
    if expected['tool'] == 'query_weather' and 'date' in args:
        try:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', args['date']):
                raise ValueError('date format')
            date.fromisoformat(args['date'])
        except (TypeError, ValueError):
            errors.append('date')
    return errors


def grade_response(case, response):
    row = {'id': case.get('id'), 'tool': case['expected']['tool'], 'calls': [], 'content': None,
           'finish_reason': None, 'usage': {}, 'selection_ok': False, 'format_ok': False,
           'parameters_ok': False, 'passed': False, 'errors': []}
    choices = list(getattr(response, 'choices', None) or [])
    if len(choices) != 1:
        row['errors'].append('response_shape')
        return row
    choice = choices[0]
    message = choice.message
    row['content'] = getattr(message, 'content', None)
    row['finish_reason'] = getattr(choice, 'finish_reason', None)
    usage = getattr(response, 'usage', None)
    row['usage'] = {key: getattr(usage, key, None) for key in ('prompt_tokens', 'completion_tokens')}
    row['usage']['reasoning_tokens'] = getattr(getattr(usage, 'completion_tokens_details', None), 'reasoning_tokens', None)
    for call in list(getattr(message, 'tool_calls', None) or []):
        function = getattr(call, 'function', None)
        row['calls'].append({'id': getattr(call, 'id', None), 'type': getattr(call, 'type', 'function'),
                             'name': getattr(function, 'name', None),
                             'arguments': getattr(function, 'arguments', None)})
    calls = row['calls']
    row['selection_ok'] = len(calls) == 1 and calls[0]['name'] == case['expected']['tool']
    if not row['selection_ok']:
        row['errors'].append('selection')
    if len(calls) != 1:
        row['errors'].append('call_count')
        return row
    call = calls[0]
    try:
        parsed = strict_object(call['arguments'])
        call['parsed_arguments'] = parsed
    except (ValueError, TypeError):
        row['errors'].append('invalid_json')
        return row
    row['format_ok'] = (call['type'] == 'function' and bool(call['id'])
                        and not (row['content'] or '').strip()
                        and row['finish_reason'] in {None, 'stop', 'tool_calls', 'function_call'})
    if not row['format_ok']:
        row['errors'].append('format')
    if row['selection_ok']:
        plan_text = next((p['result'].get('text') for p in reversed(case.get('prior_calls', []))
                          if p['tool'] == 'read_context' and p['arguments'].get('section') == 'plan'), None)
        issues = parameter_errors(case['expected'], parsed, plan_text=plan_text)
        row['parameters_ok'] = not issues
        row['errors'].extend(issues)
    row['passed'] = all(row[key] for key in ('selection_ok', 'parameters_ok', 'format_ok'))
    return row


def load_dataset(path=DATASET):
    data = strict_object(Path(path).read_text(encoding='utf-8'))
    try:
        cases = data['cases']
        if data['version'] != 1 or len(cases) != 100:
            raise ValueError('expected version 1 and 100 cases')
        if set(registry()) != set(COUNTS):
            raise ValueError('registered tool library drift')
        if len({c['id'] for c in cases}) != len(cases):
            raise ValueError('duplicate case id')
        if Counter(c['expected']['tool'] for c in cases) != Counter(COUNTS):
            raise ValueError('tool coverage drift')
        date.fromisoformat(data['current_date'])
        for case in cases:
            if set(case) != {'id', 'input', 'context', 'prior_calls', 'expected'}:
                raise ValueError('invalid case fields')
            if not isinstance(case['input'], str) or not case['input'].strip():
                raise ValueError('empty input')
            if not isinstance(case['context'], dict) or not isinstance(case['prior_calls'], list):
                raise ValueError('invalid input context')
            expected = case['expected']
            if set(expected) != {'tool', 'arguments', 'optional', 'alternatives'}:
                raise ValueError('invalid expectation fields')
            if set(expected['optional']) & set(expected['arguments']):
                raise ValueError('overlapping required and optional rules')
            if not set(expected['alternatives']) <= set(expected['arguments']):
                raise ValueError('unknown alternative rule')
            if any(not isinstance(v, list) or not v for v in expected['alternatives'].values()):
                raise ValueError('invalid alternatives')
            if parameter_errors(expected, expected['arguments']):
                raise ValueError(f'invalid gold: {case["id"]}')
            if expected['tool'] == 'submit_changes' and expected['arguments']['user_quote'] != case['input']:
                raise ValueError('gold user_quote must equal complete user input')
            if parameter_errors(expected, {**expected['arguments'], **expected['optional']}):
                raise ValueError('invalid optional parameter rule')
            for key, values in expected['alternatives'].items():
                for value in values:
                    candidate = {**expected['arguments'], key: value}
                    if parameter_errors(expected, candidate):
                        raise ValueError('invalid alternative')
            build_messages(data, case)
    except (KeyError, TypeError, ValidationError) as exc:
        raise ValueError('invalid dataset contract') from exc
    return data


def build_messages(data, case):
    base = deepcopy(data['context'])
    base.update(deepcopy(case['context']))
    profile = TripRequirements(**base['requirements'])
    trip = SimpleNamespace(requirements=profile, destination=profile.city, lodging=base['lodging'],
                           workflow_state={}, mode='city')
    basis = plan_basis(base.get('plan'))
    request = RequestContext(trip_id='eval-trip', owner_id='eval-owner', execution_task_id='eval-task',
        conversation_task_id=None, request_id='eval-request', message_id=case['id'], body=case['input'],
        entry='main', strategy='task_execution', requirements_revision=profile.revision,
        plan_basis=basis, workflow_revision=0, sequence=0, epoch=0)
    execution = TaskExecutionContext(trip=trip, messages=[], plan=base.get('plan'), metrics={},
                                     current_date=data['current_date'])
    payload = project_main_context(request_context=request, execution_context=execution,
        workflow={}, recent_chat=[], plan=execution.plan, missing_requirements=[])
    messages = [{'role': 'system', 'content': travel_role().prompt},
                {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
    for index, prior in enumerate(case['prior_calls']):
        # Frozen server results are context, not calls made during this evaluation.
        registry()[prior['tool']].validate(prior['arguments'])
        call_id = f'fixture-{index}'
        messages.extend([
            {'role': 'assistant', 'content': None, 'tool_calls': [{'id': call_id, 'type': 'function',
                'function': {'name': prior['tool'], 'arguments': json.dumps(prior['arguments'], ensure_ascii=False)}}]},
            {'role': 'tool', 'tool_call_id': call_id, 'content': json.dumps(prior['result'], ensure_ascii=False)}])
    return messages


def run_cases(data, cases, client, model):
    role = travel_role()
    for case in cases:
        started = monotonic()
        try:
            value = request_completion(client, model, build_messages(data, case), tools=list(role.tools),
                reasoning_effort=role_reasoning_effort(role), agent_role=role.name)
            row = grade_response(case, value)
        except Exception as exc:
            # Never write provider exception bodies: they may contain credentials/URLs.
            row = {'id': case['id'], 'tool': case['expected']['tool'], 'calls': [], 'content': None,
                   'usage': {}, 'finish_reason': None, 'selection_ok': None, 'parameters_ok': None,
                   'format_ok': None, 'passed': False, 'errors': ['request_error'],
                   'exception_type': type(exc).__name__}
        row['seconds'] = round(monotonic() - started, 3)
        yield row


def summarize(rows, total):
    metrics = {}
    for key in ('selection_ok', 'parameters_ok', 'format_ok'):
        count = sum(row[key] is True for row in rows)
        metrics[key] = {'passed': count, 'rate': count / total if total else 0,
                        'unavailable': sum(row[key] is None for row in rows)}
    passed = sum(row['passed'] for row in rows)
    complete = len(rows) == total
    return {'total': total, 'completed': len(rows), 'complete': complete, 'passed': passed,
            'rate': passed / total if total else 0,
            'meets_target': (passed * 100 >= total * 95) if complete and total else None,
            'metrics': metrics, 'by_tool': {name: {'total': sum(r['tool'] == name for r in rows),
                'passed': sum(r['tool'] == name and r['passed'] for r in rows)}
                for name in sorted({r['tool'] for r in rows})},
            'errors': dict(Counter(e for row in rows for e in row['errors']))}


def write_report(path, report):
    path = Path(path)
    summary = report['summary']
    subset = report.get('metadata', {}).get('subset', False)
    lines = ['# 单工具评测', '', 'SUBSET — 全量验收：未执行' if subset else 'FULL SUITE',
        'COMPLETE' if summary['complete'] else 'INCOMPLETE',
        f"完成 {summary['completed']}/{summary['total']}；通过 {summary['passed']}；比例 {summary['rate']:.2%}",
        f"达到95%目标：{summary['meets_target']}", '', '| 工具 | 已评测 | 通过 |', '|---|---:|---:|']
    lines += [f"| {name} | {value['total']} | {value['passed']} |" for name, value in summary['by_tool'].items()]
    lines += ['', '## 分项指标（分母为全部选定用例）', json.dumps(summary['metrics'], ensure_ascii=False),
              '', '## 错误分类', json.dumps(summary['errors'], ensure_ascii=False),
              '', '固定合成集单次响应正确率，不代表工具执行或生产总体成功率。',
              '请求异常/截断的原始响应在现有transport边界不可得，分项标记不可评估。']
    path.parent.mkdir(parents=True, exist_ok=True)
    for destination, text in [(path, json.dumps(report, ensure_ascii=False, indent=2)),
                              (path.with_suffix('.md'), '\n'.join(lines) + '\n')]:
        temporary = destination.with_suffix(destination.suffix + '.tmp')
        try:
            temporary.write_text(text, encoding='utf-8')
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--dataset', type=Path, default=DATASET)
    parser.add_argument('--ids', nargs='+', help='subset only; not full-suite acceptance')
    parser.add_argument('--output-dir', type=Path, default=ROOT / '.cache/tool_calling_eval')
    args = parser.parse_args(argv)
    data = load_dataset(args.dataset)
    cases = data['cases']
    if args.ids:
        unknown = set(args.ids) - {c['id'] for c in cases}
        if unknown:
            parser.error('Unknown IDs: ' + ', '.join(sorted(unknown)))
        cases = [c for c in cases if c['id'] in args.ids]
    print(f'Synthetic single-tool evaluation: {len(cases)}/100 cases, {len(registry())} tools; no tool execution.')
    if not args.live:
        print('Dataset valid. Offline only; --live required for model requests. No accuracy measured.')
        return 0
    config.load_environment()
    config.validate_configuration(scope='model')
    if config.store_responses():
        parser.error('Live evaluation requires OPENAI_STORE_RESPONSES=false (checked after loading .env).')
    role = travel_role()
    metadata = {'model': config.model_name(), 'wire_api': config.wire_api(),
        'reasoning_effort': role_reasoning_effort(role),
        'thinking': os.getenv('OPENAI_MAIN_ENABLE_THINKING', ''), 'timeout': config.model_timeout(),
        'store': False, 'retries': 0, 'fallback': False, 'subset': bool(args.ids),
        'prompt': role.prompt, 'tools': list(role.tools), 'prompt_hash': digest(role.prompt),
        'schema_hash': digest(role.tools), 'dataset_hash': digest(data), 'dataset': data,
        'started_at': datetime.now(timezone.utc).isoformat()}
    path = args.output_dir / f'{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}.json'
    rows = []
    report = {'metadata': metadata, 'rows': rows, 'summary': summarize(rows, len(cases))}
    write_report(path, report)
    try:
        with resources.client_scope(), resources.borrow_client() as client:
            for row in run_cases(data, cases, client, metadata['model']):
                rows.append(row)
                report['summary'] = summarize(rows, len(cases))
                write_report(path, report)
                print(f"{row['id']}: passed={row['passed']} errors={row['errors']}")
    finally:
        metadata['ended_at'] = datetime.now(timezone.utc).isoformat()
        write_report(path, report)
        print(f'Report: {path}')
    return 0 if report['summary']['meets_target'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
