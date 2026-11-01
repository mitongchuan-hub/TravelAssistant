"""共享 API 单例；路由在请求时动态读取，便于统一替换存储和认证。"""
from threading import Lock

from fastapi import HTTPException, Request

from app.auth import AuthStore, SESSION_COOKIE_NAME
from app.domain.models import User
from app.services.city_outings import LodgingChoices
from app.storage.store import IDEA_WALL_LIMIT, USERS, store


# Compatibility facade for existing routers. Lifespan can replace these objects
# with app-owned resources before serving requests.
auth = AuthStore(store.db_path, store.persistence_enabled)
registration_lock = Lock()
lodging_choices = LodgingChoices()


def current_user(request: Request) -> User | None:
    user_id = auth.user_id_for_session(request.cookies.get(SESSION_COOKIE_NAME, ''))
    return USERS.get(user_id) if user_id else None


def require_user(request: Request) -> User:
    user = current_user(request)
    if not user:
        raise HTTPException(401, '请先登录')
    return user


def get_owned_trip_or_404(trip_id: str, user: User):
    trip = store.trips.get(trip_id)
    if trip is None or trip.owner.id != user.id:
        raise HTTPException(404, '旅行不存在')
    return trip
