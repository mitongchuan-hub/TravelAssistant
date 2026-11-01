"""Regression tests for app.services.places (previously not unit-tested).

collect_places builds the POI catalog only from real search_places tool results
that are status-available and carry a valid, in-range, trusted-source place;
resolve_place binds an item to a catalog entry, rejects area-scope and
non-string POI ids, and stamps a selected_at.
"""
import json

from app.services.places import collect_places, resolve_place

STAMP = '2026-01-01T00:00:00+00:00'


def candidate(**overrides):
    base = {'poi_id': 'p1', 'name': '西湖', 'address': '地址', 'longitude': 120.1,
            'latitude': 30.2, 'city': '杭州', 'district': '西湖区',
            'source': '高德地图 POI', 'fetched_at': STAMP}
    base.update(overrides)
    return base


def tool_message(payload, name='search_places'):
    return {'role': 'tool', 'name': name, 'content': json.dumps(payload)}


def test_collect_places_keeps_only_valid_available_candidates():
    catalog = collect_places([
        tool_message({'status': 'available', 'candidates': [candidate()]}),
        tool_message({'status': 'unavailable', 'candidates': [candidate(poi_id='pX')]}),
        tool_message({'status': 'available', 'candidates': [candidate(poi_id='p2', longitude=200)]}),
        tool_message({'status': 'available', 'candidates': [candidate(poi_id='p3', source='other')]}),
        tool_message({'status': 'available', 'candidates': [candidate(poi_id='p4', longitude=float('nan'))]}),
        tool_message({'status': 'available', 'candidates': [candidate(poi_id='')]}, ),
        tool_message({'status': 'available', 'candidates': [candidate()]}, name='other_tool'),
        {'role': 'tool', 'name': 'search_places', 'content': 'not-json'},
        {'role': 'assistant', 'name': 'search_places', 'content': json.dumps(
            {'status': 'available', 'candidates': [candidate(poi_id='p6')]})},
    ])
    assert set(catalog) == {'p1'}
    assert catalog['p1']['name'] == '西湖'


def test_collect_places_last_valid_entry_wins_for_same_poi():
    catalog = collect_places([
        tool_message({'status': 'available', 'candidates': [candidate(name='旧名称')]}),
        tool_message({'status': 'available', 'candidates': [candidate(name='新名称')]}),
    ])
    assert catalog['p1']['name'] == '新名称'


def test_collect_places_requires_all_place_fields():
    # Missing a required PlaceDetails field (district) must drop the candidate.
    incomplete = candidate()
    del incomplete['district']
    catalog = collect_places([
        tool_message({'status': 'available', 'candidates': [incomplete]})
    ])
    assert catalog == {}


def test_resolve_place_binds_and_stamps_selected_at():
    catalog = collect_places([tool_message({'status': 'available', 'candidates': [candidate()]})])
    resolved = resolve_place({'poi_id': 'p1'}, catalog)
    assert resolved is not None
    assert resolved.name == '西湖'
    assert resolved.selected_at  # stamped at resolution time


def test_resolve_place_rejects_unknown_poi():
    catalog = collect_places([tool_message({'status': 'available', 'candidates': [candidate()]})])
    assert resolve_place({'poi_id': 'nope'}, catalog) is None


def test_resolve_place_rejects_area_scope():
    catalog = collect_places([tool_message({'status': 'available', 'candidates': [candidate()]})])
    assert resolve_place({'poi_id': 'p1', 'place_scope': 'area'}, catalog) is None


def test_resolve_place_rejects_non_string_poi():
    catalog = collect_places([tool_message({'status': 'available', 'candidates': [candidate()]})])
    assert resolve_place({'poi_id': 123}, catalog) is None


def test_resolve_place_empty_catalog():
    assert resolve_place({'poi_id': 'p1'}, {}) is None
