import importlib
import json
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from app import agent_client
from app.agent_client import AgentReply
from app.models import AgentCard, TripRequirements
from app.plan_validation import PlanValidationError, validate_plan
from app.routes import prepare_routes, seed_places
from app.store import DemoStore
from app.tools import routes as provider
from app.tools.registry import run_tool

A = dict(poi_id='a', name='景点甲', address='东路1号', longitude=120.1, latitude=30.2, city='杭州市', district='西湖区', source='高德地图 POI', fetched_at='2026-10-03T00:00:00+00:00')
B = {**A, 'poi_id': 'b', 'name': '景点乙', 'longitude': 120.2}
E = {**A, 'poi_id': 'e', 'name': '杭州东站', 'longitude': 120.3}


def fixture():
    local = DemoStore(persistence_enabled=False)
    trip = deepcopy(local.trips['trip-hangzhou'])
    trip.requirements = TripRequirements(city='杭州', start_date='2026-11-01', end_date='2026-11-01', day_count=1,
                                         arrival_time='09:00', departure_time='18:00')
    local.trips[trip.id] = trip
    raw = asdict(local._seed_plan())
    raw['days'] = raw['days'][:1]
    raw['days'][0]['date'] = '2026-11-01'
    raw['days'][0]['items'] = raw['days'][0]['items'][:2]
    for item, point, time in zip(raw['days'][0]['items'], (A, B), ('09:00-10:00', '10:10-11:00')):
        item.update(poi_id=point['poi_id'], location=point['name'], title=point['name'], time=time, travel_mode='transit')
    return local, trip, raw


@pytest.fixture
def network(monkeypatch):
    monkeypatch.setenv('AMAP_MAP_KEY', 'test')
    requests = []
    def fetch(endpoint, params):
        requests.append((endpoint, params))
        return {'status': '1', 'infocode': '10000', 'route': {'paths': [{'duration': '2100', 'distance': '5000'}], 'transits': [{'duration': '2100', 'distance': '5000'}]}}
    monkeypatch.setattr(provider, '_fetch_json', fetch)
    return requests


@pytest.mark.parametrize('mode,endpoint', [('walking','walking'), ('transit','transit/integrated'), ('driving','driving')])
def test_modes_use_provider_duration_and_real_coordinates(network, mode, endpoint):
    result = provider.query_route(A, B, mode)
    assert result['duration_minutes'] == 35 and result['distance_meters'] == 5000
    assert network[0][0] == endpoint
    assert network[0][1]['origin'] == '120.1,30.2'
    if mode == 'transit': assert network[0][1]['cityd'] == '杭州市'


@pytest.mark.parametrize('mode', ['missing_key', 'network', 'permission', 'no_routes', 'nan', 'zero'])
def test_failures_remain_unknown(monkeypatch, mode):
    monkeypatch.setenv('AMAP_MAP_KEY', '' if mode == 'missing_key' else 'test')
    def fetch(*args):
        if mode == 'network': raise URLError('unavailable')
        if mode == 'permission': return {'status': '0', 'infocode': '10005'}
        duration = 'NaN' if mode == 'nan' else '0'
        return {'status': '1', 'infocode': '10000', 'route': {'paths': [] if mode == 'no_routes' else [{'duration': duration, 'distance': '1'}]}}
    monkeypatch.setattr(provider, '_fetch_json', fetch)
    result = provider.query_route(A, B, 'walking')
    assert result['status'] == 'unavailable' and result['duration_minutes'] is None


def test_cache_reuses_queries_but_distinguishes_mode_and_coordinates(network):
    catalog, cache = {'a': A, 'b': B}, {}
    args = {'origin_poi_id': 'a', 'destination_poi_id': 'b', 'mode': 'walking'}
    run_tool('query_route', args, place_catalog=catalog, route_cache=cache)
    run_tool('query_route', args, place_catalog=catalog, route_cache=cache)
    assert len(network) == 1
    run_tool('query_route', {**args, 'mode': 'transit'}, place_catalog=catalog, route_cache=cache)
    catalog['b'] = {**B, 'longitude': 120.25}
    run_tool('query_route', args, place_catalog=catalog, route_cache=cache)
    assert len(network) == 3
    result = run_tool('query_route', {**args, 'origin_poi_id': 'fabricated'}, place_catalog=catalog, route_cache=cache)
    assert result['status'] == 'unavailable' and len(network) == 3


def test_real_gap_is_validated_and_model_route_fields_are_overwritten(network):
    _, trip, raw = fixture()
    raw['days'][0]['items'][1]['incoming_route'] = {'duration_minutes': 1, 'status': 'available'}
    prepare_routes(raw, trip, {'a': A, 'b': B}, {})
    with pytest.raises(PlanValidationError, match='预计 35 分钟'): validate_plan(raw, trip)
    raw['days'][0]['items'][1]['time'] = '10:40-11:40'
    validate_plan(raw, trip)
    assert raw['days'][0]['items'][1]['incoming_route']['duration_minutes'] == 35


def test_fixed_rest_cannot_be_used_as_travel_time(network):
    _, trip, raw = fixture()
    rest = {**deepcopy(raw['days'][0]['items'][0]), 'category': 'rest', 'poi_id': '', 'time': '10:00-10:30', 'title': '休息'}
    raw['days'][0]['items'].insert(1, rest)
    raw['days'][0]['items'][2]['time'] = '11:00-12:00'
    prepare_routes(raw, trip, {'a': A, 'b': B}, {})
    with pytest.raises(PlanValidationError, match='可用时间 30 分钟'): validate_plan(raw, trip)


def test_unknown_intermediate_place_is_not_skipped(network):
    _, trip, raw = fixture()
    unknown = {**deepcopy(raw['days'][0]['items'][0]), 'poi_id': '', 'id': 'unknown', 'title': '区域散步', 'location': '老城区', 'place_scope': 'area', 'time': '10:00-10:10'}
    raw['days'][0]['items'].insert(1, unknown)
    prepare_routes(raw, trip, {'a': A, 'b': B}, {})
    assert network == []
    assert raw['days'][0]['items'][2]['incoming_route']['status'] == 'unavailable'
    assert raw['pending_items']


def test_missing_end_time_does_not_assume_instant_visit(network):
    _, trip, raw = fixture()
    raw['days'][0]['items'][0]['time'] = '09:00'
    prepare_routes(raw, trip, {'a': A, 'b': B}, {})
    validate_plan(raw, trip)
    assert any('结束或抵达时间不完整' in line for line in raw['pending_items'])


@pytest.mark.parametrize('ambiguous', [False, True])
def test_arrival_departure_routes_and_ambiguous_endpoints(network, monkeypatch, ambiguous):
    _, trip, raw = fixture()
    trip.requirements.arrival_location = trip.requirements.departure_location = '杭州东站'
    candidates = [E, {**E, 'poi_id': 'other'}] if ambiguous else [E]
    monkeypatch.setattr(importlib.import_module('app.routes'), 'search_places', lambda args: {'status': 'available', 'candidates': candidates})
    raw['days'][0]['items'] = raw['days'][0]['items'][:1]
    raw['days'][0]['items'][0]['time'] = '09:10-17:40'
    prepare_routes(raw, trip, {'a': A}, {})
    if ambiguous:
        assert network == []
        assert raw['days'][0]['items'][0]['incoming_route']['status'] == 'unavailable'
    else:
        with pytest.raises(PlanValidationError) as error: validate_plan(raw, trip)
        assert '可用时间 10 分钟' in str(error.value) and '距离离开仅有 20 分钟' in str(error.value)
        raw['days'][0]['items'][0]['time'] = '10:00-17:00'
        validate_plan(raw, trip)


def planning_call(monkeypatch, raws, trip, catalog, cache, existing=None, target=None):
    results = [json.dumps({'body': '已生成', 'card': {}, 'idea_cards': [], 'plan': raw}, ensure_ascii=False) for raw in raws]
    requests = []
    def completion(client, model, messages, **kwargs):
        requests.append(deepcopy(messages))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=results.pop(0)))])
    monkeypatch.setattr(agent_client, '_create_chat_completion', completion)
    agent_client._request_model(object(), 'test', '行程', True, trip=trip, place_catalog=catalog,
                                route_cache=cache, existing_plan=existing, target_item_id=target)
    return requests


def test_agent_receives_evidence_and_repairs_once_without_duplicate_queries(network, monkeypatch):
    _, trip, bad = fixture()
    good = deepcopy(bad)
    good['days'][0]['items'][1]['time'] = '10:40-11:40'
    cache = {}
    requests = planning_call(monkeypatch, [bad, good], trip, {'a': A, 'b': B}, cache)
    assert len(requests) == 2 and len(network) == 1
    assert any('35' in message.get('content', '') and '真实路线查询结果' in message.get('content', '') for message in requests[1])
    assert cache['final_plan']['days'][0]['items'][1]['incoming_route']['duration_minutes'] == 35


def test_persistent_conflict_preserves_original_plan(network, monkeypatch):
    local, trip, raw = fixture()
    original = deepcopy(local.plans[trip.id])
    with pytest.raises(PlanValidationError): planning_call(monkeypatch, [raw, raw], trip, {'a': A, 'b': B}, {})
    assert local.plans[trip.id] == original and len(network) == 1


def test_single_item_repair_checks_actual_merge_and_updates_neighbor_route(network, monkeypatch):
    local, trip, original = fixture()
    original['days'][0]['items'][1]['time'] = '10:40-11:40'
    local._apply_agent_plan(trip.id, original, verified_places={'a': A, 'b': B})
    existing = deepcopy(local.plans[trip.id])
    target = existing.days[0].items[0]
    bad = asdict(existing)
    bad['days'][0]['items'][0]['time'] = '09:00-10:20'
    bad['days'][0]['items'][1]['time'] = '11:30-12:30'  # Cannot change the neighbor to hide the conflict.
    good = deepcopy(bad)
    good['days'][0]['items'][0]['time'] = '09:00-10:00'
    cache = {}
    requests = planning_call(monkeypatch, [bad, good], trip, seed_places(existing), cache, existing, target.id)
    assert len(requests) == 2
    reply = AgentReply('调整完成', AgentCard('revision', '调整', '', [], []), [], cache['final_plan'], verified_places=seed_places(existing))
    monkeypatch.setattr(importlib.import_module('app.store'), 'generate_agent_reply', lambda **kwargs: reply)
    local.request_revision(trip.id, target.id, '改动时间')
    neighbor = local.plans[trip.id].days[0].items[1]
    assert neighbor.time == '10:40-11:40' and neighbor.incoming_route.duration_minutes == 35


@pytest.mark.parametrize('hotel_known', [False, True])
def test_next_day_origin_uses_only_known_overnight_location(network, hotel_known):
    _, trip, raw = fixture()
    trip.requirements.day_count = 2
    trip.requirements.end_date = '2026-11-02'
    first = deepcopy(raw['days'][0]['items'][0])
    first.update(category='accommodation' if hotel_known else 'activity', time='18:00-19:00')
    raw['days'][0]['items'] = [first]
    second = deepcopy(raw['days'][0])
    second['date'] = '2026-11-02'
    stop = {**deepcopy(first), 'category': 'activity', 'poi_id': 'b', 'title': B['name'], 'location': B['name'], 'time': '10:00-11:00'}
    transport = {**deepcopy(first), 'category': 'transport', 'time': '09:00-10:00', 'travel_mode': 'transit'}
    second['items'] = [transport, stop]
    raw['days'].append(second)
    prepare_routes(raw, trip, {'a': A, 'b': B}, {})
    validate_plan(raw, trip)
    incoming = raw['days'][1]['items'][1]['incoming_route']
    assert (incoming['status'] == 'available') == hotel_known
    if hotel_known: assert incoming['duration_minutes'] == 35 and incoming['origin_poi_id'] == 'a'
    else: assert '当日出发地点待确认' in incoming['origin_name']


def test_unavailable_routes_are_cached_and_query_limit_is_explicit(monkeypatch):
    calls = []
    monkeypatch.setattr(provider, 'query_route', lambda origin, destination, mode: calls.append(True) or {'status': 'unavailable', 'reason': 'network'})
    cache, catalog = {}, {'a': A, 'b': B}
    arguments = {'origin_poi_id': 'a', 'destination_poi_id': 'b', 'mode': 'walking'}
    run_tool('query_route', arguments, place_catalog=catalog, route_cache=cache)
    run_tool('query_route', arguments, place_catalog=catalog, route_cache=cache)
    assert calls == [True]
    cache = {('a', str(i), 120.1, 30.2, 120.2, 30.2, 'walking'): {} for i in range(32)}
    result = run_tool('query_route', arguments, place_catalog=catalog, route_cache=cache)
    assert '上限' in result['reason'] and calls == [True]


def test_explicit_transport_is_not_a_stop_and_uses_its_actual_mode(network):
    _, trip, raw = fixture()
    # Focus on A -> B, with a separately displayed bus leg.
    bus = {**deepcopy(raw['days'][0]['items'][0]), 'category': 'transport', 'title': '公交去乙', 'time': '10:00-10:40', 'travel_mode': 'transit'}
    raw['days'][0]['items'].insert(1, bus)
    raw['days'][0]['items'][2].update(time='10:40-11:40', travel_mode='walking')
    prepare_routes(raw, trip, {'a': A, 'b': B}, {})
    validate_plan(raw, trip)
    assert len(network) == 1
    assert raw['days'][0]['items'][2]['incoming_route']['mode'] == 'transit'
    assert raw['days'][0]['items'][2]['incoming_route']['origin_poi_id'] == 'a'
    # A large gap does not justify a bus item whose own duration is too short.
    bus['time'] = '10:00-10:10'
    with pytest.raises(PlanValidationError, match='交通条目仅预留 10 分钟'): validate_plan(raw, trip)


def test_model_route_metadata_is_not_parsed():
    _, _, raw = fixture()
    raw['days'][0]['items'][0]['incoming_route'] = {'duration_minutes': 1, 'status': 'available'}
    parsed = agent_client._parse_plan(raw)
    assert 'incoming_route' not in parsed['days'][0]['items'][0]


def test_arrival_transport_window_is_counted_before_first_stop(network, monkeypatch):
    _, trip, raw = fixture()
    trip.requirements.arrival_location = '杭州东站'
    monkeypatch.setattr(importlib.import_module('app.routes'), 'search_places', lambda args: {'status': 'available', 'candidates': [E]})
    raw['days'][0]['items'] = raw['days'][0]['items'][:1]
    raw['days'][0]['items'][0].update(time='09:40-11:00', travel_mode='walking')
    bus = {**deepcopy(raw['days'][0]['items'][0]), 'category': 'transport', 'poi_id': 'e', 'title': '接驳', 'time': '09:00-09:40', 'travel_mode': 'transit'}
    raw['days'][0]['items'].insert(0, bus)
    prepare_routes(raw, trip, {'a': A, 'e': E}, {})
    validate_plan(raw, trip)
    route = raw['days'][0]['items'][1]['incoming_route']
    assert route['origin_poi_id'] == 'e' and route['mode'] == 'transit'
    assert route['duration_minutes'] == 35


def test_routes_survive_restart_and_history_restore(network, tmp_path):
    _, trip, raw = fixture()
    raw['days'][0]['items'][1]['time'] = '10:40-11:40'
    prepare_routes(raw, trip, {'a': A, 'b': B}, {})
    db = str(tmp_path / 'routes.sqlite3')
    local = DemoStore(db_path=db, persistence_enabled=True)
    local.trips[trip.id] = trip
    local._apply_agent_plan(trip.id, raw, verified_places={'a': A, 'b': B})
    local._persist()
    restored = DemoStore(db_path=db, persistence_enabled=True)
    assert restored.plans[trip.id].days[0].items[1].incoming_route.duration_minutes == 35
    restored.restore_plan_version(trip.id, restored.plan_versions[trip.id][-1].id)
    assert restored.plans[trip.id].days[0].items[1].incoming_route.destination_poi_id == 'b'
