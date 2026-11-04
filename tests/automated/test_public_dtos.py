"""Public DTOs describe the existing whitelist without validating/coercing data."""
from copy import deepcopy
from dataclasses import asdict
from typing import Required, get_args, get_origin, get_type_hints, is_typeddict

import pytest

from app.api import dependencies as deps
from app.api import schemas, serializers
from app.domain.models import AgentAction, AgentCard, Message, Trip, TripDay, TripItem, TripPlan, User
from app.storage.store import DemoStore
from app.domain.summaries import legacy_summaries


USER = User('public-user', '旅行者', '旅')
PUBLIC_USER = {'id': 'public-user', 'name': '旅行者', 'initials': '旅'}


def message(**kwargs):
    return Message('message-1', USER, 'historical-sender', '原样正文', '19:12', **kwargs)


def test_user_trip_and_empty_lodging_exact_public_shapes():
    trip = Trip('trip-1', '杭州', USER,
                'historical-status', '刚刚', legacy_metadata=legacy_summaries('未定'), workflow_state={'private': 'secret'})
    assert serializers.user_to_dict(USER) == PUBLIC_USER
    assert serializers.trip_to_dict(trip) == {
        'id': 'trip-1', 'destination': '杭州', 'date_range': '未定', 'status': 'historical-status',
        'last_activity': '刚刚', 'mode': 'city', 'owner': PUBLIC_USER,
        'requirements': {
            'start_date': '', 'end_date': '', 'city': '',
            'day_count': None, 'transport_mode': 'unspecified', 'lodging_address': '', 'text': '', 'revision': 0,
        },
        'lodging': {},
    }
    public = serializers.trip_to_dict(trip)
    public['requirements']['lodging_address'] = 'changed'
    public['owner']['name'] = 'changed'
    assert trip.requirements.lodging_address == '' and trip.owner.name == '旅行者'


def test_place_whitelist_preserves_partial_data_and_copies_values():
    assert serializers.place_to_dict({}) == {}
    place = {
        'poi_id': 'p1', 'name': '酒店', 'address': '杭州', 'longitude': 120.1,
        'latitude': 30.2, 'city': '杭州', 'district': '西湖区', 'source': 'historical',
        'fetched_at': '', 'selected_at': '', 'place_type': '', 'opening_hours': '',
        'private': {'token': 'secret'},
    }
    assert serializers.place_to_dict(place) == {key: value for key, value in place.items() if key != 'private'}
    # Historical payloads are projected, not Pydantic-normalized.
    legacy = {'name': ['legacy-name'], 'longitude': None, 'private': 'secret'}
    public = serializers.place_to_dict(legacy)
    assert public == {'name': ['legacy-name'], 'longitude': None}
    public['name'].append('changed')
    assert legacy['name'] == ['legacy-name']


def test_message_empty_progress_and_nullable_agent_card_exact_shape():
    assert serializers.message_to_dict(message()) == {
        'id': 'message-1', 'sender': PUBLIC_USER, 'sender_type': 'historical-sender',
        'body': '原样正文', 'created_at': '19:12', 'status': 'sent',
        'agent_card': None, 'execution_progress': {},
    }


def test_message_progress_optional_steps_safe_whitelist_and_copies():
    progress = {
        'state': 'historical-state', 'started_at': 100, 'updated_at': 105,
        'duration_ms': 5, 'revision': 2, 'omitted': 1, 'private': 'secret',
        'steps': [
            {'id': 4, 'kind': 'historical-kind', 'label': '查询', 'detail': '公开说明',
             'status': 'historical-status', 'started_at': 100, 'duration_ms': 5,
             'raw_result': {'private': 'secret'}},
            'ignore-non-dict', None, {'label': '仅有标签'},
        ],
    }
    card = AgentCard('historical-card', '标题', '摘要', ['要点'], [AgentAction('查看', 'board')])
    row = message(execution_progress=progress, agent_card=card)
    before = deepcopy(progress)
    public = serializers.message_to_dict(row)
    assert public == {
        'id': 'message-1', 'sender': PUBLIC_USER, 'sender_type': 'historical-sender',
        'body': '原样正文', 'created_at': '19:12', 'status': 'sent',
        'agent_card': {'kind': 'historical-card', 'title': '标题', 'summary': '摘要',
                       'bullets': ['要点'], 'actions': [{'label': '查看', 'target': 'board'}]},
        'execution_progress': {
            'state': 'historical-state', 'started_at': 100, 'updated_at': 105,
            'duration_ms': 5, 'revision': 2, 'omitted': 1,
            'steps': [
                {'id': 4, 'kind': 'historical-kind', 'label': '查询', 'detail': '公开说明',
                 'status': 'historical-status', 'started_at': 100, 'duration_ms': 5},
                {'label': '仅有标签'},
            ],
        },
    }
    public['execution_progress']['steps'][0]['detail'] = 'changed'
    public['agent_card']['bullets'].clear()
    public['agent_card']['actions'][0]['label'] = 'changed'
    assert progress == before
    assert card.bullets == ['要点'] and card.actions[0].label == '查看'
    row.execution_progress = {'state': 'running'}
    assert serializers.message_to_dict(row)['execution_progress'] == {'state': 'running', 'steps': []}
    row.execution_progress = {'private': 'secret'}
    assert serializers.message_to_dict(row)['execution_progress'] == {'steps': []}


@pytest.mark.parametrize(('task', 'expected'), [
    ({}, {'message': ''}),
    ({'status': 'historical-status', 'kind': 'historical-kind'},
     {'status': 'historical-status', 'kind': 'historical-kind', 'message': ''}),
    ({'status': 'failed', 'message': 'upstream secret', 'exception': 'private'},
     {'status': 'failed', 'message': '本次处理未完成，请重新发送消息。已有内容已保留。'}),
    ({'id': 'task-1', 'trip_id': 'trip-1', 'kind': 'plan', 'request_id': 'request-1',
      'retry_of': None, 'status': 'succeeded', 'created_at': 'now', 'updated_at': 'later',
      'message_id': None, 'thinking_id': 'thinking-1', 'plan_id': None,
      'requirements_revision': None, 'message': '完成', 'payload': {'private': 'secret'}},
     {'id': 'task-1', 'trip_id': 'trip-1', 'kind': 'plan', 'request_id': 'request-1',
      'retry_of': None, 'status': 'succeeded', 'created_at': 'now', 'updated_at': 'later',
      'message_id': None, 'thinking_id': 'thinking-1', 'plan_id': None,
      'requirements_revision': None, 'message': '完成'}),
])
def test_task_projection_missing_keys_unknown_strings_and_safe_failure(task, expected):
    assert serializers.task_to_dict(task) == expected


def test_task_selected_values_are_deep_copied():
    task = {'request_id': {'legacy': ['value']}, 'payload': {'private': 'secret'}}
    public = serializers.task_to_dict(task)
    assert public == {'request_id': {'legacy': ['value']}, 'message': ''}
    public['request_id']['legacy'].append('changed')
    assert task['request_id'] == {'legacy': ['value']}


def test_historical_plan_exact_shape_nullable_fields_and_private_data_excluded():
    item = TripItem('i1', '10:00', '散步', '西湖', '1小时', '轻松', '', satisfies=['慢游'])
    plan = TripPlan('p1', '历史行程', 'historical-status',
                    days=[TripDay('d1', 'Day 1', [item])],
                    constraints_met=['慢游'], pending_items=['预约'], risks=['下雨'],
                    evidence=[{'secret': 'raw evidence'}], display={'secret': 'raw display'},
                    display_style='private-style', requirements_snapshot={'private': 1},
                    lodging_snapshot={'private': 2})
    public = serializers.plan_to_dict(plan)
    assert public == {
        'id': 'p1', 'title': '历史行程', 'status': 'historical-status', 'text': '',
        'overview': '', 'constraints_met': ['慢游'], 'pending_items': ['预约'], 'risks': ['下雨'],
        'requirements_revision': None, 'display_status': 'not_requested', 'display_source_hash': '',
        'days': [{'id': 'd1', 'label': 'Day 1', 'date': '', 'theme': '', 'items': [{
            'id': 'i1', 'time': '10:00', 'title': '散步', 'location': '西湖',
            'duration': '1小时', 'reason': '轻松', 'notes': '', 'satisfies': ['慢游'],
            'category': 'activity', 'meal': None, 'place': None, 'place_scope': 'unknown',
            'travel_mode': 'transit', 'incoming_route': None, 'departure_route': None,
        }]}],
        'preparation': {'clothing': [], 'accommodation': []}, 'has_content': True,
    }
    public['days'][0]['items'][0]['satisfies'].clear()
    public['preparation']['clothing'].append('changed')
    public['constraints_met'].clear()
    public['pending_items'].clear()
    public['risks'].clear()
    assert item.satisfies == ['慢游'] and plan.preparation.clothing == []
    assert plan.constraints_met == ['慢游'] and plan.pending_items == ['预约'] and plan.risks == ['下雨']
    assert serializers.plan_to_dict(TripPlan('empty', '', ''))['has_content'] is False
    assert serializers.plan_to_dict(TripPlan('text', '', '', text='  正文  '))['has_content'] is True


def test_workspace_top_level_shapes_and_detached_metrics_display_state(monkeypatch):
    store = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    monkeypatch.setattr(deps, 'store', store)
    monkeypatch.setattr(deps, 'get_owned_trip_or_404', lambda trip_id, user: store.trips[trip_id])
    public = serializers.workspace_snapshot('trip-hangzhou', USER)
    assert set(public) == {'trip', 'plan', 'messages', 'tasks', 'metrics', 'display_state', 'undo'}
    assert public['tasks'] == []
    assert public['trip']['requirements']['text'] == store.trips['trip-hangzhou'].requirements.text
    assert 'idea_cards' not in public and 'wall_cards' not in public
    assert set(public['metrics']) == schemas.MetricsDTO.__required_keys__
    assert set(public['display_state']) == schemas.DisplayStateDTO.__required_keys__
    assert isinstance(public['display_state']['requirements_revision'], int)
    for key in ('enabled', 'has_text', 'guide_available', 'busy'):
        assert type(public['display_state'][key]) is bool
    for key in set(public['display_state']) - {'enabled', 'has_text', 'guide_available', 'busy', 'requirements_revision'}:
        assert isinstance(public['display_state'][key], str)
    public['metrics']['message_count'] = -1
    public['display_state']['display_status'] = 'changed'
    assert store.trip_metrics('trip-hangzhou')['message_count'] >= 0
    assert store.get_display_state('trip-hangzhou')['display_status'] != 'changed'


def test_dto_static_metadata_models_optional_projection_and_nullable_legacy_fields():
    for name in ('UserDTO', 'SessionDTO', 'RequirementsDTO', 'PlaceDTO', 'AgentActionDTO',
                 'AgentCardDTO', 'ProgressStepDTO', 'ProgressDTO', 'MealDTO', 'RouteDTO',
                 'PlanItemDTO', 'PlanDayDTO', 'PreparationDTO', 'PlanDTO', 'TaskDTO',
                 'MetricsDTO', 'DisplayStateDTO', 'TripDTO', 'MessageDTO', 'WorkspaceDTO'):
        assert is_typeddict(getattr(schemas, name)), name
    for dto in (schemas.PlaceDTO, schemas.ProgressDTO, schemas.ProgressStepDTO):
        assert dto.__required_keys__ == frozenset()
        assert dto.__optional_keys__ == frozenset(get_type_hints(dto))
    assert schemas.TaskDTO.__required_keys__ == {'message'}
    assert schemas.TaskDTO.__optional_keys__ == set(get_type_hints(schemas.TaskDTO)) - {'message'}
    assert get_origin(get_type_hints(schemas.TaskDTO, include_extras=True)['message']) is Required
    assert get_type_hints(schemas.PlanDTO)['requirements_revision'] == int | None
    for field in ('meal', 'place', 'incoming_route', 'departure_route'):
        assert type(None) in get_args(get_type_hints(schemas.PlanItemDTO)[field])
    for dto, field in ((schemas.TaskDTO, 'status'), (schemas.TaskDTO, 'kind'),
                       (schemas.ProgressDTO, 'state'), (schemas.ProgressStepDTO, 'status'),
                       (schemas.PlanDTO, 'status'), (schemas.PlanDTO, 'display_status')):
        assert get_type_hints(dto)[field] is str
    assert get_type_hints(schemas.DisplayStateDTO)['requirements_revision'] is int
    for field in ('enabled', 'has_text', 'guide_available', 'busy'):
        assert get_type_hints(schemas.DisplayStateDTO)[field] is bool
    for function, dto in ((serializers.user_to_dict, schemas.UserDTO),
                          (serializers.place_to_dict, schemas.PlaceDTO),
                          (serializers.trip_to_dict, schemas.TripDTO),
                          (serializers.message_to_dict, schemas.MessageDTO),
                          (serializers.task_to_dict, schemas.TaskDTO),
                          (serializers.plan_to_dict, schemas.PlanDTO),
                          (serializers.workspace_snapshot, schemas.WorkspaceDTO)):
        assert get_type_hints(function)['return'] is dto
