"""Manual live test: real model/tools, text itineraries and an isolated SQLite DB."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['TRAVEL_PERSISTENCE'] = '0'
os.environ['TRAVEL_AGENT_DISABLE_LLM'] = '0'
os.environ.pop('PYTEST_CURRENT_TEST', None)
from app import agent_client
from app.store import DemoStore


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def main():
    artifacts = Path(tempfile.mkdtemp(prefix='travelassistant-text-live-'))
    business = ROOT / '.cache/travelassistant.sqlite3'
    original_hash = digest(business)
    events = []
    original_add = agent_client.ConversationTrace.add
    def observe(trace, title, value):
        original_add(trace, title, value)
        if title in {'模型请求', '模型响应', '工具执行', '委派子 Agent', 'Agent Loop 状态', '共享调用预算'}:
            if title == '模型请求':
                value = {'agent_role': value['agent_role'], 'has_response_format': 'response_format' in value['params']}
            elif title == '模型响应':
                value = {k: value.get(k) for k in ('agent_role', 'seconds', 'usage')}
            events.append({'trace': trace.request_id, 'title': title, 'value': deepcopy(value)})
    def write_trace(trace, error=None):
        if error:
            trace.add('异常', type(error).__name__ + ': ' + str(error))
        (artifacts / (trace.request_id + '.txt')).write_text(''.join(trace.events), encoding='utf-8')
    agent_client.ConversationTrace.add = observe
    agent_client.ConversationTrace.write = write_trace
    local = DemoStore(db_path=str(artifacts / 'synthetic.sqlite3'), persistence_enabled=True)
    trip = local.create_trip('杭州', '2026-11-01', '人均800', '轻松',
        '当天只安排西湖湖滨散步、附近午餐和原地休息，不指定具体门店，不安排过夜。行程简洁，每日用自然段说明安排和待核实事项。')
    local.update_trip_requirements(trip.id, {
        'city': '杭州', 'start_date': '2026-11-01', 'end_date': '2026-11-01',
        'arrival_time': '10:00', 'departure_time': '18:00', 'arrival_location': '杭州东站',
        'departure_location': '杭州东站', 'travelers': 1, 'must_visit': ['西湖湖滨'], 'dietary': ['不吃海鲜'],
    }, 0)
    cases = []
    def save():
        result = {'cases': cases, 'events': events, 'plan': asdict(local.plans[trip.id]),
                  'business_db_unchanged': digest(business) == original_hash, 'artifacts': str(artifacts)}
        (artifacts / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        (ROOT / '.cache/text-plan-live-location.txt').write_text(str(artifacts), encoding='utf-8')
    def run(name, action):
        offset = len(events)
        count = len(local.plan_versions[trip.id])
        start = monotonic()
        print(json.dumps({'event': 'start', 'case': name}), flush=True)
        action()
        message = local.messages[trip.id][-1]
        case = {'case': name, 'seconds': round(monotonic() - start, 2), 'status': message.status,
                'new_versions': len(local.plan_versions[trip.id]) - count, 'events': events[offset:], 'body': message.body}
        cases.append(case)
        save()
        print(json.dumps({k: case[k] for k in ('case', 'seconds', 'status', 'new_versions')}, ensure_ascii=False), flush=True)
        assert message.status == 'sent', message.body
        assert all(not e['value']['has_response_format'] for e in case['events'] if e['title'] == '模型请求')
        assert not any(e['value'].get('mode') == 'structure' for e in case['events'] if e['title'] == 'Agent Loop 状态')
        return case
    planning = run('planning', lambda: local.generate_plan(trip.id))
    assert planning['new_versions'] == 1 and local.plans[trip.id].text and not local.plans[trip.id].days
    original = local.plans[trip.id].text
    revision = run('revision', lambda: local.request_revision(trip.id, feedback='在午餐安排后加上“点餐时再次告知不吃海鲜”，其他安排完整保留。'))
    assert revision['new_versions'] == 1
    assert '点餐时再次告知不吃海鲜' in local.plans[trip.id].text
    assert local.plan_versions[trip.id][0].text == original
    revised = deepcopy(local.plans[trip.id])
    explanation = run('explain', lambda: local.add_user_message(trip.id, '只解释午餐后为什么保留休息，不修改行程。'))
    assert explanation['new_versions'] == 0 and local.plans[trip.id] == revised
    local.confirm_plan(trip.id)
    reopened = DemoStore(db_path=local.db_path, persistence_enabled=True)
    assert reopened.plans[trip.id].text == revised.text and reopened.plans[trip.id].status == '已确认'
    assert reopened.restore_plan_version(trip.id, reopened.plan_versions[trip.id][0].id)
    assert reopened.plans[trip.id].text == original
    assert digest(business) == original_hash
    save()
    print(json.dumps({'status': 'passed', 'business_db_unchanged': True, 'artifacts': str(artifacts)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
