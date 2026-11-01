"""Offline contracts for the shared MCP transport and map-result parsing."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
import logging
import sys
from time import monotonic
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from app.services.maps import amap_mcp as agent_provider, places as agent_places
from app.services.maps import amap_mcp as web_provider, places as web_places


PROVIDERS = [agent_provider]
TRANSPORT_LOGGERS = ('httpx', 'httpcore', 'mcp.client.streamable_http',
                     'mcp.shared.session', 'mcp.client.session')


def poi(poi_id='park', **values):
    return {'id': poi_id, 'name': '测试公园', 'location': '120.16,30.25', **values}


def result(value):
    return SimpleNamespace(isError=False, structuredContent=value, content=[])


@pytest.fixture(autouse=True)
def no_connections(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('offline map contract attempted a real connection')
    for provider in PROVIDERS:
        monkeypatch.setattr(provider, 'streamable_http_client', forbidden)


def test_page_and_agent_share_one_provider_and_poi_parser(monkeypatch):
    from app.api import trips
    from app.agents.tools import registry
    assert agent_provider is web_provider
    assert agent_places is web_places
    assert trips.search_places is web_places.search_places
    assert registry.amap_mcp is agent_provider
    assert agent_provider.ALLOWED_TOOLS == {'maps_text_search', 'maps_search_detail',
        'maps_direction_walking', 'maps_direction_driving', 'maps_direction_transit_integrated'}
    assert hasattr(agent_provider, 'budget_scope')
    monkeypatch.setattr(agent_provider, 'search_pois', lambda *args: {'pois': [poi('shared')]})
    assert web_places.search_places({'keywords': '公园'})['candidates'][0]['poi_id'] == 'shared'


@pytest.mark.parametrize('arguments', [{}, {'keywords': ''}, {'keywords': '   '},
    {'keywords': None}, {'keywords': []}, {'keywords': 'x' * 301},
    {'city': 'x' * 101, 'keywords': '公园'}])
def test_web_input_rejected_before_provider(monkeypatch, arguments):
    def forbidden(*args):
        pytest.fail('invalid input reached provider')
    monkeypatch.setattr(web_provider, 'search_pois', forbidden)
    response = web_places.search_places(arguments)
    assert response['status'] == 'unavailable'
    assert response['candidates'] == []


def test_web_place_output_normalizes_metadata_and_preserves_match_count(monkeypatch):
    calls = []
    def search(city, keywords):
        calls.append((city, keywords))
        return {'total_matched': 27, 'pois': [poi(name=' 测试公园 ', address=' 测试地址 ',
            cityname=' 杭州市 ', adname=' 上城区 ', type=' 风景名胜 ',
            opentime2=' 09:00-17:00 ', open_time='不应覆盖优先值', key='never-expose'),
            poi('other', city='', type=[], address=None, open_time=' 全天 ')]}
    monkeypatch.setattr(web_provider, 'search_pois', search)
    response = web_places.search_places({'city': ' 杭州 ', 'keywords': ' 公园 '})
    assert calls == [('杭州', '公园')]
    assert response['status'] == 'available'
    assert response['city'] == '杭州' and response['keywords'] == '公园'
    assert response['total_matched'] == 27
    assert response['source'] == '高德地图 POI' and response['transport'] == 'amap_mcp'
    first, second = response['candidates']
    assert first['name'] == '测试公园' and first['address'] == '测试地址'
    assert (first['longitude'], first['latitude']) == (120.16, 30.25)
    assert first['city'] == '杭州市' and first['district'] == '上城区'
    assert first['place_type'] == '风景名胜' and first['opening_hours'] == '09:00-17:00'
    assert datetime.fromisoformat(first['fetched_at']).utcoffset().total_seconds() == 0
    assert first['fetched_at'] == second['fetched_at']
    assert second['city'] == '杭州' and second['address'] == ''
    assert second['opening_hours'] == '全天' and 'place_type' not in second
    assert 'key' not in first


@pytest.mark.parametrize('location', ['NaN,30', '120,inf', '-181,30', '120,91',
    '120', '120,30,40', 'invalid,30', '', None, ['120', '30']])
def test_web_rejects_invalid_coordinates_without_inventing_places(monkeypatch, location):
    monkeypatch.setattr(web_provider, 'search_pois', lambda *args: {'pois': [poi(location=location)]})
    response = web_places.search_places({'keywords': '公园'})
    assert response['status'] == 'unavailable' and response['candidates'] == []


def test_web_deduplicates_valid_ids_and_caps_visible_candidates(monkeypatch):
    records = [None, {}, poi('', name='无标识'), poi('bad', name=''),
               poi('duplicate', location='invalid'), poi('duplicate'), poi('duplicate')]
    records += [poi(str(i), location='180,-90') for i in range(12)]
    monkeypatch.setattr(web_provider, 'search_pois', lambda *args: {'pois': records})
    response = web_places.search_places({'keywords': '公园'})
    assert len(response['candidates']) == 10
    assert response['total_matched'] == 13  # Valid unique matches, before the UI cap.
    assert response['candidates'][0]['poi_id'] == 'duplicate'
    assert response['candidates'][1]['longitude'] == 180
    assert response['candidates'][1]['latitude'] == -90


@pytest.mark.parametrize('payload', [None, [], {}, {'status': '0'}, {'infocode': '10001'},
    {'pois': None}, {'pois': 'invalid'}])
def test_web_provider_unavailable_shapes_fail_closed(monkeypatch, payload):
    monkeypatch.setattr(web_provider, 'search_pois', lambda *args: payload)
    response = web_places.search_places({'keywords': '公园'})
    assert response['status'] == 'unavailable' and response['candidates'] == []


def test_web_provider_errors_and_missing_key_are_safe(monkeypatch):
    def failed(*args):
        raise web_provider.AmapMCPError('secret-provider-diagnostic')
    monkeypatch.setattr(web_provider, 'search_pois', failed)
    response = web_places.search_places({'keywords': '公园'})
    assert response['status'] == 'unavailable'
    assert 'secret-provider-diagnostic' not in str(response)
    monkeypatch.setattr(web_provider, 'configured', lambda: False)
    monkeypatch.setattr(web_provider, 'search_pois', lambda *args: pytest.fail('unconfigured query'))
    assert web_places.search_places({'keywords': '公园'})['status'] == 'unavailable'


def test_web_map_link_encodes_user_text_and_query_time_handles_timezone():
    url = web_places.map_url(SimpleNamespace(longitude=120.16, latitude=30.25, name='湖 & #? 公园'))
    parsed = urlsplit(url)
    assert parsed.scheme == 'https' and parsed.netloc == 'uri.amap.com'
    assert parse_qs(parsed.query) == {'position': ['120.16,30.25'], 'name': ['湖 & #? 公园'],
        'src': ['TravelAssistant'], 'coordinate': ['gaode'], 'callnative': ['0']}
    assert not parsed.fragment
    assert web_places.query_time_label('2030-01-01T20:30:00+00:00') == '2030年1月2日 04:30（北京时间）'
    for value in (None, '', 'invalid', '2030-01-01T12:00:00'):
        assert web_places.query_time_label(value) == '时间待核实'


@pytest.mark.parametrize('provider', PROVIDERS, ids=['shared'])
def test_provider_credentials_are_dynamic_and_replace_url_keys(monkeypatch, provider):
    monkeypatch.setenv('AMAP_MAPS_API_KEY', 'primary-offline-secret')
    monkeypatch.setenv('AMAP_MAP_KEY', 'alias-offline-secret')
    monkeypatch.setenv('AMAP_MCP_URL', 'https://maps.offline.invalid/mcp?Key=old&key=older&region=cn')
    assert provider.api_key() == 'primary-offline-secret'
    query = parse_qs(urlsplit(provider.endpoint()).query)
    assert query == {'region': ['cn'], 'key': ['primary-offline-secret']}
    monkeypatch.setenv('AMAP_MAPS_API_KEY', '')
    assert provider.api_key() == 'alias-offline-secret'
    monkeypatch.setenv('AMAP_MAP_KEY', '')
    assert not provider.configured()
    with pytest.raises(provider.AmapMCPError):
        with provider.session():
            pytest.fail('session accepted missing key')


@pytest.mark.parametrize('provider', PROVIDERS, ids=['shared'])
@pytest.mark.parametrize('url', ['http://maps.offline.invalid/mcp',
    'https://user:password@maps.offline.invalid/mcp', 'https://maps.offline.invalid/mcp#fragment'])
def test_provider_rejects_unsafe_endpoints(monkeypatch, provider, url):
    monkeypatch.setenv('AMAP_MCP_URL', url)
    with pytest.raises(provider.AmapMCPError) as caught:
        provider.endpoint()
    assert 'password' not in str(caught.value)


@pytest.mark.parametrize('provider', PROVIDERS, ids=['shared'])
@pytest.mark.parametrize('seconds', ['0', '-1', 'NaN', 'inf', '121', 'invalid'])
def test_provider_rejects_invalid_timeouts(monkeypatch, provider, seconds):
    monkeypatch.setenv('AMAP_MCP_TIMEOUT_SECONDS', seconds)
    with pytest.raises(provider.AmapMCPError):
        provider.timeout_seconds()


class OfflinePortal:
    def call(self, function):
        return asyncio.run(function())


@pytest.mark.parametrize('provider', PROVIDERS, ids=['shared'])
def test_provider_schema_allowlist_deadline_and_six_call_limit(monkeypatch, provider):
    calls, timeout_windows = [], []
    class Client:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return result({'pois': []})
    @contextmanager
    def timeout(seconds):
        timeout_windows.append(seconds)
        yield
    monkeypatch.setattr(provider.anyio, 'fail_after', timeout)
    schema = {'type': 'object', 'properties': {'keywords': {'type': 'string'}},
              'required': ['keywords'], 'additionalProperties': False}
    tools = provider.MCPTools(OfflinePortal(), Client(), {'maps_text_search': schema}, monotonic() + 2)
    for name, args in [('not_allowed', {}), ('maps_search_detail', {}), ('maps_text_search', {'keywords': 3})]:
        with pytest.raises(provider.AmapMCPError):
            tools.call(name, args)
    assert not calls and tools.calls == 0
    for _ in range(6):
        assert tools.call('maps_text_search', {'keywords': '公园'}) == {'pois': []}
    with pytest.raises(provider.AmapMCPError):
        tools.call('maps_text_search', {'keywords': '公园'})
    assert len(calls) == 6 and tools.calls == 6
    assert all(0 < seconds <= 2 for seconds in timeout_windows)
    tools = provider.MCPTools(OfflinePortal(), Client(), {'maps_text_search': schema}, monotonic() - 1)
    with pytest.raises(provider.AmapMCPError):
        tools.call('maps_text_search', {'keywords': '公园'})
    assert tools.calls == 0 and len(calls) == 6


@pytest.mark.parametrize('provider', PROVIDERS, ids=['shared'])
@pytest.mark.parametrize('endless', [False, True])
def test_provider_connection_discovers_only_allowed_schemas_with_bounded_pagination(monkeypatch, provider, endless):
    events, pages, options = [], [], []
    class HTTP:
        def __init__(self, **kwargs):
            options.append(kwargs)
        async def __aenter__(self):
            events.append('http-open')
            return self
        async def __aexit__(self, *args):
            events.append('http-close')
    @asynccontextmanager
    async def transport(url, *, http_client):
        assert url == 'https://maps.offline.invalid/mcp?key=offline'
        events.append('transport-open')
        try:
            yield 'read', 'write', None
        finally:
            events.append('transport-close')
    class Client:
        def __init__(self, read, write, **kwargs):
            assert (read, write) == ('read', 'write')
            assert kwargs['read_timeout_seconds'].total_seconds() == 3
        async def __aenter__(self):
            events.append('client-open')
            return self
        async def __aexit__(self, *args):
            events.append('client-close')
        async def initialize(self):
            events.append('initialize')
        async def list_tools(self, *, cursor):
            pages.append(cursor)
            return SimpleNamespace(tools=[
                SimpleNamespace(name='maps_text_search', inputSchema={'type': 'object'}),
                SimpleNamespace(name='destructive_unknown_tool', inputSchema={})],
                nextCursor='more' if endless or len(pages) == 1 else None)
    monkeypatch.setattr(provider.httpx, 'AsyncClient', HTTP)
    monkeypatch.setattr(provider, 'streamable_http_client', transport)
    monkeypatch.setattr(provider, 'ClientSession', Client)
    async def exercise():
        async with provider._connection('https://maps.offline.invalid/mcp?key=offline', 3) as (_, specs):
            assert specs == {'maps_text_search': {'type': 'object'}}
            events.append('yield')
    if endless:
        with pytest.raises(provider.AmapMCPError):
            asyncio.run(exercise())
        assert len(pages) == 8
        assert 'yield' not in events
    else:
        asyncio.run(exercise())
        assert pages == [None, 'more']
    assert events[:4] == ['http-open', 'transport-open', 'client-open', 'initialize']
    assert events[-3:] == ['client-close', 'transport-close', 'http-close']
    assert options[0]['follow_redirects'] is False
    assert options[0]['timeout'].connect == 3


@pytest.mark.parametrize('provider', PROVIDERS, ids=['shared'])
def test_provider_session_applies_shared_connection_deadline_and_closes(monkeypatch, provider):
    windows, events = [], []
    @contextmanager
    def timeout(seconds):
        windows.append(seconds)
        yield
    @asynccontextmanager
    async def connection(url, seconds):
        events.append('open')
        try:
            yield object(), {'maps_text_search': {}}
        finally:
            events.append('close')
    monkeypatch.setattr(provider.anyio, 'fail_after', timeout)
    monkeypatch.setattr(provider, '_connection', connection)
    before = monotonic()
    with provider.session() as tools:
        assert before + 5 <= tools.deadline <= monotonic() + 5
        assert events == ['open'] and windows == [5]
    assert events == ['open', 'close']
    assert provider._phase_context.get() is None


@pytest.mark.parametrize('provider', PROVIDERS, ids=['shared'])
def test_provider_detail_identity_authority_and_bounds(monkeypatch, provider):
    calls = []
    class Tools:
        def call(self, name, args):
            calls.append((name, args))
            if name == 'maps_text_search':
                return {'pois': [None, poi('a'), poi('a'), poi('b'), poi('c'), poi('d'), poi('e'), poi('f')]}
            if args['id'] == 'a':
                return {'id': 'wrong', 'location': '1,2'}
            if args['id'] == 'b':
                raise provider.AmapMCPError('detail unavailable')
            return {'id': args['id'], 'name': '详情名称', 'location': '121,31'}
    @contextmanager
    def session():
        yield Tools()
    monkeypatch.setattr(provider, 'session', session)
    response = provider.search_pois('', '公园', limit=100)
    assert calls[0] == ('maps_text_search', {'keywords': '公园', 'citylimit': False})
    assert [args['id'] for name, args in calls[1:]] == ['a', 'b', 'c', 'd', 'e']
    assert len(calls) == 6 and response['total_matched'] == 8
    assert [p['id'] for p in response['pois']] == ['c', 'd', 'e']
    assert all(p['name'] == '详情名称' and p['location'] == '121,31' for p in response['pois'])
    calls.clear()
    provider.search_pois('杭州', '公园', limit=0)
    assert calls[0][1] == {'keywords': '公园', 'citylimit': True, 'city': '杭州'}
    assert len(calls) == 2


@pytest.mark.parametrize('provider', PROVIDERS, ids=['shared'])
def test_provider_redacts_messages_arguments_and_tracebacks(monkeypatch, provider):
    monkeypatch.setenv('AMAP_MAPS_API_KEY', 'primary-private-value')
    monkeypatch.setenv('AMAP_MAP_KEY', 'alias-private-value')
    try:
        raise RuntimeError('https://maps.offline.invalid/mcp?key=query-private-value primary-private-value')
    except RuntimeError:
        record = logging.LogRecord('httpx', logging.ERROR, __file__, 1,
            'request %s alias-private-value', ('primary-private-value',), sys.exc_info())
    assert provider.RedactMCPLogs().filter(record)
    rendered = logging.Formatter().format(record)
    for secret in ('primary-private-value', 'alias-private-value', 'query-private-value'):
        assert secret not in rendered
    assert '[redacted]' in rendered and record.args == () and record.exc_info is None


@pytest.mark.parametrize('first,second', [(agent_provider, web_provider), (web_provider, agent_provider)])
def test_redactor_stable_marker_prevents_duplicate_filters_across_modules_and_reload(monkeypatch, first, second):
    loggers = [logging.getLogger(name) for name in TRANSPORT_LOGGERS]
    for logger in loggers:
        monkeypatch.setattr(logger, 'filters', [])
    first.redact_transport_logs()
    initial = [logger.filters[0] for logger in loggers]
    second.redact_transport_logs()
    # A recreated class (as on module reload) has a different identity. The marker,
    # rather than isinstance(), must remain the duplicate-detection contract.
    replacement_type = type('ReloadedRedactor', (logging.Filter,), {'_travel_amap_redactor': True})
    monkeypatch.setattr(second, '_redactor', replacement_type())
    second.redact_transport_logs()
    first.redact_transport_logs()
    for logger, original in zip(loggers, initial):
        assert logger.filters == [original]
        assert getattr(original, '_travel_amap_redactor', False)
