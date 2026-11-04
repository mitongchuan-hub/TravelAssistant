"""纯 API 装配入口：先校验配置，再初始化持久化并管理应用资源。"""
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from app.core.config import load_environment, validate_configuration
from app.domain.message import MessageValidationError
from app.core.resources import client_scope

load_environment()
validate_configuration('web')

from app.api import auth, chat, dependencies as deps, guide, trips
from app.auth.csrf import valid_csrf
from app.application.execution import TaskConflict

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application):
    # The compatibility dependency module remains importable, but application
    # ownership is explicit for every running FastAPI instance.
    application.state.store = deps.store
    application.state.auth = deps.auth
    application.state.registration_lock = deps.registration_lock
    resources = client_scope()
    await run_in_threadpool(resources.__enter__)
    try:
        deps.store.start_display_worker()
        yield
    finally:
        try:
            await run_in_threadpool(deps.store.stop_display_worker)
        finally:
            await run_in_threadpool(resources.__exit__, None, None, None)


app = FastAPI(title='TravelAssistant API', lifespan=lifespan)


def error_response(status, message, code=None):
    codes = {400: 'invalid_request', 401: 'unauthorized', 403: 'forbidden', 404: 'not_found',
             405: 'method_not_allowed', 409: 'conflict', 415: 'unsupported_media_type',
             422: 'validation_error', 500: 'internal_error'}
    return JSONResponse({'error': {'code': code or codes.get(status, 'request_failed'), 'message': message}},
                        status_code=status, headers={'Cache-Control': 'no-store'})


@app.middleware('http')
async def api_security(request: Request, call_next):
    if request.url.path == '/api' or request.url.path.startswith('/api/'):
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            if not valid_csrf(request):
                return error_response(403, '安全验证已失效，请刷新页面后重试', 'csrf_failed')
            media_type = request.headers.get('content-type', '').split(';', 1)[0].strip().lower()
            if media_type != 'application/json':
                return error_response(415, '请使用 JSON 格式提交请求')
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    return await call_next(request)


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    message = exc.detail if isinstance(exc.detail, str) and exc.status_code < 500 else '服务暂不可用，请稍后重试'
    return error_response(exc.status_code, message)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Pydantic errors include input values (including passwords); do not serialize them.
    # Only expose messages from our input validator, never arbitrary error contexts.
    for error in exc.errors():
        reason = error.get('ctx', {}).get('error')
        if isinstance(reason, MessageValidationError):
            return error_response(422, str(reason))
    return error_response(422, '请求字段格式不正确，请检查填写内容')


@app.exception_handler(TaskConflict)
async def conflict_error(request, exc):
    return error_response(409, str(exc))


@app.exception_handler(ValueError)
async def value_error(request, exc):
    return error_response(400, '请求内容不正确或当前操作不可用，请检查后重试')


@app.exception_handler(Exception)
async def server_error(request, exc):
    logger.error('Unhandled API error: %s', type(exc).__name__)
    return error_response(500, '服务暂不可用，请稍后重试')


def persistence_health():
    if not deps.store.persistence_enabled:
        return {'enabled': False, 'status': 'disabled'}
    path = Path(deps.store.db_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute('SELECT 1').fetchone()
    except sqlite3.Error:
        return {'enabled': True, 'status': 'error'}
    return {'enabled': True, 'status': 'ok'}


@app.get('/health')
def health():
    db = persistence_health()
    return {'status': 'ok' if db['status'] in {'ok', 'disabled'} else 'degraded', 'persistence': db}


@app.get('/favicon.ico')
def favicon():
    return Response(status_code=204)


app.include_router(auth.router)
app.include_router(trips.router)
app.include_router(chat.router)
app.include_router(guide.router)
