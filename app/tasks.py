"""Persistent execution-task lifecycle for the single-process travel store."""
from copy import deepcopy
from datetime import datetime, timezone
import os
from uuid import uuid4

from .agent_protocol import PlanBasis, plan_basis
from .plan_display import text_hash
from .task_context import advance_request, normalize_workflow

ACTIVE = {'queued', 'running'}
BASIS_FIELDS = {'expected_plan_id', 'source_hash', 'expected_requirements_revision'}
EXECUTION_STRATEGIES = {'legacy', 'task_execution'}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class TaskConflict(ValueError):
    pass


def validate_task_basis(plan, requirements_revision, payload):
    """Legacy callers may omit the basis; a supplied basis is one indivisible tuple."""
    present = BASIS_FIELDS.intersection(payload)
    if not present:
        return False
    if present != BASIS_FIELDS:
        raise ValueError('行程基线须同时包含计划、正文和需求版本，请刷新后重试')
    if (not isinstance(payload['expected_plan_id'], str) or not payload['expected_plan_id']
            or not isinstance(payload['source_hash'], str) or not payload['source_hash']
            or type(payload['expected_requirements_revision']) is not int
            or payload['expected_requirements_revision'] < 0):
        raise ValueError('行程基线格式不正确，请刷新后重试')
    if (payload['expected_plan_id'] != plan.id or payload['source_hash'] != text_hash(plan.text)
            or payload['expected_requirements_revision'] != requirements_revision):
        raise TaskConflict('需求或行程已变化，请刷新后基于最新内容提交')
    return True


def resolve_display_target(plan, payload):
    """Resolve only server-saved, prose-bound display data, never client labels or IDs."""
    if 'display_target' not in payload:
        return None
    if 'item_id' in payload:
        raise ValueError('展示目标与旧行程条目不能同时指定')
    if not BASIS_FIELDS.issubset(payload):
        raise ValueError('指定展示目标时必须提供完整行程基线')
    target = payload['display_target']
    if (not isinstance(target, dict) or set(target) != {'day_index', 'item_index'}
            or any(type(value) is not int or value < 0 for value in target.values())):
        raise ValueError('展示目标须为非负整数 day_index 和 item_index')
    if (not plan.text or plan.display_status != 'ready' or not isinstance(plan.display, dict)
            or plan.display_source_hash != text_hash(plan.text)
            or plan.display_source_hash != payload['source_hash']):
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
    return {**target, 'day_label': label(day.get('label'), 80), 'date': label(day.get('date'), 32),
            'title': label(item.get('title'), 200), 'time_text': label(item.get('time_text'), 120),
            'location': label(item.get('location'), 240)}


def validate_display_target_metadata(plan, metadata):
    """Internal callers must carry the resolved metadata, not invent a display target."""
    fields = {'day_index', 'item_index', 'day_label', 'date', 'title', 'time_text', 'location'}
    if plan is None or not isinstance(metadata, dict) or set(metadata) != fields:
        raise ValueError('展示目标必须由当前行程解析，不能直接提供目标文字')
    current = resolve_display_target(plan, {
        'display_target': {key: metadata[key] for key in ('day_index', 'item_index')},
        'expected_plan_id': plan.id, 'source_hash': text_hash(plan.text),
        'expected_requirements_revision': plan.requirements_revision,
    })
    if metadata != current:
        raise TaskConflict('目标安排已变化，请刷新行程')
    return current


def _operation_body(kind, payload):
    if kind == 'chat':
        return payload.get('body', '').strip()
    if kind == 'revision':
        return payload.get('feedback', '').strip()
    return '页面操作：生成当前行程'


def _configured_strategy(store):
    strategy = getattr(store, 'execution_strategy', None)
    if strategy is None:
        strategy = os.getenv('TRAVEL_AGENT_EXECUTION_STRATEGY', 'legacy')
    strategy = strategy.strip() if isinstance(strategy, str) else ''
    if strategy not in EXECUTION_STRATEGIES:
        raise ValueError('Agent执行策略必须是 legacy 或 task_execution')
    return strategy


def _restore_tasks(current, before):
    """Restore task values without invalidating references held by worker threads."""
    for task_id in list(current):
        if task_id not in before:
            del current[task_id]
    for task_id, saved in before.items():
        if task_id in current:
            current[task_id].clear()
            current[task_id].update(deepcopy(saved))
        else:
            current[task_id] = deepcopy(saved)


class TravelTasks:
    def submit_task(self, trip_id, kind, payload, request_id, retry_of=''):
        if kind not in {'chat', 'plan', 'revision'}:
            raise ValueError('任务类型不正确')
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
            raise ValueError('请求标识不正确，请刷新后重试')
        if not isinstance(payload, dict):
            raise ValueError('任务内容必须为对象')
        payload = deepcopy(payload)
        with self._lock:
            for task in self.tasks.values():
                if task['trip_id'] == trip_id and task['request_id'] == request_id:
                    if (task['kind'], task['payload'], task['retry_of']) != (kind, payload, retry_of):
                        raise TaskConflict('同一请求标识不能用于不同内容')
                    return deepcopy(task), False
            if retry_of:
                child = next((task for task in self.tasks.values()
                              if task['trip_id'] == trip_id and task['retry_of'] == retry_of), None)
                if child:
                    if (child['kind'], child['payload']) != (kind, payload):
                        raise TaskConflict('重试内容必须与原任务一致')
                    return deepcopy(child), False

            active = next((task for task in self.tasks.values()
                           if task['trip_id'] == trip_id and task['status'] in ACTIVE), None)
            original = self.tasks.get(retry_of) if retry_of else None
            if retry_of:
                if active:
                    raise TaskConflict('这次旅行还有任务正在处理，请等待完成后再重试')
                if not original or original['trip_id'] != trip_id or original['status'] != 'failed':
                    raise TaskConflict('只有失败的任务可以重试')
                if (original['kind'], original['payload']) != (kind, payload):
                    raise TaskConflict('重试内容必须与原任务一致')
            elif active and kind != 'chat':
                raise TaskConflict('这次旅行还有任务正在处理，请等待完成后再提交')

            if kind in {'plan', 'revision'}:
                validate_task_basis(self.plans[trip_id], self.trips[trip_id].requirements.revision, payload)
            if kind != 'revision' and 'display_target' in payload:
                raise ValueError('只有行程修改可以指定展示目标')
            if kind == 'revision':
                feedback = payload.get('feedback')
                if not isinstance(feedback, str) or not 1 <= len(feedback.strip()) <= 2000:
                    raise ValueError('修改意见应为 1 到 2000 字的文本')
                payload['feedback'] = feedback.strip()
                if not self.plans[trip_id].has_content:
                    raise ValueError('请先生成行程')
                resolve_display_target(self.plans[trip_id], payload)
                target = payload.get('item_id')
                if target is not None and not isinstance(target, str):
                    raise ValueError('旧行程条目标识应为文本')
                if target and not any(item.id == target for day in self.plans[trip_id].days for item in day.items):
                    raise ValueError('目标安排已变化，请刷新行程')
            if kind == 'chat':
                body = payload.get('body', '')
                if not isinstance(body, str) or not 1 <= len(body.strip()) <= 10000:
                    raise ValueError('消息应为 1 到 10000 字的文本')
                payload['body'] = body.strip()

            trip = self.trips[trip_id]
            current_basis = plan_basis(self.plans[trip_id])
            workflow = normalize_workflow(
                trip.workflow_state, trip=trip, trip_id=trip_id,
                messages=self.messages[trip_id], plan_basis=current_basis,
            )
            if original:
                try:
                    original_basis = PlanBasis.from_value(original['plan_basis'])
                except (KeyError, TypeError, ValueError):
                    raise TaskConflict('原任务缺少可验证的授权依据，请重新提交') from None
                receipt = original.get('receipt') if isinstance(original.get('receipt'), dict) else None
                if receipt and receipt.get('accepted_artifact_ids'):
                    raise TaskConflict('原任务已提交部分业务效果，请基于当前状态重新发起请求')
                expected_workflow_revision = (
                    receipt.get('after_workflow_revision') if receipt else original.get('workflow_revision')
                )
                expected_requirements_revision = (
                    receipt.get('after_requirements_revision') if receipt else original.get('requirements_revision')
                )
                expected_basis = original_basis
                if receipt and isinstance(receipt.get('after_plan_basis'), dict):
                    try:
                        expected_basis = PlanBasis.from_value(receipt['after_plan_basis'])
                    except ValueError:
                        raise TaskConflict('原任务回执缺少可验证的行程依据，请重新提交') from None
                if (workflow['sequence'] != original.get('sequence')
                        or workflow['epoch'] != original.get('epoch')
                        or workflow['revision'] != expected_workflow_revision
                        or trip.requirements.revision != expected_requirements_revision
                        or current_basis != expected_basis):
                    raise TaskConflict('原请求的会话、需求或行程依据已变化，请重新提交')
                expected_body = _operation_body(kind, payload)
                source = next((message for message in self.messages[trip_id]
                               if message.id == original.get('message_id')), None)
                if (source is None or source.sender_type != 'user'
                        or source.sender.id != trip.owner.id or source.body != expected_body):
                    raise TaskConflict('原始请求消息已失效，请重新提交')
                strategy = original.get('execution_strategy')
                if strategy not in EXECUTION_STRATEGIES:
                    raise TaskConflict('原任务缺少固定执行策略，请重新提交')
                message_id = original['message_id']
                next_workflow = workflow
            else:
                strategy = _configured_strategy(self)
                message_id = ''
                next_workflow = advance_request(workflow)

            before_tasks = deepcopy(self.tasks)
            before_messages = deepcopy(self.messages[trip_id])
            before_trip = deepcopy(trip)
            previous_defer = getattr(self, '_defer_persist', 0)
            try:
                if not original and active:
                    for previous in self.tasks.values():
                        if previous['trip_id'] == trip_id and previous['status'] in ACTIVE:
                            previous.update(
                                status='superseded', message='已由新消息替代',
                                updated_at=now(), result_url='',
                            )
                            thinking = previous.get('thinking_id')
                            self.messages[trip_id] = [row for row in self.messages[trip_id]
                                                      if row.id != thinking]
                trip.workflow_state = deepcopy(next_workflow)
                self._defer_persist = previous_defer + 1
                if original:
                    thinking_id = self.start_agent_task(trip_id).id
                else:
                    message, thinking = self.queue_user_message(
                        trip_id, _operation_body(kind, payload), trip.owner.id,
                    )
                    message_id, thinking_id = message.id, thinking.id
                self._defer_persist = previous_defer
                task_id = uuid4().hex
                # Focus is navigation only; only an active question can bind a short
                # answer to an existing conversation task.
                conversation_task_id = (
                    next_workflow['active_question']['task_id']
                    if next_workflow.get('active_question') else None
                )
                task = {
                    'id': task_id, 'trip_id': trip_id, 'kind': kind, 'payload': payload,
                    'request_id': request_id, 'retry_of': retry_of, 'status': 'queued',
                    'message': '等待处理', 'created_at': now(), 'updated_at': now(),
                    'message_id': message_id, 'thinking_id': thinking_id, 'result_url': '',
                    'plan_id': self.plans[trip_id].id,
                    'plan_basis': current_basis.to_dict(),
                    'requirements_revision': trip.requirements.revision,
                    'workflow_revision': next_workflow['revision'],
                    'sequence': next_workflow['sequence'], 'epoch': next_workflow['epoch'],
                    'conversation_task_id': conversation_task_id,
                    'execution_strategy': strategy,
                }
                self.tasks[task_id] = task
                self._persist()
            except Exception:
                self._defer_persist = previous_defer
                _restore_tasks(self.tasks, before_tasks)
                self.messages[trip_id] = before_messages
                self.trips[trip_id] = before_trip
                raise
            return deepcopy(task), True

    def get_task(self, task_id, *, trip_id=None):
        with self._lock:
            task = self.tasks.get(task_id)
            if not task or (trip_id is not None and task.get('trip_id') != trip_id):
                return None
            return deepcopy(task)

    def list_tasks(self, trip_id):
        with self._lock:
            return deepcopy([task for task in self.tasks.values() if task['trip_id'] == trip_id][-20:])

    def run_task(self, task_id):
        with self._lock:
            task = self.tasks.get(task_id)
            if not task or task['status'] != 'queued':
                return None
            previous = (task['status'], task['message'], task['updated_at'])
            task['status'], task['message'], task['updated_at'] = (
                'running', '正在处理，请稍候；可以离开页面，回来查看结果。', now()
            )
            try:
                self._persist()
            except Exception:
                task['status'], task['message'], task['updated_at'] = previous
                raise
            trip_id = task['trip_id']
        self._task_context.task_id = task_id
        try:
            with self._agent_lock(trip_id):
                return self.execute_turn(task_id)
        except Exception as exc:
            from .turn_commit import TurnPersistenceError
            if isinstance(exc, TurnPersistenceError):
                return None
            try:
                return self.fail_execution(task_id, exc)
            except TurnPersistenceError:
                return None
        finally:
            if hasattr(self._task_context, 'task_id'):
                del self._task_context.task_id
            self._notify_display_worker()

    def recover_tasks(self):
        changed = False
        for task in self.tasks.values():
            if task['status'] in ACTIVE:
                task['status'], task['updated_at'] = 'failed', now()
                task['message'] = '服务重启使任务中断。请先检查当前行程，再重试。'
                changed = True
        if changed:
            self._persist()
