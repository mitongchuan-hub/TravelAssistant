"""Pure validation and staged application of one direct-agent CRUD transaction.

A user quote binds the request, not its intent. Destructive and textual operations
also require positive, current instructions; uncertain commands fail closed.
"""
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json
import re
from types import SimpleNamespace
from uuid import uuid4

from app.domain.itinerary.identity import plan_basis
from app.application.contracts import serialize
from app.domain.models import IdeaCard, TripPlan
from app.services.maps.evidence import evidence_kind, evidence_result, place_messages
from app.domain.itinerary.prerequisites import planning_brief
from app.domain.requirements import chat_changes, update_requirements


from app.domain.changes import prepare_changes, canonical_digest, _idea_hash

def apply_changes(stage, context, payload):
    """Revalidate the same request against current private stage, then apply all effects.

    Caller must discard ``stage`` on any failure and persist before publishing it.
    Returns deterministic (effect_lines, changed_kinds).
    """
    payload = serialize(payload)
    unsigned = {key: value for key, value in payload.items() if key != 'digest'}
    if payload.get('digest') != canonical_digest(unsigned):
        raise ValueError('CRUD候选摘要不一致')
    trip_id = context.trip_id
    cards = stage.idea_cards[trip_id]
    current = {card.id: card for card in cards}
    if payload.get('idea_ids') is not None and sorted(current) != payload['idea_ids']:
        raise ValueError('想法墙在生成期间已变化，请重新审阅全部想法')
    for key, digest in payload['expected_idea_hashes'].items():
        if key not in current or _idea_hash(current[key]) != digest:
            raise ValueError('目标想法在处理期间已变化，请刷新后重试')
    task = stage.tasks.get(context.execution_task_id, {})
    target = None
    if task.get('payload', {}).get('display_target') is not None:
        from app.application.execution import resolve_display_target
        target = resolve_display_target(stage.plans[trip_id], task.get('payload', {}))
    execution = SimpleNamespace(trip=stage.trips[trip_id], plan=stage.plans[trip_id],
                                idea_cards=cards, display_target=target)
    verified = prepare_changes(payload['args'], context, execution, payload['evidence'])
    if verified != payload:
        raise ValueError('CRUD候选与当前原话、快照或页面目标不一致')
    args = payload['args']
    lines, kinds = [], []
    if payload['normalized_changes']:
        stage.trips[trip_id] = update_requirements(stage.trips[trip_id], payload['normalized_changes'])
        lines.append('需求已保存。')
        kinds.append('requirements')
    if args.get('lodging'):
        from app.domain.lodging import collect_places
        catalog = collect_places(place_messages(payload['evidence']))
        lodging = catalog[args['lodging']['poi_id']]
        trip = stage.trips[trip_id]
        stage.trips[trip_id] = update_requirements(trip, {
            'city': lodging['city'], 'lodging_address': lodging.get('address') or lodging['name']})
        stage.trips[trip_id].lodging = deepcopy(lodging)
        lines.append('住宿位置已匹配并保存。')
        kinds.append('lodging')
    for operation in args.get('ideas', []):
        action = operation['action']
        if action == 'create':
            cards.append(IdeaCard(id='idea-' + uuid4().hex, kind=operation['kind'],
                                  title=operation['title'], body=operation['body'], author='对话', status='已整理'))
        elif action == 'update':
            index = next(i for i, card in enumerate(cards) if card.id == operation['idea_id'])
            cards[index] = replace(cards[index], **operation['changes'])
        else:
            cards[:] = [card for card in cards if card.id != operation['idea_id']]
    if args.get('ideas'):
        lines.append('旅行想法已更新。')
        kinds.append('ideas')
    itinerary = args.get('itinerary', {})
    action = itinerary.get('action')
    if action in {'replace', 'edit_text'}:
        evidence = deepcopy(stage.plans[trip_id].evidence) + payload['evidence']
        stage._apply_text_plan(trip_id, payload['result_text'], evidence,
                               display_status='pending', preserve_plan_basis=action == 'edit_text')
        lines.append('行程已更新。')
        kinds.append('itinerary')
    elif action == 'confirm':
        stage.plans[trip_id].status = stage.trips[trip_id].status = '已确认'
        lines.append('当前行程已确认。')
        kinds.append('confirmation')
    elif action == 'clear':
        stage.plans[trip_id] = TripPlan(id='empty', title='还没有行程计划', status='未生成', days=[])
        stage.trips[trip_id].status = '未生成'
        stage._discard_display_jobs(trip_id)
        lines.append('当前行程已清空，旅行需求、想法和聊天记录已保留。')
        kinds.append('clear')
    return lines, kinds
