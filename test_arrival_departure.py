import json
from dataclasses import asdict
import importlib

import pytest

from app.agent_client import AgentReply, _build_context, _parse_reply
from app.models import AgentCard
from app.store import DemoStore


@pytest.mark.parametrize('start,end,arrival,departure,days', [
    ('2026-11-06', '2026-11-08', '21:00', '15:00', 3),
    ('2026-11-06', '2026-11-06', '09:00', '18:00', 1),
    ('2026-10-31', '2026-11-02', '22:30', '08:00', 3),
    ('', '', '', '', None),
])
def test_arrival_departure_save_restore_and_context(tmp_path, start, end, arrival, departure, days):
    db = str(tmp_path / 'trip.sqlite3')
    local = DemoStore(db_path=db, persistence_enabled=True)
    trip = local.create_trip('杭州', '', '', '', '')
    local.update_trip_requirements(trip.id, {
        'start_date': start, 'end_date': end, 'arrival_time': arrival, 'departure_time': departure,
    }, 0)
    restored = DemoStore(db_path=db, persistence_enabled=True)
    saved = restored.trips[trip.id]
    assert saved.requirements.arrival_time == arrival
    assert saved.requirements.departure_time == departure
    assert saved.requirements.day_count == days
    context = _build_context(saved, [], '生成')
    assert '抵离边界（旅游城市当地时间）' in context
    assert (arrival or '时间待定') in context
    assert (departure or '时间待定') in context


@pytest.mark.parametrize('arrival,departure', [('25:00', '18:00'), ('09:60', '18:00'), ('9:00', '18:00'), ('18:00', '18:00'), ('20:00', '18:00')])
def test_invalid_or_reversed_same_day_times_do_not_change_trip(arrival, departure):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    with pytest.raises(ValueError):
        local.update_trip_requirements(trip.id, {
            'start_date': '2026-11-06', 'end_date': '2026-11-06',
            'arrival_time': arrival, 'departure_time': departure,
        }, 0)
    assert local.trips[trip.id] == trip
    assert trip.requirements.revision == 0


def test_chat_updates_arrival_before_same_message_planning(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    text = '2026年11月6日晚上21点到杭州，11月8日下午15点离开，生成行程'
    seen = []
    def model(**kwargs):
        seen.append(kwargs['trip'])
        if kwargs['planning_mode']:
            return AgentReply('已生成', AgentCard('itinerary-draft', '行程', '', [], []), [], asdict(local._seed_plan()))
        return _parse_reply(json.dumps({
            'body': '已记录抵离时间', 'card': {'next_action': 'generate_plan'}, 'idea_cards': [], 'plan': None,
            'requirements_patch': {'evidence': text, 'changes': {
                'start_date': '2026-11-06', 'end_date': '2026-11-08',
                'arrival_time': '21:00', 'departure_time': '15:00',
            }},
        }, ensure_ascii=False))
    monkeypatch.setattr(importlib.import_module('app.store'), 'generate_agent_reply', model)
    local.add_user_message(trip.id, text)
    assert len(seen) == 2
    assert seen[1].requirements.arrival_time == '21:00'
    assert seen[1].requirements.departure_time == '15:00'
    assert local.trips[trip.id].requirements.arrival_time == '21:00'
    local.update_trip_requirements(trip.id, {'arrival_time': '', 'departure_time': ''}, 1)
    assert local.trips[trip.id].requirements.arrival_time == ''
