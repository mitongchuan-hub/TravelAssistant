"""Benchmark single-call latency for travel_agent and the HTML display node.

Uses current role prompts, tool schemas and transport with synthetic, offline-safe
context. These are representative requests, not production-identical payloads or
an end-to-end tool-loop/display-validation benchmark. No tools are executed.

Default mode prints the request plans without creating a client or using network.
Live calls require explicit --live (one warmup plus --runs measured calls per role).

Usage:
    E:/anaconda/envs/TravelAssistant/python.exe tests/benchmarks/agent_latency.py
    E:/anaconda/envs/TravelAssistant/python.exe tests/benchmarks/agent_latency.py --live --runs 5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

APP_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP_DIR / 'backend'))

from app.core import config, resources

from app.agents.model_client import request_completion
from app.domain.itinerary.identity import PlanBasis
from app.application.contracts import RequestContext
from app.agents.roles import display_role, role_reasoning_effort, travel_role
from app.agents.inputs.execution import TaskExecutionContext
from app.agents.travel_runtime import TaskExecutionRuntime
from app.domain.models import TripRequirements

USER_MSG = ('帮我安排11月1日开始、在杭州玩2天的行程，住西湖边的酒店，2个人，'
            '预算3000元，想逛西湖和吃杭帮菜。')
SOURCE_DATES = ['2026-11-01', '2026-11-02']
SOURCE_TEXT = '\n'.join([
    '2026-11-01：上午从酒店出发环西湖步行，中午在楼外楼用餐，下午游花港观鱼和苏堤，傍晚回酒店休息。',
    '2026-11-02：上午游灵隐寺和飞来峰，中午在灵隐附近餐馆用餐，下午在龙井村喝茶，之后返程。',
])


def travel_payload() -> dict:
    """Project a synthetic, unsaved first-turn snapshot through the real runtime."""
    trip = SimpleNamespace(requirements=TripRequirements(), destination='', note='',
                           style='', mode='city', lodging={}, workflow_state={})
    context = RequestContext(
        trip_id='bench-trip', owner_id='bench-owner', execution_task_id='bench-task',
        conversation_task_id=None, request_id='bench-request', message_id='bench-message',
        body=USER_MSG, entry='main', strategy='task_execution', requirements_revision=0,
        plan_basis=PlanBasis('', '', '', '', None, False), workflow_revision=0, sequence=0, epoch=0)
    execution = TaskExecutionContext(trip=trip, messages=[], plan=None,
                                     metrics={}, current_date='2026-10-19')
    runtime = TaskExecutionRuntime(None, 'offline-benchmark', context, execution, None)
    return runtime._initial_context()


def display_payload() -> dict:
    return {'source_text': SOURCE_TEXT, 'source_dates': list(SOURCE_DATES),
            'style_instruction': '', 'output_format': 'html'}


def build_agents() -> list[dict]:
    travel, display = travel_role(), display_role()
    return [
        {'name': travel.name, 'role': travel, 'user': travel_payload(),
         'tools': list(travel.tools), 'response_format': travel.response_format},
        {'name': display.name, 'role': display, 'user': display_payload(),
         'tools': None, 'response_format': display.response_format},
    ]


def positive_runs(value: str) -> int:
    runs = int(value)
    if runs < 1:
        raise argparse.ArgumentTypeError('--runs must be a positive integer')
    return runs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=positive_runs, default=5, help='measured runs per agent (default 5)')
    parser.add_argument('--live', action='store_true', help='allow client creation and live model requests')
    args = parser.parse_args()
    if args.live:
        config.load_environment()
        config.validate_configuration(scope='model')
    agents = build_agents()
    print('Synthetic single-call benchmark; not production-identical or end-to-end.')
    for agent in agents:
        print(f'{agent["name"]}: tools={len(agent["tools"] or [])} '
              f'response_format={agent["response_format"]} '
              f'reasoning_effort={role_reasoning_effort(agent["role"])} '
              f'payload={json.dumps(agent["user"], ensure_ascii=False, default=str)}')
    if not args.live:
        print(f'Offline plan only: --live required for network calls '
              f'({args.runs} measured runs + 1 warmup per role). No client created.')
        return 0

    model = config.model_name()

    rows: list[dict] = []
    with resources.client_scope(), resources.borrow_client() as client:
        for agent in agents:
            messages = [{'role': 'system', 'content': agent['role'].prompt},
                        {'role': 'user', 'content': json.dumps(agent['user'], ensure_ascii=False, default=str)}]
            effort = role_reasoning_effort(agent['role'])
            summary: dict | None = None
            for run in range(0, args.runs + 1):
                started = monotonic()
                error = None
                outcome = None
                usage = {}
                try:
                    response = request_completion(
                        client, model, messages, tools=agent['tools'],
                        response_format=agent['response_format'],
                        reasoning_effort=effort, agent_role=agent['name'])
                    message = response.choices[0].message
                    calls = list(getattr(message, 'tool_calls', None) or [])
                    outcome = ','.join(call.function.name for call in calls) or (
                        (message.content or '').strip()[:60])
                    usage_info = getattr(response, 'usage', None)
                    usage = {'prompt_tokens': getattr(usage_info, 'prompt_tokens', None),
                             'completion_tokens': getattr(usage_info, 'completion_tokens', None)}
                except Exception as exc:  # noqa: BLE001 - report, do not hide
                    error = f'{type(exc).__name__}: {str(exc)[:200]}'
                seconds = round(monotonic() - started, 3)
                row = {'agent': agent['name'], 'run': run, 'warmup': run == 0,
                       'seconds': seconds, 'outcome': outcome, 'usage': usage, 'error': error}
                rows.append(row)
                mark = '预热' if run == 0 else f'第{run}次'
                if error:
                    print(f'  {agent["name"]:26s} {mark} {seconds:8.3f}s  失败: {error}')
                else:
                    print(f'  {agent["name"]:26s} {mark} {seconds:8.3f}s  '
                          f'in={usage.get("prompt_tokens")} out={usage.get("completion_tokens")}  '
                          f'-> {outcome}')
            measured = [r for r in rows if r['agent'] == agent['name'] and not r['warmup']]
            ok = [r['seconds'] for r in measured if not r['error']]
            if ok:
                ok_sorted = sorted(ok)
                summary = {
                    'runs': len(measured), 'ok': len(ok),
                    'min': ok_sorted[0], 'mean': round(sum(ok) / len(ok), 3),
                    'p50': ok_sorted[len(ok_sorted) // 2], 'max': ok_sorted[-1],
                    'prompt_tokens': measured[0]['usage'].get('prompt_tokens'),
                    'completion_tokens': measured[0]['usage'].get('completion_tokens'),
                    'reasoning_effort': effort,
                }
            print(f'  -> 汇总 {agent["name"]}: {summary}\n')

    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out_dir = APP_DIR / '.cache' / 'bench_latency'
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f'{stamp}.json'
    out_file.write_text(json.dumps(
        {'model': model, 'base_url': os.getenv('OPENAI_BASE_URL'), 'runs': args.runs,
         'payload_kind': 'synthetic', 'measurement': 'single_model_call',
         'rows': rows}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'结果已保存: {out_file}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
