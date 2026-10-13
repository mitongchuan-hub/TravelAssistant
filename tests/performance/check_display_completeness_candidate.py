"""Isolated completeness/serialization prompt experiment; production remains unchanged."""
import argparse
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from time import monotonic
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ['TRAVEL_PERSISTENCE'] = '0'

SUFFIX = '''
逐日完整提取：每一天从首个条目读到当天最后一个条目，把所有原文安排按顺序放入items。items不是每日摘要，不能只保留第一项或代表性景点。原文的用餐、休息、抵离和条件备选同样需要保留；某项只写先后顺序时time_text=null，不能用“之后”替代时间。只放行李属于accommodation。天气风险或核实提醒放risks/pending_items，不另造一条预报。
时长字段只放该条目自身明示的时长及其限定；起止时刻区间不是时长，缓冲和子活动时长不等于主活动时长。原文多个交通选项的耗时不能选一个作为整个条目的时长，保留在description或notes。
文本必须形成完整句意；原文中的引号是文本的一部分。保留中文引号，JSON字符串中的英文双引号和换行按JSON规则转义，不能在原文引号处截断内容。没有明确标题时，用原文完整的总体安排句作为title，不截取孤立片段。输出长度以完整提取为准，不为简短而省略当天后续安排。
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--efforts', nargs='+', choices=('low', 'medium'), default=['low', 'medium'])
    parser.add_argument('--format', choices=('schema', 'object', 'plain', 'yaml'), default='schema')
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error('Use a new directory')
    output.mkdir(parents=True, exist_ok=True)
    from app import agent_client
    from app.agent_runtime import TravelAgentRuntime, TurnBudget
    from app.plan_display import dates_from_snapshot
    from openai import OpenAI
    case = next(c for c in json.loads((ROOT/'tests/datasets/display_semantics_v1/cases.json').read_text(encoding='utf-8'))['cases'] if c['id'] == 'LAT-015')
    prompt = (ROOT/'app/prompts/agents/display.txt').read_text(encoding='utf-8') + SUFFIX
    (output/'candidate-prompt.txt').write_text(prompt, encoding='utf-8')
    (output/'source.json').write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding='utf-8')
    rows = []
    failures = 0
    for attempt in range(1, args.repeats+1):
        for effort in args.efforts if attempt % 2 else list(reversed(args.efforts)):
            os.environ['OPENAI_REASONING_EFFORT_DISPLAY'] = effort
            trace = agent_client.ConversationTrace(uuid4().hex, datetime.now().isoformat(), 'isolated-completeness', case['source_text'])
            budget = TurnBudget(max_models=1, max_tools=0, max_external=0, seconds=180)
            started = monotonic()
            with OpenAI(api_key=os.getenv('OPENAI_API_KEY'), base_url=os.getenv('OPENAI_BASE_URL') or None, timeout=180, max_retries=0) as client:
                runtime = TravelAgentRuntime(client, os.getenv('OPENAI_MODEL'), {'source_dates': dates_from_snapshot(case['requirements_snapshot'])}, trace=trace, cache={'_budget': budget}, request=agent_client._create_chat_completion)
                runtime.configs['display'] = replace(runtime.configs['display'], prompt=prompt)
                if args.format in {'object', 'plain'}:
                    original_node = runtime.model_node
                    from app.plan_display import display_format
                    object_prompt = prompt + '\n输出结构如下（严格保留字段和类型）：\n' + json.dumps(display_format(dates_from_snapshot(case['requirements_snapshot']))['json_schema']['schema'], ensure_ascii=False)
                    runtime.configs['display'] = replace(runtime.configs['display'], prompt=object_prompt)
                    def object_node(role, payload, **kwargs):
                        return original_node(role, payload, response_format={'type': 'json_object'} if args.format == 'object' else None)
                    runtime.model_node = object_node
                if args.format == 'yaml':
                    from app.plan_display import display_format, parse_display_output, validate_display
                    yaml_prompt = prompt.replace('JSON', 'YAML') + '\n仅返回YAML，不使用JSON语法。所有文本字段用块标量|-保留完整句子及引号；日期和schema_version用带引号的字符串。空列表[]，未知null。不要使用别名、标签或重复字段。'
                    runtime.configs['display'] = replace(runtime.configs['display'], prompt=yaml_prompt)
                    try:
                        dates = dates_from_snapshot(case['requirements_snapshot'])
                        raw = runtime.model_node('display', {'source_text': case['source_text'], 'source_dates': dates,
                            'output_schema': display_format(dates)['json_schema']['schema']}, response_format=None)
                        data = validate_display(parse_display_output(raw, 'yaml'), dates)
                        reply = replace(agent_client.AgentReply(case['source_text'], None, []), display=data, display_status='ready')
                    except Exception as exc:
                        runtime.cache['display_error'] = {'error_type': type(exc).__name__, 'reason': str(exc)[:500]}
                        reply = agent_client.AgentReply(case['source_text'], None, [], display_status='fallback')
                else:
                    reply = runtime.convert_display(agent_client.AgentReply(case['source_text'], None, [], plan_text=case['source_text']))
            row = {'attempt': attempt, 'effort': effort, 'format': args.format, 'seconds': monotonic()-started, 'sdk_requests': budget.model_calls,
                   'status': reply.display_status, 'display': reply.display, 'error': runtime.cache.get('display_error')}
            rows.append(row)
            (output/'results.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
            (output/(f'{attempt}-{effort}.txt')).write_text(''.join(trace.events), encoding='utf-8')
            print(json.dumps({k: row[k] for k in ('attempt', 'effort', 'seconds', 'status')}, ensure_ascii=False), flush=True)
            failures = failures+1 if reply.display is None else 0
            if failures >= 2:
                return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
