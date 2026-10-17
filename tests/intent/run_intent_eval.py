"""Intent-node evaluation: run tests/intent/cases.json through the real pipeline.

Walks the same path as production (roles() main config + _create_chat_completion,
agent_role='main' so the no-thinking extra_body applies), then scores ten metrics:
full-chain accuracy, per-field accuracy, per-intent P/R/F1, confusion matrix,
clarification recall, spurious-question rate, quote fidelity, output validity,
latency P50/P95, and stratified accuracy (has_plan x requirements completeness).

Output: tests/intent/report.json + a human summary on stdout.
"""
import json
import math
import os
import statistics
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / '.env', override=False)

from jsonschema import Draft202012Validator  # noqa: E402
from openai import OpenAI  # noqa: E402

from app.agent_client import _create_chat_completion  # noqa: E402
from app.agent_roles import INTENT_SCHEMA, role_reasoning_effort, roles  # noqa: E402
from app.agent_runtime import TurnBudget  # noqa: E402

GAP_SECONDS = float(os.environ.get('INTENT_EVAL_GAP', '0.3'))
MODELS = int(os.environ.get('INTENT_EVAL_MODELS', '16'))

ALLOWED = {'chat': {'reply'}, 'query': {'reply'}, 'requirements': {'update_requirements'},
           'planning': {'update_and_query', 'generate', 'cancel'},
           'revision': {'modify', 'explain', 'confirm'}}

SCHEMA_VALIDATOR = Draft202012Validator(INTENT_SCHEMA)


def build_messages(config, case):
    context = case['context']
    payload = {'user_message': case['user_message'], 'conversation': context.get('conversation', []),
               'current_date': date.today().isoformat(), 'has_plan': bool(context.get('has_plan')),
               'workflow_state': context.get('workflow_state', {}),
               'requirements': context.get('requirements', {}),
               'missing_requirements': context.get('missing', [])}
    return [{'role': 'system', 'content': config.prompt},
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]


def run_case(client, model, config, case, budget):
    messages = build_messages(config, case)
    started = time.monotonic()
    try:
        response = _create_chat_completion(
            client, model, messages, tools=None,
            response_format=config.response_format, budget=budget,
            reasoning_effort=role_reasoning_effort(config), agent_role='main')
    except Exception as exc:  # noqa: BLE001
        return {'error': f'{exc.__class__.__name__}: {str(exc)[:160]}', 'ms': (time.monotonic() - started) * 1000}
    ms = (time.monotonic() - started) * 1000
    raw = (response.choices[0].message.content or '').strip()
    result = {'ms': ms, 'raw': raw, 'error': None, 'json_ok': False, 'schema_ok': False,
              'matrix_ok': False, 'intent': None, 'action': None, 'scope': None,
              'question': None, 'user_quote': None}
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        result['intent'] = 'BAD_JSON'
        return result
    result['json_ok'] = True
    if SCHEMA_VALIDATOR.is_valid(data):
        result['schema_ok'] = True
        result['intent'] = data.get('intent')
        result['action'] = data.get('action')
        result['scope'] = data.get('scope')
        result['question'] = data.get('question')
        result['user_quote'] = data.get('user_quote')
        result['matrix_ok'] = result['intent'] in ALLOWED and result['action'] in ALLOWED[result['intent']]
    return result


def score_case(case, actual):
    expected = case['expected']
    actual = {**{'json_ok': False, 'schema_ok': False, 'matrix_ok': False, 'error': None,
                 'intent': None, 'action': None, 'scope': None, 'question': None, 'user_quote': None},
              **actual}
    bits = {
        'intent': actual['intent'] == expected['intent'],
        'action': actual['action'] == expected['action'],
        'scope': actual['scope'] == expected['scope'],
    }
    question = (actual['question'] or '').strip()
    bits['question'] = (bool(question) == bool(expected['question']))
    quote = (actual['user_quote'] or '').strip()
    bits['quote'] = quote == case['user_message'].strip()
    bits['valid'] = bool(actual['json_ok'] and actual['schema_ok'] and actual['matrix_ok'] and not actual['error'])
    bits['full'] = all(bits.values())
    return bits, bool(question)


def pct(n, d):
    return round(100.0 * n / d, 1) if d else None


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(p / 100.0 * len(ordered)) - 1)
    return round(ordered[index], 1)


def prf(actual, expected):
    tp = sum(1 for a, e in zip(actual, expected) if a == e and e is not None)
    fp = sum(1 for a, e in zip(actual, expected) if a == e and e is None)
    fn = sum(1 for a, e in zip(actual, expected) if a != e and e is not None)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return round(precision, 3), round(recall, 3), round(f1, 3)


def main():
    config = roles('travel')['main']
    with open(Path(__file__).parent / 'cases.json', encoding='utf-8') as fh:
        cases = json.load(fh)['cases']
    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'), base_url=os.getenv('OPENAI_BASE_URL') or None,
                    timeout=float(os.getenv('OPENAI_TIMEOUT_SECONDS', '180')), max_retries=0)
    model = os.getenv('OPENAI_MODEL') or os.getenv('MODEL_ID') or 'qwen-plus'
    print(f'model={model}  cases={len(cases)}')

    rows = []
    for case in cases:
        budget = TurnBudget(max_models=MODELS)
        actual = run_case(client, model, config, case, budget)
        bits, had_question = score_case(case, actual)
        rows.append({'id': case['id'], 'expected': case['expected'], 'actual': actual, 'bits': bits,
                     'had_question': had_question, 'user_message': case['user_message']})
        mark = 'OK ' if bits['full'] else 'FAIL'
        print(f"  [{mark}] {case['id']:<22} {actual['ms']:6.0f} ms  "
              f"want {case['expected']['intent']}/{case['expected']['action']}/{case['expected']['scope'] or '-'}  "
              f"got {actual['intent']}/{actual['action']}/{actual['scope'] or '-'}"
              + (f"  ERROR {actual['error']}" if actual['error'] else ''))
        time.sleep(GAP_SECONDS)

    n = len(rows)
    valid = [r for r in rows if r['bits']['valid']]
    report = {
        'model': model, 'date': date.today().isoformat(), 'n_cases': n,
        'metrics': {
            'full_chain_accuracy_pct': pct(sum(r['bits']['full'] for r in rows), n),
            'field_accuracy_pct': {field: pct(sum(r['bits'][field] for r in rows), n)
                                   for field in ('intent', 'action', 'scope', 'question', 'quote')},
            'output_validity_pct': pct(len(valid), n),
            'quote_fidelity_pct': pct(sum(r['bits']['quote'] for r in valid), len(valid)),
            'question_recall_pct': pct(sum(r['bits']['question'] for r in rows if r['expected']['question']),
                                      sum(1 for r in rows if r['expected']['question'])),
            'no_spurious_question_pct': pct(sum(r['bits']['question'] for r in rows if not r['expected']['question']),
                                            sum(1 for r in rows if not r['expected']['question'])),
            'latency_ms': {'p50': percentile([r['actual']['ms'] for r in rows], 50),
                           'p95': percentile([r['actual']['ms'] for r in rows], 95),
                           'max': max(r['actual']['ms'] for r in rows) if rows else None},
        },
        'per_intent_prf': {}, 'confusion_matrix': {}, 'stratified_full_chain_pct': {},
        'failures': [r for r in rows if not r['bits']['full']],
    }

    intents = sorted({r['expected']['intent'] for r in rows})
    for intent in intents:
        group = [r for r in rows if r['expected']['intent'] == intent]
        actuals = [r['actual']['intent'] for r in group]
        expecteds = [r['expected']['intent'] for r in group]
        report['per_intent_prf'][intent] = {'n': len(group), 'precision_recall_f1': list(prf(actuals, expecteds))}
    matrix = {}
    for r in rows:
        key = f"{r['expected']['intent']} -> {r['actual']['intent']}"
        matrix[key] = matrix.get(key, 0) + 1
    report['confusion_matrix'] = dict(sorted(matrix.items()))

    by_id = {case['id']: case for case in cases}

    def stratify(pick):
        groups = {}
        for row in rows:
            groups.setdefault(pick(by_id[row['id']]), []).append(row)
        return {name: {'n': len(members),
                       'full_chain_accuracy_pct': pct(sum(x['bits']['full'] for x in members), len(members))}
                for name, members in sorted(groups.items(), key=lambda kv: str(kv[0]))}

    report['stratified_full_chain_pct'] = {
        'by_has_plan': stratify(lambda case: bool(case['context'].get('has_plan'))),
        'by_requirements_complete': stratify(lambda case: not case['context'].get('missing')),
        'by_category': stratify(lambda case: case.get('category', '未分类')),
    }

    with open(Path(__file__).parent / 'report.json', 'w', encoding='utf-8') as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    m = report['metrics']
    print('\n================ 评测结果 ================')
    print(f"全链路准确率      {m['full_chain_accuracy_pct']}%  ({sum(r['bits']['full'] for r in rows)}/{n})")
    for field, value in m['field_accuracy_pct'].items():
        print(f"  {field:<8} 准确率 {value}%")
    print(f"输出合法性        {m['output_validity_pct']}%    引用保真 {m['quote_fidelity_pct']}%")
    print(f"澄清召回          {m['question_recall_pct']}%    不误问 {m['no_spurious_question_pct']}%")
    lat = m['latency_ms']
    print(f"延迟              P50={lat['p50']} ms  P95={lat['p95']} ms  max={lat['max']} ms")
    print('\n各意图 P/R/F1:')
    for intent, item in report['per_intent_prf'].items():
        p, r, f = item['precision_recall_f1']
        print(f"  {intent:<14} n={item['n']:<3} P={p:.2f} R={r:.2f} F1={f:.2f}")
    print('\n混淆（期望 -> 实际）:')
    for key, count in report['confusion_matrix'].items():
        flag = '' if ' -> ' not in key or key.split(' -> ')[0] == key.split(' -> ')[1] else '   <-- 错'
        print(f'  {key:<36} {count}{flag}')
    print('\n分层全链路准确率:')
    for dimension, groups in report['stratified_full_chain_pct'].items():
        for name, item in groups.items():
            print(f"  [{dimension}={name}] n={item['n']}  {item['full_chain_accuracy_pct']}%")
    failed = report['failures']
    print(f"\n失败 {len(failed)}/{n} 条：")
    for r in failed:
        wrong = [k for k in ('intent', 'action', 'scope', 'question', 'quote', 'valid') if not r['bits'][k]]
        print(f"  - {r['id']}: 错在 {','.join(wrong)}  got {r['actual']['intent']}/{r['actual']['action']}/{r['actual']['scope'] or '-'}"
              f"  q={'有' if r['had_question'] else '无'}")
    print('\nreport.json 已写入 tests/intent/report.json')


if __name__ == '__main__':
    main()
