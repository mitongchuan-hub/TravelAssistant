"""Manual live acceptance: real models/tools, synthetic in-memory travel only."""
from pathlib import Path
import os
import sys
import json
import hashlib
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['TRAVEL_PERSISTENCE'] = '0'
os.environ['TRAVEL_AGENT_DISABLE_LLM'] = '0'
os.environ.pop('PYTEST_CURRENT_TEST', None)

from app import agent_client
from app.store import DemoStore
from app.plan_validation import validate_plan


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def main():
    business = ROOT / '.cache/travelassistant.sqlite3'
    before_digest = digest(business)
    local = DemoStore(persistence_enabled=False)
    assert not local.persistence_enabled
    trip = local.create_trip('杭州', '2026-11-01', '人均1000', '轻松', '')
    local.update_trip_requirements(trip.id, {
        'city': '杭州', 'start_date': '2026-11-01', 'end_date': '2026-11-01',
        'arrival_time': '10:00', 'departure_time': '20:00', 'arrival_location': '杭州东站',
        'departure_location': '杭州东站', 'travelers': 1,
        'must_visit': ['灵隐寺', '庆春朴门(灵隐店)'], 'pace': '轻松',
    }, 0)
    events = []
    original_add = agent_client.ConversationTrace.add
    def observe(trace, title, value):
        original_add(trace, title, value)
        if title in {'委派子 Agent', 'Agent Loop 状态', '共享调用预算'}:
            events.append({'trace': trace.request_id, 'title': title, 'value': value})
            if title == '委派子 Agent':
                print(json.dumps({'event': 'delegate', 'agent': value['agent'], 'scope': value.get('scope')}, ensure_ascii=False), flush=True)
    agent_client.ConversationTrace.add = observe
    results = []
    artifact = ROOT / '.cache/role-agents-live-check.json'
    def turn(name, text):
        print(json.dumps({'event': 'start', 'case': name}, ensure_ascii=False), flush=True)
        start = monotonic()
        offset = len(events)
        old_versions = len(local.plan_versions[trip.id])
        local.add_user_message(trip.id, text)
        message = local.messages[trip.id][-1]
        result = {'case': name, 'input': text, 'body': message.body, 'message_status': message.status,
                  'seconds': round(monotonic() - start, 2), 'new_versions': len(local.plan_versions[trip.id])-old_versions,
                  'events': events[offset:], 'requirements': asdict(local.trips[trip.id].requirements)}
        results.append(result)
        artifact.write_text(json.dumps({'cases': results, 'plan': asdict(local.plans[trip.id]),
                                      'business_db_unchanged': digest(business)==before_digest}, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'event': 'result', **{k:result[k] for k in ('case','message_status','seconds','new_versions')}}, ensure_ascii=False), flush=True)
        assert message.status == 'sent', result['body']
        return result
    hello = turn('casual', '你好')
    assert hello['new_versions'] == 0 and not any(e['title']=='委派子 Agent' for e in hello['events'])
    update = turn('requirements', '预算改成人均800元，不吃海鲜。先别生成行程。')
    assert update['new_versions'] == 0
    assert local.trips[trip.id].requirements.budget_amount == '800'
    assert local.trips[trip.id].requirements.dietary
    research = turn('research', '查一下杭州东站到灵隐寺的公交预计耗时，只查询，先不要生成行程。')
    assert research['new_versions'] == 0
    planning = turn('planning', '现在请生成一天杭州行程：只安排灵隐寺、庆春朴门(灵隐店)午餐和必要休息。休息就在午餐餐厅原地进行，不增加茶馆。东站往返公共交通，寺院到餐厅步行，不安排过夜。按已填写的10:00抵达、20:00离开，查询真实地点和路线，每项活动和交通都写明确起止时段。')
    assert planning['new_versions'] == 1
    validate_plan(local.plans[trip.id], local.trips[trip.id])
    items = [item for day in local.plans[trip.id].days for item in day.items]
    routes = [route for item in items for route in (item.incoming_route,item.departure_route) if route]
    assert sum(r.status=='available' and (r.duration_minutes or 0)>0 for r in routes)>=3
    original = deepcopy(local.plans[trip.id])
    explanation = turn('explain', '解释一下，为什么把休息安排在午餐餐厅？只解释，不修改。')
    assert explanation['new_versions']==0 and local.plans[trip.id]==original
    meal = next(item for day in original.days for item in day.items if item.category=='meal')
    revision = turn('revision', '只修改午餐这一项的备注：加上“点餐时再次告知不吃海鲜”。地点、时间以及其他安排全部保持原样。')
    assert revision['new_versions']==1
    changed = next(item for day in local.plans[trip.id].days for item in day.items if item.id==meal.id)
    assert changed.time==meal.time and changed.location==meal.location and '海鲜' in changed.notes
    for day in original.days:
        for item in day.items:
            saved=next(i for d in local.plans[trip.id].days for i in d.items if i.id==item.id)
            assert saved.time==item.time and saved.title==item.title and saved.location==item.location
    assert digest(business)==before_digest
    print(json.dumps({'status':'passed','case_count':len(results),'business_db_unchanged':True,'artifact':str(artifact.relative_to(ROOT))},ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
