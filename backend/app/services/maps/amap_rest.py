"""Single-request AMap v3 POI detail; explicit synchronous HTTP ownership.

No environment loading, provider fallback, retries, or client construction on import.
A synchronous in-flight request cannot be interrupted immediately; phase timeouts
and checks between chunks are not a strict wall-clock cancellation guarantee.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import re
from threading import Condition
from time import monotonic

import httpx

from app.core import config
from app.services.maps import amap_mcp

ENDPOINT = 'https://restapi.amap.com/v3/place/detail'
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class AmapRESTError(RuntimeError):
    """Only safe diagnostics, never upstream messages or credential-bearing URLs."""


@dataclass
class _Owner:
    client: object = None
    state: str = 'starting'
    borrowers: int = 0


_condition = Condition()
_owner = None


@contextmanager
def client_scope(*, client_factory=None):
    """Own one httpx client; reject overlapping scopes and drain leases on exit."""
    global _owner
    owner = _Owner()
    with _condition:
        if _owner is not None:
            raise AmapRESTError('高德REST资源scope已启动')
        _owner = owner
    try:
        amap_mcp.redact_transport_logs()
        # HTTPTransport defaults to zero retries; no user-provided URL or proxy.
        owner.client = (client_factory or httpx.Client)(
            timeout=config.amap_rest_timeout(), follow_redirects=False, trust_env=False)
        with _condition:
            owner.state = 'ready'
        yield
    finally:
        try:
            with _condition:
                owner.state = 'closing'
                _condition.notify_all()
                while owner.borrowers:
                    _condition.wait()
            if owner.client is not None:
                owner.client.close()
        finally:
            with _condition:
                owner.state = 'closed'
                _owner = None
                _condition.notify_all()


@contextmanager
def borrow_client():
    with _condition:
        owner = _owner
        if owner is None or owner.state != 'ready' or owner.client is None:
            raise AmapRESTError('高德REST资源尚未启动或正在关闭')
        owner.borrowers += 1
    try:
        yield owner.client
    finally:
        with _condition:
            owner.borrowers -= 1
            _condition.notify_all()


def _remaining(deadline):
    remaining = amap_mcp.remaining_budget()
    if deadline is not None:
        window = deadline - monotonic()
        if window <= 0:
            raise AmapRESTError('本次高德REST查询超时，信息待核实')
        remaining = min(remaining, window)
    return remaining


def _sanitize(value, secret):
    if isinstance(value, dict):
        return {k: '[redacted]' if k.lower() in {'key', 'api_key', 'apikey'} else _sanitize(v, secret)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v, secret) for v in value]
    if isinstance(value, str):
        value = value.replace(secret, '[redacted]')
        return re.sub(r'([?&]key=)[^&\s\"\'<>]+', r'\1[redacted]', value, flags=re.I)
    return value


def _valid_poi(payload, poi_id):
    if (not isinstance(payload, dict) or str(payload.get('status')) != '1'
            or str(payload.get('infocode')) != '10000'):
        raise AmapRESTError('高德REST权限、配额或查询暂不可用')
    pois = payload.get('pois')
    if not isinstance(pois, list) or len(pois) != 1 or not isinstance(pois[0], dict):
        raise AmapRESTError('高德REST地点详情为空或不唯一')
    poi = pois[0]
    if poi.get('id') != poi_id or not isinstance(poi.get('name'), str) or not poi['name'].strip():
        raise AmapRESTError('高德REST地点身份无效')
    location = poi.get('location')
    try:
        if not isinstance(location, str):
            raise ValueError
        lon, lat = map(float, location.split(','))
        if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError
    except (TypeError, ValueError):
        raise AmapRESTError('高德REST中心坐标无效') from None
    return poi


def _get(endpoint, parameters, *, deadline=None):
    """One fixed-endpoint request under the shared owner and turn budget."""
    _remaining(deadline)
    try:
        key = config.amap_api_key()
        request_deadline = monotonic() + min(config.amap_rest_timeout(), _remaining(deadline))
        deadline = min(deadline, request_deadline) if deadline is not None else request_deadline
    except config.ConfigurationError:
        raise AmapRESTError('高德REST密钥或超时配置无效') from None
    try:
        with amap_mcp.request_slot(deadline=deadline, error_factory=AmapRESTError):
            seconds = _remaining(deadline)
            with borrow_client() as client:
                amap_mcp.consume_call()
                with client.stream('GET', endpoint, params={**parameters, 'key': key},
                        timeout=httpx.Timeout(seconds), follow_redirects=False) as response:
                    if response.status_code != 200:
                        raise AmapRESTError('高德REST HTTP请求暂不可用')
                    body = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        _remaining(deadline)
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise AmapRESTError('高德REST响应体超出大小上限')
                        body.extend(chunk)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            raise AmapRESTError('高德REST未返回有效JSON') from None
        if (not isinstance(payload, dict) or str(payload.get('status')) != '1'
                or str(payload.get('infocode')) != '10000'):
            raise AmapRESTError('高德REST权限、配额或查询暂不可用')
        return _sanitize(payload, key)
    except httpx.HTTPError:
        raise AmapRESTError('高德REST请求失败或超时，信息待核实') from None
    finally:
        _remaining(deadline)


def route(name, arguments):
    paths = {'maps_direction_walking': 'walking', 'maps_direction_driving': 'driving',
             'maps_direction_transit_integrated': 'transit/integrated'}
    try:
        parameters = {key: arguments[key] for key in ('origin', 'destination')}
        if name == 'maps_direction_transit_integrated':
            if arguments.get('departure_at') is not None:
                return {'status': 'unavailable', 'reason': '供应商日期查询支持窗尚未验证，不能确认指定日期班次；未发送请求，也不会改用今天。',
                    'requested_departure': arguments['departure_at'], 'actual_parameters': None,
                    'query_semantics': 'unsupported_departure_window'}
            if 'strategy' in arguments:
                strategy = arguments['strategy']
                if type(strategy) is not int or strategy not in {0, 1, 2, 3, 5}:
                    raise AmapRESTError('公交策略只支持0、1、2、3、5')
                parameters['strategy'] = strategy
            parameters.update({key: arguments[key] for key in ('city', 'cityd')})
            parameters['extensions'] = 'all'
        payload = _get('https://restapi.amap.com/v3/direction/' + paths[name], parameters)
        return {**payload, 'requested_departure': None,
            'actual_parameters': parameters, 'query_semantics': 'query_time_reference_not_future_schedule',
            'metadata': {'provider': 'amap_rest', 'api_version': 'v3',
            'endpoint_kind': paths[name],
            'fetched_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}}
    except AmapRESTError as exc:
        return {'status': 'unavailable', 'reason': str(exc)}


def detail(poi_id, *, deadline=None):
    """Return rich POI + metadata + sanitized same-request raw envelope.

    Caller has already reserved the first external call in budget_scope; later
    requests in that scope are counted by consume_call, exactly as for MCP.
    Entrances remain auxiliary fields, never substituted for center location.
    """
    if not isinstance(poi_id, str) or not poi_id.strip() or len(poi_id) > 200:
        raise AmapRESTError('高德REST地点ID无效')
    payload = _get(ENDPOINT, {'id': poi_id, 'extensions': 'all'}, deadline=deadline)
    poi = _valid_poi(payload, poi_id)
    return {**poi,
            'metadata': {'provider': 'amap_rest', 'api_version': 'v3',
                'endpoint_kind': 'place_detail',
                'fetched_at': datetime.now(timezone.utc).isoformat(timespec='seconds')},
            'raw': payload}


def call(arguments):
    try:
        return detail(arguments.get('id'))
    except AmapRESTError as exc:
        return {'status': 'unavailable', 'reason': str(exc)}
