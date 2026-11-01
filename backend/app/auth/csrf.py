"""Cookie-bound CSRF without a second application secret or a readable cookie."""
import hashlib
import hmac
import secrets
from urllib.parse import urlsplit

from fastapi import Request
from starlette.responses import Response

from app.auth.store import SESSION_COOKIE_NAME, SESSION_TTL_SECONDS
from app.core.config import boolean

ANONYMOUS_COOKIE_NAME = 'travel_csrf_anonymous'
CSRF_HEADER = 'X-CSRF-Token'


def csrf_token(cookie: str) -> str:
    return hmac.new(cookie.encode('utf-8'), b'TravelAssistant:csrf:v1', hashlib.sha256).hexdigest()


def set_cookie(response: Response, name: str, value: str) -> None:
    response.set_cookie(name, value, max_age=SESSION_TTL_SECONDS, httponly=True,
                        secure=boolean('TRAVEL_COOKIE_SECURE'), samesite='lax', path='/')


def delete_cookie(response: Response, name: str) -> None:
    response.delete_cookie(name, httponly=True, secure=boolean('TRAVEL_COOKIE_SECURE'),
                           samesite='lax', path='/')


def new_anonymous_cookie() -> str:
    return secrets.token_urlsafe(32)


def same_origin(request: Request) -> bool:
    # Host must remain the browser-facing Host through a same-origin dev proxy.
    # Do not trust X-Forwarded-Host, nor accept suffix/substring matches.
    origin = request.headers.get('origin', '')
    try:
        parsed = urlsplit(origin)
        expected = urlsplit(f"{request.url.scheme}://{request.headers.get('host', '')}")
        def identity(value):
            return (value.scheme, value.hostname,
                    value.port or (443 if value.scheme == 'https' else 80))
        return bool(parsed.scheme in {'http', 'https'} and parsed.hostname
                    and not parsed.username and not parsed.password
                    and not parsed.path and not parsed.query and not parsed.fragment
                    and identity(parsed) == identity(expected))
    except ValueError:
        return False


def valid_csrf(request: Request) -> bool:
    cookie = request.cookies.get(SESSION_COOKIE_NAME) or request.cookies.get(ANONYMOUS_COOKIE_NAME, '')
    supplied = request.headers.get(CSRF_HEADER, '')
    return bool(cookie and len(supplied) == 64 and supplied.isascii()
                and same_origin(request) and hmac.compare_digest(supplied, csrf_token(cookie)))
