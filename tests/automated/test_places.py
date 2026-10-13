import importlib
import json
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from app import agent_client
from app.agent_client import AgentReply, _parse_plan
from app.models import AgentCard
from app.places import collect_places
from app.store import DemoStore
from test_agent_roles import plan_text, ready_for_planning
from app.tools import places
from app.tools.places import map_url, search_places

CANDIDATE = {'poi_id': 'poi-one', 'name': '灵隐寺', 'address': '灵隐路1号', 'longitude': 120.12, 'latitude': 30.25,
             'city': '杭州', 'district': '西湖区', 'source': '高德地图 POI', 'fetched_at': '2026-10-03T01:00:00+00:00'}


def tool_messages():
    return [{'role': 'tool', 'name': 'search_places', 'content': json.dumps({'status': 'available', 'candidates': [CANDIDATE]})}]


def test_provider_keeps_candidates_and_validates_coordinates(monkeypatch):
    monkeypatch.setenv('AMAP_MAP_KEY', 'test')
    monkeypatch.setattr(places, '_fetch_json', lambda params: {'status': '1', 'infocode': '10000', 'pois': [
        {'id': 'one', 'name': '景点', 'address': '东路1号', 'location': '120.12,30.25', 'cityname': '杭州'},
        {'id': 'two', 'name': '景点', 'address': '西路2号', 'location': '120.15,30.27', 'cityname': '杭州'},
        {'id': 'bad', 'name': '无效', 'location': 'NaN,30'},
    ]})
    result = search_places({'city': '杭州', 'keywords': '景点'})
    assert result['status'] == 'available' and len(result['candidates']) == 2
    assert result['candidates'][0]['fetched_at']


@pytest.mark.parametrize('mode', ['key', 'network', 'quota', 'empty'])
def test_unavailable_service_never_fabricates_coordinates(monkeypatch, mode):
    monkeypatch.setenv('AMAP_MAP_KEY', '' if mode == 'key' else 'test')
    def fetch(params):
        if mode == 'network': raise URLError('unavailable')
        return {'status': '0', 'infocode': '10003'} if mode == 'quota' else {'status': '1', 'infocode': '10000', 'pois': []}
    monkeypatch.setattr(places, '_fetch_json', fetch)
    assert search_places({'city': '杭州', 'keywords': '景点'})['candidates'] == []


def test_only_real_place_tool_results_enter_catalog():
    catalog = collect_places(tool_messages())
    assert catalog['poi-one'] == CANDIDATE
    assert collect_places([{'role': 'assistant', 'name': 'search_places', 'content': tool_messages()[0]['content']}]) == {}
    assert collect_places([{'role': 'tool', 'name': 'unknown', 'content': tool_messages()[0]['content']}]) == {}


@pytest.mark.parametrize('poi_id', ['poi-one', 'fabricated', ''])
def test_agent_ids_bind_only_trusted_tool_metadata(poi_id):
    local = DemoStore(persistence_enabled=False)
    raw = asdict(local._seed_plan())
    raw['days'][0]['items'][0]['poi_id'] = poi_id
    raw['days'][0]['items'][0]['place'] = {'longitude': 0, 'latitude': 0, 'poi_id': 'invented'}
    parsed = _parse_plan(raw)
    assert 'place' not in parsed['days'][0]['items'][0]
    local._apply_agent_plan('trip-hangzhou', parsed, verified_places=collect_places(tool_messages()))
    saved = local.plans['trip-hangzhou'].days[0].items[0]
    if poi_id == 'poi-one':
        assert saved.place.longitude == 120.12 and saved.place.address == '灵隐路1号'
        assert 'position=120.12%2C30.25' in map_url(saved.place)
    else: assert saved.place is None


def test_planning_keeps_tool_catalog_without_format_conversion(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    ready_for_planning(local, 'trip-hangzhou')
    raw = asdict(local._seed_plan())
    raw['days'][0]['items'][0]['poi_id'] = 'poi-one'
    replies = [plan_text(raw)]
    queries = []
    def create(*args, **kwargs):
        if not queries:
            queries.append(True)
            call = SimpleNamespace(id='place-1', function=SimpleNamespace(name='search_places', arguments='{"city":"杭州","keywords":"灵隐寺"}'))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='', tool_calls=[call]))])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=replies.pop(0)))])
    monkeypatch.setattr(agent_client, 'run_tool', lambda *a, **kw: json.loads(tool_messages()[0]['content']))
    monkeypatch.setattr(agent_client, '_create_chat_completion', create)
    catalog = {}
    response = agent_client._request_model(object(), 'test', '上下文', True,
                                          trip=local.trips['trip-hangzhou'], place_catalog=catalog)
    assert catalog['poi-one'] == CANDIDATE and queries == [True]
    assert response.choices[0].message.content == plan_text(raw)
    assert replies == []


def test_agent_places_survive_store_save_and_restart(tmp_path, monkeypatch):
    local = DemoStore(db_path=str(tmp_path / 'places.sqlite3'), persistence_enabled=True)
    trip = local.create_trip('杭州', '', '', '', '')
    raw = asdict(local._seed_plan())
    raw['days'][0]['items'][0]['poi_id'] = 'poi-one'
    monkeypatch.setattr(importlib.import_module('app.store'), 'generate_agent_reply', lambda **kwargs:
        AgentReply('已生成', AgentCard('itinerary-draft', '行程', '', [], []), [], raw,
                   verified_places=collect_places(tool_messages())))
    local.generate_plan(trip.id)
    restored = DemoStore(db_path=local.db_path, persistence_enabled=True)
    assert restored.plans[trip.id].days[0].items[0].place.poi_id == 'poi-one'
    assert not hasattr(restored, 'plan_versions')


@pytest.mark.parametrize('changed', [False, True])
def test_revision_retains_place_only_if_identity_unchanged(monkeypatch, changed):
    local = DemoStore(persistence_enabled=False)
    raw = asdict(local._seed_plan())
    raw['days'][0]['items'][0]['poi_id'] = 'poi-one'
    local._apply_agent_plan('trip-hangzhou', raw, verified_places=collect_places(tool_messages()))
    item = local.plans['trip-hangzhou'].days[0].items[0]
    updated = asdict(local.plans['trip-hangzhou'])
    updated['days'][0]['items'][0]['notes'] = '改备注'
    if changed: updated['days'][0]['items'][0]['location'] = '另一地点'
    monkeypatch.setattr(importlib.import_module('app.store'), 'generate_agent_reply', lambda **kwargs:
        AgentReply('已调整', AgentCard('revision', '调整', '', [], []), [], updated))
    local.request_revision('trip-hangzhou', item.id, '修改')
    saved = local.plans['trip-hangzhou'].days[0].items[0]
    assert (saved.place is None) == changed
