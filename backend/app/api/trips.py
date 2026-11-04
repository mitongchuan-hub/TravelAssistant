"""旅行归属校验、增删查改、住宿消歧及需求版本保护。"""
import json
from copy import deepcopy

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.api import dependencies as deps
from app.api.schemas import CityInput, UndoInput, RequirementsTextInput
from app.api.serializers import place_to_dict, requirements_values, trip_to_dict, workspace_snapshot
from app.services.maps.places import search_places
from app.services.city_outings import CITY_FORM_FIELDS, city_requirements
from app.domain.lodging import collect_places

router = APIRouter(prefix='/api/trips', tags=['trips'])


@router.get('')
def list_trips(request: Request):
    user = deps.require_user(request)
    with deps.store._lock:
        return {'trips': [trip_to_dict(trip) for trip in deps.store.list_trips(user.id)]}


@router.get('/{trip_id}/workspace')
def workspace(request: Request, trip_id: str):
    return workspace_snapshot(trip_id, deps.require_user(request))


@router.get('/{trip_id}')
def get_trip(request: Request, trip_id: str):
    user = deps.require_user(request)
    with deps.store._lock:
        return {'trip': trip_to_dict(deps.get_owned_trip_or_404(trip_id, user))}


@router.get('/{trip_id}/requirements')
def requirements(request: Request, trip_id: str):
    user = deps.require_user(request)
    with deps.store._lock:
        trip = deps.get_owned_trip_or_404(trip_id, user)
        return {'trip': trip_to_dict(trip), 'values': requirements_values(trip)}


def _city_requirements(values, city=''):
    try:
        return city_requirements(values, city)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


async def save_city(request, data):
    trip_id = ''
    user = deps.require_user(request)
    values = {key: getattr(data, key) for key in CITY_FORM_FIELDS}
    token = data.choice_token
    selected = None
    if token:
        try:
            draft = deps.lodging_choices.get(token, user.id, trip_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        # A choice authorizes exactly the server-held draft, not later edits. A
        # token+POI-only submission is also valid and commits that original draft.
        submitted_fields = data.model_fields_set & CITY_FORM_FIELDS
        if any(values[key] != draft['values'][key] for key in submitted_fields):
            raise HTTPException(409, '游玩信息已修改，请重新查找住宿位置后再保存')
        values, candidates = draft['values'], draft['candidates']
        selected = next((place for place in candidates if place['poi_id'] == data.poi_id), None)
        if not selected:
            raise HTTPException(400, '请选择一个住宿位置')
    if not selected:
        _city_requirements(values)
        result = await run_in_threadpool(search_places, {
            'city': '', 'keywords': values['lodging_address'].strip()})
        catalog = collect_places([{'role': 'tool', 'name': 'search_places',
                                   'content': json.dumps(result, ensure_ascii=False)}])
        candidates = [place for place in catalog.values() if place.get('city')]
        if not candidates:
            raise HTTPException(400, '暂未匹配到住宿位置，请补充城市、分店名或更详细地址后重试。地图暂不可用时可稍后重试。')
        if len(candidates) == 1 and result.get('total_matched', 1) == 1:
            selected = candidates[0]
        else:
            token = deps.lodging_choices.add(user.id, values, candidates, trip_id)
            return {'status': 'choices_required', 'choice_token': token,
                    'candidates': [place_to_dict(place) for place in candidates], 'values': values}
    changes = _city_requirements(values, selected['city'])
    with deps.store._lock:
        saved = deps.store.create_trip(f"{selected['city']}同城游玩", owner=user,
                                      requirements=changes, mode='city', lodging=selected)
        result = {'status': 'saved', 'trip': trip_to_dict(saved)}
    if token:
        deps.lodging_choices.discard(token)
    return result


@router.post('')
async def create_trip(request: Request, data: CityInput):
    return await save_city(request, data)


@router.put('/{trip_id}/requirements-text')
def save_requirements_text(request: Request, trip_id: str, data: RequirementsTextInput):
    user = deps.require_user(request)
    with deps.store._lock:
        trip = deps.get_owned_trip_or_404(trip_id, user)
        if data.expected_revision != trip.requirements.revision:
            raise HTTPException(409, '需求正文已经更新，请刷新后重新保存，当前内容未被覆盖')
        try:
            deps.store.save_requirements_text(trip_id, data.text, data.expected_revision)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return workspace_snapshot(trip_id, user)


@router.delete('/{trip_id}')
def delete_trip(request: Request, trip_id: str):
    user = deps.require_user(request)
    with deps.store._lock:
        deps.get_owned_trip_or_404(trip_id, user)
        try:
            deps.store.delete_trip(trip_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
    # Short-lived address drafts are intermediate trip data too.
    choices = deps.lodging_choices
    with choices._lock:
        choices._items = {key: value for key, value in choices._items.items()
                          if not (value['trip_id'] == trip_id and value['owner_id'] == user.id)}
    return {'deleted': True}


@router.post("/{trip_id}/undo")
def undo_trip(request: Request, trip_id: str, data: UndoInput):
    from app.application.undo import undo
    from app.workflow.tasks import TaskConflict
    user = deps.require_user(request)
    with deps.store._lock:
        deps.get_owned_trip_or_404(trip_id, user)
        try:
            return {"receipt": undo(deps.store, trip_id, data.entry_id, data.request_id, data.expected_version)}
        except TaskConflict as exc:
            raise HTTPException(409, str(exc)) from None
