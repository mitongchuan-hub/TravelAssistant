"""Navigation references are server-derived from visible successful detail evidence."""
from copy import deepcopy
import pytest
from app.services.maps.evidence import detail_endpoints
from app.services.maps.routes import normalize_route
from tests.automated.test_amap_rest import envelope
from tests.automated.test_readonly_chat_runtime import runtime, Script


def detail_row():
    return {'tool': 'maps_search_detail', 'arguments': {'id': 'park'},
            'result': envelope()['pois'][0]}


def test_refs_bind_same_detail_and_do_not_grant_around_permission():
    row = detail_row()
    refs = detail_endpoints(row)
    assert {x['kind'] for x in refs} == {'center', 'entrance'}
    assert refs == detail_endpoints(deepcopy(row))
    runner = runtime(Script('unused'))
    runner._register_endpoints(row)
    entrance = next(x for x in refs if x['kind'] == 'entrance')
    assert runner._route_endpoint({'destination_ref': entrance['ref']}, 'destination')['endpoint_kind'] == 'entrance'
    with pytest.raises(ValueError):
        runner._route_endpoint({'origin_ref': entrance['ref']}, 'origin')
    with pytest.raises(ValueError):
        runner._route_endpoint({'destination_ref': entrance['ref'], 'destination': '120,30'}, 'destination')
    with pytest.raises(ValueError):
        runner._around_center('120.11,30.21')
    with pytest.raises(ValueError):
        runner._route_endpoint({'destination_ref': 'invented'}, 'destination')


def test_runtime_ref_route_cache_and_fixed_lodging(monkeypatch):
    from app.services.maps import amap_rest
    from app.services.maps.places import normalize_pois
    row = detail_row()
    runner = runtime(Script('unused'))
    runner._register_endpoints(row)
    place = normalize_pois(row['result'])['candidates'][0]
    runner.catalog['park'] = place
    runner.trip.lodging = deepcopy(place)
    before = deepcopy(runner.trip.lodging)
    refs = detail_endpoints(row)
    center = next(p for p in refs if p['kind'] == 'center')
    entrance = next(p for p in refs if p['kind'] == 'entrance')
    calls = []
    def route(name, args):
        calls.append(deepcopy(args))
        return {'status': '1', 'infocode': '10000', 'route': {'paths': [{'duration': '120', 'distance': '90'}]}}
    monkeypatch.setattr(amap_rest, 'route', route)
    args = {'origin_ref': center['ref'], 'destination_ref': entrance['ref']}
    first = runner._query('maps_direction_walking', args)
    assert runner._query('maps_direction_walking', args) == first
    assert len(calls) == 1
    assert calls[0]['origin'] == '120.1,30.2' and calls[0]['destination'] == '120.11,30.21'
    assert first['route_estimate']['duration_minutes'] == 2
    assert runner.trip.lodging == before and runner.catalog['park'] == place
    runner._query('maps_direction_walking', {'origin_ref': center['ref'], 'destination_ref': center['ref']})
    assert len(calls) == 2


def test_failure_and_wrong_id_never_mint_refs():
    row = detail_row()
    row['result']['status'] = 'unavailable'
    assert detail_endpoints(row) == []
    row = detail_row()
    row['arguments']['id'] = 'forged'
    assert detail_endpoints(row) == []


def test_native_evidence_view_uses_actual_endpoint_and_keeps_history_immutable():
    from app.domain.models import PlaceDetails
    from app.views.route_view import _route_records, _leg
    from app.services.maps.places import normalize_pois
    row = detail_row()
    refs = detail_endpoints(row)
    center = normalize_pois(row['result'])['candidates'][0]
    entrance = next(x for x in refs if x['kind'] == 'entrance')
    target = {**center, 'longitude': entrance['longitude'], 'latitude': entrance['latitude'],
              'endpoint_ref': entrance['ref'], 'endpoint_kind': 'entrance'}
    args = {'origin': '120.1,30.2', 'destination': '120.11,30.21', 'destination_ref': entrance['ref']}
    result = normalize_route({'route': {'paths': [{'duration': '120', 'distance': '90'}]}}, center, target, 'walking')
    saved = [row, {'tool': 'maps_direction_walking', 'arguments': args, 'normalized': result}]
    before = deepcopy(saved)
    place = PlaceDetails(**center)
    records = _route_records(saved, {'park': place})
    leg = _leg(place, place, place.name, place.name, records)
    assert leg['status'] == 'available'
    from urllib.parse import urlparse, parse_qs
    assert parse_qs(urlparse(leg['navigation_url']).query)['to'][0].startswith('120.11,30.21,')
    assert saved == before
    saved[1]['arguments']['destination_ref'] = 'forged'
    assert not _route_records(saved, {'park': place})


def test_same_poi_different_endpoints_is_not_zero_minutes():
    center = {'poi_id': 'park', 'name': '公园', 'longitude': 120.1, 'latitude': 30.2}
    entrance = {**center, 'longitude': 120.11, 'latitude': 30.21}
    result = normalize_route({'route': {'paths': [{'duration': '120', 'distance': '90'}]}}, center, entrance, 'walking')
    assert result['duration_minutes'] == 2
