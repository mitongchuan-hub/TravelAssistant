"""Live reasoning-effort smoke test. No business Store import or database writes."""
from pathlib import Path
from datetime import datetime, timezone
from time import monotonic
import hashlib
import json
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['TRAVEL_PERSISTENCE'] = '0'
from app.agent_client import _create_chat_completion, ConversationTrace
from app.agent_runtime import TurnBudget, TravelAgentRuntime
from app.agent_roles import roles, role_reasoning_effort
from app.models import Trip, User
from openai import OpenAI


def main():
    path = ROOT / '.cache/travelassistant.sqlite3'
    before = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    trace = ConversationTrace('reasoning-live', datetime.now().isoformat(timespec='seconds'), 'synthetic-reasoning', '接口能力验证')
    model = os.getenv('OPENAI_MODEL', 'kimi-k3')
    result = {'model': model, 'at': datetime.now(timezone.utc).isoformat(), 'cases': []}
    def record(name, response, started, sent):
        message = response.choices[0].message
        usage = response.usage.model_dump() if response.usage else {}
        result['cases'].append({'name': name, 'sent_effort': sent,
                                'seconds': round(monotonic() - started, 3),
                                'has_tool_calls': bool(message.tool_calls), 'answer': message.content,
                                'usage': usage})
        print(json.dumps(result['cases'][-1], ensure_ascii=False), flush=True)
    with OpenAI(api_key=os.environ['OPENAI_API_KEY'], base_url=os.getenv('OPENAI_BASE_URL'), timeout=40, max_retries=0) as client:
        # Go through the actual role runner and nested delegation.
        trip = Trip('synthetic-reasoning', '杭州', '待定', '人均1000', '轻松', '', User('synthetic', '测试', '测'), '未生成', '')
        started = monotonic()
        cache = {'_budget': TurnBudget(seconds=90)}
        runtime = TravelAgentRuntime(client, model, {'trip': trip, 'user_body': '预算改成人均2000元，先别生成行程。', 'current_date': datetime.now().date().isoformat()},
                                     trace=trace, cache=cache, request=_create_chat_completion)
        response = runtime.run()
        reply = cache['final_reply']
        assert not reply.failed and reply.requirements_patch['changes']['budget_amount'] == '2000', reply.body
        assert reply.plan is None
        record('main_and_requirements_delegation', response, started, 'low')
        # Exercise medium with an actual tool roundtrip, then a strict structured result.
        tool = {'type': 'function', 'function': {'name': 'read_known_time',
                'description': '读取用户已填写的抵达和离开时刻。这个工具必须先调用。',
                'parameters': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}}}
        messages = [{'role': 'system', 'content': '请先调用 read_known_time 获取时间；收到工具结果后输出可用分钟数。不要猜时间。'},
                    {'role': 'user', 'content': '用户抵达后到离开之前有多少分钟？'}]
        budget = TurnBudget(seconds=90)
        medium = role_reasoning_effort(roles()['planning'])
        started = monotonic()
        response = _create_chat_completion(client, model, messages, tools=[tool], reasoning_effort=medium,
                                           agent_role='planning', budget=budget, trace=trace)
        assert response.choices[0].message.tool_calls, 'Expected actual tool call'
        record('planning_medium_tool_call', response, started, medium)
        assistant = response.choices[0].message
        calls = assistant.tool_calls
        messages.append({'role': 'assistant', 'content': assistant.content or '',
                         'tool_calls': [call.model_dump() for call in calls]})
        for call in calls:
            assert call.function.name == 'read_known_time'
            assert json.loads(call.function.arguments) == {}
            messages.append({'role': 'tool', 'tool_call_id': call.id,
                             'content': '{"arrival_time":"10:00","departure_time":"20:00"}'})
        schema = {'type': 'json_schema', 'json_schema': {'name': 'available_minutes', 'strict': True,
                  'schema': {'type': 'object', 'properties': {'minutes': {'type': 'integer'}},
                             'required': ['minutes'], 'additionalProperties': False}}}
        started = monotonic()
        response = _create_chat_completion(client, model, messages, reasoning_effort=medium,
                                           response_format=schema, agent_role='planning', budget=budget, trace=trace)
        assert json.loads(response.choices[0].message.content) == {'minutes': 600}
        record('planning_medium_structured_result', response, started, medium)
    result['business_db_unchanged'] = before == (hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None)
    assert result['business_db_unchanged']
    result['reasoning_fallbacks'] = sum('思考参数降级' in entry for entry in trace.events)
    # Store only selected diagnostics, never reasoning_content.
    result['model_events'] = [entry for entry in trace.events if '模型请求 ' in entry or '模型响应 ' in entry or '思考参数降级' in entry]
    result['status'] = 'passed'
    target = ROOT / '.cache/reasoning-check/live-integration.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'passed', 'business_db_unchanged': True, 'reasoning_fallbacks': result['reasoning_fallbacks']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
