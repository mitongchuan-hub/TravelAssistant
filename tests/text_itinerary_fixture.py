"""Memory-only app with deterministic prose model replies for browser acceptance."""
from pathlib import Path
import json
import os
import sys
from types import SimpleNamespace
from threading import Event

old_started = Event()
release_old = Event()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['TRAVEL_PERSISTENCE'] = '0'
os.environ['TRAVEL_AGENT_DISABLE_LLM'] = '0'
os.environ['OPENAI_API_KEY'] = 'fixture-only'
from app import agent_client
from app.main import app, auth
from app.store import store
import uvicorn

PLAN = '杭州一日行程\n\n上午：西湖散步。\n午餐：附近用餐，不吃海鲜。\n下午：原地休息。\n\n开放与交通待核实。\n<script>window.untrusted = true</script>\n' + '很长的地点说明' * 80
REVISED = PLAN.replace('原地休息', '湖边休息，减少步行')


def respond(client, model, messages, **kwargs):
    assert 'response_format' not in kwargs
    if kwargs.get('budget'):
        kwargs['budget'].model()
    role = kwargs['agent_role']
    payload = json.loads(messages[1]['content'])
    if payload.get('user_message') == '旧生成':
        old_started.set()
        release_old.wait(20)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='被废弃旧回复', tool_calls=[]))])
    if payload.get('user_message') == '最新消息':
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='最新回复已完成', tool_calls=[]))])
    if role == 'requirements':
        content = json.dumps({'status': 'complete', 'body': '全局需求未变更。', 'requirements_patch': {}}, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]))])
    if role == 'planning' and not any(m['role'] == 'tool' for m in messages):
        call = SimpleNamespace(id='weather', function=SimpleNamespace(name='query_weather', arguments='{"city":"杭州","date":"2026-11-01"}'))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='', tool_calls=[call]))])
    if role == 'revision' and not any(m['role'] == 'tool' for m in messages):
        call = SimpleNamespace(id='context', function=SimpleNamespace(name='read_context', arguments='{"section":"plan"}'))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='', tool_calls=[call]))])
    if role == 'revision':
        current = json.loads(messages[-1]['content'])['plan']['text']
        content = current.replace('原地休息', '湖边休息，减少步行')
    else:
        content = PLAN
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]))])


agent_client._create_chat_completion = respond
agent_client.run_tool = lambda *a, **kw: {'status': 'unavailable', 'reason': '超出预报范围', 'source': '受控天气资料'}
agent_client.ConversationTrace.write = lambda *a, **kw: None
user = store.create_user('文字行程浏览器测试')
auth.create_account('text-browser@example.com', user.id, 'text-browser-2026')
trip = store.create_trip('杭州文字行程', '2026-11-01', '人均800', '轻松', '', user)
store.update_trip_requirements(trip.id, {'city': '杭州', 'start_date': '2026-11-01', 'end_date': '2026-11-01',
                                        'arrival_time': '10:00', 'departure_time': '18:00'}, 0)

@app.get('/_test/old-started')
def old_state():
    return {'started': old_started.is_set()}


@app.post('/_test/release-old')
def release_request():
    release_old.set()
    return {'released': True}


if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[1]), log_level='warning')
