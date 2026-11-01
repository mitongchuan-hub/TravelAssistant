"""会话初始化及 JSON 登录、注册和退出接口。"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api import dependencies as deps
from app.api.schemas import Credentials, EmptyInput
from app.api.serializers import user_to_dict
from app.auth import SESSION_COOKIE_NAME, normalize_email, validate_email, validate_password
from app.auth.csrf import (ANONYMOUS_COOKIE_NAME, csrf_token, delete_cookie,
                           new_anonymous_cookie, set_cookie)

router = APIRouter(prefix='/api/auth', tags=['auth'])


def session_response(user, cookie):
    return JSONResponse({'user': user_to_dict(user) if user else None,
                         'csrf_token': csrf_token(cookie)})


@router.get('/session')
def session(request: Request):
    user = deps.current_user(request)
    if user:
        return session_response(user, request.cookies[SESSION_COOKIE_NAME])
    cookie = request.cookies.get(ANONYMOUS_COOKIE_NAME) or new_anonymous_cookie()
    response = session_response(None, cookie)
    if request.cookies.get(SESSION_COOKIE_NAME):
        delete_cookie(response, SESSION_COOKIE_NAME)
    set_cookie(response, ANONYMOUS_COOKIE_NAME, cookie)
    return response


def login_response(request, user):
    token = deps.auth.create_session(user.id)
    deps.auth.revoke_session(request.cookies.get(SESSION_COOKIE_NAME, ''))
    response = session_response(user, token)
    set_cookie(response, SESSION_COOKIE_NAME, token)
    delete_cookie(response, ANONYMOUS_COOKIE_NAME)
    return response


@router.post('/login')
def login(request: Request, data: Credentials):
    email = normalize_email(data.email)
    user_id = deps.auth.authenticate(email, data.password)
    user = deps.USERS.get(user_id) if user_id else None
    if not user:
        raise HTTPException(401, '邮箱或密码不正确')
    return login_response(request, user)


@router.post('/register')
def register(request: Request, data: Credentials):
    email = normalize_email(data.email)
    error = validate_email(email) or validate_password(data.password)
    if error:
        raise HTTPException(400, error)
    with deps.registration_lock:
        if deps.auth.email_exists(email):
            raise HTTPException(400, '该邮箱已注册，请直接登录')
        user = deps.store.create_user(email.split('@', 1)[0][:20] or '旅行者')
        if not deps.auth.create_account(email, user.id, data.password):
            raise HTTPException(400, '该邮箱已注册，请直接登录')
    return login_response(request, user)


@router.post('/logout')
def logout(request: Request, data: EmptyInput):
    deps.auth.revoke_session(request.cookies.get(SESSION_COOKIE_NAME, ''))
    cookie = new_anonymous_cookie()
    response = session_response(None, cookie)
    delete_cookie(response, SESSION_COOKIE_NAME)
    set_cookie(response, ANONYMOUS_COOKIE_NAME, cookie)
    return response
