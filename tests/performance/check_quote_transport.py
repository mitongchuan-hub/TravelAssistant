"""Read-only probe of preservation of quoted text across output transports."""
import argparse
import json
import os
from pathlib import Path
import sys
from time import monotonic

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ['TRAVEL_PERSISTENCE'] = '0'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error('Use a new directory')
    output.mkdir(parents=True, exist_ok=True)
    from app import agent_client
    from app.agent_runtime import TurnBudget
    from openai import OpenAI
    sentence = '请点餐时明确“不要辣、微辣也不要”，具体做法待确认。'
    schema = {'type': 'object', 'additionalProperties': False, 'required': ['text'], 'properties': {'text': {'type': 'string'}}}
    rows = []
    for mode, fmt in [('plain', None), ('object', {'type': 'json_object'}), ('schema', {'type': 'json_schema', 'json_schema': {'name': 'quote_copy', 'strict': True, 'schema': schema}})]:
        instruction = '逐字复制原句，包括中文引号，不解释、不改写、不截断。'
        instruction += '只返回原句。' if mode == 'plain' else '只返回JSON对象，text字段为完整原句，必须正确转义JSON字符串。'
        started = monotonic()
        row = {'mode': mode, 'source': sentence}
        try:
            with OpenAI(api_key=os.getenv('OPENAI_API_KEY'), base_url=os.getenv('OPENAI_BASE_URL') or None, max_retries=0, timeout=60) as client:
                result = agent_client._create_chat_completion(client, os.getenv('OPENAI_MODEL'), [{'role': 'system', 'content': instruction}, {'role': 'user', 'content': sentence}], response_format=fmt, reasoning_effort='low', agent_role='display', budget=TurnBudget(max_models=1, seconds=60))
            content = result.choices[0].message.content
            value = content if mode == 'plain' else json.loads(content)['text']
            row.update(status='returned', output=content, exact=value == sentence)
        except Exception as exc:
            row.update(status='failed', error_type=type(exc).__name__)
        row['seconds'] = monotonic()-started
        rows.append(row)
        (output/'results.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(row, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
