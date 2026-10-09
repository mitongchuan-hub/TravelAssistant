"""Run independent live dataset cases in isolated subprocesses; never grade prose automatically."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', required=True)
    p.add_argument('--workers', type=int, default=2)
    p.add_argument('--cases', nargs='+', help='Optional live case IDs; default runs all live cases')
    args = p.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    data = json.loads((ROOT/'tests/datasets/travel_agent_v1/cases.json').read_text(encoding='utf-8'))
    controlled = {f'TA-{i:03d}' for i in (30,31,32,33,34,36)}
    cases = [c['id'] for c in data['cases'] if c['id'] not in controlled]
    if args.cases:
        if set(args.cases) - set(cases):
            p.error('Unknown or controlled case ID; controlled cases use pytest')
        cases = list(dict.fromkeys(args.cases))
    metadata = {'dataset':data['dataset_id'],'workers':args.workers,
                'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                'working_tree':subprocess.check_output(['git','status','--short'],cwd=ROOT,text=True),
                'live_cases':cases, 'controlled_cases':sorted(controlled)}
    (output/'metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    def run(case):
        target = output/case
        target.mkdir(exist_ok=True)
        with (target/'execution.log').open('w',encoding='utf-8') as log:
            try:
                done = subprocess.run([sys.executable,str(ROOT/'tests/run_travel_dataset.py'),'--case',case,'--output',str(target)],
                    cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=540,env=dict(os.environ,PYTHONIOENCODING='utf-8'))
                return {'case':case,'returncode':done.returncode}
            except subprocess.TimeoutExpired:
                return {'case':case,'error':'Process deadline 540s exceeded'}
    completed = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for future in as_completed([pool.submit(run,c) for c in cases]):
            item = future.result()
            completed.append(item)
            print(json.dumps(item),flush=True)
            (output/'batch.json').write_text(json.dumps(completed,indent=2),encoding='utf-8')
    (output/'batch.done').write_text('complete',encoding='utf-8')


if __name__ == '__main__':
    main()
