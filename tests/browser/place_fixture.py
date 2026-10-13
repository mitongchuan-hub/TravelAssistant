"""Isolated Agent-output fixture, no model requests or real business data."""
import os
import sys
from pathlib import Path
from dataclasses import asdict

os.environ['TRAVEL_PERSISTENCE'] = '0'
os.environ['TRAVEL_AGENT_DISABLE_LLM'] = '1'
os.environ['OPENAI_API_KEY'] = ''
os.environ['AMAP_MAP_KEY'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import uvicorn
from app.models import RouteDetails
from app.main import app, auth
from app.store import store

candidate = {'poi_id': 'fixture-place', 'name': '灵隐寺', 'address': '灵隐路1号', 'longitude': 120.12, 'latitude': 30.25,
             'city': '杭州', 'district': '西湖区', 'source': '高德地图 POI', 'fetched_at': '2026-10-03T01:00:00+00:00'}
raw = asdict(store.plans['trip-hangzhou'])
raw['days'][0]['items'][0]['poi_id'] = 'fixture-place'
raw['days'][0]['items'][0]['incoming_route'] = asdict(RouteDetails(origin_poi_id='fixture-station', origin_name='杭州东站',
    destination_poi_id='fixture-place', destination_name='灵隐寺', mode='transit', status='available', duration_minutes=35,
    distance_meters=5000, source='高德路线规划', fetched_at='2026-10-03T01:00:00+00:00', reason='预计耗时，未来班次可能变化。'))
raw['days'][0]['items'][1]['incoming_route'] = asdict(RouteDetails(origin_name='灵隐寺', destination_name='区域建议',
    reason='具体地点未匹配，交通耗时待核实。'))
raw['days'][0]['items'][1]['place_scope'] = 'area'
store._apply_agent_plan('trip-hangzhou', raw, verified_places={'fixture-place': candidate})
auth.create_account('browser-places@example.com', 'u1', 'places-browser-2026')
if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[1]))
