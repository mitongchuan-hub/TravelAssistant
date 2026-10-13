"""Sequential, isolated acceptance jobs; retain failures and independent evidence."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jobs', nargs='+')
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error('Use a new empty directory; first attempts must be preserved')
    output.mkdir(parents=True, exist_ok=True)
    snapshot = output/'source'
    hashes = {}
    files = list((ROOT/'app').rglob('*.py')) + list((ROOT/'app/prompts').rglob('*.txt')) + list((ROOT/'app/schemas').glob('*.json'))
    for path in files:
        relative = path.relative_to(ROOT)
        target = snapshot/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        hashes[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    jobs = [
        ('display-historical', ['tests/performance/run_display_semantics.py', '--repeats', '1']),
        ('display-boundaries', ['tests/performance/run_display_semantics.py', '--dataset', 'tests/datasets/display_boundaries_v1/cases.json', '--repeats', '1']),
        ('business-chain', ['tests/run_travel_dataset.py', '--dataset', 'tests/datasets/optimization_chain_v1/cases.json', '--case', 'CHAIN-001']),
        ('business-boundaries', ['tests/run_travel_dataset_batch.py', '--cases', 'TA2-006', 'TA2-008', 'TA2-009', 'TA2-018', 'TA2-039', '--repeats', '1', '--workers', '1']),
        ('seven-day-live', ['tests/performance/run_agent_latency.py', '--cases', 'LAT-015', '--repeats', '1', '--tools', 'live', '--base-date', '2026-10-13']),
        ('amap-live', ['tests/performance/check_amap_mcp.py']),
        ('context-paired', ['tests/performance/compare_context_delivery.py', '--repeats', '3']),
    ]
    if args.jobs:
        known = {name for name, _ in jobs}
        if set(args.jobs) - known:
            parser.error('Unknown job selection')
        jobs = [job for job in jobs if job[0] in args.jobs]
    manifest = {'started_at': datetime.now().isoformat(), 'source_hashes': hashes,
                'scope': 'isolated_no_business_http', 'semantic_grade': 'pending_manual_review', 'jobs': []}
    def save():
        (output/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    for name, command in jobs:
        print(json.dumps({'event': 'job_start', 'job': name}), flush=True)
        with (output/(name+'.log')).open('w', encoding='utf-8') as stdout, (output/(name+'.err.log')).open('w', encoding='utf-8') as stderr:
            result = subprocess.run([sys.executable, '-X', 'utf8', *command, '--output', str(output/name)], cwd=ROOT, stdout=stdout, stderr=stderr)
        manifest['jobs'].append({'name': name, 'returncode': result.returncode})
        save()
        print(json.dumps({'event': 'job_finished', 'job': name, 'returncode': result.returncode}), flush=True)
    manifest['finished_at'] = datetime.now().isoformat()
    save()
    return 0 if all(job['returncode'] == 0 for job in manifest['jobs']) else 2


if __name__ == '__main__':
    raise SystemExit(main())
