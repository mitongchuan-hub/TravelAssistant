"""Stage a turn's business effects, reply and execution receipt before publishing them."""
from copy import copy, deepcopy
from datetime import datetime
from hashlib import sha256
import json
from uuid import uuid4

from app.domain.models import AgentAction, AgentCard, Message
from app.storage.persistence import serialize_state
from app.application.execution import now
from app.application.progress import finish_progress


from app.storage.errors import TurnPersistenceError


class _StagingSignal:
    def notify_all(self):
        pass


class TurnCommit:
    def capture_request_context(self, trip_id, task_id=None, *, body=None, entry='main',
                                request_id=None, message_id=None):
        """Build a trusted execution snapshot without conflating ID roles.

        The compatibility ``message_id`` argument always identifies the original
        user message. The task's ``thinking_id`` is the assistant placeholder and
        is not used as the authorization message.
        """
        from app.domain.itinerary.identity import PlanBasis, plan_basis
        from app.application.contracts import RequestContext
        from app.storage.legacy.workflow import normalize_workflow
        with self._lock:
            trip = self.trips[trip_id]
            task = self.tasks.get(task_id) if task_id else None
            workflow = normalize_workflow(trip.workflow_state, trip=trip,
                messages=self.messages[trip_id], plan=self.plans[trip_id])
            kind = task['kind'] if task else None
            if kind:
                entry = 'main'
            payload = task['payload'] if task else {}
            source = body if body is not None else payload.get('body', payload.get('feedback', ''))
            if kind == 'plan' and body is None:
                source = next((row.body for row in self.messages[trip_id] if row.id == task.get('message_id')), '')
            # The task dictionary keeps legacy keys; local names state their roles.
            user_message_id = message_id or (task.get('message_id') if task else None) or 'operation-' + uuid4().hex
            execution_task_id = task_id or 'operation-' + uuid4().hex
            request_key = request_id or (task['request_id'] if task else uuid4().hex)
            return RequestContext(
                trip_id=trip_id, owner_id=trip.owner.id,
                execution_task_id=execution_task_id,
                conversation_task_id=task.get('conversation_task_id') if task else None,
                request_id=request_key,
                message_id=user_message_id,
                body=source or '', entry=entry,
                strategy=task.get('execution_strategy', 'task_execution') if task else 'task_execution',
                requirements_revision=task['requirements_revision'] if task else trip.requirements.revision,
                plan_basis=PlanBasis.from_value(task['plan_basis']) if task and task.get('plan_basis') else plan_basis(self.plans[trip_id]),
                workflow_revision=task.get('workflow_revision', workflow['revision']) if task else workflow['revision'],
                sequence=task.get('sequence', workflow['sequence']) if task else workflow['sequence'],
                epoch=task.get('epoch', workflow['epoch']) if task else workflow['epoch'],
            )

    def _turn_conflict(self, context):
        from app.domain.itinerary.identity import plan_basis
        from app.storage.legacy.workflow import normalize_workflow
        trip = self.trips.get(context.trip_id)
        if trip is None or trip.owner.id != context.owner_id:
            return '请求不属于当前旅行。'
        task = self.tasks.get(context.execution_task_id)
        if task and (task['status'] != 'running' or task['trip_id'] != context.trip_id
                     or task['request_id'] != context.request_id
                     or task.get('execution_strategy', 'task_execution') != context.strategy
                     or task.get('message_id') != context.message_id
                     or task.get('requirements_revision') != context.requirements_revision
                     or task.get('workflow_revision') != context.workflow_revision
                     or task.get('sequence') != context.sequence or task.get('epoch') != context.epoch
                     or task.get('plan_basis') != context.plan_basis.to_dict()):
            return '本次请求已被替代或取消。'
        if task and not any(
            message.id == context.message_id and message.sender_type == 'user'
            and message.sender.id == context.owner_id and message.body == context.body
            for message in self.messages[context.trip_id]
        ):
            return '原始用户消息已失效，请重新发送请求。'
        state = normalize_workflow(trip.workflow_state, trip=trip,
            messages=self.messages[context.trip_id], plan=self.plans[context.trip_id])
        if (state['sequence'], state['epoch'], state['revision']) != (
            context.sequence, context.epoch, context.workflow_revision
        ):
            return '会话目标或追问已变化，请基于最新上下文重试。'
        if trip.requirements.revision != context.requirements_revision:
            return '旅行需求在处理期间已更新，请基于最新需求重试。'
        if plan_basis(self.plans[context.trip_id]) != context.plan_basis:
            return '行程在处理期间已变化，请基于最新行程重试。'
        return None

    def _receipt(self, context, *, committed, accepted=(), rejected=(), message_id=None,
                 message_status='', task_status='', reason='', stage=None):
        from app.application.contracts import CommitReceipt
        from app.domain.itinerary.identity import plan_basis
        from app.storage.legacy.workflow import normalize_workflow
        source = stage or self
        trip = source.trips[context.trip_id]
        workflow = normalize_workflow(trip.workflow_state)
        return CommitReceipt(
            trip_id=context.trip_id, execution_task_id=context.execution_task_id,
            request_id=context.request_id, committed=committed,
            accepted_artifact_ids=tuple(accepted), rejected_artifacts=tuple(rejected),
            after_requirements_revision=trip.requirements.revision,
            after_plan_basis=plan_basis(source.plans[context.trip_id]),
            after_workflow_revision=workflow['revision'], after_sequence=workflow['sequence'],
            after_epoch=workflow['epoch'], message_id=message_id,
            message_status=message_status, task_status=task_status, reason=reason,
            conversation_task_states={row['id']: row['status'] for row in workflow['tasks']},
        )

    def execute_turn(self, task_id):
        from app.agents import entrypoints
        from app.agents.inputs.execution import build_execution_context
        with self._lock:
            task = self.tasks[task_id]
            if task['status'] != 'running':
                return None
            trip_id = task['trip_id']
            context = self.capture_request_context(trip_id, task_id)
            from app.workflow.tasks import resolve_display_target
            # Resolve against the same locked plan used by assembly. The resolver
            # only reads saved data; assembly detaches both plan and target once.
            payload = task['payload']
            display_target = resolve_display_target(self.plans[trip_id], payload)
            execution = build_execution_context(
                trip=self.trips[trip_id], plan=self.plans[trip_id],
                messages=self.messages[trip_id], idea_cards=self.idea_cards[trip_id],
                metrics=self.trip_metrics(trip_id), current_date=datetime.now().date().isoformat(),
                entry=context.entry, payload=payload, display_target=display_target)
            conflict = self._turn_conflict(context)
        if conflict:
            return self.fail_execution(task_id, ValueError(conflict))
        cancelled = lambda: not self._execution_current(context)
        proposal = entrypoints.generate_task_result(context, execution, cancelled=cancelled,
            on_progress=lambda title, value: self.record_task_progress(task_id, title, value))
        receipt = self.commit_turn(context, proposal)
        if not receipt.committed and self.tasks.get(task_id, {}).get('status') == 'running':
            return self.fail_execution(task_id, ValueError(receipt.reason))
        return receipt

    def _execution_current(self, context):
        with self._lock:
            task = self.tasks.get(context.execution_task_id)
            return bool(task and task['status'] == 'running' and not self._turn_conflict(context))

    def confirm_current_turn(self, trip_id):
        from app.domain.changes import prepare_changes
        from app.application.contracts import ExecutionResult
        from types import SimpleNamespace
        with self._lock:
            if not self.plans[trip_id].has_content:
                return None
            context = self.capture_request_context(trip_id, entry='confirm', body='确认当前已显示的行程')
            execution = SimpleNamespace(trip=deepcopy(self.trips[trip_id]),
                plan=deepcopy(self.plans[trip_id]), idea_cards=deepcopy(self.idea_cards[trip_id]),
                display_target=None)
            changes = prepare_changes({'user_quote': context.body, 'itinerary': {'action': 'confirm'}},
                                      context, execution)
        return self.commit_turn(context, ExecutionResult(changes=changes))

    def fail_execution(self, task_id, exc):
        from app.storage.legacy.workflow import normalize_workflow
        from app.application.contracts import ExecutionResult
        if isinstance(exc, TurnPersistenceError):
            return None
        with self._lock:
            task = self.tasks.get(task_id)
            if not task or task['status'] != 'running':
                return None
            context = self.capture_request_context(task['trip_id'], task_id)
            conflict = self._turn_conflict(context)
            if conflict:
                # Never publish a result after its request was replaced or cancelled.
                stage = self._stage_turn(task['trip_id'])
                target = stage.tasks[task_id]
                target.update(status='failed', message=conflict, updated_at=now(), result_url='')
                for row in stage.messages[task['trip_id']]:
                    if row.id == task.get('thinking_id'):
                        row.status, row.body = 'failed', conflict
                        row.execution_progress = finish_progress(row.execution_progress, 'failed')
                receipt = self._receipt(context, committed=False, task_status='failed', reason=conflict, stage=stage)
                from app.application.contracts import serialize
                target['receipt'] = serialize(receipt)
                self._persist_staged_turn(stage, task['trip_id'])
                return receipt
            reason = str(exc) if isinstance(exc, ValueError) else '本次处理未完成，请重新发送消息。已有内容已保留。'
            proposal = ExecutionResult.failure(reason)
        return self.commit_turn(context, proposal)

    def commit_turn(self, context, proposal):
        from app.application.contracts import ExecutionResult, CommitReceipt, serialize
        from app.storage.legacy.workflow import normalize_workflow
        if not isinstance(proposal, ExecutionResult):
            raise ValueError('执行结果类型不正确')
        # Snapshot once; only this detached package is checked and persisted.
        proposal = deepcopy(proposal)
        proposal.__post_init__()
        submission_digest = sha256(json.dumps({
            'context': serialize(context), 'result': serialize(proposal),
        }, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
            allow_nan=False).encode('utf-8')).hexdigest()
        with self._lock:
            existing = self.tasks.get(context.execution_task_id, {})
            if context.trip_id not in self.trips:
                return CommitReceipt(trip_id=context.trip_id, execution_task_id=context.execution_task_id,
                    request_id=context.request_id, committed=False, task_status='failed',
                    reason='旅行已删除，本次结果未保存')
            if self.trips[context.trip_id].owner.id != context.owner_id:
                raise ValueError('请求不属于当前旅行所有者')
            if (existing.get('receipt') and existing.get('request_id') == context.request_id
                    and existing.get('trip_id') == context.trip_id
                    and existing.get('message_id') == context.message_id
                    and existing.get('sequence') == context.sequence and existing.get('epoch') == context.epoch):
                from app.application.contracts import CommitReceipt
                if existing.get('submission_digest') != submission_digest:
                    raise ValueError('同一请求身份不能提交不同的内容摘要')
                raw = deepcopy(existing['receipt'])
                return CommitReceipt(**raw)
            conflict = self._turn_conflict(context)
            if conflict:
                return self._receipt(context, committed=False,
                    task_status=existing.get('status', 'failed'), reason=conflict)
            stage = self._stage_turn(context.trip_id)
            trip_id = context.trip_id
            trip = stage.trips[trip_id]
            from app.application.undo import capture, record_change, manual_barrier
            undo_before = capture(stage, trip_id)
            crud_lines, crud_kinds, crud_payload = [], [], None
            if proposal.changes is not None:
                from app.domain.changes import apply_changes
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
            card = AgentCard('missing-info' if needs_input or failed else 'itinerary-draft' if has_itinerary else 'requirement-summary',
                '需要补充' if needs_input else '旅行助手', body[:120], [],
                [AgentAction('查看攻略', 'itinerary')] if has_itinerary else [AgentAction('继续聊天', 'chat')])
            from app.storage.store import USERS
            message = Message(message_id, USERS['agent'], 'agent', body, datetime.now().strftime('%H:%M'), status, card)
            messages = stage.messages[trip_id]
            index = next((index for index, row in enumerate(messages) if row.id == message_id), None)
            if index is None:
                messages.append(message)
            else:
                message.execution_progress = finish_progress(messages[index].execution_progress,
                    'failed' if failed else 'needs_input' if needs_input else 'succeeded')
                if not message.execution_progress.get('steps') and not failed:
                    from app.application.progress import new_progress
                    message.execution_progress = finish_progress(new_progress('running'), 'succeeded')
                elif not failed:
                    message.execution_progress['state'] = 'succeeded'
                messages[index] = message
            trip.last_activity = 'Agent 等待补充信息' if needs_input else 'Agent 本轮部分完成' if failed else 'Agent 已完成本轮请求'
            task_status = 'failed' if failed else 'succeeded'
            receipt = self._receipt(context, committed=True,
                message_id=message_id, message_status=status, task_status=task_status,
                reason=reason, stage=stage)
            if context.execution_task_id in stage.tasks:
                from app.application.contracts import serialize
                task = stage.tasks[context.execution_task_id]
                task.update(status=task_status, message=next_question or (reason if failed else '已完成'),
                    needs_input=needs_input, updated_at=now(), receipt=serialize(receipt),
                    committed_effect_kinds=crud_kinds,
                    submission_digest=submission_digest,
                    result_url=f'/workspace/{trip_id}?tab=' + ('itinerary' if has_itinerary else 'chat'))
            if context.entry == 'confirm' or (failed and crud_kinds):
                manual_barrier(stage, trip_id)
            elif not failed and crud_kinds:
                record_change(stage, trip_id, undo_before, message_id)
            self._persist_staged_turn(stage, trip_id)
        if has_itinerary:
            self._notify_display_worker()
        return receipt

    def _stage_turn(self, trip_id):
        """Reuse the normal builders against private aggregates, never live objects."""
        stage = copy(self)
        stage.agent_undo = deepcopy(self.agent_undo)
        stage.trips = dict(self.trips)
        stage.plans = dict(self.plans)
        stage.messages = dict(self.messages)
        stage.idea_cards = dict(self.idea_cards)
        for name in ('trips', 'plans', 'messages', 'idea_cards'):
            getattr(stage, name)[trip_id] = deepcopy(getattr(self, name)[trip_id])
        stage.tasks = deepcopy(self.tasks)
        stage.display_jobs = deepcopy(self.display_jobs)
        stage._display_changed = _StagingSignal()
        stage._defer_persist = 1
        stage.persistence_enabled = False
        return stage

    def _publish_turn(self, stage, trip_id):
        for name in ('trips', 'plans', 'messages', 'idea_cards'):
            getattr(self, name)[trip_id] = getattr(stage, name)[trip_id]
        # Keep task objects referenced by queued/running threads alive.
        for task_id, task in stage.tasks.items():
            if task.get('trip_id') == trip_id:
                if task_id in self.tasks:
                    self.tasks[task_id].clear()
                    self.tasks[task_id].update(task)
                else:
                    self.tasks[task_id] = task
        self.agent_undo = stage.agent_undo
        self.display_jobs = stage.display_jobs
        self._display_changed.notify_all()

    def _persist_staged_turn(self, stage, trip_id):
        from app.storage.store import USERS
        try:
            if self.persistence_enabled:
                from app.application.execution import save_snapshot
                save_snapshot(self.db_path, serialize_state(stage, USERS), initialize=False)
        except Exception as exc:
            raise TurnPersistenceError('最终提交未成功，当前需求、行程与任务状态未改变。') from exc
        self._publish_turn(stage, trip_id)
