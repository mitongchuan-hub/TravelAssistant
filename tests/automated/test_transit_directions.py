"""Transit instructions survive provider normalization, evidence binding and rendering.

The direct subway response mirrors the real AMap MCP shape observed on 2026-10-21.
Other routes are synthetic fixtures and make no claims about real services.
"""
from copy import deepcopy
from dataclasses import asdict
from html.parser import HTMLParser
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
import pytest

from app.agents.agent_runtime import transit_detail_guard
from app.models import Trip, TripPlan, TripRequirements, User
from app.services.plan_display import text_hash
from app.tools import routes
from app.views.display_presentation import display_view
from app.views.presentation import day_date_label
from app.views.route_view import build_route_view


QUERIED_AT = '2026-10-21T01:16:06+00:00'


def place(poi_id, name):
    return dict(poi_id=poi_id, name=name, address='测试地址', longitude=120.16,
                latitude=30.25, city='杭州市', district='上城区',
                source='高德地图 POI', fetched_at=QUERIED_AT)


@pytest.fixture
def direct_payload():
    return {'route': {'distance': '8816', 'transits': [{
        'duration': '930', 'walking_distance': '0', 'segments': [{
            'walking': {'origin': '', 'destination': '', 'distance': '', 'duration': ''},
            'bus': {'buslines': [{
                'name': '地铁1号线(萧山国际机场--湘湖)',
                'departure_stop': {'name': '火车东站'},
                'arrival_stop': {'name': '龙翔桥'},
                'duration': '930', 'distance': '8080'}]},
            'entrance': [], 'exit': [], 'railway': {}}]}]}}


def query(monkeypatch, payload, mode='transit'):
    monkeypatch.setattr(routes.amap_mcp, 'configured', lambda: True)
    monkeypatch.setattr(routes, '_fetch_json', lambda endpoint, params: deepcopy(payload))
    return routes.query_route(place('lodging', '测试住处'), place('park', '湖滨公园'), mode)


def test_subway_result_keeps_line_and_boarding_stations(monkeypatch, direct_payload):
    result = query(monkeypatch, direct_payload)
    assert result['status'] == 'available'
    assert result['transit_detail_status'] == 'available'
    assert result['duration_minutes'] == 16
    assert result['distance_meters'] == 8816
    ride = result['transit_steps'][0]
    assert ride['kind'] == 'ride'
    line = ride['lines'][0]
    assert line['name'] == '地铁1号线(萧山国际机场--湘湖)'
    assert line['departure_stop'] == '火车东站'
    assert line['arrival_stop'] == '龙翔桥'
    assert line['direction'] is None
    assert line['via_stop_count'] is None


@pytest.fixture
def transfer_payload():
    return {'route': {'transits': [{'duration': '1800', 'distance': '6400', 'segments': [
        {'walking': {'distance': '120', 'duration': '90', 'steps': [
            {'instruction': '沿站前路步行至甲站'}]},
         'bus': {'buslines': [
             {'name': '28路(甲站--乙站)', 'direction': '乙站',
              'departure_stop': {'name': '甲站'}, 'arrival_stop': {'name': '乙站'},
              'via_num': '2', 'duration': '600', 'distance': '2400'},
             {'name': '28路区间(甲站--乙站)',
              'departure_stop': {'name': '甲站'}, 'arrival_stop': {'name': '乙站'}}]},
         'entrance': [], 'exit': [], 'railway': {}},
        {'walking': {}, 'bus': {'buslines': [
            {'name': '地铁2号线(乙站--丙站)',
             'departure_stop': {'name': '乙站'}, 'arrival_stop': {'name': '丙站'},
             'via_num': '0', 'duration': '600'}]},
         'entrance': {'name': 'A口'}, 'exit': {'name': 'B口'}, 'railway': {}},
        {'walking': {'distance': '140', 'duration': '110', 'steps': [
            {'instruction': '从丙站步行至公园入口'}]}, 'bus': {}, 'railway': {}}
    ]}]}}


def test_transfers_preserve_walking_order_and_alternative_lines(monkeypatch, transfer_payload):
    result = query(monkeypatch, transfer_payload)
    steps = result['transit_steps']
    assert [step['kind'] for step in steps] == ['walking', 'ride', 'ride', 'walking']
    assert steps[0]['instructions'] == ['沿站前路步行至甲站']
    assert steps[0]['distance_meters'] == 120
    assert [line['name'] for line in steps[1]['lines']] == ['28路(甲站--乙站)', '28路区间(甲站--乙站)']
    assert steps[1]['lines'][0]['direction'] == '乙站'
    assert steps[1]['lines'][0]['via_stop_count'] == 2
    assert steps[2]['lines'][0]['via_stop_count'] == 0
    assert steps[2]['entrance'] == 'A口'
    assert steps[2]['exit'] == 'B口'
    assert result['duration_minutes'] == 30  # Whole route, not summed with walking again.


def test_details_belong_to_first_valid_route_not_skipped_route(monkeypatch, direct_payload):
    invalid = deepcopy(direct_payload['route']['transits'][0])
    invalid['duration'] = '0'
    invalid['segments'][0]['bus']['buslines'][0]['name'] = '不能采用的线路'
    direct_payload['route']['transits'].insert(0, invalid)
    result = query(monkeypatch, direct_payload)
    assert result['transit_steps'][0]['lines'][0]['name'] == '地铁1号线(萧山国际机场--湘湖)'


@pytest.mark.parametrize('value', [None, [], {}, 'invalid'])
def test_missing_segments_preserve_estimate_without_inventing_lines(monkeypatch, direct_payload, value):
    direct_payload['route']['transits'][0]['segments'] = value
    result = query(monkeypatch, direct_payload)
    assert result['status'] == 'available'
    assert result['duration_minutes'] == 16
    assert result.get('transit_steps', []) == []
    assert result['transit_detail_status'] == 'missing'
    assert '线路明细' in result['reason']


def test_usable_alternative_is_not_hidden_by_a_detail_free_plan(monkeypatch, direct_payload):
    incomplete = deepcopy(direct_payload['route']['transits'][0])
    incomplete['duration'] = '300'
    incomplete['segments'] = []
    direct_payload['route']['transits'].insert(0, incomplete)
    result = query(monkeypatch, direct_payload)
    assert result['duration_minutes'] == 16
    assert result['transit_detail_status'] == 'available'
    assert result['transit_steps'][0]['lines'][0]['name'] == '地铁1号线(萧山国际机场--湘湖)'


def test_route_guard_rejects_ok_only_when_lines_were_requested(monkeypatch, direct_payload):
    incomplete = query(monkeypatch, {'route': {'transits': [{
        'duration': '600', 'distance': '900', 'segments': []}]}})
    evidence = [{'tool': 'query_route', 'result': incomplete}]
    assert transit_detail_guard('推荐公共交通路线', evidence)
    assert transit_detail_guard('从住处到欢乐谷大概多久', evidence) is None
    complete = query(monkeypatch, direct_payload)
    assert transit_detail_guard('推荐公共交通路线', [{'tool': 'query_route', 'result': complete}]) is None


def test_malformed_optional_fields_do_not_erase_valid_route(monkeypatch, direct_payload):
    segment = direct_payload['route']['transits'][0]['segments'][0]
    segment['walking'] = {'distance': 'NaN', 'duration': True, 'steps': 'invalid'}
    line = segment['bus']['buslines'][0]
    line.update(direction=1, via_num=True, duration='NaN')
    line['arrival_stop'] = []
    result = query(monkeypatch, direct_payload)
    assert result['status'] == 'available'
    assert result['transit_detail_status'] == 'partial'
    assert result['transit_steps'][0]['kind'] == 'unknown'
    line = result['transit_steps'][1]['lines'][0]
    assert line['arrival_stop'] is None
    assert line['direction'] is None
    assert line['via_stop_count'] is None
    assert line['duration_minutes'] is None


def test_unsupported_transport_leg_stays_visible_as_unknown(monkeypatch, direct_payload):
    direct_payload['route']['transits'][0]['segments'].append({
        'walking': {}, 'bus': {}, 'railway': {'name': '未支持的交通段'}})
    result = query(monkeypatch, direct_payload)
    assert [step['kind'] for step in result['transit_steps']] == ['ride', 'unknown']


def test_empty_provider_placeholders_are_not_missing_legs(monkeypatch, direct_payload):
    segments = direct_payload['route']['transits'][0]['segments']
    segments[0].update(walking={'distance': '0', 'duration': '0', 'steps': []},
                       railway={'name': '', 'trip': ''}, taxi={'cost': '', 'distance': ''})
    segments.append({'walking': {'distance': '100', 'duration': '60'},
                     'bus': {'buslines': []}, 'railway': {'name': '', 'trip': ''}})
    result = query(monkeypatch, direct_payload)
    assert [step['kind'] for step in result['transit_steps']] == ['ride', 'walking']


@pytest.mark.parametrize('mode', ['walking', 'driving'])
def test_other_modes_do_not_acquire_subway_instructions(monkeypatch, mode):
    payload = {'route': {'paths': [{'duration': '600', 'distance': '900', 'segments': []}]}}
    result = query(monkeypatch, payload, mode)
    assert result['status'] == 'available'
    assert result['duration_minutes'] == 10
    assert not result.get('transit_steps')


def itinerary(result):
    owner = User('owner', '测试用户', '测')
    requirements = TripRequirements(city='杭州市', lodging_address='测试地址',
        start_date='2030-05-01', end_date='2030-05-01', day_count=1, transport_mode='transit')
    lodging, park = place('lodging', '测试住处'), place('park', '湖滨公园')
    trip = Trip('trip', '杭州', '', '', '', '', owner, '', '', requirements=requirements, lodging=lodging)
    text = '测试住处\n湖滨公园\n返回测试住处。'
    display = {'schema_version': '1.1', 'title': '测试行程', 'overview': None,
        'budget_note': None, 'weather': [], 'preparation': {'clothing': [], 'accommodation': []},
        'pending_items': [], 'risks': [], 'days': [{'date': '2030-05-01', 'label': '第1天',
            'theme': None, 'items': [{'title': '湖滨公园', 'category': 'activity',
                'time_text': None, 'location': '湖滨公园', 'duration_text': None,
                'description': '测试游玩安排', 'cost_text': None, 'notes': []}]}]}
    evidence = [dict(tool='search_places', result=dict(status='available', candidates=[park])),
        dict(tool='query_route', arguments=dict(origin_poi_id='lodging', destination_poi_id='park', mode='transit'),
             result=deepcopy(result))]
    plan = TripPlan('plan', '测试行程', '草案', text=text, evidence=evidence, display=display,
        display_source_hash=text_hash(text), display_status='ready',
        requirements_snapshot=asdict(requirements), requirements_revision=0, lodging_snapshot=lodging)
    return trip, plan


def render(trip, plan):
    env = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[2] / 'app' / 'templates'),
        autoescape=select_autoescape(['html']))
    env.filters['day_date_label'] = day_date_label
    env.globals['place_query_time'] = lambda value: '2026年10月21日'
    return env.get_template('partials/display_itinerary.html').render(
        trip=trip, plan=plan, display=display_view(plan.display), route_view=build_route_view(trip, plan))


class CardText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.all = []
        self.unfolded = []

    def handle_starttag(self, tag, attrs):
        if tag == 'details':
            self.depth += 1

    def handle_endtag(self, tag):
        if tag == 'details':
            self.depth -= 1

    def handle_data(self, data):
        self.all.append(data)
        if self.depth == 0:
            self.unfolded.append(data)


def test_subway_line_is_visible_without_opening_location_details(monkeypatch, direct_payload):
    trip, plan = itinerary(query(monkeypatch, direct_payload))
    parsed = CardText()
    parsed.feed(render(trip, plan))
    assert '地铁1号线' in ''.join(parsed.unfolded)
    assert '火车东站' in ''.join(parsed.unfolded)
    assert '龙翔桥' in ''.join(parsed.unfolded)
    assert '16分钟' in ''.join(parsed.all)


def test_unspecified_cost_is_shown_once_instead_of_as_a_card_fact(monkeypatch, direct_payload):
    trip, plan = itinerary(query(monkeypatch, direct_payload))
    plan.display['days'][0]['items'][0]['cost_text'] = '费用未提供，不代表免费'
    content = render(trip, plan)
    assert 'class="display-cost"' not in content
    assert content.count('费用未提供，不代表免费') == 1


def test_rendering_preserves_alternatives_and_explicit_transfer(monkeypatch, transfer_payload):
    trip, plan = itinerary(query(monkeypatch, transfer_payload))
    html = render(trip, plan)
    text = CardText()
    text.feed(html)
    content = ''.join(text.all)
    assert '28路(甲站--乙站)' in content
    assert '28路区间(甲站--乙站)' in content
    assert '或' in content
    assert '换乘' in content
    assert '沿站前路步行至甲站' in content
    assert 'B口' in content


@pytest.mark.parametrize('gap', [
    {'bus': {'duration': '180'}}, {'walking': 'invalid'},
    {'walking': {'duration': 'invalid', 'distance': 'NaN', 'steps': 'invalid'}},
])
def test_missing_ride_details_keep_gap_between_known_lines(monkeypatch, direct_payload, gap):
    segments = direct_payload['route']['transits'][0]['segments']
    next_ride = deepcopy(segments[0])
    next_ride['bus']['buslines'][0]['name'] = '测试接续线路'
    segments.extend([gap, next_ride])
    result = query(monkeypatch, direct_payload)
    assert [step['kind'] for step in result['transit_steps']] == ['ride', 'unknown', 'ride']
    trip, plan = itinerary(result)
    incoming = build_route_view(trip, plan)['days'][0]['items'][0]['incoming']
    assert '待核实路段' in incoming['line_summary']
    assert incoming['transit_steps'][-1]['transfer'] is False


@pytest.mark.parametrize('mismatch', ['reverse', 'wrong_mode', 'stale_text', 'unmatched_place'])
def test_unrelated_route_cannot_supply_card_line(monkeypatch, direct_payload, mismatch):
    trip, plan = itinerary(query(monkeypatch, direct_payload))
    row = plan.evidence[-1]
    if mismatch == 'reverse':
        row['arguments'].update(origin_poi_id='park', destination_poi_id='lodging')
        row['result'].update(origin_poi_id='park', origin_name='湖滨公园',
                             destination_poi_id='lodging', destination_name='测试住处')
        # Only the separately validated return leg may use that direction.
        view = build_route_view(trip, plan)
        assert not view['days'][0]['items'][0]['incoming'].get('transit_steps')
        return
    if mismatch == 'wrong_mode':
        row['arguments']['mode'] = row['result']['mode'] = 'driving'
    elif mismatch == 'stale_text':
        plan.text += '\n正文已修改'
    else:
        plan.display['days'][0]['items'][0]['location'] = '未匹配地点'
    assert '地铁1号线' not in render(trip, plan)


def test_legacy_estimate_shows_missing_line_details(monkeypatch, direct_payload):
    result = query(monkeypatch, direct_payload)
    result.pop('transit_steps', None)
    trip, plan = itinerary(result)
    parsed = CardText()
    parsed.feed(render(trip, plan))
    content = ''.join(parsed.all)
    assert '16分钟' in content
    assert '线路明细未提供' in content
    assert '地铁1号线' not in content


def test_same_place_needs_no_missing_transit_warning():
    lodging = place('lodging', '测试住处')
    trip, plan = itinerary(routes.query_route(lodging, lodging, 'transit'))
    plan.evidence[0]['result']['candidates'] = [lodging]
    plan.evidence[-1]['arguments']['destination_poi_id'] = 'lodging'
    plan.display['days'][0]['items'][0].update(title='测试住处', location='测试住处')
    incoming = build_route_view(trip, plan)['days'][0]['items'][0]['incoming']
    assert incoming['status'] == 'available'
    assert not incoming['missing_transit_details']
    assert '线路明细未提供' not in render(trip, plan)


def test_provider_instruction_is_escaped_when_rendered(monkeypatch, direct_payload):
    direct_payload['route']['transits'][0]['segments'][0]['bus']['buslines'][0]['name'] = '<script>alert(1)</script>路'
    trip, plan = itinerary(query(monkeypatch, direct_payload))
    html = render(trip, plan)
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;路' in html
