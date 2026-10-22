"""Comprehensive scope evaluation. Live calls require --live explicitly."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from tests.scope_eval import run_case

DATASET = Path('tests/datasets/scope_comprehensive.json')
BUSINESS = {'research', 'prepare_requirements', 'draft_itinerary', 'revise_itinerary', 'confirm_itinerary'}


def load_cases():
    cases = json.loads(DATASET.read_text(encoding='utf-8'))['cases']
    assert len({case['id'] for case in cases}) == len(cases)
    for case in cases:
        assert all(case.get(key) for key in ['id', 'category', 'level', 'input', 'rubric'])
        assert set(case.get('require_any', [])) <= BUSINESS
    return cases


def mechanical_checks(case, result):
    if 'error' in result:
        return {'execution_completed': False}
    names = {call['name'] for call in result['calls']}
    checks = {'execution_completed': not result['failed'], 'input_snapshot_unchanged': result['unchanged'],
              'no_selected_business_effects': not result['selected']}
    if case.get('forbid_business'):
        checks['no_business_delegation'] = not bool(names & BUSINESS)
    if case.get('require_any'):
        checks['expected_delegation_attempted'] = bool(names & set(case['require_any']))
    if case.get('preserve_question'):
        initial = result['initial_workflow']
        proposed = result['proposed_workflow']
        checks['waiting_question_preserved'] = bool(proposed and
            initial['active_question'] == proposed.get('active_question') and
            initial['tasks'] == proposed.get('tasks'))
    if case['id'] == 'Q04':
        checks['answer_bound_to_question'] = any(
            call['arguments'].get('answer_question_ref') == 'pending-question' and
            call['arguments'].get('task_ref') == 'pending-trip' for call in result['calls'])
    # These are contracts, never semantic pass/fail. Human review is mandatory.
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--ids', nargs='*')
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--workers', type=int, choices=[1, 2, 3], default=3)
    parser.add_argument('--label', default='comprehensive')
    args = parser.parse_args()
    assert 1 <= args.repeat <= 5
    assert args.label.replace('-', '').replace('_', '').isalnum()
    cases = load_cases()
    if args.ids:
        assert set(args.ids) <= {case['id'] for case in cases}
        cases = [case for case in cases if case['id'] in args.ids]
    if not args.live:
        from collections import Counter
        print(json.dumps(dict(count=len(cases), categories=Counter(case['category'] for case in cases),
                              levels=Counter(case['level'] for case in cases)), ensure_ascii=False, indent=2))
        return
    from openai import OpenAI
    model = os.getenv('OPENAI_MODEL') or os.getenv('MODEL_ID') or 'qwen-plus'
    prompt = Path('app/prompts/agents/main.txt').read_text(encoding='utf-8')
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    output = Path('.cache/scope_eval') / f'{args.label}-{uuid4().hex[:8]}.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    report = dict(model=model, wire_api=os.getenv('OPENAI_WIRE_API'),
        started_at=datetime.now(timezone.utc).isoformat(), prompt=prompt, prompt_sha256=prompt_hash,
        dataset_sha256=hashlib.sha256(DATASET.read_bytes()).hexdigest(), dataset=str(DATASET),
        capability_mode='unavailable; optional untrusted summary injection', results=[])

    def evaluate(case, attempt):
        try:
            result = run_case(case, client, model)
        except Exception as exc:
            result = dict(id=case['id'], error=type(exc).__name__)
        result.update(category=case['category'], level=case['level'], attempt=attempt,
                      rubric=case['rubric'], input=case['input'], semantic_review='pending')
        result['mechanical_checks'] = mechanical_checks(case, result)
        return result

    with OpenAI(api_key=os.getenv('OPENAI_API_KEY'), base_url=os.getenv('OPENAI_BASE_URL') or None,
                timeout=60, max_retries=0) as client, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(evaluate, case, attempt + 1)
                   for case in cases for attempt in range(args.repeat)]
        for future in as_completed(futures):
            result = future.result()
            report['results'].append(result)
            temp = output.with_suffix('.tmp')
            temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(output)
            print(json.dumps(dict(id=result['id'], attempt=result['attempt'],
                checks=result['mechanical_checks'], answer=result.get('answer', ''),
                question=result.get('question'), error=result.get('error'),
                reason=result.get('reason'), runtime_errors=result.get('errors')), ensure_ascii=False), flush=True)
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    report['prompt_unchanged_during_run'] = hashlib.sha256(
        Path('app/prompts/agents/main.txt').read_text(encoding='utf-8').encode()).hexdigest() == prompt_hash
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(output, flush=True)


if __name__ == '__main__':
    main()
