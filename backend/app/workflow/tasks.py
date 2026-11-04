"""Persistent execution-task lifecycle for the single-process travel store."""
from copy import deepcopy
from datetime import datetime, timezone

from app.services.itinerary.plan_display import text_hash
from app.application.progress import advance_progress, finish_progress

ACTIVE = {'queued', 'running'}
BASIS_FIELDS = {'expected_plan_id', 'source_hash', 'expected_requirements_revision'}
EXECUTION_STRATEGIES = {'task_execution'}


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


class TravelTasks:
    def submit_task(self, trip_id, kind, payload, request_id):
        from app.application.execution import submit_task
        return submit_task(self, trip_id, kind, payload, request_id)

    def get_task(self, task_id, *, trip_id=None):
        with self._lock:
            task = self.task_repository.get(task_id)
            if not task or (trip_id is not None and task.get('trip_id') != trip_id):
                return None
            return deepcopy(task)

    def list_tasks(self, trip_id, request_id=None):
        with self._lock:
            if request_id is not None:
                task = self.task_repository.find_request(trip_id, request_id)
                return [task] if task else []
            return self.task_repository.recent(trip_id)

    def cancel_task(self, trip_id, task_id):
        from app.application.execution import cancel_task
        return cancel_task(self, trip_id, task_id)

    def record_task_progress(self, task_id, title, value):
        with self._lock:
            task = self.tasks.get(task_id)
            if not task or task['status'] != 'running':
                return
            message = next((row for row in self.messages.get(task['trip_id'], [])
                            if row.id == task.get('thinking_id') and row.status == 'thinking'), None)
            if message is None:
                return
            progress = advance_progress(message.execution_progress, title, value)
            if progress is None:
                return
            previous = message.execution_progress
            message.execution_progress = progress
            try:
                self._persist()
            except Exception:
                # Observability must never turn a valid business request into a failure.
                message.execution_progress = previous

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
                self._persist(task_upserts=[task])
            except Exception:
                task['status'], task['message'], task['updated_at'] = previous
                raise
            trip_id = task['trip_id']
        self._task_context.task_id = task_id
        try:
            with self._agent_lock(trip_id):
                return self.execute_turn(task_id)
        except Exception as exc:
            from app.application.execution import TurnPersistenceError
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
        with self._lock:
            for trip_id in self.trips:
                if not any(task['trip_id'] == trip_id for task in self.tasks.values()) and not any(
                        row.status == 'thinking' for row in self.messages[trip_id]):
                    continue
                stage = self._stage_turn(trip_id)
                for task in stage.tasks.values():
                    task.update(status='failed', updated_at=now(),
                                message='本次处理未完成，请重新发送消息。已有内容已保留。')
                for row in stage.messages[trip_id]:
                    if row.status == 'thinking':
                        row.status = 'failed'
                        row.body = '本次处理未完成，请重新发送消息。已有内容已保留。'
                        row.execution_progress = finish_progress(row.execution_progress, 'failed')
                self._persist_staged_turn(stage, trip_id)
