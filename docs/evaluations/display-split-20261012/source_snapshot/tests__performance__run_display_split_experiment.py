"""Paired whole-prose / two-stage display experiment; serial and isolated."""
import argparse
from collections import defaultdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
from time import monotonic

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
DATASET = ROOT / 'tests/datasets/display_semantics_v1/cases.json'
CHECKLIST = ROOT / 'tests/datasets/display_semantics_v1/review-checklist.json'


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def report(rows, output):
    from tests.performance.review_display_semantics import inspect_display
    cases = {c['id']: c for c in json.loads(DATASET.read_text(encoding='utf-8'))['cases']}
    checks = {c['case_id']: c['checks'] for c in json.loads(CHECKLIST.read_text(encoding='utf-8'))['cases']}
    grouped = defaultdict(list)
    reviews = []
    for row in rows:
        grouped[(row['case_id'], row['strategy'])].append(row)
        ready = bool(row['result'].get('display'))
        reviews.append({'case_id': row['case_id'], 'strategy': row['strategy'], 'attempt': row['attempt'],
            'quality_grade': 'pending_review' if ready else 'not_evaluated',
            'targeted_observations': inspect_display(cases[row['case_id']], row['result']),
            'checks': [{**check, 'verdict': 'pending_review' if ready else 'not_evaluated', 'output_evidence': None} for check in checks[row['case_id']]]})
    summary = []
    lines = ['# 整篇与独立全局提取对照', '',
             'ready仅表示结构/日期通过。内容质量必须逐项对照原文；空字段/词语检查不替代人工判定。失败和成功耗时分开记录；小样本不报告稳定P95或SLA。', '',
             '| 场景 | 方案 | 成功/尝试 | 成功中位数 | 最大成功耗时 | 失败等待 | SDK请求总数 | 衣物有内容/成功 |',
             '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for (cid, strategy), selected in grouped.items():
        valid = [r for r in selected if r['result'].get('display')]
        failed = [r for r in selected if not r['result'].get('display')]
        seconds = [r['seconds'] for r in valid]
        item = {'case_id': cid, 'strategy': strategy, 'attempts': len(selected), 'ready': len(valid),
                'successful_median_seconds': statistics.median(seconds) if seconds else None,
                'successful_max_seconds': max(seconds) if seconds else None,
                'failed_seconds': sum(r['seconds'] for r in failed),
                'sdk_requests': sum(len(r['requests']) for r in selected),
                'clothing_present': sum(bool(r['result']['display']['preparation']['clothing']) for r in valid)}
        summary.append(item)
        median = f"{item['successful_median_seconds']:.2f}s" if seconds else '—'
        maximum = f"{item['successful_max_seconds']:.2f}s" if seconds else '—'
        lines.append(f"| {cid} | {strategy} | {len(valid)}/{len(selected)} | {median} | {maximum} | {item['failed_seconds']:.2f}s | {item['sdk_requests']} | {item['clothing_present']}/{len(valid)} |")
    save_json(output/'summary.json', summary)
    # This file is a draft. Reviewed grades belong in a separate semantic-review.json.
    save_json(output/'review-template.json', reviews)
    (output/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')


def run(args):
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    os.environ['TRAVEL_PERSISTENCE'] = '0'
    from app import agent_client
    from app.plan_display import dates_from_snapshot
    from tests.performance.display_split_experiment import generate_split_display
    cases = json.loads(DATASET.read_text(encoding='utf-8'))['cases']
    cases = [c for c in cases if not args.cases or c['id'] in args.cases]
    if not cases or (args.cases and set(args.cases)-{c['id'] for c in cases}):
        raise ValueError('Unknown or empty case selection')
    check_cases = {c['case_id']: c['checks'] for c in json.loads(CHECKLIST.read_text(encoding='utf-8'))['cases']}
    for case in cases:
        for check in check_cases[case['id']]:
            if check['source_quote'] not in case['source_text']:
                raise ValueError('Checklist quote absent: ' + check['id'])
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('Output must be empty; previous evidence is preserved')
    output.mkdir(parents=True, exist_ok=True)
    frozen = output/'source_snapshot'
    frozen.mkdir()
    files = ['app/agent_client.py', 'app/agent_runtime.py', 'app/agent_roles.py', 'app/model_transport.py',
             'app/plan_display.py', 'app/prompts/agents/display.txt', 'app/schemas/itinerary-display-v1.json',
             'tests/performance/display_split_experiment.py', 'tests/performance/run_display_split_experiment.py',
             'tests/performance/prompts/display_global.txt', 'tests/performance/prompts/display_days.txt',
             DATASET.relative_to(ROOT).as_posix(), CHECKLIST.relative_to(ROOT).as_posix()]
    hashes = {}
    for name in files:
        data = (ROOT/name).read_bytes()
        (frozen/name.replace('/', '__')).write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    save_json(output/'manifest.json', {'started_at': datetime.now().isoformat(), 'source_hashes': hashes,
        'strategies': args.strategies, 'case_ids': [c['id'] for c in cases], 'repeats': args.repeats,
        'order': 'alternate_strategy_order_by_attempt', 'execution': 'serial_no_store',
        'model_config': {key: os.getenv(key) for key in ('OPENAI_MODEL', 'MODEL_ID', 'OPENAI_BASE_URL',
            'OPENAI_WIRE_API', 'OPENAI_REASONING_EFFORT', 'OPENAI_REASONING_EFFORT_DISPLAY')},
        'seconds_budget': float(os.getenv('TRAVEL_DISPLAY_TIMEOUT_SECONDS', '180')),
        'sdk_budget': 2, 'split_correction': False, 'quality_grade': 'pending_review'})
    active_directory = output
    active_requests = []
    sample_started = monotonic()
    original_write, original_add = agent_client.ConversationTrace.write, agent_client.ConversationTrace.add
    def add(trace, name, value):
        if name == '模型请求':
            active_requests.append({'start_seconds': monotonic()-sample_started, 'wire_api': value['wire_api'],
                'model': value['model'], 'reasoning': value.get('reasoning'), 'status': 'no_response_recorded'})
        elif name == '模型响应' and active_requests:
            active_requests[-1].update(status='returned', seconds=value['seconds'], usage=value['usage'])
        original_add(trace, name, value)
    def write(trace, error=None):
        if error is not None:
            original_add(trace, '调用异常', type(error).__name__)
        text = '\n'.join(trace.events)
        for key, secret in os.environ.items():
            if len(secret)>8 and any(word in key for word in ('KEY', 'TOKEN', 'PASSWORD')):
                text = text.replace(secret, '[redacted]')
        (active_directory/(trace.request_id+'.txt')).write_text(text, encoding='utf-8')
    agent_client.ConversationTrace.write, agent_client.ConversationTrace.add = write, add
    rows, consecutive, blocked = [], 0, False
    try:
        for case in cases:
            for attempt in range(1, args.repeats+1):
                order = args.strategies if attempt % 2 else list(reversed(args.strategies))
                for strategy in order:
                    active_directory = output/case['id']/f'attempt-{attempt}'/strategy
                    active_directory.mkdir(parents=True)
                    active_requests = []
                    save_json(output/'progress.json', {'case_id': case['id'], 'attempt': attempt,
                        'strategy': strategy, 'completed_samples': len(rows), 'status': 'running'})
                    print(json.dumps({'event': 'start', 'case': case['id'], 'attempt': attempt, 'strategy': strategy}), flush=True)
                    dates = dates_from_snapshot(case['requirements_snapshot'])
                    sample_started = monotonic()
                    fn = agent_client.generate_plan_display if strategy == 'whole' else generate_split_display
                    result = fn(case['source_text'], dates, trip_id='isolated-display-split-experiment')
                    seconds = monotonic()-sample_started
                    row = {'case_id': case['id'], 'attempt': attempt, 'strategy': strategy, 'seconds': seconds,
                           'source_text': case['source_text'], 'source_dates': dates, 'result': result,
                           'requests': active_requests}
                    rows.append(row)
                    save_json(active_directory/'result.json', row)
                    save_json(output/'results.json', rows)
                    report(rows, output)
                    print(json.dumps({'event': 'finished', 'case': case['id'], 'attempt': attempt,
                        'strategy': strategy, 'seconds': round(seconds, 3), 'status': result['display_status'],
                        'error_type': result.get('error_type'), 'sdk_requests': len(active_requests)}), flush=True)
                    transport = result.get('error_type') in {'APITimeoutError', 'APIConnectionError', 'TimeoutError', 'ConnectError'}
                    consecutive = consecutive+1 if transport else 0
                    if consecutive >= args.max_consecutive_failures:
                        blocked = True
                        break
                if blocked:
                    break
            if blocked:
                break
    finally:
        agent_client.ConversationTrace.write, agent_client.ConversationTrace.add = original_write, original_add
    status = {'status': 'blocked' if blocked else 'completed', 'completed_samples': len(rows),
              'planned_samples': len(cases)*args.repeats*len(args.strategies),
              'reason': 'consecutive_upstream_failures' if blocked else None, 'quality_grade': 'pending_review'}
    save_json(output/'status.json', status)
    save_json(output/'progress.json', status)
    print(json.dumps(status), flush=True)
    return 2 if blocked else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--strategies', nargs='+', choices=['whole', 'split'], default=['whole', 'split'])
    parser.add_argument('--max-consecutive-failures', type=int, default=2)
    args = parser.parse_args()
    if args.repeats<1 or args.max_consecutive_failures<1 or len(set(args.strategies)) != len(args.strategies):
        parser.error('Counts must be positive and strategies unique')
    raise SystemExit(run(args))
