"""Address-only creation, trusted choices, optional settings and city planning."""
from copy import deepcopy
from dataclasses import asdict
import importlib
import json
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app import agent_client
from app.agent_runtime import TravelAgentRuntime
from app.city_outings import LodgingChoices, city_requirements
from app.planning import planning_brief, required_information
from app.requirements import update_requirements
from app.store import DemoStore
from app.tools import places
from test_agent_roles import completion, scripted, route, requirements_unchanged, display_data
from test_text_itinerary import web

main = importlib.import_module('app.main')


class FormInputs(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.fields = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if tag in {'input', 'select', 'textarea'}:
            self.fields.append(dict(attrs))


LODGING = {'poi_id': 'hotel-one', 'name': '湖滨住处', 'address': '湖滨路1号',
    'longitude': 120.15, 'latitude': 30.25, 'city': '杭州市', 'district': '上城区',
    'source': '高德地图 POI', 'fetched_at': '2026-10-15T00:00:00+00:00'}
PARK = {**LODGING, 'poi_id': 'park-one', 'name': '湖滨入口', 'address': '湖滨路2号'}


def create_local(local=None, **values):
    local = local or DemoStore(persistence_enabled=False, display_worker_enabled=False)
    changes = city_requirements({'lodging_address': '湖滨路1号', **values}, LODGING['city'])
    return local, local.create_trip('杭州同城游玩', '', '', '', '', requirements=changes,
                                    mode='city', lodging=LODGING)


@pytest.fixture
def map_result(monkeypatch):
    calls = []
    def lookup(arguments):
        calls.append(deepcopy(arguments))
        return {'status': 'available', 'total_matched': 1, 'candidates': [deepcopy(LODGING)]}
    monkeypatch.setattr(main, 'search_places', lookup)
    return calls


def created_trip(response, local):
    assert response.status_code == 303
    tid = response.headers['location'].split('/')[2].split('?')[0]
    return local.trips[tid]


def test_only_address_creates_ready_undated_city_outing(web, map_result):
    client, local, _ = web
    page = client.get('/trips/new')
    assert page.status_code == 200
    # Exactly one required field; optional settings stay available.
    assert [e.get('name') for e in FormInputs(page.text).fields if 'required' in e] == ['lodging_address']
    trip = created_trip(client.post('/trips', data={'lodging_address': '湖滨路1号'},
                                    follow_redirects=False), local)
    p = trip.requirements
    assert trip.mode == 'city' and trip.lodging == LODGING
    assert p.city == '杭州市' and p.day_count == 1 and p.travelers is None
    assert p.start_date == p.end_date == p.budget_amount == ''
    assert p.arrival_location == p.departure_location == ''
    assert required_information(trip) == []
    brief, question = planning_brief(trip, require_all=True)
    assert question is None and brief['dates'] is None and brief['day_labels'] == ['第1天']
    assert map_result == [{'city': '', 'keywords': '湖滨路1号'}]
    welcome = local.messages[trip.id][0].body
    assert '可以直接说' in welcome and '还需要补充' not in welcome
    assert '湖滨住处' in welcome
    assert '游玩设置' in client.get(f'/workspace/{trip.id}').text


def test_optional_dates_budget_preferences_are_saved(web, map_result):
    client, local, _ = web
    trip = created_trip(client.post('/trips', data={
        'lodging_address': '湖滨路1号', 'start_date': '2026-12-31', 'day_count': '3',
        'outing_start_time': '10:30', 'outing_end_time': '19:00', 'travelers': '4',
        'budget_amount': '1600', 'budget_scope': 'total', 'must_visit': '湖滨入口',
        'dietary': '不吃辣\n花生过敏', 'pace': '轻松', 'note': '带老人',
    }, follow_redirects=False), local)
    p = trip.requirements
    assert p.end_date == '2027-01-02' and p.day_count == 3 and p.travelers == 4
    assert p.budget_scope == 'total' and p.dietary == ['不吃辣', '花生过敏']
    brief, _ = planning_brief(trip)
    assert brief['dates'] == ['2026-12-31', '2027-01-01', '2027-01-02']
    assert brief['daily_start']['time'] == '10:30' and brief['daily_return']['time'] == '19:00'


@pytest.mark.parametrize('extra', [
    {'day_count': '0'}, {'day_count': '1.5'}, {'day_count': '366'}, {'day_count': 'NaN'},
    {'travelers': '0'}, {'travelers': '2.5'}, {'budget_amount': 'NaN'},
    {'budget_amount': '10', 'budget_scope': 'unspecified'},
    {'start_date': '2026-02-30'}, {'outing_start_time': '25:00'},
    {'outing_start_time': '20:00', 'outing_end_time': '10:00'},
])
def test_invalid_optional_inputs_preserve_form_without_map_or_mutation(web, map_result, extra):
    client, local, _ = web
    before = deepcopy(local.trips)
    response = client.post('/trips', data={'lodging_address': '我的住处', 'note': '保留备注', **extra})
    assert response.status_code == 400 and local.trips == before and map_result == []
    assert '我的住处' in response.text and '保留备注' in response.text


def test_ambiguous_address_is_selected_from_server_candidates(web, monkeypatch):
    client, local, _ = web
    second = {**LODGING, 'poi_id': 'hotel-two', 'address': '另一个区2号'}
    monkeypatch.setattr(main, 'search_places', lambda args: {'status': 'available',
        'candidates': [LODGING, second], 'total_matched': 2})
    before = len(local.trips)
    response = client.post('/trips', data={'lodging_address': '重名酒店', 'day_count': '2'})
    assert response.status_code == 200 and len(local.trips) == before
    token = next(field['value'] for field in FormInputs(response.text).fields if field.get('name') == 'choice_token')
    response = client.post('/trips', data={'choice_token': token, 'poi_id': 'forged',
                                         'longitude': '0', 'latitude': '0'})
    assert response.status_code == 400 and len(local.trips) == before
    trip = created_trip(client.post('/trips', data={'choice_token': token, 'poi_id': 'hotel-two',
        'day_count': '99', 'city': '伪造城市', 'longitude': '0'}, follow_redirects=False), local)
    assert trip.lodging == second and trip.requirements.day_count == 2
    assert trip.requirements.city == LODGING['city']
    assert client.post('/trips', data={'choice_token': token, 'poi_id': 'hotel-two'}).status_code == 400


def test_partial_map_details_require_confirmation_even_with_one_candidate(web, monkeypatch):
    client, local, _ = web
    monkeypatch.setattr(main, 'search_places', lambda args: {'status': 'available',
        'candidates': [LODGING], 'total_matched': 8})
    before = len(local.trips)
    page = client.post('/trips', data={'lodging_address': '酒店'})
    assert 'choice_token' in page.text and len(local.trips) == before


def test_lodging_choice_is_owner_trip_bound_and_expires():
    choices = LodgingChoices(ttl=60)
    token = choices.add('alice', {'lodging_address': '原地址'}, [LODGING], 'trip-one')
    for owner, trip in [('bob', 'trip-one'), ('alice', 'trip-two')]:
        with pytest.raises(ValueError): choices.get(token, owner, trip)
    assert choices.get(token, 'alice', 'trip-one')['candidates'] == [LODGING]
    expired = LodgingChoices(ttl=-1)
    token = expired.add('alice', {}, [LODGING])
    with pytest.raises(ValueError): expired.get(token, 'alice')


def test_map_failure_does_not_create_trip_and_keeps_address(web, monkeypatch):
    client, local, _ = web
    monkeypatch.setattr(main, 'search_places', lambda args: {'status': 'unavailable', 'candidates': []})
    before = deepcopy(local.trips)
    page = client.post('/trips', data={'lodging_address': '<script>住处</script>'})
    assert page.status_code == 400 and local.trips == before
    assert '<script>住处</script>' not in page.text and '&lt;script&gt;' in page.text


def test_edit_preferences_reuses_lodging_and_stale_form_preserves_state(web, map_result):
    client, local, old = web
    trip = created_trip(client.post('/trips', data={'lodging_address': '湖滨路1号'},
                                    follow_redirects=False), local)
    calls = len(map_result)
    url = f'/workspace/{trip.id}/requirements'
    page = client.get(url)
    assert '游玩设置' in page.text and 'name="arrival_location"' not in page.text
    values = {'lodging_address': '湖滨路1号', 'revision': str(trip.requirements.revision),
              'dietary': '不吃海鲜', 'day_count': '2', 'pace': '慢一些'}
    assert client.post(url, data=values, follow_redirects=False).status_code == 303
    saved = deepcopy(local.trips[trip.id])
    assert saved.lodging == LODGING and saved.requirements.day_count == 2
    assert saved.requirements.dietary == ['不吃海鲜'] and len(map_result) == calls
    assert client.post(url, data=values).status_code == 400
    assert local.trips[trip.id] == saved
    # Existing full-travel records retain their own editor and prerequisites.
    assert 'name="arrival_location"' in client.get(f'/workspace/{old.id}/requirements').text
    incomplete = deepcopy(old)
    incomplete.requirements.budget_amount = ''
    assert '预算金额及总额/人均口径' in required_information(incomplete)


def test_changing_lodging_invalidates_coordinates_and_does_not_reuse_old_routes():
    local, trip = create_local()
    local._apply_text_plan(trip.id, '原行程')
    updated = update_requirements(trip, {'lodging_address': '新住处'})
    assert updated.lodging == {} and trip.lodging == LODGING
    brief, question = planning_brief(updated)
    assert brief is None and '位置确认' in question
    assert local.plans[trip.id].text == '原行程'


def test_lodging_and_undated_settings_survive_restart_and_chat_clear(tmp_path):
    path = tmp_path / 'city.sqlite3'
    local = DemoStore(db_path=str(path), persistence_enabled=True, display_worker_enabled=False)
    local, trip = create_local(local, day_count='2')
    local.clear_chat(trip.id)
    restored = DemoStore(db_path=str(path), persistence_enabled=True, display_worker_enabled=False)
    saved = restored.trips[trip.id]
    assert saved.mode == 'city' and saved.lodging == LODGING
    assert saved.requirements.day_count == 2 and required_information(saved) == []
    assert restored.messages[trip.id] == []


def test_city_generation_receives_lodging_and_only_real_tool_routes(monkeypatch):
    local, trip = create_local()
    source = '以湖滨为主线，日期未定。\n\n---\n第1天：住处出发，午餐与休息后返回。'
    queries = []
    def tool(name, args, **kw):
        queries.append((name, deepcopy(args)))
        if name == 'search_places': return {'status': 'available', 'candidates': [PARK]}
        return {'status': 'unavailable', 'duration_minutes': None, 'reason': '本次路线失败'}
    monkeypatch.setattr(agent_client, 'run_tool', tool)
    calls = scripted(monkeypatch, [completion(calls=[('search_places', {'city': '杭州市', 'keywords': '湖滨入口'})]),
        completion(calls=[('query_route', {'origin_poi_id': 'hotel-one', 'destination_poi_id': 'park-one', 'mode': 'transit'}),
                          ('query_route', {'origin_poi_id': 'park-one', 'destination_poi_id': 'hotel-one', 'mode': 'transit'})]),
        completion(source)])
    local.generate_plan(trip.id)
    assert local.plans[trip.id].text == source and local.plans[trip.id].display_status == 'pending'
    payload = json.loads(calls[0][0][1]['content'])
    assert payload['travel_window']['dates'] is None
    assert payload['travel_window']['lodging'] == LODGING
    assert payload['requirements']['trip']['mode'] == 'city'
    assert queries[0] == ('search_places', {'city': '杭州市', 'keywords': '湖滨入口'})
    assert len(queries) == 3 and {kw['agent_role'] for _, kw in calls} == {'planning'}


def test_undated_weather_does_not_call_external_service(monkeypatch):
    local, trip = create_local()
    runtime = TravelAgentRuntime(object(), 'fixture', {'trip': trip}, request=None)
    runtime.action = 'generate'; runtime.requirements_checked = True
    monkeypatch.setattr(agent_client, 'run_tool', lambda *a, **kw: pytest.fail('No external call'))
    output, direct = runtime.execute_tool('planning', 'query_weather', {'city': '杭州市', 'date': '2026-10-15'})
    assert output['status'] == 'unavailable' and direct is None
    assert runtime.budget.external_calls == 0


def test_undated_card_generation_uses_null_dates_and_preserves_text(monkeypatch):
    local, trip = create_local()
    source = '日期未定的一天路线。\n\n---\n第1天：住处出发并返回。'
    data = display_data((None,))
    calls = scripted(monkeypatch, [completion(source), completion(data)])
    monkeypatch.setenv('OPENAI_DISPLAY_OUTPUT_FORMAT', 'json')
    local.generate_plan(trip.id)
    local.run_pending_display_jobs()
    assert local.plans[trip.id].text == source
    assert local.plans[trip.id].display_status == 'ready'
    assert local.plans[trip.id].display['days'][0]['date'] is None
    payload = json.loads(calls[-1][0][1]['content'])
    assert payload['source_dates'] is None


def test_chat_can_record_optional_budget_without_generating(monkeypatch):
    local, trip = create_local()
    text = '预算改成人均500，节奏慢一些，先记录'
    changes = {key: asdict(trip.requirements)[key] for key in (
        'city', 'lodging_address', 'start_date', 'end_date', 'day_count', 'outing_start_time',
        'outing_end_time', 'travelers', 'budget_amount', 'budget_scope', 'budget_coverage')}
    changes.update(budget_amount='500', budget_scope='per_person', pace='慢一些')
    calls = scripted(monkeypatch, [route('planning', text, 'update_requirements'),
        completion(calls=[('update_requirements', {'user_quote': text, 'changes': changes})]),
        completion('已记录人均500元和慢一些的节奏。')])
    local.add_user_message(trip.id, text)
    saved = local.trips[trip.id]
    assert saved.requirements.budget_amount == '500' and saved.requirements.travelers is None
    assert saved.lodging == LODGING and saved.requirements.pace == '慢一些'
    assert not local.plans[trip.id].has_content and len(calls) == 3


@pytest.mark.parametrize('name,args', [
    ('search_places', {'city': '上海市', 'keywords': '景点'}),
    ('search_food', {'city': '上海', 'keywords': '午餐'}),
    ('query_route', {'origin_poi_id': 'hotel-one', 'destination_poi_id': 'outside', 'mode': 'transit'}),
])
def test_city_generation_does_not_query_other_city(monkeypatch, name, args):
    _, trip = create_local()
    runtime = TravelAgentRuntime(object(), 'fixture', {'trip': trip}, request=None)
    runtime.action = 'generate'; runtime.requirements_checked = True
    runtime.catalog['outside'] = {**PARK, 'poi_id': 'outside', 'city': '上海市'}
    monkeypatch.setattr(agent_client, 'run_tool', lambda *a, **kw: pytest.fail('Out-of-city call'))
    output, direct = runtime.execute_tool('planning', name, args)
    assert output['status'] == 'unavailable' and direct is None
    assert runtime.budget.external_calls == 0


def test_editor_replaces_lodging_only_after_new_map_confirmation(web, map_result, monkeypatch):
    client, local, _ = web
    trip = created_trip(client.post('/trips', data={'lodging_address': '湖滨路1号'}, follow_redirects=False), local)
    local._apply_text_plan(trip.id, '原计划原文')
    new_place = {**LODGING, 'poi_id': 'new-hotel', 'name': '新住宿', 'address': '南路8号'}
    monkeypatch.setattr(main, 'search_places', lambda args: {'status': 'available',
        'candidates': [new_place], 'total_matched': 1})
    response = client.post(f'/workspace/{trip.id}/requirements', data={
        'lodging_address': '南路8号', 'revision': str(trip.requirements.revision)}, follow_redirects=False)
    assert response.status_code == 303
    assert local.trips[trip.id].lodging == new_place
    assert local.trips[trip.id].requirements.lodging_address == '南路8号'
    assert local.plans[trip.id].text == '原计划原文'


def test_editing_dietary_keeps_custom_fee_coverage_from_chat(web, map_result):
    client, local, _ = web
    trip = created_trip(client.post('/trips', data={'lodging_address': '湖滨路1号'}, follow_redirects=False), local)
    saved = local.update_trip_requirements(trip.id, {'budget_coverage': '只含餐饮；交通门票已支付'}, trip.requirements.revision)
    response = client.post(f'/workspace/{trip.id}/requirements', data={
        'lodging_address': '湖滨路1号', 'revision': str(saved.requirements.revision),
        'dietary': '不吃辣'}, follow_redirects=False)
    assert response.status_code == 303
    assert local.trips[trip.id].requirements.budget_coverage == '只含餐饮；交通门票已支付'
    assert '只含餐饮' in client.get(f'/workspace/{trip.id}/requirements').text


def test_address_in_another_city_updates_default_title_without_losing_old_plan(web, map_result, monkeypatch):
    client, local, _ = web
    trip = created_trip(client.post('/trips', data={'lodging_address': '湖滨路1号'}, follow_redirects=False), local)
    local._apply_text_plan(trip.id, '旧行程原文')
    new_place = {**LODGING, 'poi_id': 'shanghai-hotel', 'city': '上海市', 'name': '新住处'}
    monkeypatch.setattr(main, 'search_places', lambda args: {'status': 'available',
        'total_matched': 1, 'candidates': [new_place]})
    response = client.post(f'/workspace/{trip.id}/requirements', data={
        'lodging_address': '上海新住处', 'revision': str(trip.requirements.revision)}, follow_redirects=False)
    assert response.status_code == 303
    assert local.trips[trip.id].destination == '上海市同城游玩'
    assert local.trips[trip.id].lodging == new_place
    assert local.plans[trip.id].text == '旧行程原文'


def test_nationwide_search_keeps_opening_reference_and_city_from_provider(monkeypatch):
    monkeypatch.setattr(places.amap_mcp, 'configured', lambda: True)
    args = []
    def fetch(values):
        args.append(values)
        return {'pois': [{'id': 'one', 'name': '住处', 'location': '120.1,30.2',
                         'city': '杭州市', 'type': '住宿服务', 'opentime2': '周一至周日全天'}]}
    monkeypatch.setattr(places, '_fetch_json', fetch)
    result = places.search_places({'keywords': '杭州某住处'})
    candidate = result['candidates'][0]
    assert candidate['city'] == '杭州市' and candidate['opening_hours'] == '周一至周日全天'
    assert candidate['place_type'] == '住宿服务' and args[0]['city'] == ''
