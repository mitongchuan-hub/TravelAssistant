"""Paired same-window classifier comparison for removal of duplicate user messages."""
import argparse
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import statistics
import sys
from time import monotonic
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ['TRAVEL_PERSISTENCE'] = '0'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    output = args.output.resolve()
    if args.repeats < 1 or output.exists() and any(output.iterdir()):
        parser.error('Positive repeats and a new empty directory are required')
    output.mkdir(parents=True, exist_ok=True)
    from app import agent_client
    from app.agent_runtime import TravelAgentRuntime, TurnBudget
    from app.store import DemoStore
    from openai import OpenAI
    local = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    user = '预算改成两人合计2500元，费用范围不变，先记录，不要生成行程。'
    snapshot = {'trip': local.trips['trip-hangzhou'], 'plan': None, 'user_body': user,
                'current_date': '2026-10-13', 'recent_chat': [
                    {'role': 'user', 'body': '旅行节奏轻松，保留不吃辣，不安排购物店。'},
                    {'role': 'agent', 'body': '已记录偏好，未核实的价格、开放、预约和交通耗时保持待核实。'}]}
    records = []
    def save():
        (output/'results.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(1, args.repeats+1):
        # Reverse every second pair to reduce fixed ordering bias.
        order = ['duplicate', 'compact'] if attempt % 2 else ['compact', 'duplicate']
        for strategy in order:
            row = {'attempt': attempt, 'strategy': strategy, 'model': os.getenv('OPENAI_MODEL')}
            budget = TurnBudget(max_models=1, max_tools=0, max_external=0, seconds=180)
            def request(client, model, messages, **kwargs):
                sent = deepcopy(messages)
                if strategy == 'duplicate':
                    sent.append({'role': 'user', 'content': user})
                row.update(message_count=len(sent), input_characters=sum(len(m['content']) for m in sent))
                response = agent_client._create_chat_completion(client, model, sent, **kwargs)
                usage = getattr(response, 'usage', None)
                row['usage'] = {field: getattr(usage, field, None) for field in ('prompt_tokens', 'completion_tokens')}
                return response
            trace = agent_client.ConversationTrace(uuid4().hex, datetime.now().isoformat(), 'isolated-context', user)
            started = monotonic()
            try:
                with OpenAI(api_key=os.getenv('OPENAI_API_KEY'), base_url=os.getenv('OPENAI_BASE_URL') or None,
                            timeout=180, max_retries=0) as client:
                    runtime = TravelAgentRuntime(client, os.getenv('OPENAI_MODEL'), snapshot, trace=trace,
                                                 cache={'_budget': budget}, request=request)
                    route = runtime.classify()
                row.update(status='returned', route=route,
                           expected_route=route['intent'] == 'planning' and route['action'] == 'update_requirements')
            except Exception as exc:
                row.update(status='failed', error_type=type(exc).__name__)
            row.update(seconds=monotonic()-started, sdk_requests=budget.model_calls)
            records.append(row)
            save()
            (output/(f'{attempt}-{strategy}.txt')).write_text(''.join(trace.events), encoding='utf-8')
            print(json.dumps({k: row[k] for k in ('attempt', 'strategy', 'status', 'seconds')}), flush=True)
            if len(records) >= 2 and all(r['status'] == 'failed' for r in records[-2:]):
                print('Stopped after two consecutive failures', flush=True)
                return 2
    summary = {'scope': 'classifier_only_same_prompt_schema_snapshot_sdk; no business/tools/browser latency',
               'quality_scope': 'intent/action and schema, not full business semantics',
               'strategies': {}}
    for strategy in ('duplicate', 'compact'):
        rows = [r for r in records if r['strategy'] == strategy]
        summary['strategies'][strategy] = {'samples': len(rows), 'successes': sum(r.get('expected_route', False) for r in rows),
            'median_seconds_including_failures': statistics.median(r['seconds'] for r in rows),
            'input_characters': rows[0].get('input_characters')}
    (output/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if all(r.get('expected_route') for r in records) else 2


if __name__ == '__main__':
    raise SystemExit(main())
