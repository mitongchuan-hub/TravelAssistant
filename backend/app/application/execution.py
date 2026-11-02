from __future__ import annotations
import json
from copy import deepcopy
from hashlib import sha256
from dataclasses import asdict
from datetime import date, datetime
from threading import RLock
from contextlib import contextmanager
from uuid import uuid4
from app.domain.models import AgentAction, AgentCard, IdeaCard, Message, Trip, TripPlan, User
from app.domain.requirements import initial_requirements, update_requirements
from app.storage.state import next_id, IDEA_WALL_LIMIT, IDEA_ROTATIONS
from app.storage.persistence import save_snapshot
from app.storage.errors import TurnPersistenceError

# Compatibility seam used by the preserved workflow commit implementation and
# its fault-injection tests. The callable is intentionally module-owned.
'Persistent execution-task lifecycle for the single-process travel store.'
from copy import deepcopy
from datetime import datetime, timezone
import os
from uuid import uuid4
from app.domain.message import normalize_message_text
from app.domain.itinerary.identity import PlanBasis, plan_basis
from app.services.itinerary.plan_display import text_hash
from app.storage.legacy.workflow import advance_request, normalize_workflow
from app.application.progress import advance_progress, finish_progress
ACTIVE = {'queued', 'running'}
BASIS_FIELDS = {'expected_plan_id', 'source_hash', 'expected_requirements_revision'}
EXECUTION_STRATEGIES = {'task_execution'}

def now():
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

from app.workflow.tasks import TaskConflict

def validate_task_basis(plan, requirements_revision, payload):
    """Legacy callers may omit the basis; a supplied basis is one indivisible tuple."""
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    present = BASIS_FIELDS.intersection(payload)
    if not present:
        return False
    if present != BASIS_FIELDS:
        raise ValueError('行程基线须同时包含计划、正文和需求版本，请刷新后重试')
    if not isinstance(payload['expected_plan_id'], str) or not payload['expected_plan_id'] or (not isinstance(payload['source_hash'], str)) or (not payload['source_hash']) or (type(payload['expected_requirements_revision']) is not int) or (payload['expected_requirements_revision'] < 0):
        raise ValueError('行程基线格式不正确，请刷新后重试')
    if payload['expected_plan_id'] != plan.id or payload['source_hash'] != text_hash(plan.text) or payload['expected_requirements_revision'] != requirements_revision:
        raise TaskConflict('需求或行程已变化，请刷新后基于最新内容提交')
    return True

def resolve_display_target(plan, payload):
    """Resolve only server-saved, prose-bound display data, never client labels or IDs."""
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    if 'display_target' not in payload:
        return None
    if 'item_id' in payload:
        raise ValueError('展示目标与旧行程条目不能同时指定')
    if not BASIS_FIELDS.issubset(payload):
        raise ValueError('指定展示目标时必须提供完整行程基线')
    target = payload['display_target']
    if not isinstance(target, dict) or set(target) != {'day_index', 'item_index'} or any((type(value) is not int or value < 0 for value in target.values())):
        raise ValueError('展示目标须为非负整数 day_index 和 item_index')
    if not plan.text or plan.display_status != 'ready' or (not isinstance(plan.display, dict)) or (plan.display_source_hash != text_hash(plan.text)) or (plan.display_source_hash != payload['source_hash']):
        raise TaskConflict('展示安排已变化或尚未就绪，请刷新后重试')
    try:
        days = plan.display['days']
        if not isinstance(days, list):
            raise TypeError()
        day = days[target['day_index']]
        items = day['items']
        if not isinstance(items, list):
            raise TypeError()
        item = items[target['item_index']]
        if not isinstance(day, dict) or not isinstance(item, dict):
            raise TypeError()
    except (IndexError, KeyError, TypeError):
        raise TaskConflict('目标安排已变化，请刷新行程') from None

    def label(value, limit):
        return value.strip()[:limit] if isinstance(value, str) else None
    if not label(item.get('title'), 200):
        raise TaskConflict('目标安排缺少有效标题，请刷新行程')
    return {**target, 'day_label': label(day.get('label'), 80), 'date': label(day.get('date'), 32), 'title': label(item.get('title'), 200), 'time_text': label(item.get('time_text'), 120), 'location': label(item.get('location'), 240)}

def validate_display_target_metadata(plan, metadata):
    """Internal callers must carry the resolved metadata, not invent a display target."""
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    fields = {'day_index', 'item_index', 'day_label', 'date', 'title', 'time_text', 'location'}
    if plan is None or not isinstance(metadata, dict) or set(metadata) != fields:
        raise ValueError('展示目标必须由当前行程解析，不能直接提供目标文字')
    current = resolve_display_target(plan, {'display_target': {key: metadata[key] for key in ('day_index', 'item_index')}, 'expected_plan_id': plan.id, 'source_hash': text_hash(plan.text), 'expected_requirements_revision': plan.requirements_revision})
    if metadata != current:
        raise TaskConflict('目标安排已变化，请刷新行程')
    return current

def _configured_strategy(store):
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    return 'task_execution'

def _restore_tasks(current, before):
    """Restore task values without invalidating references held by worker threads."""
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    for task_id in list(current):
        if task_id not in before:
            del current[task_id]
    for task_id, saved in before.items():
        if task_id in current:
            current[task_id].clear()
            current[task_id].update(deepcopy(saved))
        else:
            current[task_id] = deepcopy(saved)

def submit_task(self, trip_id, kind, payload, request_id):
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    if kind != 'chat':
        raise ValueError('任务类型不正确')
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
        raise ValueError('请求标识不正确，请刷新后重试')
    if not isinstance(payload, dict):
        raise ValueError('任务内容必须为对象')
    payload = deepcopy(payload)
    payload['body'] = normalize_message_text(payload.get('body'))
    if set(payload) != {'body'}:
        raise ValueError('聊天任务只接受正文')
    with self._lock:
        for task in self.tasks.values():
            if task['trip_id'] == trip_id and task['request_id'] == request_id:
                if (task['kind'], task['payload']) != (kind, payload):
                    raise TaskConflict('同一请求标识不能用于不同内容')
                return (deepcopy(task), False)
        active = next((task for task in self.tasks.values() if task['trip_id'] == trip_id and task['status'] in ACTIVE), None)
        trip = self.trips[trip_id]
        current_basis = plan_basis(self.plans[trip_id])
        workflow = normalize_workflow(trip.workflow_state, trip=trip, trip_id=trip_id, messages=self.messages[trip_id], plan_basis=current_basis)
        strategy = _configured_strategy(self)
        next_workflow = advance_request(workflow)
        before_tasks = deepcopy(self.tasks)
        before_messages = deepcopy(self.messages[trip_id])
        before_trip = deepcopy(trip)
        previous_defer = getattr(self, '_defer_persist', 0)
        try:
            if active:
                for previous in self.tasks.values():
                    if previous['trip_id'] == trip_id and previous['status'] in ACTIVE:
                        previous.update(status='superseded', message='已由新消息替代', updated_at=now(), result_url='')
                        thinking = previous.get('thinking_id')
                        for row in self.messages[trip_id]:
                            if row.id == thinking:
                                row.execution_progress = finish_progress(row.execution_progress, 'superseded')
                                row.status, row.body = ('superseded', '这次处理已由你的新消息替代。')
            trip.workflow_state = deepcopy(next_workflow)
            self._defer_persist = previous_defer + 1
            user_message, assistant_message = self.queue_user_message(trip_id, payload['body'], trip.owner.id)
            user_message_id, assistant_message_id = (user_message.id, assistant_message.id)
            self._defer_persist = previous_defer
            task_id = uuid4().hex
            conversation_task_id = next_workflow['active_question']['task_id'] if next_workflow.get('active_question') else None
            task = {'id': task_id, 'trip_id': trip_id, 'kind': kind, 'payload': payload, 'request_id': request_id, 'status': 'queued', 'message': '等待处理', 'created_at': now(), 'updated_at': now(), 'message_id': user_message_id, 'thinking_id': assistant_message_id, 'result_url': '', 'plan_id': self.plans[trip_id].id, 'plan_basis': current_basis.to_dict(), 'requirements_revision': trip.requirements.revision, 'workflow_revision': next_workflow['revision'], 'sequence': next_workflow['sequence'], 'epoch': next_workflow['epoch'], 'conversation_task_id': conversation_task_id, 'execution_strategy': strategy}
            self.tasks[task_id] = task
            self._persist()
        except Exception:
            self._defer_persist = previous_defer
            _restore_tasks(self.tasks, before_tasks)
            self.messages[trip_id] = before_messages
            self.trips[trip_id] = before_trip
            raise
        return (deepcopy(task), True)

def cancel_task(self, trip_id, task_id):
    """Invalidate an execution cooperatively, publishing only after persistence."""
    from app.storage.legacy.workflow import bump_epoch
    from app.storage.transactions import _stage_turn, _persist_staged_turn
    with self._lock:
        task = self.tasks.get(task_id)
        if not task or task.get('trip_id') != trip_id:
            return None
        if task['status'] not in ACTIVE:
            return deepcopy(task)
        stage = _stage_turn(self, trip_id)
        target = stage.tasks[task_id]
        target.update(status='superseded', message='已终止，已有内容已保留', updated_at=now(), result_url='')
        stage.trips[trip_id].workflow_state = bump_epoch(stage.trips[trip_id].workflow_state)
        for row in stage.messages[trip_id]:
            if row.id == target.get('thinking_id'):
                row.status, row.body = 'superseded', '本次处理已终止，消息、需求和行程已保留。'
                row.execution_progress = finish_progress(row.execution_progress, 'superseded')
        _persist_staged_turn(self, stage, trip_id)
        return deepcopy(self.tasks[task_id])


def get_task(self, task_id, *, trip_id=None):
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    with self._lock:
        task = self.tasks.get(task_id)
        if not task or (trip_id is not None and task.get('trip_id') != trip_id):
            return None
        return deepcopy(task)

def list_tasks(self, trip_id):
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    with self._lock:
        return deepcopy([task for task in self.tasks.values() if task['trip_id'] == trip_id][-20:])

def record_task_progress(self, task_id, title, value):
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    with self._lock:
        task = self.tasks.get(task_id)
        if not task or task['status'] != 'running':
            return
        message = next((row for row in self.messages.get(task['trip_id'], []) if row.id == task.get('thinking_id') and row.status == 'thinking'), None)
        if message is None:
            return
        progress = advance_progress(message.execution_progress, title, value)
        if progress is None:
            return
        previous = message.execution_progress
        message.execution_progress = progress
        try:
            _fn__persist(self)
        except Exception:
            message.execution_progress = previous

def run_task(self, task_id):
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    with self._lock:
        task = self.tasks.get(task_id)
        if not task or task['status'] != 'queued':
            return None
        previous = (task['status'], task['message'], task['updated_at'])
        task['status'], task['message'], task['updated_at'] = ('running', '正在处理，请稍候；可以离开页面，回来查看结果。', now())
        try:
            _fn__persist(self)
        except Exception:
            task['status'], task['message'], task['updated_at'] = previous
            raise
        trip_id = task['trip_id']
    self._task_context.task_id = task_id
    try:
        with _agent_lock(self, trip_id):
            return execute_turn(self, task_id)
    except Exception as exc:
        from app.storage.errors import TurnPersistenceError
        if isinstance(exc, TurnPersistenceError):
            return None
        try:
            return fail_execution(self, task_id, exc)
        except TurnPersistenceError:
            return None
    finally:
        if hasattr(self._task_context, 'task_id'):
            del self._task_context.task_id
        _fn__notify_display_worker(self)

def recover_tasks(self):
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    changed = False
    for task in self.tasks.values():
        if task['status'] in ACTIVE:
            task['status'], task['updated_at'] = ('failed', now())
            task['message'] = '本次处理未完成，请重新发送消息。已有内容已保留。'
            changed = True
    if changed:
        _fn__persist(self)
from app.storage.errors import TurnPersistenceError

def capture_request_context(self, trip_id, task_id=None, *, body=None, entry='main', request_id=None, message_id=None):
    """Build a trusted execution snapshot without conflating ID roles.

    The compatibility ``message_id`` argument always identifies the original
    user message. The task's ``thinking_id`` is the assistant placeholder and
    is not used as the authorization message.
    """
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    from app.domain.itinerary.identity import PlanBasis, plan_basis
    from app.application.contracts import RequestContext
    from app.storage.legacy.workflow import normalize_workflow
    with self._lock:
        trip = self.trips[trip_id]
        task = self.tasks.get(task_id) if task_id else None
        workflow = normalize_workflow(trip.workflow_state, trip=trip, messages=self.messages[trip_id], plan=self.plans[trip_id])
        kind = task['kind'] if task else None
        if kind:
            entry = 'main'
        payload = task['payload'] if task else {}
        source = body if body is not None else payload.get('body', payload.get('feedback', ''))
        if kind == 'plan' and body is None:
            source = next((row.body for row in self.messages[trip_id] if row.id == task.get('message_id')), '')
        user_message_id = message_id or (task.get('message_id') if task else None) or 'operation-' + uuid4().hex
        execution_task_id = task_id or 'operation-' + uuid4().hex
        request_key = request_id or (task['request_id'] if task else uuid4().hex)
        return RequestContext(trip_id=trip_id, owner_id=trip.owner.id, execution_task_id=execution_task_id, conversation_task_id=task.get('conversation_task_id') if task else None, request_id=request_key, message_id=user_message_id, body=source or '', entry=entry, strategy=task.get('execution_strategy', 'task_execution') if task else 'task_execution', requirements_revision=task['requirements_revision'] if task else trip.requirements.revision, plan_basis=PlanBasis.from_value(task['plan_basis']) if task and task.get('plan_basis') else plan_basis(self.plans[trip_id]), workflow_revision=task.get('workflow_revision', workflow['revision']) if task else workflow['revision'], sequence=task.get('sequence', workflow['sequence']) if task else workflow['sequence'], epoch=task.get('epoch', workflow['epoch']) if task else workflow['epoch'])

def _turn_conflict(self, context):
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    from app.domain.itinerary.identity import plan_basis
    from app.storage.legacy.workflow import normalize_workflow
    trip = self.trips.get(context.trip_id)
    if trip is None or trip.owner.id != context.owner_id:
        return '请求不属于当前旅行。'
    task = self.tasks.get(context.execution_task_id)
    if task and (task['status'] != 'running' or task['trip_id'] != context.trip_id or task['request_id'] != context.request_id or (task.get('execution_strategy', 'task_execution') != context.strategy) or (task.get('message_id') != context.message_id) or (task.get('requirements_revision') != context.requirements_revision) or (task.get('workflow_revision') != context.workflow_revision) or (task.get('sequence') != context.sequence) or (task.get('epoch') != context.epoch) or (task.get('plan_basis') != context.plan_basis.to_dict())):
        return '本次请求已被替代或取消。'
    if task and (not any((message.id == context.message_id and message.sender_type == 'user' and (message.sender.id == context.owner_id) and (message.body == context.body) for message in self.messages[context.trip_id]))):
        return '原始用户消息已失效，请重新发送请求。'
    state = normalize_workflow(trip.workflow_state, trip=trip, messages=self.messages[context.trip_id], plan=self.plans[context.trip_id])
    if (state['sequence'], state['epoch'], state['revision']) != (context.sequence, context.epoch, context.workflow_revision):
        return '会话目标或追问已变化，请基于最新上下文重试。'
    if trip.requirements.revision != context.requirements_revision:
        return '旅行需求在处理期间已更新，请基于最新需求重试。'
    if plan_basis(self.plans[context.trip_id]) != context.plan_basis:
        return '行程在处理期间已变化，请基于最新行程重试。'
    return None

def _receipt(self, context, *, committed, accepted=(), rejected=(), message_id=None, message_status='', task_status='', reason='', stage=None):
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    from app.application.contracts import CommitReceipt
    from app.domain.itinerary.identity import plan_basis
    from app.storage.legacy.workflow import normalize_workflow
    source = stage or self
    trip = source.trips[context.trip_id]
    workflow = normalize_workflow(trip.workflow_state)
    return CommitReceipt(trip_id=context.trip_id, execution_task_id=context.execution_task_id, request_id=context.request_id, committed=committed, accepted_artifact_ids=tuple(accepted), rejected_artifacts=tuple(rejected), after_requirements_revision=trip.requirements.revision, after_plan_basis=plan_basis(source.plans[context.trip_id]), after_workflow_revision=workflow['revision'], after_sequence=workflow['sequence'], after_epoch=workflow['epoch'], message_id=message_id, message_status=message_status, task_status=task_status, reason=reason, conversation_task_states={row['id']: row['status'] for row in workflow['tasks']})

def execute_turn(self, task_id):
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    from app.agents import entrypoints
    from app.agents.inputs.execution import build_execution_context
    with self._lock:
        task = self.tasks[task_id]
        if task['status'] != 'running':
            return None
        trip_id = task['trip_id']
        context = capture_request_context(self, trip_id, task_id)
        payload = task['payload']
        display_target = resolve_display_target(self.plans[trip_id], payload)
        execution = build_execution_context(trip=self.trips[trip_id], plan=self.plans[trip_id], messages=self.messages[trip_id], idea_cards=self.idea_cards[trip_id], metrics=_fn_trip_metrics(self, trip_id), current_date=datetime.now().date().isoformat(), entry=context.entry, payload=payload, display_target=display_target)
        conflict = _turn_conflict(self, context)
    if conflict:
        return fail_execution(self, task_id, ValueError(conflict))
    cancelled = lambda: not _execution_current(self, context)
    proposal = entrypoints.generate_task_result(context, execution, cancelled=cancelled, on_progress=lambda title, value: self.record_task_progress(task_id, title, value))
    receipt = commit_turn(self, context, proposal)
    if not receipt.committed and self.tasks.get(task_id, {}).get('status') == 'running':
        return fail_execution(self, task_id, ValueError(receipt.reason))
    return receipt

def _execution_current(self, context):
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    with self._lock:
        task = self.tasks.get(context.execution_task_id)
        return bool(task and task['status'] == 'running' and (not _turn_conflict(self, context)))

def confirm_current_turn(self, trip_id):
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    from app.domain.changes import prepare_changes
    from app.application.contracts import ExecutionResult
    from types import SimpleNamespace
    with self._lock:
        if not self.plans[trip_id].has_content:
            return None
        context = capture_request_context(self, trip_id, entry='confirm', body='确认当前已显示的行程')
        execution = SimpleNamespace(trip=deepcopy(self.trips[trip_id]), plan=deepcopy(self.plans[trip_id]), idea_cards=deepcopy(self.idea_cards[trip_id]), display_target=None)
        changes = prepare_changes({'user_quote': context.body, 'itinerary': {'action': 'confirm'}}, context, execution)
    return commit_turn(self, context, ExecutionResult(changes=changes))

def fail_execution(self, task_id, exc):
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    from app.storage.legacy.workflow import normalize_workflow
    from app.application.contracts import ExecutionResult
    if isinstance(exc, TurnPersistenceError):
        return None
    with self._lock:
        task = self.tasks.get(task_id)
        if not task or task['status'] != 'running':
            return None
        context = capture_request_context(self, task['trip_id'], task_id)
        conflict = _turn_conflict(self, context)
        if conflict:
            stage = _fn__stage_turn(self, task['trip_id'])
            target = stage.tasks[task_id]
            target.update(status='failed', message=conflict, updated_at=now(), result_url='')
            for row in stage.messages[task['trip_id']]:
                if row.id == task.get('thinking_id'):
                    row.status, row.body = ('failed', conflict)
                    row.execution_progress = finish_progress(row.execution_progress, 'failed')
            receipt = _receipt(self, context, committed=False, task_status='failed', reason=conflict, stage=stage)
            from app.application.contracts import serialize
            target['receipt'] = serialize(receipt)
            _fn__persist_staged_turn(self, stage, task['trip_id'])
            return receipt
        reason = str(exc) if isinstance(exc, ValueError) else '本次处理未完成，请重新发送消息。已有内容已保留。'
        proposal = ExecutionResult.failure(reason)
    return commit_turn(self, context, proposal)

def commit_turn(self, context, proposal):
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    from app.application.contracts import ExecutionResult, CommitReceipt, serialize
    from app.storage.legacy.workflow import normalize_workflow
    if not isinstance(proposal, ExecutionResult):
        raise ValueError('执行结果类型不正确')
    proposal = deepcopy(proposal)
    proposal.__post_init__()
    submission_digest = sha256(json.dumps({'context': serialize(context), 'result': serialize(proposal)}, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    with self._lock:
        existing = self.tasks.get(context.execution_task_id, {})
        if context.trip_id not in self.trips:
            return CommitReceipt(trip_id=context.trip_id, execution_task_id=context.execution_task_id, request_id=context.request_id, committed=False, task_status='failed', reason='旅行已删除，本次结果未保存')
        if self.trips[context.trip_id].owner.id != context.owner_id:
            raise ValueError('请求不属于当前旅行所有者')
        if existing.get('receipt') and existing.get('request_id') == context.request_id and (existing.get('trip_id') == context.trip_id) and (existing.get('message_id') == context.message_id) and (existing.get('sequence') == context.sequence) and (existing.get('epoch') == context.epoch):
            from app.application.contracts import CommitReceipt
            if existing.get('submission_digest') != submission_digest:
                raise ValueError('同一请求身份不能提交不同的内容摘要')
            raw = deepcopy(existing['receipt'])
            return CommitReceipt(**raw)
        conflict = _turn_conflict(self, context)
        if conflict:
            return _receipt(self, context, committed=False, task_status=existing.get('status', 'failed'), reason=conflict)
        stage = _fn__stage_turn(self, context.trip_id)
        trip_id = context.trip_id
        trip = stage.trips[trip_id]
        from app.application.undo import capture, record_change, manual_barrier
        undo_before = capture(stage, trip_id)
        crud_lines, crud_kinds, crud_payload = ([], [], None)
        if proposal.changes is not None:
            from app.application.changes import apply_changes
            crud_payload = proposal.changes
            crud_lines, crud_kinds = apply_changes(stage, context, crud_payload)
            trip = stage.trips[trip_id]
        workflow = normalize_workflow(trip.workflow_state)
        if (workflow['sequence'], workflow['epoch']) != (context.sequence, context.epoch):
            raise ValueError('执行结果不能改写请求代次')
        workflow['revision'] = context.workflow_revision + 1
        trip.workflow_state = workflow
        message_id = existing.get('thinking_id') or 'm-' + uuid4().hex
        effect_lines = list(crud_lines)
        parts = effect_lines + ([proposal.answer.strip()] if proposal.answer.strip() else [])
        if crud_payload:
            if 'itinerary' in crud_kinds:
                parts.append(crud_payload['result_text'])
            if crud_payload['args'].get('next_question'):
                parts.append(crud_payload['args']['next_question'])
        failed = proposal.failed
        reason = proposal.reason
        if failed:
            parts.append('本次处理未完成，请重新发送消息。已有内容已保留。')
        body = '\n\n'.join(parts) or '本轮已结束，当前行程未修改。'
        status = 'failed' if failed else 'sent'
        next_question = crud_payload['args'].get('next_question', '') if crud_payload else ''
        needs_input = bool(next_question)
        has_itinerary = 'itinerary' in crud_kinds
        card = AgentCard('missing-info' if needs_input or failed else 'itinerary-draft' if has_itinerary else 'requirement-summary', '需要补充' if needs_input else '旅行助手', body[:120], [], [AgentAction('查看攻略', 'itinerary')] if has_itinerary else [AgentAction('继续聊天', 'chat')])
        message = Message(message_id, self.users['agent'], 'agent', body, datetime.now().strftime('%H:%M'), status, card)
        messages = stage.messages[trip_id]
        index = next((index for index, row in enumerate(messages) if row.id == message_id), None)
        if index is None:
            messages.append(message)
        else:
            message.execution_progress = finish_progress(messages[index].execution_progress, 'failed' if failed else 'needs_input' if needs_input else 'succeeded')
            messages[index] = message
        trip.last_activity = 'Agent 等待补充信息' if needs_input else 'Agent 本轮部分完成' if failed else 'Agent 已完成本轮请求'
        task_status = 'failed' if failed else 'succeeded'
        receipt = _receipt(self, context, committed=True, message_id=message_id, message_status=status, task_status=task_status, reason=reason, stage=stage)
        if context.execution_task_id in stage.tasks:
            from app.application.contracts import serialize
            task = stage.tasks[context.execution_task_id]
            task.update(status=task_status, message=next_question or (reason if failed else '已完成'), needs_input=needs_input, updated_at=now(), receipt=serialize(receipt), committed_effect_kinds=crud_kinds, submission_digest=submission_digest, result_url=f'/workspace/{trip_id}?tab=' + ('itinerary' if has_itinerary else 'chat'))
        if context.entry == 'confirm' or (failed and crud_kinds):
            manual_barrier(stage, trip_id)
        elif not failed and crud_kinds:
            record_change(stage, trip_id, undo_before, message_id)
        _fn__persist_staged_turn(self, stage, trip_id)
    if has_itinerary:
        _fn__notify_display_worker(self)
    return receipt

def clear_chat(self, trip_id: str) -> None:
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    from app.storage.legacy.workflow import clear_workflow
    with self._lock:
        stage = _fn__stage_turn(self, trip_id)
        stage.trips[trip_id].workflow_state = clear_workflow(stage.trips[trip_id].workflow_state)
        for task in stage.tasks.values():
            if task['trip_id'] == trip_id and task['status'] in {'queued', 'running'}:
                task.update(status='superseded', message='聊天已清空，本轮已取消', result_url='')
        stage.messages[trip_id] = []
        _fn_cancel_display_jobs(stage, trip_id)
        stage.trips[trip_id].last_activity = '聊天记录已清空'
        _fn__persist_staged_turn(self, stage, trip_id)
    _fn__notify_display_worker(self)

def add_user_message(self, trip_id: str, body: str, sender_id: str='u1') -> Message:
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    with self._lock:
        clean_body = body.strip()
        owner = self.trips[trip_id].owner
        sender = self.users.get(sender_id, owner)
        if sender.id != owner.id:
            sender = owner
        message = Message(id=f'm-{next_id(self)}', sender=sender, sender_type='user', body=clean_body, created_at=_now_label())
        self.messages[trip_id].append(message)
        self.trips[trip_id].last_activity = '已发送消息，Agent 正在思考'
        _fn__persist(self)
    return message

def queue_user_message(self, trip_id: str, body: str, sender_id: str) -> tuple[Message, Message]:
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    message = add_user_message(self, trip_id, body, sender_id)
    return (message, start_agent_task(self, trip_id))

def start_agent_task(self, trip_id: str) -> Message:
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    from app.application.progress import new_progress
    with self._lock:
        thinking_message = Message(id=f'm-{next_id(self)}', sender=self.users['agent'], sender_type='agent', body='思考中 •••', created_at=_now_label(), status='thinking', execution_progress=new_progress('queued'))
        self.messages[trip_id].append(thinking_message)
        task_id = getattr(self._task_context, 'task_id', None)
        if task_id:
            self.tasks[task_id]['thinking_id'] = thinking_message.id
        self.trips[trip_id].last_activity = 'Agent 正在整理旅行信息'
        _fn__persist(self)
        return thinking_message

def _task_current(self) -> bool:
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    task_id = getattr(self._task_context, 'task_id', None)
    with self._lock:
        return task_id is None or self.tasks.get(task_id, {}).get('status') == 'running'

@contextmanager
def _agent_lock(self, trip_id: str):
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    with _fn__foreground_operation(self, trip_id):
        if getattr(self._task_context, 'task_id', None):
            yield
        else:
            with self._lock:
                lock = self._agent_locks.setdefault(trip_id, RLock())
            with lock:
                yield

def fail_agent_task(self, trip_id: str, thinking_message_id: str | None=None) -> None:
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    with self._lock:
        if not _task_current(self):
            return
        error_message = Message(id=f'm-{next_id(self)}', sender=self.users['agent'], sender_type='agent', status='failed', body='本次处理未完成，请重新发送消息。已有内容已保留。', created_at=_now_label(), agent_card=AgentCard(kind='missing-info', title='Agent 暂时没有回复成功', summary='你的消息已经保留，这次处理没有完成。', bullets=['消息不会丢失', '可以重新发送上一条消息', '也可以先继续补充旅行想法'], actions=[AgentAction(label='继续聊天', target='chat')]))
        if thinking_message_id:
            for index, message in enumerate(self.messages[trip_id]):
                if message.id == thinking_message_id:
                    from app.application.progress import finish_progress
                    error_message.execution_progress = finish_progress(message.execution_progress, 'failed')
                    self.messages[trip_id][index] = error_message
                    break
            else:
                self.messages[trip_id].append(error_message)
        else:
            self.messages[trip_id].append(error_message)
        self.trips[trip_id].last_activity = 'Agent 整理失败，等待新消息'
        _fn__persist(self)

def confirm_plan(self, trip_id: str):
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    return confirm_current_turn(self, trip_id)

def _apply_text_plan(self, trip_id: str, text: str, evidence: list[dict] | None=None, *, display=None, display_source_hash='', display_status='not_requested', preserve_plan_basis=False) -> None:
    from app.application.guide import _discard_display_jobs as _fn__discard_display_jobs
    from app.application.guide import _foreground_operation as _fn__foreground_operation
    from app.application.guide import _notify_display_worker as _fn__notify_display_worker
    from app.application.guide import _queue_display as _fn__queue_display
    from app.application.guide import cancel_display_jobs as _fn_cancel_display_jobs
    from app.application.workspace import trip_metrics as _fn_trip_metrics
    from app.storage.state import _persist as _fn__persist
    from app.storage.transactions import _persist_staged_turn as _fn__persist_staged_turn
    from app.storage.transactions import _stage_turn as _fn__stage_turn
    if not isinstance(text, str) or not text.strip():
        raise ValueError('行程内容不能为空')
    from app.services.itinerary.plan_display import validate_display, text_hash, dates_from_snapshot
    trip, previous = (self.trips[trip_id], self.plans[trip_id])
    requirements_snapshot = deepcopy(previous.requirements_snapshot) if preserve_plan_basis else asdict(trip.requirements)
    requirements_revision = previous.requirements_revision if preserve_plan_basis else trip.requirements.revision
    lodging_snapshot = deepcopy(previous.lodging_snapshot if preserve_plan_basis else trip.lodging)
    if display is not None:
        try:
            if display_source_hash != text_hash(text):
                raise ValueError('展示数据与原文不对应')
            display = validate_display(display, dates_from_snapshot(requirements_snapshot))
        except Exception:
            display, display_status = (None, 'fallback')
    self.plans[trip_id] = TripPlan(id=f'plan-{next_id(self)}', title=display['title'] if display else f'{self.trips[trip_id].requirements.city or self.trips[trip_id].destination}行程', status='草案', text=text, evidence=deepcopy(evidence or []), requirements_revision=requirements_revision, display=deepcopy(display), display_source_hash=display_source_hash, display_status=display_status, display_style=previous.display_style, requirements_snapshot=requirements_snapshot, lodging_snapshot=lodging_snapshot)
    self.trips[trip_id].status = '草案'
    _fn__discard_display_jobs(self, trip_id)
    if display_status == 'pending':
        _fn__queue_display(self, trip_id)
