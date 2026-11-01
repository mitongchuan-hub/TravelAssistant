"""仅旅行所有者可操作的想法墙接口。"""
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request

from app.api import dependencies as deps
from app.api.schemas import IdeaInput

router = APIRouter(prefix='/api/trips/{trip_id}/ideas', tags=['ideas'])


@router.post('')
def add_idea(request: Request, trip_id: str, data: IdeaInput):
    user = deps.require_user(request)
    if not data.body.strip():
        raise HTTPException(400, '想法不能为空')
    with deps.store._lock:
        deps.get_owned_trip_or_404(trip_id, user)
        return {'idea': asdict(deps.store.add_idea(trip_id, data.body.strip()))}


@router.delete('/{idea_id}')
def delete_idea(request: Request, trip_id: str, idea_id: str):
    user = deps.require_user(request)
    with deps.store._lock:
        deps.get_owned_trip_or_404(trip_id, user)
        if not deps.store.delete_idea(trip_id, idea_id):
            raise HTTPException(404, '想法不存在')
    return {'deleted': True}
