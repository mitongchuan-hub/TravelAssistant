"""Manual live-model smoke test. Uses synthetic requirements; never saves user data."""
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.agent_client import generate_agent_reply
from app.models import Trip, TripPlan, TripRequirements, User
from app.plan_validation import validate_plan

if __name__ == '__main__':
    trip = Trip(id='live-route-case', destination='杭州', date_range='2026-11-01', budget='800元/人',
                style='轻松', note='', owner=User('synthetic', '测试用户', '测'), status='待规划', last_activity='',
                requirements=TripRequirements(city='杭州', start_date='2026-11-01', end_date='2026-11-01',
                    arrival_time='10:00', departure_time='20:00', arrival_location='杭州东站', departure_location='杭州东站',
                    day_count=1, travelers=1, must_visit=['灵隐寺', '庆春朴门(灵隐店)'], pace='轻松'))
    reply = generate_agent_reply(trip, [], '请生成一天杭州行程：只安排灵隐寺、庆春朴门(灵隐店)午餐和必要休息。休息就在午餐餐厅原地进行，不增加茶馆。东站往返以公共交通为主，灵隐寺到餐厅步行，不安排过夜。查询这些具体地点和真实路线，留足交通时间，从抵达到离开之间安排。',
                                  plan=TripPlan('empty', '待规划', '草案'), planning_mode=True)
    result = {'failed': not reply or reply.failed, 'body': reply.body if reply else '', 'plan': reply.plan if reply else None,
              'verified_places': reply.verified_places if reply else {}}
    target = Path('.cache/routes-live-check.json')
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    if not reply or reply.failed or not reply.plan:
        print(json.dumps({'status': 'failed', 'body': result['body']}, ensure_ascii=False))
        raise SystemExit(1)
    validate_plan(reply.plan, trip)
    items = [item for day in reply.plan['days'] for item in day['items']]
    routes = [item[field] for item in items for field in ('incoming_route', 'departure_route') if item.get(field)]
    if sum(route['status'] == 'available' and route['duration_minutes'] > 0 for route in routes) < 3 or any(route['status'] != 'available' for route in routes):
        print('Incomplete live fixture: expected matched arrival, meal and departure routes.')
        raise SystemExit(1)
    print(json.dumps({'status': 'passed', 'item_count': len(items), 'matched_places': sum(bool(item.get('poi_id')) for item in items),
                      'routes_available': sum(route['status'] == 'available' for route in routes),
                      'routes_pending': sum(route['status'] != 'available' for route in routes),
                      'verified_place_count': len(reply.verified_places)}, ensure_ascii=False))
