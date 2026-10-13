"""Memory-only visual fixture using fixed evaluation prose and cards."""
from copy import deepcopy
from pathlib import Path
import importlib
import json
import os
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ['TRAVEL_PERSISTENCE'] = '0'
os.environ['TRAVEL_AGENT_DISABLE_LLM'] = '1'
from app.auth import AuthStore
from app.plan_display import text_hash
from app.store import DemoStore
import uvicorn

main = importlib.import_module('app.main')
local = DemoStore(persistence_enabled=False, display_worker_enabled=False)
auth = AuthStore('unused.sqlite3', False)
user = local.create_user('卡片视觉测试')
auth.create_account('cards@example.com', user.id, 'cards-browser-2026')
rows = json.loads((ROOT/'docs/evaluations/display-combined-weather-20261012/results.json').read_text(encoding='utf-8'))
cases = {c['id']: c for c in json.loads((ROOT/'tests/datasets/display_semantics_v1/cases.json').read_text(encoding='utf-8'))['cases']}
trips = {}
for cid in ['LAT-012', 'LAT-014', 'LAT-015']:
    row = next(r for r in rows if r['case_id'] == cid and r['attempt'] == 2)
    fields = deepcopy(cases[cid]['requirements_snapshot'])
    fields.pop('revision', None)
    trip = local.create_trip('杭州行程', '', '', '', '', user, requirements=fields)
    cards = deepcopy(row['result']['display'])
    cards['schema_version'] = '1.1'
    local._apply_text_plan(trip.id, row['source_text'], display=cards,
                          display_source_hash=text_hash(row['source_text']), display_status='ready')
    trips[cid] = trip.id
# Deliberate repetitions verify projection-only removal and protected wording.
duplicate_fields = deepcopy(cases['LAT-012']['requirements_snapshot'])
duplicate_fields.pop('revision', None)
duplicate_trip = local.create_trip('重复内容测试', '', '', '', '', user, requirements=duplicate_fields)
duplicate_cards = deepcopy(next(r for r in rows if r['case_id']=='LAT-012' and r['attempt']==2)['result']['display'])
duplicate_cards['schema_version'] = '1.1'
duplicate_cards['days'][0]['items'] = [{'title':'西湖散步','time_text':'11:00左右','category':'activity',
    'location':'西湖散步','duration_text':None,'description':'西湖散步。','cost_text':None,
    'notes':['开放待核实','开放待核实。','建议最晚15:00前出发','最晚15:00前出发']}]
duplicate_cards['pending_items'] = ['门票待核实','门票待核实。']
duplicate_cards['risks'] = ['门票待核实','微辣也不要']
duplicate_cards['weather'][0]['notes'].append('<script>window.weatherInjected = true</script>')
local._apply_text_plan(duplicate_trip.id, '西湖散步。门票待核实。微辣也不要。', display=duplicate_cards,
    display_source_hash=text_hash('西湖散步。门票待核实。微辣也不要。'), display_status='ready')
trips['duplicates'] = duplicate_trip.id
main.store, main.auth = local, auth

@main.app.get('/_test/cards-source')
def original_duplicate_cards():
    return local.plans[duplicate_trip.id].display

@main.app.get('/_test/cards')
def fixture_trips():
    return {'trips': trips}

if __name__ == '__main__':
    uvicorn.run(main.app, host='127.0.0.1', port=int(sys.argv[1]), log_level='warning')
