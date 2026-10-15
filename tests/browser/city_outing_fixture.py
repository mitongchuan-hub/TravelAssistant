"""Memory-only browser fixture for city outing creation and saved text/cards."""
from pathlib import Path
import os
import sys
from dataclasses import asdict
import importlib

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ['TRAVEL_PERSISTENCE'] = '0'
os.environ['TRAVEL_AGENT_DISABLE_LLM'] = '0'
import uvicorn
from app.agent_client import AgentReply
from app.auth import AuthStore
from app.models import AgentCard
from app.plan_display import text_hash
from app.store import DemoStore

main = importlib.import_module('app.main')
store_module = importlib.import_module('app.store')
client_module = importlib.import_module('app.agent_client')
local = DemoStore(persistence_enabled=False, display_worker_enabled=True)
user = local.create_user('同城预览')
auth = AuthStore('unused-city-browser.sqlite3', persistence_enabled=False)
auth.create_account('city-preview@example.com', user.id, 'city-preview-2026')
main.store, main.auth = local, auth
LODGING = {'poi_id': 'fixture-hotel', 'name': '湖滨住处', 'address': '湖滨路1号',
    'longitude': 120.15, 'latitude': 30.25, 'city': '杭州市', 'district': '上城区',
    'source': '高德地图 POI', 'fetched_at': '2026-10-15T00:00:00+00:00'}
SOURCE = ('建议围绕湖滨片区散步、用餐和休息，傍晚回住处。这是日期未定的一天路线，开放与费用以出发前确认为准。'
    '\n\n---\n\n第1天：上午从湖滨住处出发，到湖滨片区散步。午餐选择不辣家常饭并预留一小时。'
    '下午保留休息，晚餐后返回湖滨住处。交通耗时、开放与价格尚待核实，不计住宿和跨城交通。')


def lookup(args):
    candidates = [LODGING]
    if args['keywords'] == '重名酒店':
        candidates = [LODGING, {**LODGING, 'poi_id': 'fixture-other', 'address': '西路2号'}]
    return {'status': 'available', 'total_matched': len(candidates), 'candidates': candidates}


def reply(**kwargs):
    body = SOURCE if kwargs.get('planning_mode') else '已收到，可以继续补充同城游玩偏好。'
    return AgentReply(body, AgentCard('itinerary-draft' if kwargs.get('planning_mode') else 'requirement-summary',
        '同城路线', body[:100], [], []), [], plan_text=body if kwargs.get('planning_mode') else '',
        display_status='pending' if kwargs.get('planning_mode') else 'not_requested',
        display_source_hash=text_hash(body) if kwargs.get('planning_mode') else '')


def display(source, dates, trip_id, **kwargs):
    data = {'schema_version': '1.1', 'title': '湖滨同城一天', 'overview': SOURCE.split('\n\n---')[0],
        'budget_note': None, 'days': [{'date': None, 'label': '第1天', 'theme': '湖滨散步',
        'items': [
            {'time_text': time, 'title': title, 'category': category, 'location': location,
             'duration_text': duration, 'description': description, 'cost_text': None, 'notes': notes}
            for time, title, category, location, duration, description, notes in [
                ('上午', '湖滨片区散步', 'activity', '湖滨片区', None, '从湖滨住处出发，到湖滨片区散步。', ['开放与价格尚待核实。']),
                ('午餐', '不辣家常饭', 'meal', None, '一小时', '选择不辣家常饭并预留一小时。', []),
                ('下午', '保留休息', 'rest', None, None, '下午保留休息。', []),
                ('晚餐后', '返回湖滨住处', 'transport', '湖滨住处', None, '晚餐后返回湖滨住处。', ['交通耗时尚待核实。']),
            ]
        ]}], 'preparation': {'clothing': [], 'accommodation': []}, 'weather': [],
        'pending_items': ['出发前确认开放与费用'], 'risks': []}
    return {'display': data, 'display_status': 'ready', 'display_source_hash': text_hash(source)}


main.search_places = lookup
store_module.generate_agent_reply = reply
client_module.generate_plan_display = display

@main.app.get('/_test/city-state')
def state():
    return {'trip_count': len(local.trips), 'trips': {tid: {'mode': trip.mode,
        'requirements': asdict(trip.requirements), 'lodging': trip.lodging,
        'plan_text': local.plans[tid].text, 'display_status': local.plans[tid].display_status}
        for tid, trip in local.trips.items() if trip.mode == 'city'}}

if __name__ == '__main__':
    uvicorn.run(main.app, host='127.0.0.1', port=int(sys.argv[1]), log_level='warning')
