"""Serial fixed-prose display evaluation; stop after consecutive upstream failures."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from time import monotonic

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def run(args):
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    os.environ['TRAVEL_PERSISTENCE'] = '0'
    from app import agent_client
    from app.plan_display import dates_from_snapshot
    from tests.performance.review_display_semantics import build_report
    dataset_path = Path(getattr(args, 'dataset', ROOT/'tests/datasets/display_semantics_v1/cases.json')).resolve()
    dataset = json.loads(dataset_path.read_text(encoding='utf-8'))
    cases = [c for c in dataset['cases'] if not args.cases or c['id'] in args.cases]
    if not cases or (args.cases and set(args.cases)-{c['id'] for c in cases}):
        raise ValueError('Unknown or empty case selection')
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('Output directory must be empty; previous failures are preserved')
    output.mkdir(parents=True, exist_ok=True)
    frozen = output/'source_snapshot'
    frozen.mkdir()
    hashes = {}
    for name in ['app/prompts/agents/display.txt', 'app/schemas/itinerary-display-v1.json',
                 'app/plan_display.py', 'app/agent_runtime.py', 'app/agent_client.py',
                 'tests/datasets/display_semantics_v1/cases.json']:
        content = (ROOT/name).read_bytes()
        (frozen/name.replace('/', '__')).write_bytes(content)
        hashes[name] = hashlib.sha256(content).hexdigest()
    (output/'dataset.json').write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding='utf-8')
    hashes['evaluation_dataset'] = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    manifest = {'started_at': datetime.now().isoformat(), 'model': os.getenv('OPENAI_MODEL'),
                'repeats': args.repeats, 'case_ids': [c['id'] for c in cases], 'source_hashes': hashes,
                'delivery': 'standalone_display', 'quality_grade': 'pending_manual_review'}
    (output/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    rows, consecutive, blocked = [], 0, False
    active_directory = output
    original_write = agent_client.ConversationTrace.write
    def trace_write(trace, error=None):
        if error is not None:
            trace.add('调用异常', type(error).__name__)
        text = '\n'.join(trace.events)
        for key, secret in os.environ.items():
            if len(secret)>8 and any(word in key for word in ('KEY', 'TOKEN', 'PASSWORD')):
                text = text.replace(secret, '[redacted]')
        (active_directory/(trace.request_id+'.txt')).write_text(text, encoding='utf-8')
    agent_client.ConversationTrace.write = trace_write
    try:
        for case in cases:
            for attempt in range(1, args.repeats+1):
                active_directory = output/case['id']/f'attempt-{attempt}'
                active_directory.mkdir(parents=True)
                print(json.dumps({'event':'start', 'case':case['id'], 'attempt':attempt}), flush=True)
                dates = dates_from_snapshot(case['requirements_snapshot'])
                started = monotonic()
                result = agent_client.generate_plan_display(case['source_text'], dates, trip_id='isolated-display-semantics')
                row = {'case_id':case['id'], 'attempt':attempt, 'seconds':monotonic()-started,
                       'source_text':case['source_text'], 'source_dates':dates, 'result':result}
                rows.append(row)
                (active_directory/'result.json').write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding='utf-8')
                (output/'results.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps({'event':'finished', 'case':case['id'], 'attempt':attempt,
                                  'status':result['display_status'], 'seconds':round(row['seconds'],3)}), flush=True)
                transport = result.get('error_type') in {'APITimeoutError', 'APIConnectionError', 'TimeoutError', 'ConnectError'}
                consecutive = consecutive+1 if transport else 0
                if consecutive >= args.max_consecutive_failures:
                    blocked = True
                    break
            if blocked:
                break
    finally:
        agent_client.ConversationTrace.write = original_write
    status = {'status':'blocked' if blocked else 'completed', 'completed_samples':len(rows),
              'planned_samples':len(cases)*args.repeats,
              'reason':'consecutive_upstream_failures' if blocked else None,
              'quality_grade':'pending_manual_review'}
    (output/'status.json').write_text(json.dumps(status, indent=2), encoding='utf-8')
    build_report(args.before, output/'results.json', output/'comparison', fixtures=dataset_path)
    print(json.dumps(status), flush=True)
    return 2 if blocked else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=ROOT/'tests/datasets/display_semantics_v1/cases.json')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--before', type=Path)
    parser.add_argument('--max-consecutive-failures', type=int, default=2)
    args = parser.parse_args()
    if args.repeats<1 or args.max_consecutive_failures<1:
        parser.error('Counts must be positive')
    raise SystemExit(run(args))
