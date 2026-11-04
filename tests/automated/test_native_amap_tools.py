"""Native AMap contracts: real tool schemas, raw results and trusted business evidence."""
from copy import deepcopy

import pytest

from app.agents.tools import registry
from app.agents.tools.weather import query_weather, WEATHER_TOOL_SPEC
from app.services.maps import amap_mcp, amap_rest
from app.domain.changes import prepare_changes
from app.application.contracts import ExecutionResult
from tests.automated.test_readonly_chat_runtime import runtime, Script


@pytest.fixture(autouse=True)
def no_real_transport(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Offline native-tool test attempted a live MCP connection')
    monkeypatch.setattr(amap_mcp, 'streamable_http_client', forbidden)


def test_exact_native_schema_and_independent_weather_are_registered():
    tools = registry.agent_tool_registry(read_context=lambda args: {}, query=lambda *args: {},
                                         save_trip_changes=lambda args: {})
    assert set(tools) == amap_mcp.ALLOWED_TOOLS | {'query_weather', 'read_context', 'read_skill', 'save_trip_changes'}
    for native in registry._NATIVE:
        exposed = tools[native['name']].model_spec()['function']
        assert exposed == native
    assert tools['query_weather'].parameters == WEATHER_TOOL_SPEC['function']['parameters']
    assert not {'search_places', 'search_food', 'query_route', 'maps_weather'} & tools.keys()
    assert registry.query_weather is query_weather


def test_native_search_does_not_silently_fetch_every_detail(monkeypatch):
    calls = []
    raw = {'pois': [{'id': 'hotel', 'name': '测试酒店'}, {'id': 'other', 'name': '另一家酒店'}]}
    monkeypatch.setattr(amap_mcp, 'call', lambda name, args: calls.append((name, args)) or deepcopy(raw))
    runner = runtime(Script('unused'))
    result = runner._query('maps_text_search', {'keywords': '酒店', 'city': '杭州'})
    assert result == raw and len(calls) == 1
    assert not runner.catalog
    assert runner.search_ids == {'hotel', 'other'}
    assert runner.evidence[0]['result'] == raw
    assert runner.evidence[0]['normalized']['candidates'] == []


@pytest.mark.parametrize('name,args', [
    ('maps_search_detail', {'id': 'invented'}),
    ('maps_direction_walking', {'origin': '120,30', 'destination': '121,31'}),
    ('maps_direction_driving', {'origin': 'NaN,30', 'destination': '121,31'}),
])
def test_unknown_pois_and_coordinates_cannot_reach_provider(monkeypatch, name, args):
    calls = []
    monkeypatch.setattr(amap_mcp, 'call', lambda *args: calls.append(args))
    with pytest.raises(ValueError):
        runtime(Script('unused'))._query(name, args)
    assert not calls


def test_returned_detail_must_match_the_requested_id(monkeypatch):
    runner = runtime(Script('unused'))
    runner.search_ids.add('hotel')
    monkeypatch.setattr(amap_rest, 'call', lambda *args: {'id': 'another', 'name': '错配酒店',
                                                    'location': '120,30', 'city': '杭州'})
    with pytest.raises(ValueError, match='ID'):
        runner._query('maps_search_detail', {'id': 'hotel'})
    assert not runner.catalog and not runner.evidence


def test_native_detail_evidence_can_save_lodging_without_model_coordinates(monkeypatch):
    from app.storage.store import DemoStore
    from app.agents.inputs.execution import TaskExecutionContext
    from app.agents.travel_runtime import TaskExecutionRuntime
    store = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    key = 'trip-hangzhou'
    from tests.automated.task_fixtures import running_context
    context = running_context(store, key, '我住测试酒店西湖店')
    trip = deepcopy(store.trips[key])
    execution = TaskExecutionContext(trip, [],
                                     deepcopy(store.plans[key]), {}, '2030-01-01')
    runner = TaskExecutionRuntime(None, 'offline', context, execution, Script('unused'))
    raw = {'id': 'hotel-test', 'name': '测试酒店西湖店', 'address': '测试路1号',
           'city': '杭州', 'location': '120.1,30.2', 'rating': '4.6', 'cost': '未知'}
    def call(name, args):
        return {'pois': [{'id': raw['id'], 'name': raw['name']}]} if name == 'maps_text_search' else deepcopy(raw)
    monkeypatch.setattr(amap_mcp, 'call', call)
    monkeypatch.setattr(amap_rest, 'call', lambda args: deepcopy(raw))
    runner._query('maps_text_search', {'keywords': '测试酒店西湖店', 'city': '杭州'})
    detail = runner._query('maps_search_detail', {'id': 'hotel-test'})
    assert {k: v for k, v in detail.items() if k != 'navigation_endpoints'} == raw
    assert detail['navigation_endpoints'][0]['kind'] == 'center'
    assert runner.catalog['hotel-test']['longitude'] == 120.1
    before = deepcopy(store.trips[key].lodging)
    with pytest.raises(ValueError):
        prepare_changes({'user_quote': context.body, 'lodging': {'poi_id': 'hotel-test'}},
                        context, execution, runner.evidence)
    assert store.trips[key].lodging == before


def test_wrong_transit_city_cannot_reach_provider(monkeypatch):
    runner = runtime(Script('unused'))
    runner.catalog = {key: dict(poi_id=key, name=key, longitude=i, latitude=1, city='杭州')
                      for i, key in enumerate(['a', 'b'])}
    with pytest.raises(ValueError, match='城市'):
        runner._query('maps_direction_transit_integrated',
                      {'origin': '0,1', 'destination': '1,1', 'city': '北京', 'cityd': '杭州'})
    assert runner.budget.external_calls == 0


def test_failed_query_stays_unknown_and_is_not_cached(monkeypatch):
    runner = runtime(Script('unused'))
    calls = []
    monkeypatch.setattr(amap_mcp, 'call', lambda *args: calls.append(args) or
                        {'status': 'unavailable', 'reason': '地图服务暂不可用'})
    args = {'keywords': '酒店'}
    for _ in range(2):
        result = runner._query('maps_text_search', args)
        assert result['status'] == 'unavailable'
    assert len(calls) == 2 and not runner.catalog and not runner.external_results
