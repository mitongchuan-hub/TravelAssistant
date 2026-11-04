"""JSON 聊天任务提交、轮询、终止及聊天清空接口。"""
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api import dependencies as deps
from app.api.schemas import EmptyInput, MessageInput, TaskInput
from app.api.serializers import message_to_dict, task_to_dict, workspace_snapshot
from app.application.execution import TaskConflict

router = APIRouter(prefix='/api/trips/{trip_id}', tags=['chat'])


def normalized_payload(data):
    payload = data.payload
    if payload.keys() - {'body'}:
        raise HTTPException(400, '任务内容不正确')
    return {'body': payload.get('body')}


def submit(request, trip_id, kind, payload, request_id, background_tasks, include_messages=False):
    user = deps.require_user(request)
    with deps.store._lock:
        deps.get_owned_trip_or_404(trip_id, user)
        try:
            task, created = deps.store.submit_task(trip_id, kind, payload, request_id)
        except TaskConflict:
            raise
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        result = {'task': task_to_dict(task)}
        if include_messages:
            result['messages'] = [message_to_dict(row) for row in deps.store.messages[trip_id]
                                  if row.id in {task['message_id'], task['thinking_id']}]
    if created:
        background_tasks.add_task(deps.store.run_task, task['id'])
    return JSONResponse(result, status_code=202 if task['status'] in {'queued', 'running'} else 200)


@router.get('/tasks')
def tasks(request: Request, trip_id: str, request_id: str | None = None):
    user = deps.require_user(request)
    with deps.store._lock:
        deps.get_owned_trip_or_404(trip_id, user)
        return {'tasks': [task_to_dict(row) for row in deps.store.list_tasks(trip_id, request_id=request_id)]}


@router.post('/tasks')
def submit_task(request: Request, trip_id: str, data: TaskInput, background_tasks: BackgroundTasks):
    return submit(request, trip_id, data.kind, normalized_payload(data), data.request_id, background_tasks)


@router.post('/tasks/{task_id}/cancel')
def cancel_task(request: Request, trip_id: str, task_id: str, data: EmptyInput):
    user = deps.require_user(request)
    with deps.store._lock:
        deps.get_owned_trip_or_404(trip_id, user)
        task = deps.store.cancel_task(trip_id, task_id)
        if task is None:
            raise HTTPException(404, '任务不存在')
        return {'task': task_to_dict(task)}


@router.get('/messages')
def messages(request: Request, trip_id: str, after: str = ''):
    user = deps.require_user(request)
    with deps.store._lock:
        deps.get_owned_trip_or_404(trip_id, user)
        rows = deps.store.messages[trip_id]
        if after:
            index = next((index for index, row in enumerate(rows) if row.id == after), None)
            rows = rows[index + 1:] if index is not None else []
        return {'messages': [message_to_dict(row) for row in rows]}


@router.post('/messages')
def add_message(request: Request, trip_id: str, data: MessageInput, background_tasks: BackgroundTasks):
    return submit(request, trip_id, 'chat', {'body': data.body}, data.request_id,
                  background_tasks, include_messages=True)


@router.delete('/messages')
def clear_messages(request: Request, trip_id: str):
    user = deps.require_user(request)
    with deps.store._lock:
        deps.get_owned_trip_or_404(trip_id, user)
        deps.store.clear_chat(trip_id)
        return workspace_snapshot(trip_id, user)


@router.post('/messages/clear')
def clear_messages_post(request: Request, trip_id: str, data: EmptyInput):
    return clear_messages(request, trip_id)
