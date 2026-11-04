"""公开字段白名单；不直接导出内部任务、证据和模型运行记录。"""
from copy import deepcopy
from dataclasses import asdict
from typing import Any, cast

from app.api import dependencies as deps
from app.api.schemas import (
    AgentCardDTO, DisplayStateDTO, MessageDTO, MetricsDTO, PlaceDTO,
    PlanDTO, ProgressDTO, TaskDTO, TripDTO, UserDTO, WorkspaceDTO,
)
from app.domain.models import Message, Trip, TripPlan, User
from app.services.city_outings import CITY_FORM_FIELDS


def user_to_dict(user: User) -> UserDTO:
    return {'id': user.id, 'name': user.name, 'initials': user.initials}


def place_to_dict(place: dict[str, Any]) -> PlaceDTO:
    keys = ('poi_id', 'name', 'address', 'longitude', 'latitude', 'city', 'district',
            'source', 'fetched_at', 'selected_at', 'place_type', 'opening_hours')
    return cast(PlaceDTO, {key: deepcopy(place[key]) for key in keys if key in place})


def trip_to_dict(trip: Trip) -> TripDTO:
    return cast(TripDTO, {**{key: getattr(trip, key) for key in
               ('id', 'destination', 'date_range', 'status', 'last_activity', 'mode')},
            'owner': user_to_dict(trip.owner), 'requirements': asdict(trip.requirements),
            'lodging': place_to_dict(trip.lodging)})


def message_to_dict(message: Message) -> MessageDTO:
    card = message.agent_card
    progress = message.execution_progress or {}
    public_progress = {key: deepcopy(progress[key]) for key in
                       ('state', 'started_at', 'updated_at', 'duration_ms', 'revision', 'omitted') if key in progress}
    if progress:
        if progress.get('reason') in ('cancelled', 'superseded'):
            public_progress['reason'] = progress['reason']
        public_progress['steps'] = [
            {key: deepcopy(step[key]) for key in
             ('id', 'kind', 'label', 'detail', 'status', 'started_at', 'duration_ms') if key in step}
            for step in progress.get('steps', []) if isinstance(step, dict)]
    return {'id': message.id, 'sender': user_to_dict(message.sender),
            'sender_type': message.sender_type, 'body': message.body,
            'created_at': message.created_at, 'status': message.status,
            'agent_card': cast(AgentCardDTO, asdict(card)) if card else None,
            'execution_progress': cast(ProgressDTO, public_progress)}


def task_to_dict(task: dict[str, Any]) -> TaskDTO:
    keys = ('id', 'trip_id', 'kind', 'request_id', 'retry_of', 'status', 'created_at', 'updated_at',
            'message_id', 'thinking_id', 'plan_id', 'requirements_revision')
    result = {key: deepcopy(task[key]) for key in keys if key in task}
    result['body'] = task.get('payload', {}).get('body', '')
    # Runtime failures can include upstream exception text; expose only a safe label.
    result['message'] = ('本次处理未完成，请重新发送消息。已有内容已保留。' if task.get('status') == 'failed'
                         else task.get('message', ''))
    return cast(TaskDTO, result)


def plan_to_dict(plan: TripPlan) -> PlanDTO:
    keys = ('id', 'title', 'status', 'text', 'overview', 'constraints_met', 'pending_items', 'risks',
            'requirements_revision', 'display_status', 'display_source_hash')
    return cast(PlanDTO, {**{key: deepcopy(getattr(plan, key)) for key in keys},
            'days': [asdict(day) for day in plan.days], 'preparation': asdict(plan.preparation),
            'has_content': plan.has_content})


def requirements_values(trip: Trip) -> dict[str, str]:
    values = {}
    for key in CITY_FORM_FIELDS:
        value = getattr(trip.requirements, key)
        values[key] = '' if value is None else str(value)
    return values


def workspace_snapshot(trip_id: str, user: User) -> WorkspaceDTO:
    # One lock covers ownership and all related collections: polling cannot mix turns.
    with deps.store._lock:
        trip = deps.get_owned_trip_or_404(trip_id, user)
        from app.application.undo import metadata
        return {'queue': queue_to_dict(deps.store.get_queue(trip_id)), 'undo': metadata(deps.store, trip_id), 'trip': trip_to_dict(trip), 'plan': plan_to_dict(deps.store.plans[trip_id]),
                'messages': [message_to_dict(row) for row in deps.store.messages[trip_id]],
                'tasks': [task_to_dict(row) for row in deps.store.list_tasks(trip_id)],
                'metrics': cast(MetricsDTO, deepcopy(deps.store.trip_metrics(trip_id))),
                'display_state': cast(DisplayStateDTO, deepcopy(deps.store.get_display_state(trip_id)))}


def queue_to_dict(queue):
    return {**queue, 'current_task': task_to_dict(queue['current_task']) if queue['current_task'] else None,
            'waiting_tasks': [task_to_dict(row) for row in queue['waiting_tasks']]}
