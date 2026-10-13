"""Memory-only conversation fixture: drafts, long replies, gates and task recovery."""
from pathlib import Path
import os
import sys
from threading import Event
from dataclasses import asdict

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ['TRAVEL_PERSISTENCE'] = '0'
os.environ['TRAVEL_AGENT_DISABLE_LLM'] = '0'
from app.main import app, auth
from app.store import store
from app.models import Message, AgentCard, AgentAction
from app.agent_client import AgentReply
import importlib
import uvicorn

hold = Event()
release = Event()
started = Event()
user = store.create_user('聊天预览')
auth.create_account('chat-preview@example.com', user.id, 'chat-preview-2026')
trip = store.create_trip('杭州慢旅行', '', '', '', '', user, requirements={
    'city': '杭州', 'start_date': '2026-11-14', 'end_date': '2026-11-15',
    'arrival_time': '10:00', 'departure_time': '18:00', 'arrival_location': '杭州东站',
    'departure_location': '杭州东站', 'travelers': 2, 'budget_amount': '2500', 'budget_scope': 'total',
    'budget_coverage': '含住宿餐饮市内交通，不含往返车票', 'pace': '轻松'})
TID = trip.id
long = '## 杭州两日，慢慢走\n\n按你们的节奏，每天留出午休和自由时间。预算是两人合计2500元，含住宿、餐饮和市内交通，不含往返车票；实际费用待核实。\n\n'
long += '\n\n'.join(f'### 安排 {i+1}\n\n午后在湖边散步，累了就原地休息。保留不吃辣的要求，点餐时再次确认。交通、营业及价格仍待核实。' for i in range(12))
long += '\n\n<script>window.chatInjected = true</script>\n\n[不可信链接](javascript:alert(1))'
rows = [
    ('agent', '你好，一起聊聊这趟杭州旅行。\n\n想去哪儿，或者先说一个小想法？', '09:40', None),
    ('user', '我们想去西湖。两个人，轻松一点，别把时间排满。', '09:41', None),
    ('agent', '好，把西湖留作主要安排，午后多留一点休息时间。\n\n大概哪两天去？', '09:41', None),
    ('user', '11月14日到，15日回。预算两人合计2500元，不吃辣。', '09:42', None),
    ('agent', '**已记下**。两人总预算2500元，不吃辣；行程按轻松节奏安排。\n\n住宿价格和具体接驳耗时，先保留待核实。', '09:42', AgentCard('requirement-summary', '已记下', '已记下', [], [AgentAction('继续聊天', 'chat')])),
    ('user', '好，帮我出一版。', '10:05', None),
    ('agent', long, '10:06', AgentCard('itinerary-draft', '行程草案', long[:120], [], [AgentAction('查看行程', 'itinerary')])),
    ('agent', '你可以先看看整体节奏，再告诉我想多休息，还是多走一段。', '10:06', None),
]
store.messages[TID] = [Message(f'preview-{i}', user if role == 'user' else store.trips[TID].owner,
    role, body, stamp, 'sent', card) for i, (role, body, stamp, card) in enumerate(rows)]
initial = [asdict(message) for message in store.messages[TID]]


def reply(**kwargs):
    if hold.is_set():
        started.set()
        release.wait(20)
    return AgentReply('收到你的补充。\n\n**继续保留轻松节奏**，其他安排等你确认后再调整。',
                      AgentCard('requirement-summary', '已收到', '已收到', [], []), [])


importlib.import_module('app.store').generate_agent_reply = reply

@app.get('/_test/state')
def state():
    return {'trip_id': TID, 'started': started.is_set(), 'initial_unchanged': [asdict(m) for m in store.messages[TID]][:len(initial)] == initial}

@app.post('/_test/block')
def block():
    started.clear(); release.clear(); hold.set()
    return {'blocked': True}

@app.post('/_test/release')
def unblock():
    hold.clear(); release.set()
    return {'released': True}

@app.post('/_test/new-reply')
def incoming():
    store.messages[TID].append(Message(f'incoming-{len(store.messages[TID])}', trip.owner, 'agent',
        '补充说明：具体营业和价格仍待核实，出发前再确认。', '10:25', 'sent', None))
    return {'added': True}

if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[1]), log_level='warning')
