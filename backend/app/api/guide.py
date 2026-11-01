"""版本绑定的攻略访问、展示重试及行程确认接口。"""
from copy import deepcopy
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.api import dependencies as deps
from app.api.schemas import BasisInput, DisplayInput
from app.api.serializers import workspace_snapshot
from app.services.itinerary.guide_html import GUIDE_CSP, GUIDE_SANDBOX, guide_document, validate_guide_display
from app.services.itinerary.plan_display import dates_from_snapshot
from app.workflow.tasks import TaskConflict, validate_task_basis

router = APIRouter(prefix='/api/trips/{trip_id}', tags=['guide'])


@router.get('/plan-state')
def plan_state(request: Request, trip_id: str):
    user = deps.require_user(request)
    with deps.store._lock:
        deps.get_owned_trip_or_404(trip_id, user)
        return deps.store.get_display_state(trip_id)


@router.post('/display')
def retry_display(request: Request, trip_id: str, data: DisplayInput):
    user = deps.require_user(request)
    with deps.store._lock:
        deps.get_owned_trip_or_404(trip_id, user)
        try:
            deps.store.retry_display(trip_id, data.plan_id, data.source_hash)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return JSONResponse(deps.store.get_display_state(trip_id), status_code=202)


@router.post('/plan/confirm')
def confirm(request: Request, trip_id: str, data: BasisInput):
    user = deps.require_user(request)
    with deps.store._lock:
        trip = deps.get_owned_trip_or_404(trip_id, user)
        if not validate_task_basis(deps.store.plans[trip_id], trip.requirements.revision, data.model_dump()):
            raise TaskConflict('确认当前行程需要完整页面版本，请刷新后重试')
        receipt = deps.store.confirm_current_turn(trip_id)
        if receipt is None or not receipt.committed:
            raise TaskConflict(receipt.reason if receipt is not None else '当前没有可确认的行程')
        return workspace_snapshot(trip_id, user)


@router.get('/guide')
def guide(request: Request, trip_id: str, plan_id: str = '', source_hash: str = '',
          version: str = '', download: bool = False):
    user = deps.require_user(request)
    with deps.store._lock:
        deps.get_owned_trip_or_404(trip_id, user)
        state = deps.store.get_display_state(trip_id)
        if (not state['guide_available'] or state['plan_id'] != plan_id
                or state['source_hash'] != source_hash or state['guide_version'] != version):
            raise HTTPException(409, '攻略已更新或尚未生成，请返回聊天查看最新版本')
        plan = deepcopy(deps.store.plans[trip_id])
    try:
        display = validate_guide_display(plan.display, dates_from_snapshot(plan.requirements_snapshot))
        document = guide_document(display)
    except (TypeError, ValueError, KeyError):
        raise HTTPException(409, '攻略网页暂不可用，可查看完整文字或重新生成') from None
    headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
               'Referrer-Policy': 'no-referrer',
               'Content-Security-Policy': GUIDE_CSP + '; ' + GUIDE_SANDBOX + "; frame-ancestors 'self'"}
    if download:
        headers['Content-Disposition'] = ("attachment; filename=travel-guide.html; filename*=UTF-8''"
                                          + quote(display['title'][:80] + '.html', safe=''))
    return HTMLResponse(document, headers=headers)
