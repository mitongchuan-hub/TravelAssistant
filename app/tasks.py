"""Persistent task lifecycle for the single-process travel store."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

ACTIVE = {'queued', 'running'}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class TaskConflict(ValueError):
    pass


class TravelTasks:
    def submit_task(self, trip_id, kind, payload, request_id, retry_of=''):
        if kind not in {'chat', 'plan', 'revision'}:
            raise ValueError('任务类型不正确')
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
            raise ValueError('请求标识不正确，请刷新后重试')
        with self._lock:
            for task in self.tasks.values():
                if task['trip_id'] == trip_id and task['request_id'] == request_id:
                    if (task['kind'], task['payload'], task['retry_of']) != (kind, payload, retry_of):
                        raise TaskConflict('同一请求标识不能用于不同内容')
                    return deepcopy(task), False
            if retry_of:
                child = next((task for task in self.tasks.values() if task['trip_id'] == trip_id and task['retry_of'] == retry_of), None)
                if child:
                    return deepcopy(child), False
            active = next((task for task in self.tasks.values() if task['trip_id'] == trip_id and task['status'] in ACTIVE), None)
            if active and kind != 'chat':
                raise TaskConflict('这次旅行还有任务正在处理，请等待完成后再提交')
            original = self.tasks.get(retry_of) if retry_of else None
            if retry_of and (not original or original['trip_id'] != trip_id or original['status'] != 'failed'):
                raise TaskConflict('只有失败的任务可以重试')
            if original and (original['kind'] != kind or original['payload'] != payload):
                raise TaskConflict('重试内容必须与原任务一致')
            if kind == 'revision':
                feedback = payload.get('feedback')
                if not isinstance(feedback, str) or not 1 <= len(feedback.strip()) <= 2000:
                    raise ValueError('修改意见应为 1 到 2000 字的文本')
                if not self.plans[trip_id].has_content:
                    raise ValueError('请先生成行程')
                target = payload.get('item_id')
                if target and not any(item.id == target for day in self.plans[trip_id].days for item in day.items):
                    raise ValueError('目标安排已变化，请刷新行程')
            message_id = thinking_id = ''
            if kind == 'chat':
                body = payload.get('body', '').strip()
                if not body or len(body) > 10000:
                    raise ValueError('消息应为 1 到 10000 字的文本')
                if active:
                    for previous in self.tasks.values():
                        if previous['trip_id'] == trip_id and previous['status'] in ACTIVE:
                            previous['status'] = 'superseded'
                            previous['message'] = '已由新消息替代'
                            previous['updated_at'] = now()
                            previous['result_url'] = ''
                            thinking = previous.get('thinking_id')
                            self.messages[trip_id] = [m for m in self.messages[trip_id] if m.id != thinking]
                self._defer_persist = getattr(self, '_defer_persist', 0) + 1
                try:
                    if original:
                        message_id = original['message_id']
                        thinking_id = self.start_agent_task(trip_id).id
                    else:
                        message, thinking = self.queue_user_message(trip_id, body, self.trips[trip_id].owner.id)
                        message_id, thinking_id = message.id, thinking.id
                finally:
                    self._defer_persist -= 1
            task = {
                'id': uuid4().hex, 'trip_id': trip_id, 'kind': kind, 'payload': deepcopy(payload),
                'request_id': request_id, 'retry_of': retry_of, 'status': 'queued',
                'message': '等待处理', 'created_at': now(), 'updated_at': now(),
                'message_id': message_id, 'thinking_id': thinking_id, 'result_url': '',
                'plan_id': self.plans[trip_id].id, 'requirements_revision': self.trips[trip_id].requirements.revision,
            }
            self.tasks[task['id']] = task
            self._persist()
            return deepcopy(task), True

    def list_tasks(self, trip_id):
        with self._lock:
            return deepcopy([task for task in self.tasks.values() if task['trip_id'] == trip_id][-20:])

    def run_task(self, task_id):
        with self._lock:
            task = self.tasks.get(task_id)
            if not task or task['status'] != 'queued':
                return
            task['status'], task['message'], task['updated_at'] = 'running', '正在处理，请稍候；可以离开页面，回来查看结果。', now()
            self._persist()
        trip_id = task['trip_id']
        self._task_context.task_id = task_id
        self._task_context.needs_input = False
        try:
            with self._agent_lock(trip_id):
                with self._lock:
                    if task['status'] != 'running':
                        return
                    if task['kind'] != 'chat' and (
                        self.trips[trip_id].requirements.revision != task['requirements_revision']
                        or self.plans[trip_id].id != task['plan_id']
                    ):
                        raise TaskConflict('需求或行程已变化，请基于最新内容重试')
                    before = deepcopy(self.plans[trip_id])
                if task['kind'] == 'chat':
                    last = self.finish_agent_task(trip_id, task['payload']['body'], self.trips[trip_id].owner.id, task['thinking_id'])
                elif task['kind'] == 'plan':
                    last = self.generate_plan(trip_id)
                else:
                    last = self.request_revision(trip_id, task['payload'].get('item_id'), task['payload']['feedback'], self.trips[trip_id].owner)
                with self._lock:
                    if task['status'] != 'running':
                        return
                    saved = self.plans[trip_id] != before
                    needs_input = self._task_context.needs_input
                    success = last is not None and last.status == 'sent' and (saved or task['kind'] == 'chat' or needs_input)
                    task['status'] = 'succeeded' if success else 'failed'
                    task['needs_input'] = needs_input
                    task['message'] = last.body if success and needs_input else '已完成' if success else (last.body if last else '本次处理未完成，请重试')
                    task['result_url'] = f'/workspace/{trip_id}?tab=' + ('itinerary' if success and saved else 'chat')
                    if success and saved and task['kind'] == 'revision' and task['payload'].get('item_id') and not self.plans[trip_id].text:
                        task['result_url'] += '#itinerary-item-' + task['payload']['item_id']
                    task['updated_at'] = now()
                    self._persist()
        except Exception as exc:
            with self._lock:
                if task['status'] != 'running':
                    return
                if task['kind'] == 'chat':
                    self.fail_agent_task(trip_id, task['thinking_id'])
                task['status'], task['updated_at'] = 'failed', now()
                task['message'] = str(exc) if isinstance(exc, TaskConflict) else '本次处理未完成，原行程已保留，请稍后重试。'
                self._persist()
        finally:
            del self._task_context.task_id
            del self._task_context.needs_input
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
