import json
import logging
import os
import sqlite3
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
from .models import AgentCard, Message, User
from .store import USERS, store

app = FastAPI(title="TravelAssistant Python H5")
auth = AuthStore(store.db_path, store.persistence_enabled)
registration_lock = Lock()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["static_version"] = "20261002-itinerary-summary-idea-auth"
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


def finish_agent_message_task(trip_id: str, body: str, sender_id: str, thinking_message_id: str) -> None:
    if trip_id in store.trips:
        try:
            store.finish_agent_task(trip_id, body, sender_id, thinking_message_id)
        except Exception as exc:
            logger.warning("Agent background task failed (%s)", type(exc).__name__)
            store.fail_agent_task(trip_id, thinking_message_id)


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
    return templates.TemplateResponse(request, "trips.html", {"trips": store.list_trips(user.id), "current_user": user})


@app.get("/trips/new", response_class=HTMLResponse)
def new_trip(request: Request) -> Response:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "create_trip.html", {"current_user": user})


@app.post("/trips", response_class=HTMLResponse)
def create_trip(
    request: Request,
    destination: str = Form(""),
    date_range: str = Form(""),
    budget: str = Form(""),
    style: str = Form(""),
    note: str = Form(""),
) -> RedirectResponse:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    trip = store.create_trip(destination, date_range, budget, style, note, user)
    return RedirectResponse(url=f"/workspace/{trip.id}?tab=chat", status_code=303)


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
            "plan": store.plans[trip_id],
            "plan_versions": store.plan_versions[trip_id],
            "metrics": store.trip_metrics(trip_id),
            "current_user": user,
        },
    )


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
    message, thinking = await run_in_threadpool(store.queue_user_message, trip_id, body, user.id)
    background_tasks.add_task(finish_agent_message_task, trip_id, body, user.id, thinking.id)
    return JSONResponse({"messages": [message_to_dict(message), message_to_dict(thinking)]})


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
def add_message(request: Request, trip_id: str, background_tasks: BackgroundTasks, body: str = Form("")) -> RedirectResponse:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    get_owned_trip_or_404(trip_id, user)
    if body.strip():
        _, thinking = store.queue_user_message(trip_id, body, user.id)
        background_tasks.add_task(finish_agent_message_task, trip_id, body, user.id, thinking.id)
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
def request_revision(request: Request, trip_id: str, item_id: str | None = Form(None), feedback: str = Form("")) -> RedirectResponse:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    get_owned_trip_or_404(trip_id, user)
    before = len(store.plan_versions[trip_id])
    store.request_revision(trip_id, item_id, feedback, user)
    tab = "itinerary" if len(store.plan_versions[trip_id]) > before else "chat"
    return RedirectResponse(url=f"/workspace/{trip_id}?tab={tab}", status_code=303)


@app.post("/workspace/{trip_id}/plan")
def generate_plan(request: Request, trip_id: str) -> RedirectResponse:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    get_owned_trip_or_404(trip_id, user)
    before = len(store.plan_versions[trip_id])
    store.generate_plan(trip_id)
    tab = "itinerary" if len(store.plan_versions[trip_id]) > before else "chat"
    return RedirectResponse(url=f"/workspace/{trip_id}?tab={tab}", status_code=303)


@app.post("/workspace/{trip_id}/plan/confirm")
def confirm_plan(request: Request, trip_id: str) -> RedirectResponse:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    get_owned_trip_or_404(trip_id, user)
    store.confirm_plan(trip_id)
    return RedirectResponse(url=f"/workspace/{trip_id}?tab=itinerary", status_code=303)


@app.post("/workspace/{trip_id}/plan/restore")
def restore_plan(request: Request, trip_id: str, version_id: str = Form("")) -> RedirectResponse:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    get_owned_trip_or_404(trip_id, user)
    if version_id:
        store.restore_plan_version(trip_id, version_id)
    return RedirectResponse(url=f"/workspace/{trip_id}?tab=itinerary", status_code=303)
