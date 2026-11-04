"""Offline REST detail contracts; never load project environment or use network."""
from contextlib import contextmanager
from copy import deepcopy
from threading import Event, Thread
import json
import logging

import httpx
import pytest

from app.agents.budget import AgentBudgetError, TurnBudget
from app.domain.models import PlaceDetails
from app.services.maps import amap_mcp, amap_rest
from app.services.maps.places import normalize_pois
from tests.automated.test_readonly_chat_runtime import runtime, Script, call


def envelope(**changes):
    poi = {'id': 'park', 'name': '测试公园', 'location': '120.1,30.2',
           'pname': '浙江省', 'cityname': '杭州市', 'adname': '西湖区', 'adcode': '330106',
           'tel': '0571-test', 'website': 'https://example.invalid',
           'photos': [{'url': 'https://example.invalid/1'}, {'url': 'https://example.invalid/2'}],
           'biz_ext': {'open_time': '09:00-17:00', 'rating': '4.7', 'cost': 0},
           'entr_location': '120.11,30.21', 'exit_location': [], 'navi_poiid': 'nav',
           'timestamp': '2026-10-01 12:00:00'}
    poi.update(changes)
    return {'status': '1', 'infocode': '10000', 'pois': [poi]}


@contextmanager
def scope(handler):
    with amap_rest.client_scope(client_factory=lambda **kw: httpx.Client(
            **kw, transport=httpx.MockTransport(handler))):
        yield


def test_fixed_single_request_rich_projection_and_candidate(caplog):
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=envelope(key='offline-map-key'))
    caplog.set_level(logging.INFO, logger='httpx')
    with scope(handler):
        result = amap_rest.detail('park')
    assert len(calls) == 1
    request = calls[0]
    assert str(request.url.copy_with(query=None)) == 'https://restapi.amap.com/v3/place/detail'
    assert dict(request.url.params) == {'id': 'park', 'key': 'offline-map-key', 'extensions': 'all'}
    assert request.method == 'GET'
    assert result['tel'] == '0571-test' and len(result['photos']) == 2
    assert result['biz_ext']['cost'] == 0 and result['website'] == 'https://example.invalid'
    assert result['metadata'] == {'provider': 'amap_rest', 'api_version': 'v3',
        'endpoint_kind': 'place_detail', 'fetched_at': result['metadata']['fetched_at']}
    assert result['timestamp'] == '2026-10-01 12:00:00'
    assert result['raw']['pois'][0]['key'] == '[redacted]'
    assert 'offline-map-key' not in json.dumps(result) + caplog.text
    normalized = normalize_pois(result)
    assert normalized['transport'] == 'amap_rest'
    candidate = normalized['candidates'][0]
    assert candidate['opening_hours'] == '09:00-17:00'
    assert candidate['fetched_at'] == result['metadata']['fetched_at']
    assert PlaceDetails(**candidate).source == '高德地图 POI'
    assert candidate['longitude'] == 120.1 and 'metadata' not in candidate
    assert normalized['rating'] == '4.7' and normalized['cost'] == 0


@pytest.mark.parametrize('body', [None, [], {}, {'status': '0', 'infocode': '10003'},
    {'status': '1', 'infocode': '10001', 'pois': []},
    {'status': '1', 'infocode': '10000', 'pois': []},
    {'status': '1', 'infocode': '10000', 'pois': envelope()['pois'] * 2},
    envelope(id='wrong'), envelope(name=[]), envelope(location='NaN,30'),
    envelope(location='120,91'), envelope(location=[]), envelope(location='120,30,4')])
def test_malformed_or_untrusted_results_fail_closed(body):
    with scope(lambda req: httpx.Response(200, json=body)):
        assert amap_rest.call({'id': 'park'})['status'] == 'unavailable'


@pytest.mark.parametrize('status', [301, 302, 403, 429, 500])
def test_http_failure_never_redirects_retries_or_falls_back(monkeypatch, status):
    calls = []
    monkeypatch.setattr(amap_mcp, 'call', lambda *args: pytest.fail('MCP fallback'))
    with scope(lambda req: calls.append(req) or httpx.Response(status,
            headers={'location': 'https://evil.invalid?key=offline-map-key'})):
        assert amap_rest.call({'id': 'park'})['status'] == 'unavailable'
    assert len(calls) == 1


@pytest.mark.parametrize('content', [b'not json', b'x' * 1025])
def test_nonjson_and_body_limit_are_safe(monkeypatch, content):
    monkeypatch.setattr(amap_rest, 'MAX_RESPONSE_BYTES', 1024)
    with scope(lambda req: httpx.Response(200, content=content)):
        result = amap_rest.call({'id': 'park'})
    assert result['status'] == 'unavailable' and 'offline-map-key' not in str(result)


def test_transport_errors_and_logs_are_key_safe():
    def fail(req):
        raise httpx.ConnectError(str(req.url), request=req)
    with scope(fail):
        result = amap_rest.call({'id': 'park'})
    assert result['status'] == 'unavailable' and 'offline-map-key' not in str(result)


def test_unstarted_and_nested_scope_and_reuse():
    with pytest.raises(amap_rest.AmapRESTError):
        amap_rest.detail('park')
    clients = []
    def factory(**kw):
        assert kw['follow_redirects'] is False and kw['trust_env'] is False
        client = httpx.Client(**kw, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=envelope())))
        clients.append(client)
        return client
    with amap_rest.client_scope(client_factory=factory):
        with pytest.raises(amap_rest.AmapRESTError):
            with amap_rest.client_scope(client_factory=factory):
                pass
        assert amap_rest.detail('park')['id'] == amap_rest.detail('park')['id']
        assert not clients[0].is_closed
    assert len(clients) == 1 and clients[0].is_closed


def test_close_drains_borrowers():
    entered, release, closed = Event(), Event(), Event()
    def owner():
        with scope(lambda req: httpx.Response(200, json=envelope())):
            entered.set()
            release.wait(2)
        closed.set()
    thread = Thread(target=owner)
    thread.start()
    assert entered.wait(2)
    with amap_rest.borrow_client():
        release.set()
        assert not closed.wait(.05)
    thread.join(2)
    assert closed.is_set()


def test_runtime_rest_only_single_budget_cache_evidence_and_fixed_lodging(monkeypatch):
    runner = runtime(Script('unused'))
    runner.search_ids.add('park')
    calls = []
    monkeypatch.setattr(amap_mcp, 'call', lambda *a: pytest.fail('detail called MCP'))
    with scope(lambda req: calls.append(req) or httpx.Response(200, json=envelope())):
        first = runner._query('maps_search_detail', {'id': 'park'})
        assert runner._query('maps_search_detail', {'id': 'park'}) == first
    assert len(calls) == runner.budget.external_calls == 1
    assert runner.evidence[0]['result']['raw']['pois'][0]['tel'] == '0571-test'
    assert runner.evidence[0]['normalized']['transport'] == 'amap_rest'
    with pytest.raises(ValueError, match='坐标'):
        runner._known_coordinate('120.11,30.21')
    runner.trip.lodging = deepcopy(runner.catalog['park'])
    runner.trip.lodging['longitude'] = 119
    runner._merge_places({'park': {'longitude': 121}})
    assert runner.catalog['park']['longitude'] == 119


def test_failed_runtime_detail_not_cached():
    runner = runtime(Script('unused'))
    runner.search_ids.add('park')
    calls = []
    with scope(lambda req: calls.append(req) or httpx.Response(429)):
        for _ in range(2):
            assert runner._query('maps_search_detail', {'id': 'park'})['status'] == 'unavailable'
    assert len(calls) == runner.budget.external_calls == 2 and not runner.external_results


@pytest.mark.parametrize('when', ['before', 'after'])
def test_cancellation_is_not_converted_to_unavailable(when):
    stopped = [when == 'before']
    budget = TurnBudget(cancelled=lambda: stopped[0])
    def handler(req):
        stopped[0] = True
        return httpx.Response(200, json=envelope())
    with scope(handler), amap_mcp.budget_scope(budget):
        with pytest.raises(AgentBudgetError):
            amap_rest.call({'id': 'park'})


def test_lodging_search_closes_mcp_then_attempts_five_distinct_ids(monkeypatch):
    active, calls = [False], []
    class Tools:
        def call(self, name, args):
            assert name == 'maps_text_search'
            return {'pois': [{'id': i, 'name': '摘要', 'location': '1,2'}
                for i in ['a', 'a', 'b', 'c', 'd', 'e', 'f']]}
    @contextmanager
    def session():
        active[0] = True
        yield Tools()
        active[0] = False
    def detail(poi_id, *, deadline=None):
        assert not active[0] and deadline is not None
        calls.append((poi_id, deadline))
        if poi_id in {'a', 'b'}:
            raise amap_rest.AmapRESTError('unavailable')
        return envelope(id=poi_id)['pois'][0]
    monkeypatch.setattr(amap_mcp, 'session', session)
    monkeypatch.setattr(amap_rest, 'detail', detail)
    result = amap_mcp.search_pois('杭州', '酒店')
    assert [c[0] for c in calls] == ['a', 'b', 'c', 'd', 'e']
    assert len({c[1] for c in calls}) == 1
    assert [p['id'] for p in result['pois']] == ['c', 'd', 'e']


def test_model_receives_rich_detail_not_only_hidden_raw():
    script = Script([call('maps_search_detail', {'id': 'park'})], '营业时间以现场为准。')
    runner = runtime(script)
    runner.search_ids.add('park')
    with scope(lambda req: httpx.Response(200, json=envelope())):
        assert not runner.run().failed
    output = json.loads(script.transcripts[1][-1]['content'])
    assert output['tel'] == '0571-test' and output['adname'] == '西湖区'
    assert output['biz_ext'] == envelope()['pois'][0]['biz_ext']
    assert output['photos'] == envelope()['pois'][0]['photos']
    assert output['metadata']['provider'] == 'amap_rest'


@pytest.mark.parametrize('value', ['', [], None, 0, '0'])
def test_optional_types_and_real_zero_preserved(value):
    with scope(lambda req: httpx.Response(200, json=envelope(
            website=value, biz_ext={'rating': value, 'cost': value, 'open_time': []}))):
        detail = amap_rest.detail('park')
    assert detail['website'] == value and detail['biz_ext']['cost'] == value
    normalized = normalize_pois(detail)
    assert 'opening_hours' not in normalized['candidates'][0]
    if value in (0, '0'):
        assert normalized['cost'] == value
    else:
        assert 'cost' not in normalized


def test_budget_timeout_minimum_and_exhaustion_not_swallowed(monkeypatch):
    budget = TurnBudget(max_external=1)
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=envelope())
    monkeypatch.setattr(amap_rest, 'monotonic', lambda: 100)
    with scope(handler), amap_mcp.budget_scope(budget):
        budget.external()  # The dispatch owns the first reservation.
        amap_rest.detail('park', deadline=100.25)
        with pytest.raises(AgentBudgetError):
            amap_rest.detail('park', deadline=100.25)
    assert len(calls) == 1 and budget.external_calls == 1
    assert all(v == .25 for v in calls[0].extensions['timeout'].values())


def test_lodging_shared_deadline_stops_without_filling_failures(monkeypatch):
    clock, calls = [100], []
    monkeypatch.setattr(amap_mcp, 'monotonic', lambda: clock[0])
    class Tools:
        def call(self, name, args):
            return {'pois': [{'id': str(i)} for i in range(10)]}
    @contextmanager
    def session():
        yield Tools()
    def detail(poi_id, *, deadline):
        calls.append((poi_id, deadline))
        clock[0] = deadline
        raise amap_rest.AmapRESTError('timeout')
    monkeypatch.setattr(amap_mcp, 'session', session)
    monkeypatch.setattr(amap_rest, 'detail', detail)
    assert amap_mcp.search_pois('', '酒店')['pois'] == []
    assert calls == [('0', 105)]


def test_http_body_limit_exact_boundary_and_stream_close(monkeypatch):
    body = json.dumps(envelope()).encode()
    monkeypatch.setattr(amap_rest, 'MAX_RESPONSE_BYTES', len(body))
    closed = []
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield body[:100]
            yield body[100:]
        def close(self):
            closed.append(True)
    with scope(lambda req: httpx.Response(200, stream=Stream())):
        assert amap_rest.detail('park')['id'] == 'park'
    assert closed == [True]


def test_expired_deadline_never_sends_http(monkeypatch):
    calls = []
    monkeypatch.setattr(amap_rest, 'monotonic', lambda: 100)
    with scope(lambda req: calls.append(req) or httpx.Response(200, json=envelope())):
        with pytest.raises(amap_rest.AmapRESTError, match='超时'):
            amap_rest.detail('park', deadline=99)
    assert not calls


def test_cancellation_after_http_failure_wins_over_unavailable():
    stopped = [False]
    def handler(req):
        stopped[0] = True
        return httpx.Response(429)
    with scope(handler), amap_mcp.budget_scope(TurnBudget(cancelled=lambda: stopped[0])):
        with pytest.raises(AgentBudgetError):
            amap_rest.call({'id': 'park'})


def test_lodging_details_consume_shared_budget_once_per_actual_request(monkeypatch):
    budget, calls = TurnBudget(max_external=3), []
    class Tools:
        def call(self, name, args):
            amap_mcp.consume_call()
            return {'pois': [{'id': str(i)} for i in range(5)]}
    @contextmanager
    def session():
        yield Tools()
    monkeypatch.setattr(amap_mcp, 'session', session)
    def handler(req):
        calls.append(req)
        return httpx.Response(429)
    with scope(handler), amap_mcp.budget_scope(budget):
        budget.external()
        with pytest.raises(AgentBudgetError):
            amap_mcp.search_pois('', '酒店')
    assert len(calls) == 2 and budget.external_calls == 3


def test_startup_failure_releases_scope():
    def broken(**kw):
        raise RuntimeError('synthetic startup failure')
    with pytest.raises(RuntimeError):
        with amap_rest.client_scope(client_factory=broken):
            pass
    with scope(lambda req: httpx.Response(200, json=envelope())):
        assert amap_rest.detail('park')['id'] == 'park'
