"""Trip-local, bounded Agent undo. Snapshots are private and commits remain atomic."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
from hashlib import sha256
import json
from uuid import uuid4

from app.domain.models import Message, TripRequirements
from app.services.itinerary.freshness import plan_freshness
from app.services.itinerary.plan_display import text_hash, dates_from_snapshot
from app.services.itinerary.guide_html import validate_guide_display
from app.workflow.counters import normalize_workflow
from app.workflow.tasks import TaskConflict

FIELDS = ('status',)


def state(store, trip_id):
    return store.agent_undo.setdefault(trip_id, {'version': 0, 'entries': [], 'receipts': {}})


def capture(store, trip_id):
    trip, plan = store.trips[trip_id], store.plans[trip_id]
    raw = asdict(plan)
    # Never retain pending work or a display not bound to this exact source.
    raw.update(display=None, display_status='not_requested', display_source_hash='')
    if plan.text and plan.display_status == 'ready' and plan.display_source_hash == text_hash(plan.text):
        try:
            raw.update(display=validate_guide_display(plan.display, dates_from_snapshot(plan.requirements_snapshot)),
                       display_status='ready', display_source_hash=text_hash(plan.text))
        except (ValueError, TypeError, KeyError):
            pass
    return {'trip': {key: deepcopy(getattr(trip, key)) for key in FIELDS},
            'requirements': {'text': trip.requirements.text, 'revision': trip.requirements.revision},
            'plan': raw, 'freshness': plan_freshness(trip, plan)['basis_status']}


def fingerprint(store, trip_id):
    raw = capture(store, trip_id)
    for key in ('display', 'display_status', 'display_source_hash', 'display_style'):
        raw['plan'].pop(key, None)
    return sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def manual_barrier(store, trip_id):
    row = state(store, trip_id)
    row['version'] += 1
    row['entries'] = []


def _business_content(snapshot):
    raw = deepcopy(snapshot)
    raw['requirements'].pop('revision', None)
    for key in ('id', 'requirements_revision', 'display', 'display_status', 'display_source_hash', 'display_style'):
        raw['plan'].pop(key, None)
    snapshot = raw['plan'].get('requirements_snapshot')
    if isinstance(snapshot, dict):
        snapshot.pop('revision', None)
    return raw


def record_change(store, trip_id, before, message_id):
    if _business_content(before) == _business_content(capture(store, trip_id)):
        entries = state(store, trip_id)['entries']
        if entries:
            entries[-1]['expected_after'] = fingerprint(store, trip_id)
        return
    row = state(store, trip_id)
    row['version'] += 1
    row['entries'].append({'id': uuid4().hex, 'before': before,
                           'expected_after': fingerprint(store, trip_id), 'message_id': message_id})
    row['entries'] = row['entries'][-3:]


def metadata(store, trip_id):
    row = state(store, trip_id)
    entries = row['entries']
    active = any(t.get('trip_id') == trip_id and t.get('status') in {'queued', 'running'} for t in store.tasks.values())
    matches = bool(entries and entries[-1]['expected_after'] == fingerprint(store, trip_id))
    return {'available_count': len(entries) if matches else 0,
            'latest_entry_id': entries[-1]['id'] if matches else '',
            'message_id': entries[-1]['message_id'] if matches else '',
            'version': row['version'], 'can_undo': matches and not active,
            'reason': '请先终止当前任务' if active else '' if matches else '没有可撤销的 Agent 修改'}


def undo(store, trip_id, entry_id, request_id, expected_version):
    with store._lock:
        row = state(store, trip_id)
        payload = {'entry_id': entry_id, 'expected_version': expected_version}
        previous = row['receipts'].get(request_id)
        if previous:
            if previous['payload'] != payload:
                raise TaskConflict('同一请求标识不能用于不同撤销内容')
            return deepcopy(previous['result'])
        public = metadata(store, trip_id)
        if not public['can_undo'] or public['latest_entry_id'] != entry_id or row['version'] != expected_version:
            raise TaskConflict(public['reason'] or '旅行已变化，请刷新后再撤销')
        stage = store._stage_turn(trip_id)
        target = state(stage, trip_id)
        saved = target['entries'].pop()['before']
        trip = stage.trips[trip_id]
        revision = trip.requirements.revision + 1
        trip.status = saved['trip']['status']
        trip.requirements.text = saved['requirements']['text']
        trip.requirements.revision = revision
        from app.storage.persistence import _plan
        plan = _plan(saved['plan'], False)
        plan.id = 'plan-' + uuid4().hex
        if saved['freshness'] == 'fresh':
            plan.requirements_revision = revision
            plan.requirements_snapshot = asdict(trip.requirements)
        elif saved['freshness'] == 'stale' and isinstance(plan.requirements_snapshot, dict) and plan.requirements_snapshot:
            plan.requirements_revision = revision - 1
            plan.requirements_snapshot['revision'] = revision - 1
        if saved['freshness'] == 'unknown':
            plan.requirements_basis_complete = False
        if plan.display is not None:
            try:
                if plan.display_source_hash != text_hash(plan.text):
                    raise ValueError('网页来源不匹配')
                plan.display = validate_guide_display(plan.display, dates_from_snapshot(plan.requirements_snapshot))
            except (ValueError, TypeError, KeyError):
                plan.display, plan.display_status, plan.display_source_hash = None, 'not_requested', ''
        stage.plans[trip_id] = plan
        workflow = normalize_workflow(trip.workflow_state)
        workflow.update(revision=workflow['revision'] + 1, sequence=workflow['sequence'] + 1, epoch=workflow['epoch'] + 1)
        trip.workflow_state = workflow
        stage._discard_display_jobs(trip_id)
        if plan.text and plan.display_status != 'ready':
            plan.display_status = 'pending'
            stage._queue_display(trip_id)
        trip.last_activity = '已撤销上次 Agent 修改，聊天记录已保留'
        users = getattr(store, 'users', None)
        if users is None:
            from app.storage.store import USERS
            users = USERS
        message_id = 'm-' + uuid4().hex
        stage.messages[trip_id].append(Message(message_id, users['agent'], 'agent',
            '已撤销本次修改：恢复当时的需求正文和行程，固定创建资料不变；聊天记录已保留。', datetime.now().strftime('%H:%M')))
        target['version'] += 1
        if target['entries']:
            target['entries'][-1]['expected_after'] = fingerprint(stage, trip_id)
        result = {'undone_entry_id': entry_id, 'version': target['version'], 'message_id': message_id}
        target['receipts'][request_id] = {'payload': payload, 'result': result}
        store._persist_staged_turn(stage, trip_id)
    store._notify_display_worker()
    return deepcopy(result)
