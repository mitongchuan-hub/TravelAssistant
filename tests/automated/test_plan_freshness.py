"""Offline characterization of saved-plan freshness and its shared validators."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from threading import RLock

import pytest

from app.domain.models import Trip, TripDay, TripPlan, TripRequirements, User
from app.services.itinerary import freshness
from app.services.itinerary.plan_display import text_hash
from app.views import route_view
from app.application.guide import DisplayTasks


BASIS_NOTE = '站点为方案顺序，不是地图几何；历史路线仅为查询时估算，不证明开放、免费、门票、预约或遮蔽条件。'
MISSING_NOTE = ' 原方案住宿依据不完整，不使用当前住宿补齐原路线。'
MESSAGES = {
    'incomplete': '原方案的需求版本或需求快照不完整，无法确认是否仍适用。',
    'settings': '游玩设置或旅行需求已变化，以下为原方案的历史依据，请重新生成方案。',
    'location': '无法完整核对原方案与当前已确认住宿的位置依据。',
    'changed': '已确认住宿发生变化，以下仅使用原方案住宿，未混入当前住宿，请重新生成方案。',
    'city': '方案与当前游玩设置及已确认住宿一致，出发前仍需核实。',
    'travel': '方案与当前旅行需求一致，出发前仍需核实。',
}
STAMP = '2026-10-21T01:16:06+00:00'


def place(**changes):
    return dict(poi_id='hotel', name='测试住处', address='测试地址', longitude=120.16,
                latitude=30.25, city='杭州市', district='上城区', source='高德地图 POI',
                fetched_at=STAMP, **changes)


def itinerary():
    requirements = TripRequirements(city='杭州', day_count=1, revision=2)
    trip = Trip('trip', '杭州', User('owner', '测试', '测'), '', '',
                requirements=requirements, lodging=place())
    plan = TripPlan('plan', '方案', '草案', text='测试住处\n湖滨公园\n返回测试住处。',
                    requirements_revision=2, requirements_snapshot=asdict(requirements),
                    lodging_snapshot=place())
    return trip, plan


def expected(status, message, missing=False):
    return {'basis_status': status,
            'basis_message': MESSAGES[message] + (MISSING_NOTE if missing else '') + ' ' + BASIS_NOTE}


@pytest.mark.parametrize('case,status,message,missing', [
    ('city', 'fresh', 'city', False),
    ('travel', 'fresh', 'travel', False),
    ('snapshot_missing_key', 'unknown', 'incomplete', False),
    ('snapshot_extra_key', 'unknown', 'incomplete', False),
    ('snapshot_not_dict', 'unknown', 'incomplete', False),
    ('revision_missing', 'unknown', 'incomplete', False),
    ('revision_bool', 'unknown', 'incomplete', False),
    ('revision_negative', 'unknown', 'incomplete', False),
    ('revision_float', 'unknown', 'incomplete', False),
    ('current_revision_bool', 'unknown', 'incomplete', False),
    ('current_revision_float', 'unknown', 'incomplete', False),
    ('snapshot_revision_bool', 'unknown', 'incomplete', False),
    ('snapshot_revision_float', 'unknown', 'incomplete', False),
    ('snapshot_revision_mismatch', 'unknown', 'incomplete', False),
    ('current_revision_changed', 'stale', 'settings', False),
    ('snapshot_changed', 'stale', 'settings', False),
    ('snapshot_equal_bool', 'stale', 'settings', False),
    ('snapshot_equal_float', 'stale', 'settings', False),
    ('snapshot_equal_tuple', 'stale', 'settings', False),
    ('saved_missing', 'unknown', 'location', True),
    ('current_missing', 'unknown', 'location', False),
    ('current_city_blank', 'unknown', 'location', False),
    ('saved_wrong_city', 'unknown', 'location', True),
    ('current_wrong_city', 'unknown', 'location', False),
    ('settings_and_missing', 'stale', 'settings', True),
    ('incomplete_and_missing', 'unknown', 'incomplete', True),
    ('metadata_only', 'fresh', 'city', False),
    ('normalized_identity', 'fresh', 'city', False),
])
def test_exact_freshness_matrix_without_mutation(case, status, message, missing):
    trip, plan = itinerary()
    if case == 'travel':
        trip.mode, trip.lodging, plan.lodging_snapshot = 'travel', {}, {}
    elif case == 'snapshot_missing_key':
        plan.requirements_snapshot.pop('transport_mode')
    elif case == 'snapshot_extra_key':
        plan.requirements_snapshot['extra'] = ''
    elif case == 'snapshot_not_dict':
        plan.requirements_snapshot = []
    elif case.startswith('revision_'):
        plan.requirements_revision = {'revision_missing': None, 'revision_bool': True,
                                     'revision_negative': -1, 'revision_float': 2.0}[case]
    elif case.startswith('current_revision_'):
        trip.requirements.revision = {'current_revision_bool': True,
                                      'current_revision_float': 2.0, 'current_revision_changed': 3}[case]
    elif case.startswith('snapshot_revision_'):
        plan.requirements_snapshot['revision'] = {'snapshot_revision_bool': True,
            'snapshot_revision_float': 2.0, 'snapshot_revision_mismatch': 1}[case]
    elif case in {'snapshot_changed', 'settings_and_missing'}:
        trip.requirements.transport_mode = 'driving'
        if case == 'settings_and_missing':
            plan.lodging_snapshot = {}
    elif case == 'snapshot_equal_bool':
        plan.requirements_snapshot['day_count'] = True
    elif case == 'snapshot_equal_float':
        plan.requirements_snapshot['day_count'] = 1.0
    elif case == 'snapshot_equal_tuple':
        plan.requirements_snapshot['lodging_address'] = ()
    elif case in {'saved_missing', 'incomplete_and_missing'}:
        plan.lodging_snapshot = {}
        if case == 'incomplete_and_missing':
            plan.requirements_snapshot = {}
    elif case == 'current_missing':
        trip.lodging = {}
    elif case == 'current_city_blank':
        trip.requirements.city = ' '
        plan.requirements_snapshot['city'] = ' '
    elif case == 'saved_wrong_city':
        plan.lodging_snapshot['city'] = '苏州'
    elif case == 'current_wrong_city':
        trip.lodging['city'] = '苏州'
    elif case == 'metadata_only':
        trip.lodging.update(fetched_at='2026-10-22T01:16:06+08:00', selected_at='not-a-timestamp',
                            opening_hours='待核实', place_type='宾馆')
    elif case == 'normalized_identity':
        trip.lodging.update(name='  测试住处  ', city='杭州', address=' 测试地址 ', district=' 上城区 ')
    before = deepcopy((trip, plan))
    assert freshness.plan_freshness(trip, plan) == expected(status, message, missing)
    assert (trip, plan) == before


@pytest.mark.parametrize('field,value', [('poi_id', 'other'), ('name', '另一住处'),
    ('longitude', 120.17), ('latitude', 30.26), ('district', '另一区'), ('address', '另一地址')])
def test_all_lodging_identity_fields_detect_changes(field, value):
    trip, plan = itinerary()
    trip.lodging[field] = value
    before = deepcopy((trip, plan))
    assert freshness.plan_freshness(trip, plan) == expected('stale', 'changed')
    assert (trip, plan) == before


@pytest.mark.parametrize('kind', ['none', 'blank', 'days'])
def test_empty_and_legacy_day_content(kind):
    trip, plan = itinerary()
    if kind == 'none':
        plan = None
    else:
        plan.text = ' \n '
        plan.requirements_snapshot = {}
        if kind == 'days':
            plan.days = [TripDay('day', '第1天', [])]
    before = deepcopy((trip, plan))
    result = freshness.plan_freshness(trip, plan)
    assert result == (expected('unknown', 'incomplete') if kind == 'days' else
                      {'basis_status': 'empty', 'basis_message': '尚无行程方案。'})
    assert (trip, plan) == before


@pytest.mark.parametrize('value', [None, True, 1, '', ' ', 'not-a-date',
    '2026-10-21T01:16:06', '2026-99-21T01:16:06+00:00', '2026-10-21T01:16:06+99:00'])
def test_timestamp_rejects_malformed_and_naive_values(value):
    assert freshness.parse_timestamp(value) is None


@pytest.mark.parametrize('value', [STAMP, '2026-10-21T01:16:06Z', '2026-10-21T09:16:06+08:00'])
def test_timestamp_keeps_timezone_aware_instants(value):
    assert freshness.parse_timestamp(value) == datetime.fromisoformat(STAMP)


@pytest.mark.parametrize('field,value', [
    ('longitude', True), ('latitude', False), ('longitude', '120.16'),
    ('longitude', float('inf')), ('latitude', float('nan')), ('longitude', 10 ** 1000),
    ('longitude', 181), ('latitude', -91), ('poi_id', True), ('name', 1),
    ('poi_id', ' '), ('name', ''), ('city', ''), ('address', None), ('district', []),
    ('source', '其他来源'), ('fetched_at', '2026-10-21T01:16:06'),
    ('fetched_at', 'invalid'), ('fetched_at', True), ('selected_at', 1),
    ('place_type', {}), ('opening_hours', False), ('unexpected', 'field'),
])
def test_place_validation_rejects_unsafe_data_without_mutation(field, value):
    raw = place()
    raw[field] = value
    before = deepcopy(raw)
    assert freshness.validated_place(raw) is None
    # NaN deliberately compares unequal to itself; compare each untouched object value.
    assert raw.keys() == before.keys()
    assert all(raw[key] == before[key] or raw[key] is value for key in raw)


@pytest.mark.parametrize('raw', [None, [], '', True, {}])
def test_place_validation_requires_complete_mapping(raw):
    assert freshness.validated_place(raw) is None


def test_public_helpers_are_the_only_route_validation_implementations():
    for alias, public in [('_name', 'normalize_name'), ('_city', 'normalize_city'),
        ('_timestamp', 'parse_timestamp'), ('_place', 'validated_place'),
        ('_saved_city', 'saved_city'), ('_saved_lodging', 'saved_lodging'),
        ('_current_lodging', 'current_lodging'), ('_lodging_identity', 'lodging_identity')]:
        assert getattr(route_view, alias) is getattr(freshness, public)
    assert route_view.plan_freshness is freshness.plan_freshness
    assert not hasattr(route_view, 'plan_basis')
    assert freshness.BASIS_NOTE == route_view.BASIS_NOTE == BASIS_NOTE
    assert freshness.normalize_name('  A\t B \n') == 'a b'
    assert freshness.normalize_city(' 杭州市 ') == '杭州'
    assert freshness.normalize_name(True) == freshness.normalize_city(None) == ''
    raw = place()
    raw.update(longitude=180, latitude=-90)
    assert freshness.validated_place(raw, '杭州') is not None
    assert freshness.validated_place(raw, '苏州') is None


class DisplayHarness(DisplayTasks):
    """Exercise the real polling projection without a store, workers or settings."""
    def __init__(self, trip, plan):
        self._lock = RLock()
        self.trips = {trip.id: trip}
        self.plans = {trip.id: plan}
        self.display_jobs = {}
        self.tasks = {}
        self._foreground_operations = {}

    @staticmethod
    def display_enabled():
        return False


def test_display_state_and_route_view_reuse_freshness_without_mutation():
    trip, plan = itinerary()
    park = place()
    park.update(poi_id='park', name='湖滨公园')
    plan.display = {'days': [{'items': [{'title': '湖滨公园', 'location': '湖滨公园',
                                      'category': 'activity'}]}]}
    plan.display_source_hash = text_hash(plan.text)
    plan.evidence = [
        {'tool': 'search_places', 'result': {'status': 'available', 'candidates': [park]}},
        {'tool': 'query_route',
         'arguments': {'origin_poi_id': 'hotel', 'destination_poi_id': 'park', 'mode': 'walking'},
         'result': {'origin_poi_id': 'hotel', 'destination_poi_id': 'park', 'mode': 'walking',
                    'origin_name': '测试住处', 'destination_name': '湖滨公园',
                    'source': '高德路线规划', 'status': 'available', 'fetched_at': STAMP,
                    'duration_minutes': 10, 'distance_meters': 500}},
    ]
    harness = DisplayHarness(trip, plan)
    before = deepcopy((trip, plan))
    state = harness.get_display_state(trip.id)
    view = route_view.build_route_view(trip, plan)
    for result in (state, view):
        assert {key: result[key] for key in ('basis_status', 'basis_message')} == expected('fresh', 'city')
    assert view['lodging']['poi_id'] == 'hotel'
    assert view['days'][0]['items'][0]['place']['poi_id'] == 'park'
    incoming = view['days'][0]['items'][0]['incoming']
    assert incoming['status'] == 'available'
    assert incoming['summary'] == '步行 · 查询时约10分钟 · 500米'
    assert incoming['queried_at'] == STAMP
    assert incoming['navigation_url'].startswith('https://uri.amap.com/navigation?')
    assert (trip, plan) == before
    trip.lodging['longitude'] += 0.01
    before = deepcopy((trip, plan))
    changed = harness.get_display_state(trip.id)
    historical = route_view.build_route_view(trip, plan)
    assert changed['version'] != state['version']
    for result in (changed, historical):
        assert {key: result[key] for key in ('basis_status', 'basis_message')} == expected('stale', 'changed')
    assert historical['lodging'] == view['lodging']
    assert historical['days'] == view['days']
    assert (trip, plan) == before
    assert not harness.display_jobs


def test_bad_poi_timestamp_is_rejected_by_both_lodging_and_route_catalog():
    trip, plan = itinerary()
    invalid = place()
    invalid.update(poi_id='park', name='湖滨公园', fetched_at='2026-10-21T01:16:06')
    plan.lodging_snapshot = invalid
    plan.display = {'days': [{'items': [{'title': '湖滨公园', 'category': 'activity'}]}]}
    plan.display_source_hash = text_hash(plan.text)
    plan.evidence = [{'tool': 'search_places', 'result': {'status': 'available', 'candidates': [invalid]}}]
    before = deepcopy((trip, plan))
    view = route_view.build_route_view(trip, plan)
    assert view['lodging'] is None
    assert view['current_lodging']['poi_id'] == 'hotel'
    assert view['days'][0]['items'][0]['place'] is None
    assert view['days'][0]['items'][0]['incoming']['navigation_url'] is None
    assert view['basis_message'] == expected('unknown', 'location', True)['basis_message']
    assert (trip, plan) == before
