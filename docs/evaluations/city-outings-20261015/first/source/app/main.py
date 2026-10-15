import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from threading import Lock

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import (
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    AuthStore,
    normalize_email,
    validate_email,
    validate_password,
)
from urllib.parse import quote
from uuid import uuid4
from .tasks import TaskConflict
from .tools.places import map_url, query_time_label, search_places
from .places import collect_places
from .city_outings import CITY_FORM_FIELDS, LodgingChoices, city_requirements
from .models import AgentCard, Message, User
from .requirements import BASIC_FIELDS, LIST_FIELDS, PROFILE_FIELDS
from .chat_presentation import chat_reply_view, chat_time_break, chat_actions, chat_supplemental
from .presentation import day_date_label, timeline_time_parts
from .display_presentation import display_view, coverage_detail
from .store import IDEA_WALL_LIMIT, USERS, store

@asynccontextmanager
async def lifespan(application):
    store.start_display_worker()
    try:
        yield
    finally:
        store.stop_display_worker()


app = FastAPI(title="TravelAssistant 同城游玩", lifespan=lifespan)
lodging_choices = LodgingChoices()
auth = AuthStore(store.db_path, store.persistence_enabled)
registration_lock = Lock()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["static_version"] = "20261015-city-1"
templates.env.filters.update(day_date_label=day_date_label, timeline_time_parts=timeline_time_parts,
                             display_view=display_view, coverage_detail=coverage_detail,
                             chat_reply_view=chat_reply_view, chat_time_break=chat_time_break,
                             chat_actions=chat_actions, chat_supplemental=chat_supplemental)
templates.env.globals["task_request_id"] = lambda: uuid4().hex
templates.env.globals["place_map_url"] = map_url
templates.env.globals["place_query_time"] = query_time_label
logger = logging.getLogger(__name__)


@app.exception_handler(404)
def not_found(request: Request, exc: HTTPException) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "status_code": 404,
            "title": "没有找到这次旅行",
            "message": "这次旅行不存在，或者它不属于当前账号。",
            "hint": "可以回到我的旅行，打开已有计划或创建一次新旅行。",
            "action_href": "/trips",
            "action_label": "回到我的旅行",
        },
        status_code=404,
    )


@app.exception_handler(403)
def forbidden(request: Request, exc: HTTPException) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "status_code": 403,
            "title": "没有访问权限",
            "message": "当前账号不能访问这次旅行。",
            "hint": "请返回我的旅行，选择属于当前账号的计划。",
            "action_href": "/trips",
            "action_label": "回到我的旅行",
        },
        status_code=403,
    )


@app.exception_handler(500)
def server_error(request: Request, exc: Exception) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "status_code": 500,
            "title": "页面暂时没加载出来",
            "message": "服务刚刚遇到一点问题，但你的旅行数据会尽量保留。",
            "hint": "可以先返回我的旅行，或者稍后刷新再试。",
            "action_href": "/trips",
            "action_label": "回到我的旅行",
        },
        status_code=500,
    )


def message_to_dict(message: Message) -> dict:
    return {
        "id": message.id,
        "sender": {"id": message.sender.id, "name": message.sender.name, "initials": message.sender.initials},
        "sender_type": message.sender_type,
        "body": message.body,
        "created_at": message.created_at,
        "status": message.status,
        "agent_card": agent_card_to_dict(message.agent_card) if message.agent_card else None,
    }


def agent_card_to_dict(card: AgentCard) -> dict:
    return {
        "kind": card.kind,
        "title": card.title,
        "summary": card.summary,
        "bullets": card.bullets,
        "actions": [{"label": action.label, "target": action.target} for action in card.actions],
    }


def persistence_health() -> dict[str, str | bool]:
    if not store.persistence_enabled:
        return {"enabled": False, "status": "disabled", "path": store.db_path}
    path = Path(store.db_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute("SELECT 1").fetchone()
    except sqlite3.Error:
        return {"enabled": True, "status": "error", "path": store.db_path}
    return {"enabled": True, "status": "ok", "path": store.db_path}


def current_user(request: Request) -> User | None:
    session_token = request.cookies.get(SESSION_COOKIE_NAME, "")
    user_id = auth.user_id_for_session(session_token)
    return USERS.get(user_id) if user_id else None


def get_owned_trip_or_404(trip_id: str, user: User):
    trip = store.trips.get(trip_id)
    if not trip or trip.owner.id != user.id:
        raise HTTPException(status_code=404, detail="旅行不存在")
    return trip


def _secure_cookie(request: Request) -> bool:
    configured = os.getenv("TRAVEL_COOKIE_SECURE", "").strip().lower()
    if configured:
        return configured in {"1", "true", "yes", "on"}
    return request.url.scheme == "https"


def _auth_page(
    request: Request,
    mode: str = "login",
    email: str = "",
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "mode": "register" if mode == "register" else "login",
            "email": email,
            "error": error,
        },
        status_code=status_code,
    )


def redirect_with_session(request: Request, url: str, user: User, status_code: int = 303) -> RedirectResponse:
    session_token = auth.create_session(user.id)
    response = RedirectResponse(url=url, status_code=status_code)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="lax",
        path="/",
    )
    return response


@app.get("/", response_class=HTMLResponse)
def login(request: Request, mode: str = "login") -> Response:
    if current_user(request):
        return RedirectResponse(url="/trips", status_code=303)
    return _auth_page(request, mode=mode)


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@app.post("/login")
def login_submit(request: Request, email: str = Form(""), password: str = Form("")) -> Response:
    normalized_email = normalize_email(email)
    user_id = None
    if not validate_email(normalized_email) and password:
        user_id = auth.authenticate(normalized_email, password)
    user = USERS.get(user_id) if user_id else None
    if not user:
        return _auth_page(
            request,
            mode="login",
            email=normalized_email,
            error="邮箱或密码不正确",
            status_code=401,
        )
    return redirect_with_session(request, "/trips", user)


@app.post("/register")
def register_submit(request: Request, email: str = Form(""), password: str = Form("")) -> Response:
    normalized_email = normalize_email(email)
    error = validate_email(normalized_email) or validate_password(password)
    if error:
        return _auth_page(request, mode="register", email=normalized_email, error=error, status_code=400)
    with registration_lock:
        if auth.email_exists(normalized_email):
            return _auth_page(
                request,
                mode="register",
                email=normalized_email,
                error="该邮箱已注册，请直接登录",
                status_code=400,
            )

        display_name = normalized_email.split("@", 1)[0][:20] or "旅行者"
        user = store.create_user(display_name)
        if not auth.create_account(normalized_email, user.id, password):
            return _auth_page(
                request,
                mode="register",
                email=normalized_email,
                error="该邮箱已注册，请直接登录",
                status_code=400,
            )
    return redirect_with_session(request, "/trips", user)


@app.post("/logout")
def logout(request: Request) -> RedirectResponse:
    auth.revoke_session(request.cookies.get(SESSION_COOKIE_NAME, ""))
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="lax",
        path="/",
    )
    return response


@app.get("/health")
def health() -> JSONResponse:
    db = persistence_health()
    status = "ok" if db["status"] in {"ok", "disabled"} else "degraded"
    return JSONResponse(
        {
            "status": status,
            "trip_count": len(store.list_trips()),
            "message_count": sum(len(messages) for messages in store.messages.values()),
            "persistence": db,
            "static_version": templates.env.globals["static_version"],
        }
    )


@app.get("/trips", response_class=HTMLResponse)
def trips(request: Request) -> Response:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "trips.html", {
        "trips": store.list_trips(user.id),
        "current_user": user,
        "notice": request.query_params.get("notice", ""),
        "error": request.query_params.get("error", ""),
    })


@app.get("/trips/new", response_class=HTMLResponse)
def new_trip(request: Request) -> Response:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    return _city_form_page(request, user, {})


def _city_form_page(request, user, values, *, trip=None, error='', candidates=None, token='', status=200):
    return templates.TemplateResponse(request, 'city_outing_form.html', {
        'current_user': user, 'trip': trip, 'values': values, 'error': error,
        'candidates': candidates or [], 'choice_token': token,
        'saved': request.query_params.get('saved') == '1',
    }, status_code=status)


async def _save_city_form(request, user, data, trip=None):
    values = {key: data.get(key, '') for key in CITY_FORM_FIELDS}
    token = data.get('choice_token', '')
    candidates = []
    try:
        if not isinstance(token, str):
            raise ValueError('住宿位置选择格式不正确')
        selected = None
        if token:
            draft = lodging_choices.get(token, user.id, trip.id if trip else '')
            values, candidates = draft['values'], draft['candidates']
            selected = next((p for p in candidates if p['poi_id'] == data.get('poi_id')), None)
            if not selected:
                raise ValueError('请选择一个住宿位置')
        else:
            city_requirements(values)
            if (trip and trip.lodging and values['lodging_address'].strip() == trip.requirements.lodging_address
                    and not values['city_hint'].strip()):
                selected = deepcopy(trip.lodging)
            else:
                result = await run_in_threadpool(search_places, {
                    'city': values['city_hint'].strip(), 'keywords': values['lodging_address'].strip()})
                catalog = collect_places([{'role': 'tool', 'name': 'search_places',
                    'content': json.dumps(result, ensure_ascii=False)}])
                candidates = [p for p in catalog.values() if p.get('city')]
                if not candidates:
                    raise ValueError('暂未匹配到住宿位置，请补充城市、分店名或更详细地址后重试。地图暂不可用时可稍后重试。')
                if len(candidates) == 1 and result.get('total_matched', 1) == 1:
                    selected = candidates[0]
                else:
                    token = lodging_choices.add(user.id, values, candidates, trip.id if trip else '')
                    return _city_form_page(request, user, values, trip=trip, candidates=candidates, token=token)
        changes = city_requirements(values, selected['city'])
        if trip:
            revision = values.get('revision', '')
            if not revision.isascii() or not revision.isdigit():
                raise ValueError('请刷新游玩设置后再保存')
            store.update_city_outing(trip.id, changes, int(revision), selected)
            url = f'/workspace/{trip.id}/requirements?saved=1'
        else:
            trip = store.create_trip(f"{selected['city']}同城游玩", '', '', '', '', user,
                requirements=changes, mode='city', lodging=selected)
            url = f'/workspace/{trip.id}?tab=chat'
        if token:
            lodging_choices.discard(token)
        return RedirectResponse(url=url, status_code=303)
    except ValueError as exc:
        return _city_form_page(request, user, values, trip=trip, error=str(exc),
                               candidates=candidates if token else [], token=token, status=400)


@app.post("/trips", response_class=HTMLResponse)
async def create_trip(request: Request) -> Response:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    return await _save_city_form(request, user, await request.form())


@app.post("/trips/{trip_id}/delete")
def delete_trip_route(request: Request, trip_id: str) -> Response:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    get_owned_trip_or_404(trip_id, user)
    try:
        store.delete_trip(trip_id)
    except ValueError as exc:
        return RedirectResponse(url=f"/trips?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(url="/trips?notice=旅行已删除", status_code=303)


@app.get("/workspace/{trip_id}/requirements", response_class=HTMLResponse)
def edit_requirements(request: Request, trip_id: str) -> Response:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    trip = get_owned_trip_or_404(trip_id, user)
    return _requirements_page(request, trip, user)


def _requirements_page(request: Request, trip, user: User, values: dict | None = None, error: str = "", status_code: int = 200) -> HTMLResponse:
    if values is None:
        values = {key: getattr(trip, key) for key in BASIC_FIELDS}
        values.update({key: getattr(trip.requirements, key) for key in PROFILE_FIELDS})
        for key in LIST_FIELDS:
            values[key] = "\n".join(values[key])
        values["revision"] = trip.requirements.revision
    if trip.mode == 'city':
        return _city_form_page(request, user, values, trip=trip, error=error, status=status_code)
    return templates.TemplateResponse(request, "requirements.html", {
        "trip": trip, "current_user": user, "values": values, "error": error,
        "saved": request.query_params.get("saved") == "1",
        "idea_cards": store.idea_cards[trip.id],
    }, status_code=status_code)


@app.post("/workspace/{trip_id}/requirements", response_class=HTMLResponse)
async def save_requirements(request: Request, trip_id: str) -> Response:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    trip = get_owned_trip_or_404(trip_id, user)
    data = await request.form()
    if trip.mode == 'city':
        return await _save_city_form(request, user, data, trip)
    values = {key: data.get(key, "") for key in BASIC_FIELDS | PROFILE_FIELDS | {"revision"}}
    try:
        if not all(isinstance(value, str) for value in values.values()):
            raise ValueError("需求格式不正确")
        if not values["revision"].isdigit():
            raise ValueError("请刷新页面后再保存")
        changes = {key: value for key, value in values.items() if key != "revision"}
        for key in LIST_FIELDS:
            changes[key] = changes[key].splitlines()
        store.update_trip_requirements(trip_id, changes, int(values["revision"]))
    except ValueError as exc:
        return _requirements_page(request, trip, user, values, str(exc), 400)
    return RedirectResponse(url=f"/workspace/{trip_id}/requirements?saved=1", status_code=303)


@app.get("/workspace/{trip_id}", response_class=HTMLResponse)
def workspace(request: Request, trip_id: str, tab: str = "chat") -> Response:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    trip = get_owned_trip_or_404(trip_id, user)
    active_tab = tab if tab in {"chat", "board", "itinerary"} else "chat"
    return templates.TemplateResponse(
        request,
        "workspace.html",
        {
            "trip": trip,
            "tab": active_tab,
            "messages": store.messages[trip_id],
            "idea_cards": store.idea_cards[trip_id],
            "wall_cards": store.idea_cards[trip_id][-IDEA_WALL_LIMIT:],
            "plan": store.plans[trip_id],
            "metrics": store.trip_metrics(trip_id),
            "current_user": user,
            "travel_tasks": store.list_tasks(trip_id),
            "display_state": store.get_display_state(trip_id),
        },
    )


@app.get('/api/trips/{trip_id}/plan-state')
def api_plan_state(request: Request, trip_id: str) -> Response:
    user = current_user(request)
    if not user:
        return JSONResponse({'error': '请先登录'}, status_code=401)
    get_owned_trip_or_404(trip_id, user)
    return JSONResponse(store.get_display_state(trip_id), headers={'Cache-Control': 'no-store'})


@app.post('/api/trips/{trip_id}/display')
async def api_retry_display(request: Request, trip_id: str) -> Response:
    user = current_user(request)
    if not user:
        return JSONResponse({'error': '请先登录'}, status_code=401)
    get_owned_trip_or_404(trip_id, user)
    try:
        data = await request.json()
        if not isinstance(data, dict) or not isinstance(data.get('plan_id'), str) or not isinstance(data.get('source_hash'), str):
            raise ValueError('请刷新行程后再整理卡片')
        store.retry_display(trip_id, data['plan_id'], data['source_hash'])
    except (ValueError, UnicodeDecodeError) as exc:
        return JSONResponse({'error': str(exc)}, status_code=409)
    return JSONResponse(store.get_display_state(trip_id), status_code=202, headers={'Cache-Control': 'no-store'})


@app.get('/workspace/{trip_id}/display/partial', response_class=HTMLResponse)
def display_partial(request: Request, trip_id: str, plan_id: str = '', source_hash: str = '') -> Response:
    user = current_user(request)
    if not user:
        return RedirectResponse(url='/', status_code=303)
    get_owned_trip_or_404(trip_id, user)
    with store._lock:
        state = store.get_display_state(trip_id)
        if state['plan_id'] != plan_id or state['source_hash'] != source_hash:
            return Response(status_code=409)
        plan = deepcopy(store.plans[trip_id])
    return templates.TemplateResponse(request, 'partials/display_panel.html',
        {'plan': plan, 'display_state': state}, headers={'Cache-Control': 'no-store'})


@app.get("/api/trips/{trip_id}/tasks")
def api_tasks(request: Request, trip_id: str) -> Response:
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "请先登录"}, status_code=401)
    get_owned_trip_or_404(trip_id, user)
    return JSONResponse({"tasks": store.list_tasks(trip_id)}, headers={"Cache-Control": "no-store"})


def task_response(request: Request, trip_id: str, kind: str, payload: dict, request_id: str, background_tasks: BackgroundTasks, retry_of: str = "") -> Response:
    try:
        task, created = store.submit_task(trip_id, kind, payload, request_id, retry_of)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409 if isinstance(exc, TaskConflict) else 400)
    if created:
        background_tasks.add_task(store.run_task, task["id"])
    return JSONResponse({"task": task}, status_code=202 if task["status"] in {"queued", "running"} else 200)


@app.post("/api/trips/{trip_id}/tasks")
async def api_submit_task(request: Request, trip_id: str, background_tasks: BackgroundTasks) -> Response:
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "请先登录"}, status_code=401)
    get_owned_trip_or_404(trip_id, user)
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError()
        kind, request_id = data.get("kind"), data.get("request_id")
        if not isinstance(kind, str) or not isinstance(request_id, str):
            raise ValueError()
        payload = data.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError()
        if kind == "plan":
            payload = {}
        elif kind == "revision":
            if not isinstance(payload.get("feedback"), str) or not 1 <= len(payload["feedback"].strip()) <= 2000:
                raise ValueError()
            if payload.get("item_id") is not None and not isinstance(payload["item_id"], str):
                raise ValueError()
            payload = {"feedback": payload["feedback"].strip(), "item_id": payload.get("item_id")}
        elif kind == "chat":
            if not isinstance(payload.get("body"), str):
                raise ValueError()
            payload = {"body": payload["body"].strip()}
        else:
            raise ValueError()
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "任务内容不正确"}, status_code=400)
    return task_response(request, trip_id, kind, payload, request_id, background_tasks)


@app.post("/api/trips/{trip_id}/tasks/{task_id}/retry")
async def api_retry_task(request: Request, trip_id: str, task_id: str, background_tasks: BackgroundTasks) -> Response:
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "请先登录"}, status_code=401)
    get_owned_trip_or_404(trip_id, user)
    original = next((task for task in store.list_tasks(trip_id) if task["id"] == task_id), None)
    if not original:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    try:
        data = await request.json()
        request_id = data.get("request_id") if isinstance(data, dict) else None
    except (ValueError, UnicodeDecodeError):
        request_id = None
    return task_response(request, trip_id, original["kind"], original["payload"], request_id, background_tasks, task_id)


@app.get("/api/trips/{trip_id}/messages")
def api_messages(request: Request, trip_id: str, after: str = "") -> Response:
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "请先登录"}, status_code=401)
    get_owned_trip_or_404(trip_id, user)
    messages = store.messages[trip_id]
    if after:
        try:
            index = next(index for index, message in enumerate(messages) if message.id == after)
            messages = messages[index + 1 :]
        except StopIteration:
            messages = []
    return JSONResponse({"messages": [message_to_dict(message) for message in messages]})


@app.post("/api/trips/{trip_id}/messages")
async def api_add_message(request: Request, trip_id: str, background_tasks: BackgroundTasks) -> Response:
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "请先登录"}, status_code=401)
    get_owned_trip_or_404(trip_id, user)
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "消息格式不正确"}, status_code=400)
    if not isinstance(data, dict) or not isinstance(data.get("body"), str):
        return JSONResponse({"error": "消息必须是文本"}, status_code=400)
    body = data["body"].strip()
    if not body:
        return JSONResponse({"error": "消息不能为空"}, status_code=400)
    request_id = data.get("request_id", uuid4().hex)
    try:
        task, created = await run_in_threadpool(store.submit_task, trip_id, "chat", {"body": body}, request_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409 if isinstance(exc, TaskConflict) else 400)
    if created:
        background_tasks.add_task(store.run_task, task["id"])
    messages = [message for message in store.messages[trip_id] if message.id in {task["message_id"], task["thinking_id"]}]
    return JSONResponse({"messages": [message_to_dict(message) for message in messages], "task": task})


@app.get("/workspace/{trip_id}/messages/partial", response_class=HTMLResponse)
def messages_partial(request: Request, trip_id: str) -> Response:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    trip = get_owned_trip_or_404(trip_id, user)
    return templates.TemplateResponse(
        request,
        "partials/chat_messages.html",
        {"trip": trip, "messages": store.messages[trip_id]},
    )


@app.post("/workspace/{trip_id}/messages")
def add_message(request: Request, trip_id: str, background_tasks: BackgroundTasks, body: str = Form(""), request_id: str = Form("")) -> RedirectResponse:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    get_owned_trip_or_404(trip_id, user)
    if body.strip():
        try:
            task, created = store.submit_task(trip_id, "chat", {"body": body.strip()}, request_id or uuid4().hex)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        if created:
            background_tasks.add_task(store.run_task, task["id"])
    return RedirectResponse(url=f"/workspace/{trip_id}?tab=chat", status_code=303)


@app.post("/workspace/{trip_id}/messages/clear")
def clear_messages(request: Request, trip_id: str) -> RedirectResponse:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    get_owned_trip_or_404(trip_id, user)
    store.clear_chat(trip_id)
    return RedirectResponse(url=f"/workspace/{trip_id}?tab=chat", status_code=303)


@app.post("/workspace/{trip_id}/ideas")
def add_idea(request: Request, trip_id: str, body: str = Form("")) -> RedirectResponse:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    get_owned_trip_or_404(trip_id, user)
    if body.strip():
        store.add_idea(trip_id, body)
    return RedirectResponse(url=f"/workspace/{trip_id}?tab=board", status_code=303)


@app.post("/workspace/{trip_id}/ideas/delete")
def delete_idea(request: Request, trip_id: str, idea_id: str = Form("")) -> RedirectResponse:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    get_owned_trip_or_404(trip_id, user)
    if idea_id:
        store.delete_idea(trip_id, idea_id)
    return RedirectResponse(url=f"/workspace/{trip_id}?tab=board", status_code=303)


@app.post("/workspace/{trip_id}/revision")
def request_revision(request: Request, trip_id: str, background_tasks: BackgroundTasks, item_id: str | None = Form(None), feedback: str = Form(""), request_id: str = Form("")) -> Response:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    get_owned_trip_or_404(trip_id, user)
    try:
        task, created = store.submit_task(trip_id, "revision", {"item_id": item_id, "feedback": feedback.strip()}, request_id or uuid4().hex)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409 if isinstance(exc, TaskConflict) else 400)
    if created:
        background_tasks.add_task(store.run_task, task["id"])
    return RedirectResponse(url=f"/workspace/{trip_id}?tab=itinerary&task={task['id']}", status_code=303)


@app.post("/workspace/{trip_id}/plan")
def generate_plan(request: Request, trip_id: str, background_tasks: BackgroundTasks, request_id: str = Form("")) -> Response:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    get_owned_trip_or_404(trip_id, user)
    try:
        task, created = store.submit_task(trip_id, "plan", {}, request_id or uuid4().hex)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409 if isinstance(exc, TaskConflict) else 400)
    if created:
        background_tasks.add_task(store.run_task, task["id"])
    return RedirectResponse(url=f"/workspace/{trip_id}?tab=itinerary&task={task['id']}", status_code=303)


@app.post("/workspace/{trip_id}/plan/confirm")
def confirm_plan(request: Request, trip_id: str) -> RedirectResponse:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    get_owned_trip_or_404(trip_id, user)
    store.confirm_plan(trip_id)
    return RedirectResponse(url=f"/workspace/{trip_id}?tab=itinerary", status_code=303)
