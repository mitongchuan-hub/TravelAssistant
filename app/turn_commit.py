"""Stage a turn's business effects, reply and execution receipt before publishing them."""
from copy import copy, deepcopy
from datetime import datetime
from uuid import uuid4

from .models import AgentAction, AgentCard, Message
from .persistence import save_snapshot, serialize_state
from .requirements import chat_changes, update_requirements
from .tasks import now


class TurnPersistenceError(RuntimeError):
    """A failed final transaction must not be followed by a second success write."""


class _StagingSignal:
    def notify_all(self):
        pass


class TurnCommit:
    def capture_request_context(self, trip_id, task_id=None, *, body=None, entry='main',
                                request_id=None, message_id=None):
        from .agent_protocol import PlanBasis, RequestContext, plan_basis
        from .task_context import normalize_workflow
        with self._lock:
            trip = self.trips[trip_id]
            task = self.tasks.get(task_id) if task_id else None
            workflow = normalize_workflow(trip.workflow_state, trip=trip,
                messages=self.messages[trip_id], plan=self.plans[trip_id])
            kind = task['kind'] if task else None
            if kind:
                entry = {'chat': 'main', 'plan': 'planning', 'revision': 'revision'}[kind]
            payload = task['payload'] if task else {}
            source = body if body is not None else payload.get('body', payload.get('feedback', ''))
            if kind == 'plan' and body is None:
                source = next((row.body for row in self.messages[trip_id] if row.id == task.get('message_id')), '')
            return RequestContext(
                trip_id=trip_id, owner_id=trip.owner.id,
                execution_task_id=task_id or 'operation-' + uuid4().hex,
                conversation_task_id=task.get('conversation_task_id') if task else None,
                request_id=request_id or (task['request_id'] if task else uuid4().hex),
                message_id=message_id or (task.get('message_id') if task else None) or 'operation-' + uuid4().hex,
                body=source or '', entry=entry,
                strategy=task.get('execution_strategy', 'task_execution'),
                requirements_revision=task['requirements_revision'] if task else trip.requirements.revision,
                plan_basis=PlanBasis.from_value(task['plan_basis']) if task and task.get('plan_basis') else plan_basis(self.plans[trip_id]),
                workflow_revision=task.get('workflow_revision', workflow['revision']) if task else workflow['revision'],
                sequence=task.get('sequence', workflow['sequence']) if task else workflow['sequence'],
                epoch=task.get('epoch', workflow['epoch']) if task else workflow['epoch'],
            )

    def _turn_conflict(self, context):
        from .agent_protocol import plan_basis
        from .task_context import normalize_workflow
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
        from .agent_protocol import CommitReceipt, plan_basis
        from .task_context import normalize_workflow
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
        from . import agent_client
        from .task_runtime import TaskExecutionContext
        with self._lock:
            task = self.tasks[task_id]
            if task['status'] != 'running':
                return None
            trip_id = task['trip_id']
            context = self.capture_request_context(trip_id, task_id)
            snapshot = deepcopy({
                'trip': self.trips[trip_id], 'plan': self.plans[trip_id],
                'messages': [row for row in self.messages[trip_id] if row.status == 'sent'],
                'idea_cards': self.idea_cards[trip_id], 'metrics': self.trip_metrics(trip_id),
            })
            from .tasks import resolve_display_target
            display_target = resolve_display_target(snapshot['plan'], task['payload']) if task['kind'] == 'revision' else None
            conflict = self._turn_conflict(context)
        if conflict:
            return self.fail_execution(task_id, ValueError(conflict))
        cancelled = lambda: not self._execution_current(context)
        execution = TaskExecutionContext(**snapshot, current_date=datetime.now().date().isoformat(),
            entry=context.entry, scope='item' if task['payload'].get('item_id') or display_target else '',
            target_item_id=task['payload'].get('item_id'), display_target=display_target)
        proposal = agent_client.generate_task_proposal(context, execution, cancelled=cancelled)
        receipt = self.commit_turn(context, proposal)
        if not receipt.committed and self.tasks.get(task_id, {}).get('status') == 'running':
            return self.fail_execution(task_id, ValueError(receipt.reason))
        return receipt

    def _execution_current(self, context):
        with self._lock:
            task = self.tasks.get(context.execution_task_id)
            return bool(task and task['status'] == 'running' and not self._turn_conflict(context))

    def confirm_current_turn(self, trip_id):
        from .task_context import normalize_workflow
        from .turn_workspace import TurnWorkspace
        with self._lock:
            if not self.plans[trip_id].has_content:
                return None
            context = self.capture_request_context(trip_id, entry='confirm', body='确认当前已显示的行程')
            workflow = normalize_workflow(self.trips[trip_id].workflow_state)
            workspace = TurnWorkspace(context, workflow_state=workflow,
                authorized_message_ids=[context.message_id], visible_task_ids=[row['id'] for row in workflow['tasks']])
            artifact = workspace.register_artifact('confirmation', {'plan_basis': context.plan_basis.to_dict()},
                source_capability='confirm_itinerary')
            proposal = workspace.finish(selected_artifact_ids=[artifact.id], workflow_state=workflow)
        return self.commit_turn(context, proposal)

    def fail_execution(self, task_id, exc):
        from .task_context import normalize_workflow
        from .turn_workspace import TurnWorkspace
        if isinstance(exc, TurnPersistenceError):
            return None
        with self._lock:
            task = self.tasks.get(task_id)
            if not task or task['status'] != 'running':
                return None
            context = self.capture_request_context(task['trip_id'], task_id)
            conflict = self._turn_conflict(context)
            if conflict:
                # Never publish a stale proposal or its task-state candidates.
                stage = self._stage_turn(task['trip_id'])
                target = stage.tasks[task_id]
                target.update(status='failed', message=conflict, updated_at=now(), result_url='')
                stage.messages[task['trip_id']] = [row for row in stage.messages[task['trip_id']] if row.id != task.get('thinking_id')]
                receipt = self._receipt(context, committed=False, task_status='failed', reason=conflict, stage=stage)
                from .agent_protocol import thaw
                target['receipt'] = thaw(receipt)
                self._persist_staged_turn(stage, task['trip_id'])
                return receipt
            state = normalize_workflow(self.trips[task['trip_id']].workflow_state)
            workspace = TurnWorkspace(context, workflow_state=state)
            reason = str(exc) if isinstance(exc, ValueError) else '本次处理未完成，当前需求和行程已保留，请稍后重试。'
            proposal = workspace.fail_closed(reason)
        return self.commit_turn(context, proposal)

    def commit_turn(self, context, proposal):
        from .agent_protocol import ProtocolError, RejectedArtifact, thaw, validate_terminal_proposal
        from .task_context import normalize_workflow
        validate_terminal_proposal(proposal, context)
        with self._lock:
            existing = self.tasks.get(context.execution_task_id, {})
            if self.trips[context.trip_id].owner.id != context.owner_id:
                raise ProtocolError('请求不属于当前旅行所有者')
            if (existing.get('receipt') and existing.get('request_id') == context.request_id
                    and existing.get('trip_id') == context.trip_id
                    and existing.get('message_id') == context.message_id
                    and existing.get('sequence') == context.sequence and existing.get('epoch') == context.epoch):
                from .agent_protocol import CommitReceipt, RejectedArtifact
                raw = deepcopy(existing['receipt'])
                raw['rejected_artifacts'] = tuple(RejectedArtifact(**row) for row in raw['rejected_artifacts'])
                return CommitReceipt(**raw)
            conflict = self._turn_conflict(context)
            if conflict:
                return self._receipt(context, committed=False,
                    rejected=tuple(RejectedArtifact(row.id, conflict) for row in proposal.artifacts),
                    task_status=existing.get('status', 'failed'), reason=conflict)
            stage = self._stage_turn(context.trip_id)
            trip_id = context.trip_id
            trip = stage.trips[trip_id]
            selected = {artifact.id: artifact for artifact in proposal.artifacts}
            evidence = {row.id: row for row in proposal.evidence}
            accepted, rejected = [], []
            itinerary = confirmation = None
            original = deepcopy(trip)
            all_changes = {}
            for artifact in proposal.artifacts:
                if not artifact.belongs_to(context):
                    raise ProtocolError('候选引用不属于当前请求')
                if artifact.effect == 'hypothetical' or artifact.condition != 'satisfied':
                    raise ProtocolError('假设或未知条件不能提交正式写入')
                if artifact.effect == 'conditional' and not artifact.evidence_refs:
                    raise ProtocolError('条件写入缺少有效证据')
                if set(artifact.depends_on) - set(selected) or set(artifact.evidence_refs) - set(evidence):
                    raise ProtocolError('候选依赖未被采用或证据已失效')
                if (artifact.requirements_revision != context.requirements_revision or artifact.plan_basis != context.plan_basis
                        or artifact.workflow_revision != context.workflow_revision):
                    raise ProtocolError('候选业务依据与请求不一致')
                permitted_sources = {context.message_id}
                if artifact.conversation_task_id:
                    source_task = next((row for row in normalize_workflow(original.workflow_state)['tasks']
                                        if row['id'] == artifact.conversation_task_id), None)
                    if source_task and source_task['status'] not in {'completed', 'cancelled', 'failed'}:
                        permitted_sources.update(source_task['source_message_ids'])
                        permitted_sources.update(source_task['authorization_refs'])
                if set(artifact.authorization_refs) - permitted_sources:
                    raise ProtocolError('候选引用了不属于当前任务的原始授权')
                payload = thaw(artifact.payload)
                if artifact.kind == 'requirements':
                    raw = payload['raw_patch']
                    changes = chat_changes(raw, context.body, original)
                    if changes != payload['normalized_changes']:
                        raise ProtocolError('需求候选与原话校验结果不一致')
                    if changes:
                        all_changes.update(changes)
                        trip = update_requirements(original, all_changes)
                    stage.trips[trip_id] = trip
                elif artifact.kind == 'itinerary':
                    if itinerary is not None:
                        raise ProtocolError('一次只能采用一份最终行程')
                    itinerary = artifact
                elif artifact.kind == 'confirmation':
                    if confirmation is not None:
                        raise ProtocolError('确认候选重复')
                    confirmation = artifact
                accepted.append(artifact.id)
            if confirmation is not None and (itinerary is not None or trip.requirements != original.requirements):
                raise ProtocolError('确认不能与需求或新草稿混合采用')
            if itinerary is not None:
                payload = thaw(itinerary.payload)
                if payload['preserve_plan_basis'] and trip.requirements != original.requirements:
                    raise ProtocolError('精确文字编辑不能把同轮需求变更静默混入旧方案依据')
                if trip.requirements != original.requirements and not any(
                    selected[ref].kind == 'requirements' for ref in itinerary.depends_on
                ):
                    raise ProtocolError('新行程没有绑定同轮需求候选')
                before_plan, before_jobs = deepcopy(stage.plans[trip_id]), deepcopy(stage.display_jobs)
                try:
                    stage._apply_text_plan(trip_id, payload['text'], payload['evidence'],
                        display_source_hash='', display_status='pending',
                        preserve_plan_basis=payload['preserve_plan_basis'])
                except ValueError as exc:
                    stage.plans[trip_id], stage.display_jobs = before_plan, before_jobs
                    rejected.append(RejectedArtifact(itinerary.id, '行程候选未通过检查：' + str(exc)))
                    accepted.remove(itinerary.id)
                    itinerary = None
                trip = stage.trips[trip_id]
            if confirmation is not None:
                from .agent_protocol import PlanBasis
                if PlanBasis.from_value(confirmation.payload['plan_basis']) != context.plan_basis or not context.plan_basis.has_content:
                    raise ProtocolError('只能确认已交付且依据未变的当前方案')
                stage.plans[trip_id].status = trip.status = '已确认'
            visible_messages = {row.id for row in stage.messages[trip_id]} | {context.message_id}
            raw_workflow = thaw(proposal.workflow_state) if proposal.workflow_state is not None else trip.workflow_state
            pending_question = raw_workflow.get('active_question') if isinstance(raw_workflow, dict) else None
            if proposal.question and pending_question:
                visible_messages.add(pending_question['assistant_message_id'])
            workflow = normalize_workflow(raw_workflow, messages=visible_messages)
            old_tasks = {row['id']: row for row in normalize_workflow(original.workflow_state)['tasks']}
            selected_task_refs = {row.payload.get('task_ref') for row in proposal.artifacts if row.kind == 'task'}
            removed_tasks = set(old_tasks) - {row['id'] for row in workflow['tasks']}
            if any(old_tasks[task_id]['status'] not in {'completed', 'cancelled', 'failed'} for task_id in removed_tasks):
                raise ProtocolError('不能通过删除未完成事项记录伪装任务完成')
            question_task = proposal.question.conversation_task_id if proposal.question else None
            if workflow.get('active_question') and not proposal.question:
                original_question = normalize_workflow(original.workflow_state).get('active_question')
                if workflow['active_question'] != original_question:
                    raise ProtocolError('新追问必须与最终可见消息一起提交')
            original_question = normalize_workflow(original.workflow_state).get('active_question')
            if original_question and not workflow.get('active_question') and not proposal.question:
                if original_question['task_id'] not in selected_task_refs:
                    raise ProtocolError('不能无依据清除另一个任务的活动追问')
            for row in workflow['tasks']:
                previous = old_tasks.get(row['id'])
                previous_question = normalize_workflow(original.workflow_state).get('active_question')
                displaced_waiter = bool(question_task and previous_question and previous
                    and previous_question['task_id'] == row['id'] and row['id'] != question_task
                    and previous['status'] == 'waiting_user' and row['status'] == 'suspended')
                if row != previous and row['id'] not in selected_task_refs and row['id'] != question_task and not displaced_waiter:
                    if previous is None:
                        raise ProtocolError('新任务候选未被明确采用')
                    if row['status'] != previous['status'] or row.get('progress') != previous.get('progress'):
                        raise ProtocolError('任务状态变化缺少已采用的效果引用')
                if previous and previous['status'] in {'completed', 'cancelled', 'failed'} and row['status'] != previous['status']:
                    raise ProtocolError('已经结束的会话任务不能复活')
            for artifact in proposal.artifacts:
                if artifact.kind == 'task':
                    payload = thaw(artifact.payload)
                    if set(payload.get('effect_refs', [])) - set(accepted):
                        task_id = payload.get('task_ref')
                        previous = old_tasks.get(task_id)
                        if previous:
                            workflow['tasks'] = [deepcopy(previous) if row['id'] == task_id else row for row in workflow['tasks']]
                        else:
                            for row in workflow['tasks']:
                                if row['id'] == task_id:
                                    row['status'] = 'failed'
                        if artifact.id in accepted:
                            accepted.remove(artifact.id)
                        rejected.append(RejectedArtifact(artifact.id, '依赖的业务效果未提交，任务未标记完成'))
            if (workflow['sequence'], workflow['epoch']) != (context.sequence, context.epoch):
                raise ProtocolError('候选不能改写请求代次')
            workflow['revision'] = context.workflow_revision + 1
            trip.workflow_state = workflow
            message_id = existing.get('thinking_id') or 'm-' + uuid4().hex
            if proposal.question:
                question = proposal.question
                task = next((row for row in workflow['tasks'] if row['id'] == question.conversation_task_id), None)
                if task is None or task['status'] in {'cancelled', 'completed', 'failed'}:
                    raise ProtocolError('追问任务不存在或已经结束')
                task['status'] = 'waiting_user'
                workflow['focus_task_id'] = task['id']
                workflow['active_question'] = {
                    'id': question.id, 'task_id': task['id'], 'assistant_message_id': message_id,
                    'text': question.text, 'answer_target': thaw(question.answer_target),
                    'source_message_ids': list(question.source_message_ids) or [context.message_id],
                }
            effect_lines = []
            if trip.requirements != original.requirements:
                effect_lines.append('需求已保存。')
            if itinerary:
                effect_lines.append('行程已更新。')
            if confirmation:
                effect_lines.append('当前行程已确认。')
            cancelled = [row for row in workflow['tasks'] if row['status'] == 'cancelled']
            old_cancelled = {row['id'] for row in normalize_workflow(original.workflow_state)['tasks'] if row['status'] == 'cancelled'}
            if any(row['id'] not in old_cancelled for row in cancelled):
                effect_lines.append('对应会话任务已取消；已保存需求和行程继续保留。')
            parts = effect_lines + ([proposal.answer.strip()] if proposal.answer.strip() and not rejected else [])
            if itinerary:
                parts.append(thaw(itinerary.payload)['text'])
            if proposal.question:
                parts.append(proposal.question.text)
            failed = proposal.failed or bool(rejected)
            reason = proposal.reason or '；'.join(row.reason for row in rejected)
            if failed:
                parts.append('仍未完成：' + (reason or '本次处理未全部完成，请稍后重试。'))
            body = '\n\n'.join(parts) or '本轮已结束，当前行程未修改。'
            status = 'failed' if failed else 'sent'
            card = AgentCard('missing-info' if proposal.question or failed else 'itinerary-draft' if itinerary else 'requirement-summary',
                '需要补充' if proposal.question else '旅行助手', body[:120], [],
                [AgentAction('查看行程', 'itinerary')] if itinerary else [AgentAction('继续聊天', 'chat')])
            from .store import USERS
            message = Message(message_id, USERS['agent'], 'agent', body, datetime.now().strftime('%H:%M'), status, card)
            messages = stage.messages[trip_id]
            index = next((index for index, row in enumerate(messages) if row.id == message_id), None)
            if index is None:
                messages.append(message)
            else:
                messages[index] = message
            trip.last_activity = 'Agent 等待补充信息' if proposal.question else 'Agent 本轮部分完成' if failed else 'Agent 已完成本轮请求'
            task_status = 'failed' if failed else 'succeeded'
            receipt = self._receipt(context, committed=True, accepted=accepted, rejected=rejected,
                message_id=message_id, message_status=status, task_status=task_status,
                reason=reason, stage=stage)
            if context.execution_task_id in stage.tasks:
                from .agent_protocol import thaw
                task = stage.tasks[context.execution_task_id]
                task.update(status=task_status, message=proposal.question.text if proposal.question else reason if failed else '已完成',
                    needs_input=proposal.question is not None, updated_at=now(), receipt=thaw(receipt),
                    committed_effect_kinds=[selected[ref].kind for ref in accepted],
                    result_url=f'/workspace/{trip_id}?tab=' + ('itinerary' if itinerary else 'chat'))
            self._persist_staged_turn(stage, trip_id)
        self._notify_display_worker()
        return receipt

    def _stage_turn(self, trip_id):
        """Reuse the normal builders against private aggregates, never live objects."""
        stage = copy(self)
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
        self.display_jobs = stage.display_jobs
        self._display_changed.notify_all()

    def _persist_staged_turn(self, stage, trip_id):
        from .store import USERS
        try:
            if self.persistence_enabled:
                save_snapshot(self.db_path, serialize_state(stage, USERS), initialize=False)
        except Exception as exc:
            raise TurnPersistenceError('最终提交未成功，当前需求、行程与任务状态未改变。') from exc
        self._publish_turn(stage, trip_id)
