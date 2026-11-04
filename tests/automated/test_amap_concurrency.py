"""Offline real-thread checks for the process-wide MCP/REST in-flight limit."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, asynccontextmanager, contextmanager
from threading import Barrier, Event, Lock
from time import monotonic, sleep

import asyncio
import json
import anyio
import httpx
import pytest

from app.agents.budget import AgentBudgetError, TurnBudget
from app.agents.tools import weather
from app.services.maps import amap_mcp, amap_rest


@pytest.mark.parametrize('persistent_get', [False, True])
def test_real_sdk_session_id_does_not_double_http_inflight(monkeypatch, persistent_get):
    lock, release, three = Lock(), Event(), Event()
    counts = {'active': 0, 'peak': 0}
    methods = []
    original = httpx.AsyncClient

    class SSE(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b': heartbeat\n\n'
                await asyncio.sleep(.01)

        async def aclose(self):
            with lock:
                counts['active'] -= 1

    async def handler(req):
        body = json.loads(req.content) if req.method == 'POST' else {}
        method = body.get('method', req.method)
        if method in ('GET', 'tools/list'):
            with lock:
                counts['active'] += 1
                counts['peak'] = max(counts['peak'], counts['active'])
                methods.append(method)
                if counts['active'] >= 3:
                    three.set()
            if method == 'GET' and persistent_get:
                return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=SSE())
            try:
                while not release.is_set():
                    await asyncio.sleep(.01)
            finally:
                with lock:
                    counts['active'] -= 1
        if req.method == 'GET':
            return httpx.Response(405)
        if 'id' not in body:
            return httpx.Response(202)
        result = ({'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}},
                   'serverInfo': {'name': 'offline', 'version': '1'}}
                  if method == 'initialize' else {'tools': []})
        return httpx.Response(200, headers={'content-type': 'application/json',
            'mcp-session-id': 'offline'}, json={'jsonrpc': '2.0', 'id': body['id'], 'result': result})

    def factory(**kwargs):
        transport = kwargs.pop('transport', None)
        if transport is None:
            transport = httpx.MockTransport(handler)
        else:
            transport.inner = httpx.MockTransport(handler)
        return original(transport=transport, **kwargs)

    monkeypatch.setattr(amap_mcp.httpx, 'AsyncClient', factory)

    def run():
        with amap_mcp.session():
            pass

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run) for _ in range(3)]
        try:
            assert three.wait(2)
            sleep(.15)
            assert counts['peak'] == 3, methods
        finally:
            release.set()
        for future in futures:
            future.result(3)
    assert counts['active'] == 0
    # No permit leaks, including the GET that waited outside the common pool.
    with ExitStack() as stack:
        for _ in range(3):
            stack.enter_context(amap_mcp.request_slot(deadline=monotonic() + 1))
    assert amap_mcp._get_stream_slots.acquire(blocking=False)
    assert amap_mcp._get_stream_slots.acquire(blocking=False)
    amap_mcp._get_stream_slots.release()
    amap_mcp._get_stream_slots.release()


def test_async_stream_cancel_during_close_holds_slot_until_actual_close():
    async def check():
        started, release, finished = anyio.Event(), anyio.Event(), anyio.Event()
        scopes = []

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'body'

            async def aclose(self):
                started.set()
                await release.wait()
                finished.set()

        async def handler(req):
            return httpx.Response(200, stream=Body())

        transport = amap_mcp._LimitedTransport(3, inner=httpx.MockTransport(handler))
        response = await transport.handle_async_request(httpx.Request('POST', 'https://offline.invalid'))

        async def close():
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                await response.aclose()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(close)
            await started.wait()
            scopes[0].cancel()
            await anyio.sleep(.02)
            assert not finished.is_set()
            assert amap_mcp._request_slots.acquire(blocking=False)
            assert amap_mcp._request_slots.acquire(blocking=False)
            try:
                assert not amap_mcp._request_slots.acquire(blocking=False)
            finally:
                amap_mcp._request_slots.release()
                amap_mcp._request_slots.release()
                release.set()
        assert finished.is_set()
        await response.aclose()  # Idempotent: no double semaphore release.

    anyio.run(check)


class Network:
    def __init__(self):
        self.lock = Lock()
        self.active = self.peak = self.entered = 0
        self.three = Event()
        self.release = Event()
        self.closing = Event()
        self.close_release = Event()
        self.close_release.set()
        self.timeouts = []
        self.fail_mcp = self.fail_rest = False

    @contextmanager
    def connection(self):
        with self.lock:
            self.active += 1
            self.entered += 1
            self.peak = max(self.peak, self.active)
            if self.active >= 3:
                self.three.set()
        try:
            yield
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def network(monkeypatch):
    network = Network()

    # Each fake MCP session owns one actual async transport stream. SDK fan-out
    # itself is covered separately with the real ClientSession above.
    @asynccontextmanager
    async def connection(url, seconds):
        class Inner(httpx.AsyncBaseTransport):
            async def handle_async_request(self, req):
                network.timeouts.append(('mcp', req.extensions['timeout']['read']))
                lease = network.connection()
                lease.__enter__()
                if network.fail_mcp:
                    lease.__exit__(None, None, None)
                    raise RuntimeError('fake connection failure')

                class Body(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        yield b'{}'

                    async def aclose(self):
                        network.closing.set()
                        while not network.close_release.is_set():
                            await asyncio.sleep(.01)
                        lease.__exit__(None, None, None)

                return httpx.Response(200, stream=Body())

        transport = amap_mcp._LimitedTransport(seconds, inner=Inner())
        response = await transport.handle_async_request(httpx.Request('POST', url,
            extensions={'timeout': {'read': seconds}}))
        try:
            yield object(), {}
        finally:
            await response.aclose()

    class Client:
        @contextmanager
        def stream(self, method, url, **kwargs):
            network.timeouts.append(('rest', kwargs['timeout'].read))
            with network.connection():
                if network.fail_rest:
                    raise httpx.ReadError('fake stream failure')
                assert network.release.wait(3)
                try:
                    yield httpx.Response(200, json={'status': '1', 'infocode': '10000'})
                finally:
                    network.closing.set()
                    assert network.close_release.wait(3)

        def close(self):
            pass

    monkeypatch.setattr(amap_mcp, '_connection', connection)
    with amap_rest.client_scope(client_factory=lambda **kw: Client()):
        yield network
    assert network.active == 0


def request(kind, network, *, budget=None, deadline=None, barrier=None):
    if barrier:
        barrier.wait(3)
    with amap_mcp.budget_scope(budget):
        if kind == 'mcp':
            with amap_mcp.session():
                assert network.release.wait(3)
            return 'ok'
        assert amap_rest._get(amap_rest.ENDPOINT, {}, deadline=deadline)['status'] == '1'
        return 'ok'


def test_mixed_requests_share_three_slots_and_excess_queue_to_success(network):
    barrier = Barrier(9)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(request, kind, network, barrier=barrier)
                   for kind in ['mcp', 'rest'] * 4]
        barrier.wait(3)
        try:
            assert network.three.wait(2), 'must allow at least three real connections'
            sleep(.15)
            assert network.peak == network.active == 3
            assert network.entered == 3, 'queued requests must not open network resources'
        finally:
            network.release.set()
        assert [future.result(3) for future in futures] == ['ok'] * 8
    assert network.peak == 3 and network.entered == 8


@pytest.mark.parametrize('kind', ['mcp', 'rest'])
def test_failure_releases_slot_and_does_not_reduce_capacity(network, kind):
    setattr(network, 'fail_' + kind, True)
    error = amap_mcp.AmapMCPError if kind == 'mcp' else amap_rest.AmapRESTError
    for _ in range(4):
        with pytest.raises(error):
            request(kind, network)
    setattr(network, 'fail_' + kind, False)
    network.entered = network.peak = 0
    test_mixed_requests_share_three_slots_and_excess_queue_to_success(network)


@contextmanager
def occupy_mcp_slots():
    with ExitStack() as stack:
        for _ in range(3):
            stack.enter_context(amap_mcp.session())
        yield


@pytest.mark.parametrize('kind', ['mcp', 'rest'])
@pytest.mark.parametrize('stop', ['cancel', 'budget_timeout', 'provider_timeout'])
def test_waiter_checks_cancellation_and_deadlines_without_network(network, monkeypatch, kind, stop):
    checked, cancelled = Event(), Event()

    class Budget(TurnBudget):
        def remaining(self):
            checked.set()
            return super().remaining()

    budget = Budget(seconds=.15 if stop == 'budget_timeout' else 5, cancelled=cancelled.is_set)
    with occupy_mcp_slots(), ThreadPoolExecutor(max_workers=1) as pool:
        if stop == 'provider_timeout':
            monkeypatch.setattr(amap_mcp, 'amap_timeout', lambda: .15)
            monkeypatch.setattr(amap_rest.config, 'amap_rest_timeout', lambda: .15)
        started = monotonic()
        future = pool.submit(request, kind, network, budget=budget)
        try:
            assert checked.wait(1)
            if stop == 'cancel':
                cancelled.set()
            error = (AgentBudgetError if stop != 'provider_timeout' else
                     amap_mcp.AmapMCPError if kind == 'mcp' else amap_rest.AmapRESTError)
            with pytest.raises(error):
                future.result(1)
            assert monotonic() - started < 1
            assert network.entered == 3
            assert budget.external_calls == 0
        finally:
            network.release.set()


@pytest.mark.parametrize('kind', ['mcp', 'rest'])
def test_wait_time_is_subtracted_from_network_timeout(network, monkeypatch, kind):
    monkeypatch.setattr(amap_mcp, 'amap_timeout', lambda: 1.)
    monkeypatch.setattr(amap_rest.config, 'amap_rest_timeout', lambda: 1.)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with occupy_mcp_slots():
            checked = Event()

            class Budget(TurnBudget):
                def remaining(self):
                    checked.set()
                    return super().remaining()

            future = pool.submit(request, kind, network, budget=Budget(seconds=5))
            assert checked.wait(1)
            sleep(.15)
            assert network.entered == 3
        network.release.set()
        assert future.result(2) == 'ok'
    assert 0 < network.timeouts[-1][1] < .95


def test_explicit_rest_deadline_expires_in_queue(network):
    with occupy_mcp_slots(), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(request, 'rest', network, deadline=monotonic() + .1)
        try:
            with pytest.raises(amap_rest.AmapRESTError):
                future.result(1)
            assert network.entered == 3
        finally:
            network.release.set()


def test_body_and_tool_errors_release_slots(network, monkeypatch):
    network.release.set()

    @asynccontextmanager
    async def connection(url, seconds):
        class Client:
            async def call_tool(self, name, arguments):
                raise RuntimeError('fake tool failure')
        with network.connection():
            yield Client(), {'maps_text_search': {}}

    monkeypatch.setattr(amap_mcp, '_connection', connection)
    for _ in range(4):
        with pytest.raises(amap_mcp.AmapMCPError):
            with amap_mcp.session() as tools:
                tools.call('maps_text_search', {})

    class Body:
        status_code = 200

        def iter_bytes(self, **kwargs):
            raise httpx.ReadError('fake body failure')

    @contextmanager
    def stream(*args, **kwargs):
        with network.connection():
            yield Body()

    with amap_rest.borrow_client() as client:
        monkeypatch.setattr(client, 'stream', stream)
        for _ in range(4):
            with pytest.raises(amap_rest.AmapRESTError):
                amap_rest._get(amap_rest.ENDPOINT, {})
    assert network.entered == 8 and network.active == 0


@pytest.mark.parametrize('kind', ['mcp', 'rest'])
def test_slot_is_held_until_transport_close_finishes(network, kind):
    network.release.set()
    network.close_release.clear()
    with ThreadPoolExecutor(max_workers=4) as pool:
        holders = [pool.submit(request, kind, network) for _ in range(3)]
        waiter = None
        try:
            assert network.three.wait(1) and network.closing.wait(1)
            waiter = pool.submit(request, 'rest' if kind == 'mcp' else 'mcp', network)
            sleep(.1)
            assert network.active == network.entered == 3
            assert not waiter.done()
        finally:
            network.close_release.set()
        assert [future.result(2) for future in holders] == ['ok'] * 3
        assert waiter.result(2) == 'ok'
    assert network.peak == 3 and network.entered == 4


def test_weather_does_not_wait_for_or_consume_amap_slots(network, monkeypatch):
    calls = []

    def fetch(url, params):
        calls.append(url)
        if 'geocoding' in url:
            return {'results': [{'name': '杭州', 'latitude': 30, 'longitude': 120}]}
        return {'daily': {'time': ['2030-01-01'], 'weather_code': [0]}}

    monkeypatch.setattr(weather, '_fetch_json', fetch)
    with occupy_mcp_slots(), ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(weather.query_weather, {'city': '杭州'}).result(1)
        assert result['status'] == 'available'
        assert len(calls) == 2 and network.active == network.entered == 3
