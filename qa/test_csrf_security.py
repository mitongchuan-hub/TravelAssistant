"""Regression tests for app.auth.csrf (previously not unit-tested directly).

Covers token determinism, the same-origin decision (subdomain / port / scheme /
userinfo / path attacks) and the composite valid_csrf gate (length, ASCII,
cookie binding, origin). These assert the intended security contract; the
reverse-proxy intent (browser-facing Host preserved) is verified in
vite.config.ts (changeOrigin: false) and docs/deployment/nginx.conf
(proxy_set_header Host $http_host), so matching Host/Origin here is correct.
"""
import pytest

from starlette.requests import Request

from app.auth.csrf import (
    ANONYMOUS_COOKIE_NAME, SESSION_COOKIE_NAME,
    csrf_token, same_origin, valid_csrf,
)


def make_request(origin=None, host='127.0.0.1:8000', scheme='http'):
    headers = [(b'host', host.encode('utf-8'))]
    if origin is not None:
        headers.append((b'origin', origin.encode('utf-8')))
    scope = {
        'type': 'http', 'method': 'POST', 'path': '/api/x', 'query_string': b'',
        'scheme': scheme, 'server': ('127.0.0.1', 8000), 'client': ('127.0.0.1', 0),
        'headers': headers,
    }
    return Request(scope)


def make_request_with_cookie(cookie_name, cookie_value, token, origin, host='127.0.0.1:8000'):
    headers = [(b'host', host.encode('utf-8'))]
    if origin is not None:
        headers.append((b'origin', origin.encode('utf-8')))
    if token is not None:
        headers.append((b'x-csrf-token', token.encode('utf-8')))
    if cookie_value is not None:
        headers.append((b'cookie', f'{cookie_name}={cookie_value}'.encode('utf-8')))
    scope = {
        'type': 'http', 'method': 'POST', 'path': '/api/x', 'query_string': b'',
        'scheme': 'http', 'server': ('127.0.0.1', 8000), 'client': ('127.0.0.1', 0),
        'headers': headers,
    }
    return Request(scope)


# --- token ------------------------------------------------------------------

def test_token_is_stable_64_hex_and_cookie_bound():
    token = csrf_token('cookie-value')
    assert len(token) == 64
    assert token == csrf_token('cookie-value')
    assert token != csrf_token('another-cookie')
    assert token == csrf_token('cookie-value')  # deterministic, not random


# --- same_origin ------------------------------------------------------------

def test_same_origin_accepts_exact_match():
    assert same_origin(make_request(origin='http://127.0.0.1:8000')) is True


def test_same_origin_accepts_default_port_for_its_scheme():
    # Origin omits the port (=> 80) but the app is served on :80, so they match.
    assert same_origin(make_request(origin='http://127.0.0.1', host='127.0.0.1:80')) is True


def test_same_origin_accepts_www_form_to_match_host():
    assert same_origin(make_request(origin='http://www.example.com', host='www.example.com')) is True


@pytest.mark.parametrize('origin', [
    'http://evil.com',                      # different host
    'http://127.0.0.1:8000.evil.com',       # suffix injection
    'http://127.0.0.1:8000/',               # path in origin
    'http://127.0.0.1:8000/?x=1',           # query in origin
    'http://user:pass@127.0.0.1:8000',      # userinfo
    'not a url',
    '',
])
def test_same_origin_rejects_attack_origins(origin):
    assert same_origin(make_request(origin=origin)) is False


def test_same_origin_rejects_scheme_mismatch():
    # Request served over http, origin claims https: different origin.
    assert same_origin(make_request(origin='https://127.0.0.1:8000')) is False


def test_same_origin_rejects_wrong_port():
    # App on :8000, origin on :443 => different port => different origin.
    assert same_origin(make_request(origin='http://127.0.0.1:443')) is False


def test_same_origin_rejects_port_when_host_uses_default():
    # App on default :80; origin explicitly :8000 is a different origin.
    assert same_origin(make_request(origin='http://127.0.0.1:8000', host='127.0.0.1')) is False


# --- valid_csrf (composite gate) -------------------------------------------

def _valid(cookie_name=SESSION_COOKIE_NAME, token=None, origin='http://127.0.0.1:8000'):
    return valid_csrf(make_request_with_cookie(cookie_name, 'sess-cookie', token, origin))


def test_valid_csrf_accepts_correct_token_and_origin():
    assert _valid(token=csrf_token('sess-cookie')) is True


def test_valid_csrf_accepts_anonymous_cookie():
    assert valid_csrf(make_request_with_cookie(
        ANONYMOUS_COOKIE_NAME, 'sess-cookie', csrf_token('sess-cookie'),
        'http://127.0.0.1:8000')) is True


def test_valid_csrf_rejects_wrong_token():
    assert _valid(token='f' * 64) is False


def test_valid_csrf_rejects_wrong_length():
    token = csrf_token('sess-cookie')
    assert _valid(token=token[:63]) is False
    assert _valid(token=token + 'a') is False


def test_valid_csrf_rejects_non_ascii_token():
    assert _valid(token='é' + csrf_token('sess-cookie')[1:]) is False


def test_valid_csrf_rejects_missing_cookie():
    assert _valid(token=csrf_token('other-cookie')) is False


def test_valid_csrf_rejects_wrong_origin():
    assert valid_csrf(make_request_with_cookie(
        SESSION_COOKIE_NAME, 'sess-cookie', csrf_token('sess-cookie'),
        'http://evil.com')) is False
