import hashlib
import sqlite3
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .models import AgentCard, Message, User
from .store import USERS, store

app = FastAPI(title="TravelAssistant Python H5")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["static_version"] = "20260815-quality-pass"


@app.exception_handler(404)
def not_found(request: Request, exc: HTTPException) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "status_code": 404,
            "title": "没有找到这个旅行群",
            "message": "这个链接可能已经失效，或者服务重启前的旧旅行群不存在了。",
            "hint": "可以回到旅行群列表，重新创建或打开一个新的邀请链接。",
            "action_href": "/trips",
            "action_label": "回到旅行群",
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
            "title": "这个邀请链接不可用",
            "message": "邀请校验没有通过，可能是链接复制不完整。",
            "hint": "请让发起人重新复制邀请页里的完整链接。",
            "action_href": "/trips",
            "action_label": "回到旅行群",
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
            "hint": "可以先返回旅行群列表，或者稍后刷新再试。",
            "action_href": "/trips",
            "action_label": "回到旅行群",
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
        except Exception:
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
    user_id = request.cookies.get("travel_user_id")
    if user_id and user_id in USERS:
        return USERS[user_id]
    return None


def require_user(request: Request) -> User:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def get_trip_or_404(trip_id: str):
    if trip_id not in store.trips:
        raise HTTPException(status_code=404, detail="旅行群不存在")
    return store.get_trip(trip_id)


def invite_token_for(trip_id: str) -> str:
    trip = get_trip_or_404(trip_id)
    raw = f"{trip.id}:{trip.initiator.id}:travelassistant-invite"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def valid_invite_token(trip_id: str, token: str) -> bool:
    return token == invite_token_for(trip_id)


def redirect_with_user(url: str, user: User, status_code: int = 303) -> RedirectResponse:
    response = RedirectResponse(url=url, status_code=status_code)
    response.set_cookie("travel_user_id", user.id, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return response


@app.get("/", response_class=HTMLResponse)
def login(request: Request, switch: str = "") -> Response:
    user = current_user(request)
    if user and not switch:
        return RedirectResponse(url="/trips", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"current_user": user})


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@app.post("/login")
def login_submit(nickname: str = Form("")) -> RedirectResponse:
    if not nickname.strip():
        return RedirectResponse(url="/", status_code=303)
    user = store.get_or_create_user(nickname)
    return redirect_with_user("/trips", user)


@app.post("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("travel_user_id")
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
    return templates.TemplateResponse(request, "trips.html", {"trips": store.list_trips(), "current_user": user})


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
    return RedirectResponse(url=f"/invite/{trip.id}", status_code=303)


@app.get("/invite/{trip_id}", response_class=HTMLResponse)
def invite(request: Request, trip_id: str) -> HTMLResponse:
    trip = get_trip_or_404(trip_id)
    invite_token = invite_token_for(trip_id)
    invite_url = f'{request.url_for("invite", trip_id=trip_id)}?token={invite_token}'
    return templates.TemplateResponse(
        request,
        "invite.html",
        {"trip": trip, "current_user": current_user(request), "invite_url": invite_url, "invite_token": invite_token},
    )


@app.post("/invite/{trip_id}/join")
def join_invite(request: Request, trip_id: str, token: str = Form("")) -> RedirectResponse:
    get_trip_or_404(trip_id)
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    if not valid_invite_token(trip_id, token):
        raise HTTPException(status_code=403, detail="邀请链接不可用")
    store.add_member(trip_id, user)
    return redirect_with_user(f"/workspace/{trip_id}?tab=chat", user)


@app.get("/workspace/{trip_id}", response_class=HTMLResponse)
def workspace(request: Request, trip_id: str, tab: str = "board") -> Response:
    trip = get_trip_or_404(trip_id)
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request,
        "workspace.html",
        {
            "trip": trip,
            "tab": tab,
            "messages": store.messages[trip_id],
            "idea_cards": store.idea_cards[trip_id],
            "plan": store.plans[trip_id],
            "plan_versions": store.plan_versions[trip_id],
            "preferences": store.preferences[trip_id],
            "metrics": store.trip_metrics(trip_id),
            "agent_steps": store.agent_steps,
            "member_choices": store.trip_member_choices(trip_id),
            "current_user": user,
        },
    )


@app.get("/api/trips/{trip_id}/messages")
def api_messages(request: Request, trip_id: str, after: str = "") -> Response:
    get_trip_or_404(trip_id)
    if not current_user(request):
        return JSONResponse({"error": "请先登录"}, status_code=401)
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
    get_trip_or_404(trip_id)
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "请先登录"}, status_code=401)
    data = await request.json()
    body = str(data.get("body") or "").strip()
    if not body:
        return JSONResponse({"error": "消息不能为空"}, status_code=400)
    message = store.add_user_message(trip_id, body, user.id, process_agent=False)
    response_messages = [message]
    if store.should_trigger_agent(body):
        thinking = store.start_agent_task(trip_id, body, user)
        response_messages.append(thinking)
        background_tasks.add_task(finish_agent_message_task, trip_id, body, user.id, thinking.id)
    return JSONResponse({"messages": [message_to_dict(item) for item in response_messages]})


@app.get("/workspace/{trip_id}/messages/partial", response_class=HTMLResponse)
def messages_partial(request: Request, trip_id: str) -> Response:
    trip = get_trip_or_404(trip_id)
    if not current_user(request):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request,
        "partials/chat_messages.html",
        {"trip": trip, "messages": store.messages[trip_id]},
    )


@app.post("/workspace/{trip_id}/messages")
def add_message(request: Request, trip_id: str, body: str = Form(""), sender_id: str | None = Form(None)) -> RedirectResponse:
    get_trip_or_404(trip_id)
    if body.strip():
        current = current_user(request)
        if not current:
            return RedirectResponse(url="/", status_code=303)
        user = current if sender_id is None else USERS.get(sender_id, current)
        store.add_user_message(trip_id, body, user.id)
    return RedirectResponse(url=f"/workspace/{trip_id}?tab=chat", status_code=303)


@app.post("/workspace/{trip_id}/ideas")
def add_idea(request: Request, trip_id: str, body: str = Form("")) -> RedirectResponse:
    get_trip_or_404(trip_id)
    if not current_user(request):
        return RedirectResponse(url="/", status_code=303)
    if body.strip():
        store.add_idea(trip_id, body)
    return RedirectResponse(url=f"/workspace/{trip_id}?tab=board", status_code=303)


@app.post("/workspace/{trip_id}/revision")
def request_revision(request: Request, trip_id: str, item_id: str | None = Form(None), feedback: str = Form("")) -> RedirectResponse:
    get_trip_or_404(trip_id)
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    store.request_revision(trip_id, item_id, feedback, user)
    return RedirectResponse(url=f"/workspace/{trip_id}?tab=itinerary", status_code=303)


@app.post("/workspace/{trip_id}/plan")
def generate_plan(request: Request, trip_id: str) -> RedirectResponse:
    get_trip_or_404(trip_id)
    if not current_user(request):
        return RedirectResponse(url="/", status_code=303)
    store.generate_plan(trip_id)
    return RedirectResponse(url=f"/workspace/{trip_id}?tab=itinerary", status_code=303)


@app.post("/workspace/{trip_id}/plan/confirm")
def confirm_plan(request: Request, trip_id: str) -> RedirectResponse:
    get_trip_or_404(trip_id)
    if not current_user(request):
        return RedirectResponse(url="/", status_code=303)
    store.confirm_plan(trip_id)
    return RedirectResponse(url=f"/workspace/{trip_id}?tab=itinerary", status_code=303)


@app.post("/workspace/{trip_id}/plan/restore")
def restore_plan(request: Request, trip_id: str, version_id: str = Form("")) -> RedirectResponse:
    get_trip_or_404(trip_id)
    if not current_user(request):
        return RedirectResponse(url="/", status_code=303)
    if version_id:
        store.restore_plan_version(trip_id, version_id)
    return RedirectResponse(url=f"/workspace/{trip_id}?tab=itinerary", status_code=303)
