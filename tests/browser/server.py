"""Test-only localhost SPA/API fixture. Never reads .env or calls a paid service.

Run with the project's fixed conda interpreter. Playwright starts this server;
SQLite lives in TemporaryDirectory and is removed when the process exits.
"""
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import atexit
import json
import os
import socket
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
TEMP = tempfile.TemporaryDirectory(prefix='travelassistant-browser-')
atexit.register(TEMP.cleanup)
os.environ.update({
    'OPENAI_API_KEY': 'offline-browser-key', 'OPENAI_BASE_URL': 'http://127.0.0.1:9/v1',
    'OPENAI_MODEL': 'offline-browser-model', 'OPENAI_FALLBACK_MODEL': '',
    'OPENAI_TIMEOUT_SECONDS': '5', 'OPENAI_WIRE_API': 'chat_completions',
    'OPENAI_STORE_RESPONSES': 'false', 'CITY_DB_PATH': str(Path(TEMP.name) / 'browser.sqlite3'),
    'TRAVEL_PERSISTENCE': 'true', 'TRAVEL_DISPLAY_ENABLED': 'true',
    'TRAVEL_DISPLAY_TIMEOUT_SECONDS': '5', 'TRAVEL_COOKIE_SECURE': 'false',
    'TRAVEL_AGENT_DISABLE_LLM': 'true', 'AMAP_MAPS_API_KEY': 'offline-browser-map',
    'AMAP_MAP_KEY': '', 'AMAP_MCP_URL': 'https://maps.offline.invalid/mcp',
    'AMAP_MCP_TIMEOUT_SECONDS': '5',
})
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False
sys.path.insert(0, str(ROOT / 'backend'))

# Defense in depth: even a missed fake cannot connect to an external host.
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex

def _local_connect(original):
    def connect(sock, address):
        if isinstance(address, tuple) and address[0] not in {'127.0.0.1', '::1', 'localhost'}:
            raise AssertionError('Browser fixture forbids non-local network connections')
        return original(sock, address)
    return connect
socket.socket.connect = _local_connect(_original_connect)
socket.socket.connect_ex = _local_connect(_original_connect_ex)

from app.main import app
from app.api import dependencies as deps, trips
from app.agents import agent_client
from app.agents.task_runtime import TaskExecutionRuntime
from app.services.itinerary.guide_html import build_guide_display
from app.services.itinerary.plan_display import dates_from_snapshot
from app.agents.tools import registry
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PLAN_TEXT = '# 西湖慢游\n\n从湖滨酒店出发，轻松游玩西湖。\n\n---\n\n## 第一天\n\n09:00 步行到湖滨，沿湖散步。\n\n12:00 湖滨午餐，费用以现场为准。\n\n16:00 返回酒店。\n\n交通时间为查询估算，营业时间请出发前核实。'
GUIDE_HTML = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>西湖慢游攻略</title><style>body{margin:0;background:#f6f2e9;color:#254236;font:16px/1.7 sans-serif}main{max-width:640px;margin:auto;padding:24px}h1{font-size:32px}section{border-top:1px solid #b8c6b7;padding:16px 0}a{color:#245b44}</style></head><body><main><p>HANGZHOU · 一日慢游</p><h1>西湖慢游攻略</h1><p>从湖滨酒店出发，沿湖散步，留一点时间给风景。</p><section><h2>09:00 湖滨散步</h2><p>步行到西湖，交通时间为查询估算。</p></section><section><h2>12:00 湖滨午餐</h2><p>费用以现场为准。</p></section><section><h2>16:00 返回酒店</h2><p>营业时间请出发前核实。</p></section></main></body></html>'''


def fake_places(arguments):
    return {'status': 'available', 'total_matched': 2, 'candidates': [
        {'poi_id': f'browser-hotel-{i}', 'name': name, 'address': address, 'city': '杭州',
         'district': '上城区', 'longitude': 120.15 + i / 1000, 'latitude': 30.25,
         'source': '高德地图 POI', 'fetched_at': '2030-01-01T00:00:00+00:00'}
        for i, (name, address) in enumerate([('湖滨酒店（西湖店）', '湖滨路1号'), ('湖滨酒店（城站店）', '城站路2号')]) ]}
trips.search_places = fake_places
def fake_tool(name, arguments, **kwargs):
    if name == 'maps_text_search':
        return {'pois': [{'id': row['poi_id'], 'name': row['name']} for row in fake_places(arguments)['candidates']]}
    if name == 'maps_search_detail':
        row = next(row for row in fake_places(arguments)['candidates'] if row['poi_id'] == arguments['id'])
        return {'id': row['poi_id'], 'name': row['name'], 'address': row['address'], 'city': row['city'],
                'location': f"{row['longitude']},{row['latitude']}"}
    raise AssertionError('Unexpected external tool in browser fixture')
registry.run_tool = fake_tool


class ScriptedModel:
    def __init__(self, context, execution):
        body = context.body
        changes = {'user_quote': body}
        if '我住' in body:
            changes.update(requirements_patch={'start_date': '2030-05-01', 'end_date': '2030-05-01',
                'day_count': 1, 'transport_mode': 'transit'}, lodging={'poi_id': 'browser-hotel-0'})
            steps = [('maps_text_search', {'city': '杭州', 'keywords': '湖滨酒店（西湖店）'}),
                     ('maps_search_detail', {'id': 'browser-hotel-0'})]
        elif '确认' in body:
            changes['itinerary'] = {'action': 'confirm'}
            steps = []
        elif '生成' in body:
            steps = []
            changes['itinerary'] = {'action': 'replace', 'text': PLAN_TEXT,
                'idea_outcomes': [{'idea_ref': card.id, 'status': 'excluded', 'reason': '本轮一日路线暂不安排夜骑，保留想法供后续讨论'} for card in execution.idea_cards]}
        else:
            steps = []
            changes['ideas'] = [{'action': 'create', 'kind': '地点', 'title': '晚上夜骑', 'body': '晚上夜骑'}]
        self.steps = iter([*steps, ('read_context', {'section': 'ideas'}), ('submit_changes', changes)])

    def __call__(self, client, model, messages, **kwargs):
        kwargs['budget'].model()
        try:
            name, arguments = next(self.steps)
            call = SimpleNamespace(id='offline-' + str(len(messages)), type='function',
                function=SimpleNamespace(name=name, arguments=json.dumps(arguments, ensure_ascii=False)))
            message = SimpleNamespace(content=None, tool_calls=[call])
        except StopIteration:
            message = SimpleNamespace(content='已按你的请求处理。', tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason='stop')])


def generate(context, execution, *, cancelled=None, on_progress=None):
    # Delay allows the browser to observe actual queued/running progress.
    if on_progress:
        on_progress('模型请求', {'agent_role': 'travel_agent'})
    time.sleep(1.6)
    if on_progress:
        on_progress('模型响应', {'agent_role': 'travel_agent'})
    runtime = TaskExecutionRuntime(None, 'offline-browser-model', context, execution,
        ScriptedModel(context, execution))
    return runtime.run()
agent_client.generate_task_result = generate


def display(source_text, source_dates, trip_id, **kwargs):
    time.sleep(.2)
    from app.services.itinerary.plan_display import text_hash
    return {'display': build_guide_display(GUIDE_HTML, source_dates),
            'display_source_hash': text_hash(source_text), 'display_status': 'ready'}
agent_client.generate_plan_display = display

# Keep the real API/auth/SQLite/task/display composition; serve only the built SPA.
DIST = ROOT / 'frontend' / 'dist'
if not (DIST / 'index.html').exists():
    raise RuntimeError('Build frontend before starting the browser fixture')
app.mount('/assets', StaticFiles(directory=DIST / 'assets'), name='spa-assets')

@app.get('/{path:path}', include_in_schema=False)
def spa(path: str):
    if path == 'api' or path.startswith('api/'):
        from fastapi import HTTPException
        raise HTTPException(404, 'API endpoint not found')
    return FileResponse(DIST / 'index.html', headers={'Cache-Control': 'no-store'})

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8765, log_level='warning')
