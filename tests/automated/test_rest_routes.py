"""Fixed REST route contracts: no MCP fallback or loss of failure envelopes."""
import httpx
import pytest

from app.agents.tools.registry import run_tool
from app.services.maps import amap_rest
from app.services.maps.routes import normalize_route
from tests.automated.test_amap_rest import scope
from tests.automated.test_readonly_chat_runtime import runtime, Script


@pytest.mark.parametrize('mode,path', [('walking', 'walking'), ('driving', 'driving'),
    ('transit_integrated', 'transit/integrated')])
def test_fixed_route_single_request(mode, path):
    calls = []
    args = {'origin': '120.1,30.2', 'destination': '120.2,30.3'}
    if mode == 'transit_integrated':
        args.update(city='杭州', cityd='杭州')
    body = {'status': '1', 'infocode': '10000', 'route': {'paths': []}}
    with scope(lambda req: calls.append(req) or httpx.Response(200, json=body)):
        result = run_tool('maps_direction_' + mode, args)
    assert result['route'] == body['route']
    assert len(calls) == 1
    assert calls[0].url.path == '/v3/direction/' + path
    assert result['metadata']['provider'] == 'amap_rest'


def test_runtime_retains_rest_failure_envelope():
    runner = runtime(Script('unused'))
    points = [{'poi_id': 'a', 'name': '甲', 'longitude': 120, 'latitude': 30, 'city': '杭州'},
              {'poi_id': 'b', 'name': '乙', 'longitude': 121, 'latitude': 30, 'city': '杭州'}]
    runner.catalog.update({p['poi_id']: p for p in points})
    args = dict(zip(('origin', 'destination'),
        [f"{p['longitude']},{p['latitude']}" for p in points[:2]]))
    with scope(lambda req: httpx.Response(200, json={'status': '0', 'infocode': '10003',
            'route': {'paths': [{'duration': '60', 'distance': '10'}]}})):
        result = runner._query('maps_direction_walking', args)
    assert result['status'] == 'unavailable'
    assert runner.evidence[-1]['normalized']['status'] == 'unavailable'


def test_unverified_departure_window_never_silently_queries_today():
    calls = []
    args = {'origin': '120,30', 'destination': '121,30', 'city': '杭州', 'cityd': '杭州',
            'departure_at': '2030-01-01T09:00:00+08:00', 'strategy': 0}
    with scope(lambda req: calls.append(req) or httpx.Response(200, json={})):
        result = run_tool('maps_direction_transit_integrated', args)
    assert not calls
    assert result['status'] == 'unavailable'
    assert result['query_semantics'] == 'unsupported_departure_window'
    assert result['requested_departure'] == args['departure_at']
    assert result['actual_parameters'] is None


def test_runtime_unsupported_departure_reserves_no_external_call():
    runner = runtime(Script('unused'))
    points = [{'poi_id': 'a', 'name': '甲', 'longitude': 120, 'latitude': 30, 'city': '杭州'},
              {'poi_id': 'b', 'name': '乙', 'longitude': 121, 'latitude': 30, 'city': '杭州'}]
    runner.catalog.update({p['poi_id']: p for p in points})
    result = runner._query('maps_direction_transit_integrated', {'origin': '120,30',
        'destination': '121,30', 'city': '杭州', 'cityd': '杭州', 'departure_at': '2030-01-01T09:00:00+08:00'})
    assert result['status'] == 'unavailable'
    assert result['route_estimate']['status'] == 'unavailable'
    assert runner.budget.external_calls == 0


def test_reference_strategy_maps_exactly_and_invalid_enum_is_rejected():
    from jsonschema import ValidationError
    calls = []
    args = {'origin': '120,30', 'destination': '121,30', 'city': '杭州', 'cityd': '杭州', 'strategy': 5}
    with scope(lambda req: calls.append(req) or httpx.Response(200, json={'status': '1', 'infocode': '10000', 'route': {}})):
        result = run_tool('maps_direction_transit_integrated', args)
        with pytest.raises(ValidationError):
            run_tool('maps_direction_transit_integrated', {**args, 'strategy': 4})
    assert len(calls) == 1 and calls[0].url.params['strategy'] == '5'
    assert result['actual_parameters']['strategy'] == 5
    assert result['query_semantics'] == 'query_time_reference_not_future_schedule'


def test_same_endpoint_does_not_hide_explicit_service_failure():
    point = {'poi_id': 'a', 'name': '甲', 'longitude': 120, 'latitude': 30}
    assert normalize_route({'status': 'unavailable'}, point, point, 'walking')['status'] == 'unavailable'


def test_selected_transit_fare_same_plan_and_empty_unknown():
    a = {'poi_id': 'a', 'name': '甲', 'longitude': 120, 'latitude': 30}
    b = {**a, 'poi_id': 'b', 'name': '乙', 'longitude': 121}
    ride = {'bus': {'buslines': [{'name': '公交', 'departure_stop': {'name': '甲站'},
                                'arrival_stop': {'name': '乙站'}}]}}
    payload = {'route': {'transits': [{'duration': '60', 'cost': '1'},
        {'duration': '120', 'cost': '2.5', 'segments': [ride]}]}}
    result = normalize_route(payload, a, b)
    assert result['duration_minutes'] == 2 and result['fare_yuan'] == 2.5
    payload['route']['transits'][1]['cost'] = []
    assert normalize_route(payload, a, b)['fare_yuan'] is None
    assert 'MCP' not in result['reason']
