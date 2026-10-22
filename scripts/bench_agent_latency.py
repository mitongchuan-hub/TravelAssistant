"""Benchmark per-agent model response time with production-equivalent requests.

Each agent is measured on a single representative model call built exactly the
way production builds it: same system prompt, same tool schemas, same payload
shape, same reasoning effort and thinking flag, through the same
``_create_chat_completion`` wire path.  This measures the model's response time
for that agent's task (prefill + generation), not the full multi-step tool
loop (that is a separate end-to-end measurement).

Usage:
    E:/anaconda/envs/TravelAssistant/python.exe scripts/bench_agent_latency.py [--runs 5]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from time import monotonic

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from dotenv import load_dotenv

load_dotenv(APP_DIR / ".env", override=False)

from openai import OpenAI

from app.agents.agent_client import _create_chat_completion
from app.agents.agent_roles import execution_roles, roles, role_reasoning_effort
from app.services.plan_display import display_format

USER_MSG = ('帮我安排11月1日开始、在杭州玩2天的行程，住西湖边的酒店，2个人，'
            '预算3000元，想逛西湖和吃杭帮菜。')

MAIN_PAYLOAD = {
    'message': {'id': 'msg-1', 'body': USER_MSG, 'created_at': '2026-10-19T10:00:00'},
    'environment': {'today': '2026-10-19', 'mode': 'city', 'entry': 'chat'},
    'requirements': {'revision': 0, 'city': '', 'start_date': '', 'end_date': '',
                     'travelers': None, 'budget_amount': '', 'budget_scope': 'unspecified'},
    'current_plan': None,
    'conversation': [{'id': 'msg-1', 'sender': 'user', 'body': USER_MSG}],
    'task_context': {'tasks': [], 'active_question': None},
}

SOURCES = [
    '2026-11-01：上午从酒店出发环西湖步行，中午在楼外楼用餐，下午游花港观鱼和苏堤，傍晚回酒店休息。',
    '2026-11-02：上午游灵隐寺和飞来峰，中午在灵隐附近餐馆用餐，下午在龙井村喝茶，之后返程。',
]
DAYS = {'2026-11-01': '第1天', '2026-11-02': '第2天'}
SOURCE_TEXT = '\n'.join(SOURCES)


def capability_payload(task: str, *, scope: str = '', plan_id: str | None = None,
                       with_idea_wall: bool = False) -> dict:
    payload = {
        'capability_request': task,
        'user_message': USER_MSG,
        'request_message_ref': 'msg-1',
        'conversation_task_ref': None,
        'requirements_ref': None,
        'scope': scope,
        'target_item_id': None,
        'current_date': '2026-10-19',
        'requirements': {'revision': 1, 'city': '杭州', 'start_date': '2026-11-01',
                         'end_date': '2026-11-02', 'travelers': 2, 'budget_amount': '3000',
                         'budget_scope': 'total', 'lodging_address': '西湖边的酒店'},
        'plan_basis': {'plan_id': plan_id, 'revision': 0} if plan_id else {'plan_id': None, 'revision': 0},
        'external_query_budget': {'remaining': 32, 'search_max_calls': 6},
    }
    if with_idea_wall:
        payload['idea_wall'] = {'items': [{'ref': 'idea-1', 'title': '杭帮菜',
                                           'body': '想吃楼外楼或同级餐馆', 'kind': '预算'}]}
    return payload


def display_payload() -> tuple[dict, dict | None]:
    schema_root = display_format(DAYS)
    payload = {'source_text': SOURCE_TEXT, 'source_dates': DAYS, 'output_format': 'yaml',
               'output_schema': schema_root['json_schema']['schema']}
    return payload, schema_root


def build_agents():
    exec_cfgs = execution_roles('city')
    roles_cfgs = roles('city')
    requirements = capability_payload('整理本轮正式需求：杭州、11月1日起2天、2人、预算3000、住西湖边。')
    research = capability_payload('查询：杭州11月1日至11月2日的天气，以及楼外楼的位置和大致营业时间。')
    planning = capability_payload('根据需求生成完整文字行程。', with_idea_wall=True)
    revision = capability_payload('把第2天中午的用餐改成楼外楼，其他保持不变。', scope='item', plan_id='plan-1')
    display_payload_body, display_format_spec = display_payload()
    return [
        {'name': 'main_execution', 'role': exec_cfgs['main_execution'],
         'user': MAIN_PAYLOAD, 'tools': list(exec_cfgs['main_execution'].tools), 'response_format': None},
        {'name': 'requirements_capability', 'role': exec_cfgs['requirements_capability'],
         'user': requirements, 'tools': list(exec_cfgs['requirements_capability'].tools),
         'response_format': None},
        {'name': 'research_capability', 'role': exec_cfgs['research_capability'],
         'user': research, 'tools': list(exec_cfgs['research_capability'].tools), 'response_format': None},
        {'name': 'planning_capability', 'role': exec_cfgs['planning_capability'],
         'user': planning, 'tools': list(exec_cfgs['planning_capability'].tools), 'response_format': None},
        {'name': 'revision_capability', 'role': exec_cfgs['revision_capability'],
         'user': revision, 'tools': list(exec_cfgs['revision_capability'].tools), 'response_format': None},
        {'name': 'display', 'role': roles_cfgs['display'],
         'user': display_payload_body, 'tools': None, 'response_format': display_format_spec},
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=int, default=5, help='measured runs per agent (default 5)')
    args = parser.parse_args()

    if not os.getenv('OPENAI_API_KEY'):
        print('OPENAI_API_KEY 未设置，无法运行基准测试。')
        return 1
    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'),
                    base_url=os.getenv('OPENAI_BASE_URL') or None,
                    timeout=float(os.getenv('OPENAI_TIMEOUT_SECONDS', '180')), max_retries=0)
    model = os.getenv('OPENAI_MODEL') or os.getenv('MODEL_ID') or 'qwen-plus'
    agents = build_agents()

    rows: list[dict] = []
    try:
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
                    response = _create_chat_completion(
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
    finally:
        if callable(getattr(client, 'close', None)):
            client.close()

    stamp = datetime.now().strftime('%Y%m%dT%H%M%SZ')
    out_dir = APP_DIR / '.cache' / 'bench_latency'
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f'{stamp}.json'
    out_file.write_text(json.dumps(
        {'model': model, 'base_url': os.getenv('OPENAI_BASE_URL'), 'runs': args.runs,
         'rows': rows}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'结果已保存: {out_file}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
