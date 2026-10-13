"""MCP protocol, adapters, credentials and actual external-call budgets."""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
import json
import logging

import httpx
import pytest
from mcp.types import CallToolResult, TextContent
from app.agent_runtime import TurnBudget, AgentBudgetError
from app.tools import amap_mcp, places, food, routes

DETAIL = {'id': 'poi-one', 'name': '地点甲', 'location': '120.1,30.2', 'address': '东路1号', 'city': '杭州市',
          'rating': '4.9', 'cost': '82.00', 'opentime2': '周一至周日 11:00-23:00'}


def tool_result(data=None, *, error=False, structured=False):
    return CallToolResult(isError=error, structuredContent=data if structured else None,
                          content=[] if structured else [TextContent(type='text', text=json.dumps(data, ensure_ascii=False))])


@pytest.fixture
def mcp_server(monkeypatch):
    """Use the actual SDK initialize/list/call flow over a mock HTTP transport."""
    monkeypatch.setenv('AMAP_MAP_KEY', 'private-test-key')
    monkeypatch.delenv('AMAP_MAPS_API_KEY', raising=False)
    requests = []
    results = {
        'maps_text_search': {'pois': [{'id':'poi-one', 'name':'地点甲', 'address':'东路1号'}]},
        'maps_search_detail': DETAIL,
        'maps_direction_walking': {'route': {'paths':[{'duration':2100, 'distance':5000}]}},
        'maps_direction_driving': {'paths':[{'duration':'2100', 'distance':'5000'}]},
        'maps_direction_transit_integrated': {'distance':'5000', 'transits':[{'duration':'2100'}]},
    }
    def respond(request):
        assert request.url.host == 'mcp.amap.com' and request.url.params['key'] == 'private-test-key'
        assert 'application/json' in request.headers['accept'] and 'text/event-stream' in request.headers['accept']
        payload=json.loads(request.content)
        requests.append(payload)
        method=payload['method']
        if method=='notifications/initialized':
            return httpx.Response(202)
        if method=='initialize':
            data={'protocolVersion':'2025-03-26','capabilities':{'tools':{}},'serverInfo':{'name':'mock-amap','version':'1.0'}}
        elif method=='tools/list':
            data={'tools':[{'name':name,'inputSchema':{'type':'object','properties':{},'additionalProperties':True}}
                           for name in results]}
        elif method=='tools/call':
            name=payload['params']['name']
            assert 'key' not in payload['params']['arguments']
            value=results[name]
            result=value if isinstance(value,CallToolResult) else tool_result(value)
            data=result.model_dump(exclude_none=True)
        else:
            raise AssertionError(method)
        return httpx.Response(200, headers={'Content-Type':'application/json'},
                              json={'jsonrpc':'2.0','id':payload['id'],'result':data})
    original=httpx.AsyncClient
    monkeypatch.setattr(amap_mcp.httpx, 'AsyncClient', lambda **kwargs:original(transport=httpx.MockTransport(respond), **kwargs))
    return requests, results


def test_sdk_negotiates_and_search_details_supply_real_coordinates(mcp_server):
    requests,_=mcp_server
    result=places.search_places({'city':'杭州','keywords':'地点甲'})
    assert result['status']=='available'
    assert result['candidates'][0]['longitude']==120.1
    assert result['transport']=='amap_mcp'
    from app.places import collect_places
    catalog=collect_places([{'role':'tool','name':'search_places','content':json.dumps(result)}])
    assert catalog['poi-one']['longitude']==120.1
    assert [r['method'] for r in requests]==['initialize','notifications/initialized','tools/list','tools/call','tools/call']
    calls=[r['params'] for r in requests if r['method']=='tools/call']
    assert calls[0]['arguments']=={'city':'杭州','keywords':'地点甲','citylimit':True}
    assert calls[1]['arguments']=={'id':'poi-one'}


@pytest.mark.parametrize('mode', ['walking','driving','transit'])
def test_mcp_route_variants_normalize_without_model_coordinates(mcp_server, mode):
    requests,_=mcp_server
    a={'poi_id':'a','name':'甲','longitude':120.1,'latitude':30.2,'city':'杭州市'}
    b={**a,'poi_id':'b','name':'乙','longitude':120.2}
    result=routes.query_route(a,b,mode)
    assert result['duration_minutes']==35 and result['distance_meters']==5000
    call=next(r['params'] for r in requests if r['method']=='tools/call')
    assert call['arguments']['origin']=='120.1,30.2'
    if mode=='transit': assert call['arguments']['cityd']=='杭州市'


def test_flat_restaurant_details_keep_price_and_opening_but_missing_phone_stays_unknown(mcp_server):
    result=food.search_food({'city':'杭州','keywords':'杭帮菜'})
    assert result['status']=='available' and result['transport']=='amap_mcp'
    item=result['recommendations'][0]
    assert item['rating']=='4.9' and item['cost_per_person_cny']==82
    assert item['open_time']=='周一至周日 11:00-23:00' and item['tel'] is None
    assert '非全城排名' in result['note']


@pytest.mark.parametrize('failure', ['mismatched_id','bad_coordinate','detail_error','malformed','missing_tool'])
def test_invalid_details_or_protocol_never_create_poi_coordinates(mcp_server, failure):
    _, results=mcp_server
    if failure=='mismatched_id':results['maps_search_detail']={**DETAIL,'id':'some-other-poi'}
    if failure=='bad_coordinate':results['maps_search_detail']={**DETAIL,'location':'NaN,30'}
    if failure=='detail_error':results['maps_search_detail']=tool_result({'error':'private-test-key'},error=True)
    if failure=='malformed':results['maps_search_detail']=CallToolResult(content=[TextContent(type='text',text='not JSON')])
    if failure=='missing_tool':del results['maps_search_detail']
    result=places.search_places({'city':'杭州','keywords':'地点甲'})
    assert result['status']=='unavailable' and result['candidates']==[]
    assert 'private-test-key' not in json.dumps(result)


def test_each_extra_mcp_detail_consumes_shared_external_budget_before_request(mcp_server):
    requests,_=mcp_server
    budget=TurnBudget(max_external=2)
    budget.external()  # The app counts the first search, details count in MCP.
    with amap_mcp.budget_scope(budget):
        result=places.search_places({'city':'杭州','keywords':'地点甲'})
    assert result['status']=='available' and budget.external_calls==2
    assert len([r for r in requests if r['method']=='tools/call'])==2


def test_exhausted_external_budget_blocks_details_without_network_or_fabrication(mcp_server):
    requests,_=mcp_server
    budget=TurnBudget(max_external=1)
    budget.external()
    with amap_mcp.budget_scope(budget), pytest.raises(AgentBudgetError):
        places.search_places({'city':'杭州','keywords':'地点甲'})
    assert len([r for r in requests if r['method']=='tools/call'])==1


def test_missing_key_returns_unavailable_without_initialization(monkeypatch):
    monkeypatch.setenv('AMAP_MAP_KEY','')
    monkeypatch.setenv('AMAP_MAPS_API_KEY','')
    monkeypatch.setattr(amap_mcp, '_connection', lambda *a,**kw:pytest.fail('must not connect'))
    assert places.search_places({'city':'杭州','keywords':'景点'})['status']=='unavailable'
    assert food.search_food({'city':'杭州'})['status']=='unavailable'


def test_mcp_error_and_credentials_never_escape_in_tool_result_or_http_log(monkeypatch, caplog):
    monkeypatch.setenv('AMAP_MAP_KEY','private-test-key')
    monkeypatch.setenv('AMAP_MAPS_API_KEY','')
    @contextmanager
    def unavailable():
        raise amap_mcp.AmapMCPError('safe error')
        yield
    monkeypatch.setattr(amap_mcp,'session',unavailable)
    assert 'private-test-key' not in json.dumps(places.search_places({'city':'杭州','keywords':'景点'}))
    amap_mcp.redact_transport_logs()
    with caplog.at_level(logging.INFO, logger='httpx'):
        logging.getLogger('httpx').info('HTTP POST https://mcp.amap.com/mcp?key=private-test-key')
        try:
            raise RuntimeError('Request https://mcp.amap.com/mcp?key=private-test-key failed')
        except RuntimeError:
            logging.getLogger('mcp.client.streamable_http').exception('Transport failed')
    assert 'private-test-key' not in caplog.text and '[redacted]' in caplog.text


def test_schema_and_tool_allowlist_fail_closed(mcp_server):
    with amap_mcp.session() as tools:
        with pytest.raises(amap_mcp.AmapMCPError):tools.call('maps_schema_take_taxi',{})
    assert not any(r.get('params',{}).get('name')=='maps_schema_take_taxi' for r in mcp_server[0])


def test_structured_result_is_supported_and_tool_error_rejected():
    assert amap_mcp.result_object(tool_result({'pois':[]},structured=True))=={'pois':[]}
    with pytest.raises(amap_mcp.AmapMCPError):amap_mcp.result_object(tool_result({'error':'bad'},error=True))


def test_discovery_failure_and_timeout_are_bounded_and_redact_original_url(monkeypatch):
    from contextlib import asynccontextmanager
    import anyio
    monkeypatch.setenv('AMAP_MAP_KEY','private-test-key')
    monkeypatch.setenv('AMAP_MAPS_API_KEY','')
    monkeypatch.setenv('AMAP_MCP_TIMEOUT_SECONDS','0.02')
    @asynccontextmanager
    async def blocked(*args):
        await anyio.sleep(1)
        yield
    monkeypatch.setattr(amap_mcp,'_connection',blocked)
    from time import monotonic
    started=monotonic()
    result=places.search_places({'city':'杭州','keywords':'景点'})
    assert monotonic()-started < 1
    assert result['status']=='unavailable' and 'private-test-key' not in json.dumps(result)


def test_synchronous_tool_can_be_called_from_an_existing_async_loop(mcp_server):
    import asyncio
    async def invoke():
        return places.search_places({'city':'杭州','keywords':'景点'})
    assert asyncio.run(invoke())['status']=='available'


def test_missing_scores_and_nonfinite_prices_never_become_verified_food_quotes(mcp_server):
    _,results=mcp_server
    results['maps_search_detail']={**DETAIL,'cost':'NaN'}
    result=food.search_food({'city':'杭州'})
    assert result['recommendations'][0]['cost_per_person_cny'] is None
    results['maps_search_detail']={**DETAIL,'rating':''}
    assert food.search_food({'city':'杭州'})['status']=='unavailable'


def test_search_has_fixed_maximum_details_and_duplicates_do_not_trigger_extra_calls(mcp_server):
    requests,results=mcp_server
    results['maps_text_search']={'pois':[{'id':'poi-one','name':'甲'}]*30}
    result=amap_mcp.search_pois('杭州','甲',999)
    assert len(result['pois'])==1
    assert len([r for r in requests if r['method']=='tools/call'])==2
