"""Freeze a dataset run, execute live/controlled cases, and retain ungraded evidence."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys

from travel_dataset_support import DEFAULT_DATASET, ROOT, load_dataset


def select_cases(data, profile, ids=None):
    if data['dataset_id'] == 'travel_agent_v1':
        controlled = {f'TA-{i:03d}' for i in (30, 31, 32, 33, 34, 36)}
        selected = [dict(c, execution_mode='controlled' if c['id'] in controlled else 'live_agent',
                         acceptance='current') for c in data['cases']]
        if profile != 'system':
            selected = [c for c in selected if c['execution_mode'] == 'live_agent']
    else:
        if profile not in data['profiles']:
            raise ValueError('Unknown profile: ' + profile)
        selected = [c for c in data['cases'] if c['id'] in data['profiles'][profile]]
    if ids:
        known = {c['id']: c for c in data['cases']}
        if set(ids) - set(known):
            raise ValueError('Unknown case IDs')
        selected = [known[i] for i in dict.fromkeys(ids)]
        if data['dataset_id'] == 'travel_agent_v1':
            selected = [dict(c, execution_mode='controlled' if c['id'] in controlled else 'live_agent',
                             acceptance='current') for c in selected]
    return selected


def snapshot_source(output):
    files = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT).split(b'\0')
    manifest = {}
    for name in files:
        if not name:
            continue
        relative = Path(name.decode('utf-8'))
        if any(p.startswith('.env') or p in {'.cache', '__pycache__', '.pytest_cache', '.git'} for p in relative.parts):
            continue
        source = ROOT / relative
        if not source.is_file() or output == source.resolve() or output in source.resolve().parents:
            continue
        target = output / 'source' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        manifest[relative.as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
    (output / 'source-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')


def summarize_live(rows):
    groups = defaultdict(list)
    for row in rows:
        for step in row.get('result', {}).get('steps', []):
            groups[step['input']['action']].append(step)
    summary = {}
    for action, steps in groups.items():
        seconds = [s['seconds'] for s in steps]
        summary[action] = {
            'samples': len(steps), 'median_seconds': statistics.median(seconds), 'max_seconds': max(seconds),
            'machine_checks_pass': sum(s['machine_checks_pass'] for s in steps),
            'display_fallbacks': sum((s.get('display_conversion') or {}).get('status') == 'fallback' or
                any(c['status'] == 'fallback' for c in s['display_checks']) for s in steps),
            'model_seconds': sum(s['timing']['model_seconds'] for s in steps),
            'display_seconds': sum(s['timing']['display_seconds'] for s in steps),
            'external_seconds': sum(s['timing']['external_seconds'] for s in steps),
        }
    return summary


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--dataset', default=str(DEFAULT_DATASET))
    p.add_argument('--profile', default='current')
    p.add_argument('--output')
    p.add_argument('--workers', type=int, default=1, help='Serial by default for local-model latency comparisons')
    p.add_argument('--repeats', type=int, default=1)
    p.add_argument('--cases', nargs='+')
    p.add_argument('--list', action='store_true', help='Validate and list selected cases without calling models')
    args = p.parse_args()
    data = load_dataset(args.dataset)
    try:
        selected = select_cases(data, args.profile, args.cases)
    except ValueError as exc:
        p.error(str(exc))
    if min(args.workers, args.repeats) < 1:
        p.error('workers and repeats must be positive')
    if args.list:
        print(json.dumps({'dataset': data['dataset_id'], 'version': data['version'],
            'counts': dict(Counter(c['execution_mode'] for c in selected)),
            'cases': [{'id': c['id'], 'title': c.get('title', c['category']),
                       'mode': c['execution_mode'], 'acceptance': c['acceptance']} for c in selected]}, ensure_ascii=False, indent=2))
        return 0
    if not args.output:
        p.error('--output is required for execution')
    if any(c['execution_mode'] == 'spec_only' for c in selected):
        p.error('Target cases are pending specifications. Use --profile target --list to review them')
    output = Path(args.output).resolve()
    if output == ROOT / '.cache' or ROOT / '.cache' in output.parents:
        p.error('Output must be outside the business cache')
    if output.exists() and any(output.iterdir()):
        p.error('Use a new output directory; earlier attempts must remain unchanged')
    output.mkdir(parents=True, exist_ok=True)
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=False)
    secrets = sorted({v for k, v in os.environ.items() if v and len(v) > 12
                      and any(t in k for t in ('KEY', 'TOKEN', 'PASSWORD'))}, key=len, reverse=True)

    def dump(path, value):
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        for secret in secrets:
            text = text.replace(secret, '[REDACTED]')
        path.write_text(text, encoding='utf-8')

    metadata = {'dataset': data['dataset_id'], 'dataset_version': data['version'], 'workers': args.workers,
                'repeats': args.repeats, 'profile': args.profile, 'started_at': datetime.now().isoformat(),
                'workflow': 'four_intents_latest_only_sync_display', 'model': os.getenv('OPENAI_MODEL'),
                'base_url': os.getenv('OPENAI_BASE_URL'), 'wire_api': os.getenv('OPENAI_WIRE_API'),
                'store_responses': os.getenv('OPENAI_STORE_RESPONSES'), 'fallback_model': os.getenv('OPENAI_FALLBACK_MODEL'),
                'reasoning_config': {k: v for k, v in os.environ.items() if k.startswith('OPENAI_REASONING_EFFORT')},
                'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'working_tree': subprocess.check_output(['git', 'status', '--short'], cwd=ROOT, text=True),
                'cases': [c['id'] for c in selected], 'external_tools': 'controlled_synthetic',
                'latency_scope': 'Server-side only; controlled tools do not measure real external latency. Display is part of model time.'}
    dump(output / 'metadata.json', metadata)
    shutil.copy2(args.dataset, output / 'dataset.json')
    template = Path(args.dataset).with_name('review-template.json')
    if template.exists():
        review = json.loads(template.read_text(encoding='utf-8'))
        review['reviews'] = [dict(r, attempt=attempt) for r in review['reviews'] if r['case_id'] in metadata['cases']
                             for attempt in range(1, args.repeats+1)]
        dump(output / 'review.json', review)
    snapshot_source(output)
    live = [c for c in selected if c['execution_mode'] in {'live_agent', 'live_display'}]
    controlled = [c for c in selected if c['execution_mode'] == 'controlled']
    completed = []

    def run(case, attempt):
        target = output / case['id'] / f'attempt-{attempt}'
        target.mkdir(parents=True)
        row = {'case': case['id'], 'attempt': attempt}
        with (target / 'execution.log').open('w', encoding='utf-8') as log:
            try:
                done = subprocess.run([sys.executable, str(ROOT / 'tests/run_travel_dataset.py'),
                    '--dataset', str(Path(args.dataset).resolve()), '--case', case['id'], '--output', str(target / 'evidence')],
                    cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=240*len(case['steps'])+90,
                    env=dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1'))
                row['returncode'] = done.returncode
            except subprocess.TimeoutExpired:
                row['error'] = 'Process deadline exceeded; retain partial evidence'
        result_path = target / 'evidence/result.json'
        if result_path.exists():
            row['result'] = json.loads(result_path.read_text(encoding='utf-8'))
        else:
            row['error'] = row.get('error', 'No result.json; inspect execution.log')
        return row

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for future in as_completed([pool.submit(run, c, a) for c in live for a in range(1, args.repeats+1)]):
            item = future.result()
            completed.append(item)
            print(json.dumps({'case': item['case'], 'attempt': item['attempt'],
                'status': item.get('result', {}).get('status', 'execution_error'),
                'machine_checks_pass': item.get('result', {}).get('machine_checks_pass')}, ensure_ascii=False), flush=True)
            dump(output / 'batch.json', completed)
    system = None
    if controlled:
        nodes = list(dict.fromkeys(n for c in controlled for n in c.get('pytest_nodes', [])))
        if data['dataset_id'] == 'travel_agent_v1':
            nodes = ['tests/automated/test_travel_dataset_system.py']
        dump(output / 'system-case-mapping.json', {c['id']: c.get('pytest_nodes', nodes) for c in controlled})
        with (output / 'system.log').open('w', encoding='utf-8') as log:
            done = subprocess.run([sys.executable, '-m', 'pytest', *nodes, '-q',
                '--junitxml=' + str(output / 'system-results.xml')], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                env=dict(os.environ, TRAVEL_PERSISTENCE='0', TRAVEL_AGENT_DISABLE_LLM='1', PYTHONIOENCODING='utf-8'), timeout=180)
        system = {'case_count': len(controlled), 'returncode': done.returncode, 'nodes': nodes}
    summary = {'live_attempts': len(completed), 'execution_status_counts': dict(Counter(
        r.get('result', {}).get('status', 'execution_error') for r in completed)),
        'machine_checks_pass': sum(r.get('result', {}).get('machine_checks_pass', False) for r in completed),
        'quality_grade': 'pending_review' if live else 'not_applicable', 'system': system,
        'timing_by_action': summarize_live(completed), 'finished_at': datetime.now().isoformat()}
    dump(output / 'summary.json', summary)
    (output / 'batch.done').write_text('complete', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 1 if system and system['returncode'] or any(r.get('returncode', 1) != 0 or r.get('error') for r in completed) else 0


if __name__ == '__main__':
    sys.exit(main())
