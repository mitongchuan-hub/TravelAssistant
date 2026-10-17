"""Bounded main task executor over trusted candidates and professional capabilities."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import json
from time import monotonic
from typing import Any, Mapping
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError

from .agent_protocol import (
    CapabilityStatus,
    FrozenDict,
    RequestContext,
    TerminalProposal,
    TerminalQuestion,
    thaw,
)
from .agent_roles import execution_roles, role_reasoning_effort
from .agent_runtime import AgentBudgetError, TravelAgentRuntime, TurnBudget
from .main_input import (
    project_capability_observation,
    project_main_context,
    project_main_read_context,
    project_tasks,
)
from .planning import planning_brief, required_information
from .task_context import (
    add_task,
    consume_active_question,
    normalize_workflow,
    set_active_question,
    transition_task,
)
from .turn_workspace import TurnWorkspace, WorkspaceError


_MAIN_CAPABILITIES = frozenset({
    'prepare_requirements', 'research', 'draft_itinerary', 'revise_itinerary',
})
_TERMINAL_TOOLS = frozenset({'ask_user', 'finish_turn'})


@dataclass(frozen=True)
class TaskExecutionContext:
    """Detached application snapshot consumed by one task-execution run."""

    trip: Any
    messages: list[Any] | tuple[Any, ...]
    idea_cards: list[Any] | tuple[Any, ...]
    plan: Any
    metrics: Mapping[str, Any]
    current_date: str
    entry: str = 'main'
    scope: str = ''
    target_item_id: str | None = None
    display_target: Mapping[str, Any] | None = None

    def __post_init__(self):
        if self.entry not in {'main', 'planning', 'revision', 'confirm'}:
            raise ValueError('未知任务执行入口')
        if self.scope not in {'', 'item', 'all'}:
            raise ValueError('任务执行范围无效')
        if self.display_target is not None and self.entry != 'revision':
            raise ValueError('展示目标只能用于修改入口')


@dataclass
class TaskExecutionRuntime:
    """Run the new main loop without strategy selection or Store authority."""

    client: Any
    model: str
    request_context: RequestContext
    execution_context: TaskExecutionContext
    request: Any
    trace: Any = None
    budget: TurnBudget | None = None
    capability_overrides: Mapping[str, Any] | None = None
    cache: dict = field(default_factory=dict)
    max_steps: int = 12
    max_corrections: int = 3
    finish_model_reserve: int = 1
    finish_seconds_reserve: float = 5.0

    def __post_init__(self):
        if not isinstance(self.request_context, RequestContext):
            raise ValueError('request_context必须由服务器创建')
        if self.request_context.strategy != 'task_execution':
            raise ValueError('TaskExecutionRuntime只接受task_execution策略')
        if not isinstance(self.execution_context, TaskExecutionContext):
            raise ValueError('execution_context类型不正确')
        if self.request_context.entry != self.execution_context.entry:
            raise ValueError('执行入口与可信请求不一致')
        self.snapshot = self._snapshot()
        self.budget = self.budget or TurnBudget(cancelled=self.snapshot.get('cancelled'))
        self.cache.setdefault('_budget', self.budget)
        self.config = execution_roles(self.execution_context.trip.mode if self.execution_context.trip else 'travel')['main_execution']
        self._schemas = {item['function']['name']: item['function']['parameters'] for item in self.config.tools}
        self._messages = self._message_ids()
        workflow = normalize_workflow(
            getattr(self.execution_context.trip, 'workflow_state', {}),
            messages=self._messages,
            plan_basis=self.request_context.plan_basis,
            requirements_revision=self.request_context.requirements_revision,
        )
        visible_tasks = [row['id'] for row in workflow['tasks']]
        self.workspace = TurnWorkspace(
            self.request_context,
            workflow_state=workflow,
            authorized_message_ids=self._messages,
            visible_task_ids=visible_tasks,
        )
        self.workflow = workflow
        self._base_workflow = deepcopy(workflow)
        self._workflow_snapshots: dict[str, dict] = {}
        self._answer_binding_refs: list[str] = []
        self.catalog: dict[str, Any] = {}
        self.capability_runtime = TravelAgentRuntime(
            self.client, self.model, self.snapshot, trace=self.trace,
            catalog=self.catalog, cache=self.cache, request=self.request,
        )
        self._repeat_failures: dict[tuple[str, str, str], int] = {}
        self._corrections = 0
        self._started = 0.0

    def _snapshot(self):
        context = self.execution_context
        return deepcopy({
            'trip': context.trip,
            'messages': list(context.messages),
            'idea_cards': list(context.idea_cards),
            'plan': context.plan,
            'metrics': dict(context.metrics),
            'user_body': self.request_context.body,
            'recent_chat': [
                {'id': getattr(row, 'id', ''), 'role': getattr(row, 'sender_type', ''),
                 'body': getattr(row, 'body', '')}
                for row in context.messages if getattr(row, 'status', 'sent') == 'sent'
            ][-8:],
            'current_date': context.current_date,
            'workflow_state': deepcopy(getattr(context.trip, 'workflow_state', {})),
            'cancelled': self.budget.cancelled if self.budget else None,
        })

    def _message_ids(self):
        values = [getattr(row, 'id', None) for row in self.execution_context.messages]
        return [value for value in values if isinstance(value, str) and value]

    def event(self, name, value):
        if self.trace:
            self.trace.add(name, value)

    @property
    def usage(self):
        return self.budget.usage()

    def run(self) -> TerminalProposal:
        self._started = monotonic()
        if self.execution_context.entry == 'confirm':
            return self._confirm_fast_path()
        if self.execution_context.entry in {'planning', 'revision'}:
            return self._capability_fast_path()

        messages = [
            {'role': 'system', 'content': self.config.prompt},
            {'role': 'user', 'content': json.dumps(self._initial_context(), ensure_ascii=False, default=str)},
        ]
        try:
            for step in range(self.max_steps):
                # Preserve a model call and a small wall-clock reserve until a
                # programmatic fail-close path is all that remains.
                if step < self.max_steps - 1:
                    self.budget.reserve(models=1, seconds=self.finish_seconds_reserve)
                else:
                    self.budget.reserve(models=1)
                response = self.request(
                    self.client, self.model, messages, tools=list(self.config.tools),
                    trace=self.trace, budget=self.budget,
                    reasoning_effort=role_reasoning_effort(self.config),
                    agent_role=self.config.name,
                )
                assistant = response.choices[0].message
                calls = list(getattr(assistant, 'tool_calls', None) or [])
                if not calls:
                    content = (assistant.content or '').strip()
                    if self._can_finish_prose():
                        return self.workspace.finish(content, workflow_state=self.workflow)
                    messages.extend([self._assistant_message(assistant, ()), {
                        'role': 'user',
                        'content': '本轮含写入候选或未完成事项，必须调用ask_user或finish_turn结构化结束；不要输出普通正文。',
                    }])
                    self._corrections += 1
                    if self._corrections > self.max_corrections:
                        return self.workspace.fail_closed('主Agent未按结构化合同结束本轮。')
                    continue
                if len(calls) != 1:
                    messages.append(self._assistant_message(assistant, calls))
                    self.budget.tools(len(calls))
                    for call in calls:
                        messages.append(self._tool_message(call, {
                            'status': 'unavailable',
                            'reason': '一次响应只能选择一个能力；本批调用均未执行。',
                        }))
                    self._corrections += 1
                    if self._corrections > self.max_corrections:
                        return self.workspace.fail_closed('主Agent重复返回多个能力调用。')
                    continue

                call = calls[0]
                self.budget.tools(1)
                messages.append(self._assistant_message(assistant, calls))
                output, terminal = self._execute_call(call)
                if terminal is not None:
                    return terminal
                messages.append(self._tool_message(call, output))
            return self.workspace.fail_closed('主Agent达到本轮工作步数上限。')
        except (AgentBudgetError, TimeoutError) as exc:
            return self.workspace.fail_closed(str(exc))
        except Exception as exc:
            self.event('Task Runtime异常', {'error_type': type(exc).__name__, 'reason': str(exc)[:500]})
            return self.workspace.fail_closed('本次任务执行未完成，当前需求和行程已保留。')
        finally:
            self.event('Task Runtime预算', self.usage)
            self.event('Task Runtime耗时', {'seconds': round(monotonic() - self._started, 3)})

    def _initial_context(self):
        return project_main_context(
            request_context=self.request_context,
            execution_context=self.execution_context,
            workflow=self.workflow,
            recent_chat=self.execution_context.messages,
            plan=self.execution_context.plan,
            missing_requirements=required_information(self.execution_context.trip),
        )

    def _execute_call(self, call):
        name = call.function.name
        try:
            args = json.loads(call.function.arguments or '{}')
            if not isinstance(args, dict) or name not in self._schemas:
                raise ValueError('未知主工具或参数不是对象')
            Draft202012Validator(self._schemas[name]).validate(args)
            if name in _MAIN_CAPABILITIES:
                if name == 'prepare_requirements' and args['message_ref'] != self.request_context.message_id:
                    raise ValueError('需求依据必须引用服务器提供的本轮完整消息')
                task_ref = args.get('task_ref')
                if task_ref is not None and task_ref not in {row['id'] for row in self.workflow['tasks']}:
                    raise ValueError('专业能力引用了不可见的会话任务')
                if name in {'prepare_requirements', 'research'}:
                    self._bind_active_answer(args)
                result = self._run_capability(name, args)
                return self._result_output(result), None
            if name == 'read_context':
                return self._read_context(args), None
            if name == 'manage_task':
                return self._manage_task(args), None
            if name == 'confirm_itinerary':
                return self._confirm(args), None
            if name in _TERMINAL_TOOLS:
                return {}, self._terminal(name, args)
            raise ValueError('未知主工具')
        except (ValueError, TypeError, KeyError, ValidationError, WorkspaceError) as exc:
            key = (name, call.function.arguments or '{}', str(exc))
            self._repeat_failures[key] = self._repeat_failures.get(key, 0) + 1
            self._corrections += 1
            if self._repeat_failures[key] > 1 or self._corrections > self.max_corrections:
                return {
                    'status': 'failed', 'recoverable': False,
                    'reason': '相同无效调用已重复，必须安全结束或提出澄清。',
                }, None
            return {'status': 'unavailable', 'recoverable': True, 'reason': str(exc)}, None

    def _bind_active_answer(self, args):
        question_ref = args.get('answer_question_ref')
        if question_ref is None:
            return
        active = self.workflow.get('active_question')
        if not active or active.get('id') != question_ref:
            raise ValueError('answer_question_ref不是当前唯一活动追问')
        if args.get('task_ref') != active.get('task_id'):
            raise ValueError('短答只能绑定活动追问所属任务')
        if self.request_context.conversation_task_id not in {None, active.get('task_id')}:
            raise ValueError('请求绑定的会话任务与活动追问不一致')
        namespace = str((active.get('answer_target') or {}).get('namespace') or '')
        if 'effect' in args and namespace.startswith(('research.', 'hotel.', 'weather.', 'route.')):
            raise ValueError('该追问答案属于任务局部查询，不能直接写正式旅行需求')
        self.workflow, consumed = consume_active_question(
            self.workflow, question_id=question_ref,
            answer_message_id=self.request_context.message_id,
        )
        # Preserve the original question's sources/answer target as data; consuming
        # it authorizes this task-local answer use, not a formal requirements write.
        self.workspace.set_workflow_candidate(self.workflow)
        artifact = self.workspace.register_artifact('task', {
            'operation': 'consume_answer', 'task_ref': consumed['task_id'],
            'goal': self._task_goal(consumed['task_id']),
            'effect_kind': next(row['effect_kind'] for row in self.workflow['tasks']
                                if row['id'] == consumed['task_id']),
            'effect_refs': [], 'question_ref': question_ref,
            'answer_target': deepcopy(consumed['answer_target']),
            'source_message_ids': list(consumed['source_message_ids']),
            'answer_message_ref': self.request_context.message_id,
        }, source_capability='manage_task', conversation_task_id=consumed['task_id'])
        self._workflow_snapshots[artifact.id] = deepcopy(self.workflow)
        self._answer_binding_refs.append(artifact.id)
        args['_bound_answer'] = {
            'question_ref': question_ref, 'task_ref': consumed['task_id'],
            'answer_target': deepcopy(consumed['answer_target']),
            'source_message_ids': list(consumed['source_message_ids']),
            'task_effect_ref': artifact.id,
        }

    def _run_capability(self, name, args):
        override = (self.capability_overrides or {}).get(name)
        if override is not None:
            result = override(args, self.workspace, self) if callable(override) else override
            return self.workspace.observe(result)
        requirements_ref = args.get('requirements_ref')
        if requirements_ref is not None:
            artifact = self.workspace.get_artifact(requirements_ref)
            if artifact.kind != 'requirements' or self.workspace.invalidation_reason(requirements_ref):
                raise ValueError('需求候选引用无效或已过期')
            self._apply_requirements_candidate(artifact)
        if name == 'draft_itinerary':
            _, question = planning_brief(self.capability_runtime.trip, require_all=True)
            if question:
                # Missing saved prerequisites are a user question, not proof that
                # this message contains an unprocessed requirements change.
                from .agent_protocol import CapabilityResult
                return self.workspace.observe(CapabilityResult(CapabilityStatus.NEEDS_INPUT, name,
                    summary=question, input_request=self._capability_question(name, question)))
        if name == 'revise_itinerary':
            if not self.execution_context.plan or not self.execution_context.plan.has_content:
                from .agent_protocol import CapabilityResult
                return self.workspace.observe(CapabilityResult(CapabilityStatus.NEEDS_INPUT, name,
                    summary='当前没有可修改的行程。',
                    input_request=self._capability_question(name, '当前没有可修改的行程，请先生成方案。')))
            if args.get('plan_ref') != self.request_context.plan_basis.plan_id:
                raise ValueError('行程引用不是当前已提交方案')
        task = args.get('question') or args.get('constraints') or args.get('instruction') or self.request_context.body
        bound = args.get('_bound_answer')
        if bound:
            task = json.dumps({'task': task, 'bound_answer': bound,
                'instruction': 'bound_answer仅用于answer_target指定的任务局部用途；除非用户明确采纳为正式旅行需求，不能写入其他字段。'},
                ensure_ascii=False)
        policy = self._candidate_policy(name, args)
        with self.budget.child_scope(reserve_models=self.finish_model_reserve,
                                     reserve_seconds=self.finish_seconds_reserve,
                                     max_models=execution_roles(self.execution_context.trip.mode)[
                                         {'prepare_requirements': 'requirements_capability',
                                          'research': 'research_capability',
                                          'draft_itinerary': 'planning_capability',
                                          'revise_itinerary': 'revision_capability'}[name]
                                     ].max_steps):
            return self.capability_runtime.run_capability(
                name, task, workspace=self.workspace, requirements_ref=requirements_ref,
                scope=args.get('scope', self.execution_context.scope),
                target_item_id=args.get('target_item_id', self.execution_context.target_item_id),
                display_target=self.execution_context.display_target,
                conversation_task_id=args.get('task_ref') or self.request_context.conversation_task_id,
                question_factory=self._capability_question,
                candidate_policy=policy,
            )

    def _apply_requirements_candidate(self, artifact):
        from .requirements import chat_changes, update_requirements
        payload = thaw(artifact.payload)
        # Re-derive the candidate from the immutable request, then apply it only
        # to the detached capability snapshot. The Store will repeat the same
        # validation against current state at commit time.
        changes = chat_changes(payload['raw_patch'], self.request_context.body,
                               self.capability_runtime.original_trip)
        if changes != payload['normalized_changes']:
            raise ValueError('需求候选与原始消息的本地校验不一致')
        updated = update_requirements(self.capability_runtime.original_trip, changes)
        self.capability_runtime.trip = updated
        self.capability_runtime.patch = {}
        self.capability_runtime.changes = {}
        self.capability_runtime._merge_places({})

    def _candidate_policy(self, capability, args):
        if capability not in {'prepare_requirements', 'draft_itinerary', 'revise_itinerary'}:
            return {'effect': 'independent', 'condition': 'satisfied', 'evidence_refs': ()}
        effect = args.get('effect', 'independent')
        condition = args.get('condition', 'satisfied')
        refs = tuple(args.get('condition_evidence_refs') or ())
        if effect == 'independent' and (condition != 'satisfied' or refs):
            raise ValueError('独立效果不能携带未决条件或条件证据')
        if effect == 'hypothetical':
            if capability == 'prepare_requirements':
                raise ValueError('假设值不能进入正式需求能力')
            condition = 'unknown' if condition == 'satisfied' else condition
        if effect == 'conditional':
            if not refs:
                raise ValueError('条件写入必须引用本轮服务器登记的证据')
            self._validate_condition_evidence(refs, args.get('condition_predicate'), condition)
        return {'effect': effect, 'condition': condition, 'evidence_refs': refs}

    def _validate_condition_evidence(self, refs, predicate, claimed_condition):
        if not isinstance(predicate, dict) or predicate.get('kind') != 'weather_precipitation_at_least':
            raise ValueError('首版仅支持结构化天气降雨阈值条件；其他条件保持unknown或向用户澄清')
        requirements = self.execution_context.trip.requirements
        city = str(predicate.get('city') or '').removesuffix('市')
        date = predicate.get('date')
        threshold = predicate.get('threshold')
        expected_city = (requirements.city or '').removesuffix('市')
        if not city or expected_city and city != expected_city:
            raise ValueError('天气条件必须明确绑定当前旅行城市')
        if not isinstance(date, str) or not date or (requirements.start_date and not (
            requirements.start_date <= date <= requirements.end_date
        )):
            raise ValueError('天气条件必须明确绑定当前旅行日期')
        observed = None
        for ref in refs:
            artifact = self.workspace.get_artifact(ref)
            if artifact.kind != 'evidence' or artifact.source_capability != 'research':
                raise ValueError('天气条件必须引用本轮research登记的证据')
            payload = thaw(artifact.payload)
            if payload.get('evidence_kind') != 'weather':
                raise ValueError('非天气证据不能证明降雨条件')
            for row in payload.get('results') or []:
                if not isinstance(row, dict) or row.get('tool') != 'query_weather':
                    continue
                result = row.get('result') or {}
                if (result.get('status') == 'available'
                        and str(result.get('city') or '').removesuffix('市') == city
                        and result.get('requested_date') == date
                        and isinstance(result.get('precipitation_probability_max'), (int, float))):
                    observed = result['precipitation_probability_max']
        if observed is None:
            raise ValueError('天气证据缺少同城同日可用降雨概率')
        actual = 'satisfied' if observed >= threshold else 'not_satisfied'
        if claimed_condition != actual:
            raise ValueError('模型条件判断与结构化天气证据不一致')

    def _result_output(self, result):
        return project_capability_observation(result)

    def _read_context(self, args):
        if args['section'] == 'tasks':
            query = args.get('query', '').casefold()
            projected = project_tasks(self.workflow, message_ids=self._messages,
                plan_basis_value=self.request_context.plan_basis,
                requirements_revision=self.request_context.requirements_revision)
            tasks = projected['open_tasks']
            if query:
                tasks = [row for row in tasks if query in json.dumps(row, ensure_ascii=False).casefold()]
            offset, limit = args.get('offset', 0), args.get('limit', 20)
            return {'focus_task_ref': projected['focus_task_ref'], 'active_question': projected['active_question'],
                    'items': tasks[offset:offset + limit], 'total': len(tasks)}
        section = args['section']
        if section == 'evidence':
            raw = {
                'results': list(self.capability_runtime.plan.evidence if self.capability_runtime.plan else ())
            }
            for artifact in self.workspace.artifacts:
                if artifact.kind != 'evidence' or self.workspace.invalidation_reason(artifact.id):
                    continue
                payload = thaw(artifact.payload)
                raw['results'].append({
                    '_artifact_payload': payload,
                    '_source_capability': artifact.source_capability,
                    '_artifact_ref': artifact.id,
                })
        elif section == 'messages':
            # Read from the detached snapshot directly so the shared read contract
            # cannot double-page or expose sender/provider metadata to main.
            raw = {'items': self.capability_runtime.snapshot.get('messages', ())}
            if not raw['items']:
                raw = self.capability_runtime.read_context(args)
        elif section == 'ideas':
            raw = {'items': self.capability_runtime.snapshot.get('idea_cards', ())}
            if not raw['items']:
                raw = self.capability_runtime.read_context(args)
        else:
            raw = {}
        return project_main_read_context(
            section, raw,
            trip=self.capability_runtime.trip,
            plan=self.capability_runtime.plan,
            expected_basis=self.request_context.plan_basis,
            current_message_id=self.request_context.message_id,
            request_args=args,
        )

    def _basis(self):
        return {
            'requirements_revision': self.request_context.requirements_revision,
            'plan_basis': self.request_context.plan_basis.to_dict(),
            'workflow_revision': self.request_context.workflow_revision,
        }

    def _manage_task(self, args):
        operation = args['operation']
        task_ref = args.get('task_ref')
        goal = (args.get('goal') or '').strip()
        effect_refs = list(args.get('effect_refs') or ())
        unresolved = list(args.get('unresolved_items') or ())
        if operation == 'create':
            if not goal:
                raise ValueError('新任务需要明确目标')
            task_ref = self.workspace.reserve_task_id()
            self.workflow, task_ref = add_task(
                self.workflow, goal=goal, effect_kind=args['effect_kind'],
                source_message_ids=[self.request_context.message_id],
                authorization_refs=[self.request_context.message_id],
                basis={
                    'requirements_revision': self.request_context.requirements_revision,
                    'plan_basis': self.request_context.plan_basis.to_dict()
                    if self.request_context.plan_basis.has_content else None,
                }, task_id=task_ref,
            )
            self.workspace.set_workflow_candidate(self.workflow)
            self.workspace.allow_task(task_ref)
            artifact = self.workspace.register_artifact('task', {
                'operation': operation, 'task_ref': task_ref, 'goal': goal,
                'effect_kind': args['effect_kind'], 'effect_refs': [], 'unresolved_items': unresolved,
            }, source_capability='manage_task', conversation_task_id=task_ref)
            self._workflow_snapshots[artifact.id] = deepcopy(self.workflow)
            return {'status': 'ok', 'task_ref': task_ref, 'artifact_ref': artifact.id,
                    'instruction': '任务候选尚未提交；结束时必须把artifact_ref放入adopt_refs。'}
        if not task_ref or task_ref not in {row['id'] for row in self.workflow['tasks']}:
            raise ValueError('任务引用不可见或已经失效')
        statuses = {
            'complete': 'completed', 'suspend': 'suspended', 'resume': 'active', 'cancel': 'cancelled',
        }
        if operation == 'adopt':
            for ref in effect_refs:
                artifact = self.workspace.get_artifact(ref)
                self.workspace.adopt(ref)
                if artifact.kind == 'task' and artifact.payload.get('operation') in {'cancel', 'suspend'}:
                    state = self._workflow_snapshots.get(ref)
                    if state is None:
                        raise ValueError('取消/挂起候选缺少服务器工作流快照')
                    self.workflow = deepcopy(state)
                    self.workspace.set_workflow_candidate(self.workflow)
                    self.workspace.adopt_workflow()
            return {'status': 'ok', 'task_ref': task_ref, 'adopted_refs': effect_refs,
                    'ready_to_commit': [row.id for row in self.workspace.ready_to_commit]}
        if operation not in statuses:
            raise ValueError('任务操作无效')
        for ref in effect_refs:
            artifact = self.workspace.get_artifact(ref)
            if artifact.kind == 'evidence' or artifact.conversation_task_id not in {None, task_ref}:
                raise ValueError('效果引用不属于该任务')
            self.workspace.adopt(ref)
        effect_kind = next(row['effect_kind'] for row in self.workflow['tasks'] if row['id'] == task_ref)
        if args['effect_kind'] != effect_kind:
            raise ValueError('任务效果类型不能由后续调用改写')
        if operation == 'complete':
            self._validate_task_effects(effect_kind, effect_refs)
        self.workflow = transition_task(
            self.workflow, task_ref, statuses[operation], reason=args.get('item') or '',
            progress=effect_refs if effect_refs else None,
            unresolved_items=unresolved if unresolved else None,
        )
        self.workspace.set_workflow_candidate(self.workflow)
        artifact = self.workspace.register_artifact('task', {
            'operation': operation, 'task_ref': task_ref, 'goal': self._task_goal(task_ref),
            'effect_kind': effect_kind, 'effect_refs': effect_refs, 'unresolved_items': unresolved,
        }, source_capability='manage_task', conversation_task_id=task_ref,
           depends_on=effect_refs)
        self._workflow_snapshots[artifact.id] = deepcopy(self.workflow)
        if operation in {'cancel', 'suspend'}:
            # A subsequent explicit adopt marks this independent task effect ready;
            # fail_closed can then retain it without another model request.
            return {'status': 'ok', 'task_ref': task_ref, 'artifact_ref': artifact.id,
                    'instruction': '取消/挂起候选尚未提交；调用manage_task adopt(effect_refs=[artifact_ref])后才可在后续失败时保留。'}
        return {'status': 'ok', 'task_ref': task_ref, 'artifact_ref': artifact.id,
                'instruction': '任务候选尚未提交；结束时必须把artifact_ref放入adopt_refs。'}

    def _task_goal(self, task_ref):
        return next(row['goal'] for row in self.workflow['tasks'] if row['id'] == task_ref)

    def _validate_task_effects(self, effect_kind, effect_refs):
        kinds = {self.workspace.get_artifact(ref).kind for ref in effect_refs}
        expected = {
            'requirements': {'requirements'}, 'itinerary': {'itinerary'},
            'confirmation': {'confirmation'},
        }
        if effect_kind == 'read':
            if effect_refs:
                raise ValueError('只读任务完成不采用业务写入候选')
            return
        if not effect_refs:
            raise ValueError('写入任务完成必须引用已验证并采用的业务效果')
        if effect_kind == 'mixed':
            if not kinds or kinds <= {'task', 'evidence'}:
                raise ValueError('混合任务完成缺少业务效果引用')
            return
        if not expected[effect_kind] <= kinds:
            raise ValueError('任务效果引用种类与声明的effect_kind不一致')

    def _confirm(self, args):
        if not self.request_context.plan_basis.has_content:
            raise ValueError('当前没有可确认的行程')
        if args['plan_ref'] != self.request_context.plan_basis.plan_id:
            raise ValueError('只能确认用户已看到的当前方案')
        artifact = self.workspace.register_artifact(
            'confirmation', {'plan_basis': self.request_context.plan_basis.to_dict()},
            source_capability='confirm_itinerary',
        )
        return {'status': 'ok', 'artifact_ref': artifact.id}

    def _terminal(self, name, args):
        selected = list(args.get('adopt_refs') or ())
        for ref in selected:
            self.workspace.get_artifact(ref)
        # Answer binding is a server-validated prerequisite for using a short
        # reply in a capability; it must travel with any workflow commit.
        for ref in self._answer_binding_refs:
            if ref not in selected:
                selected.insert(0, ref)
        if name == 'finish_turn':
            self._apply_item_completion(args, selected)
            workflow = self._selected_workflow(selected)
            return self.workspace.finish(
                args['reply'].strip(), selected_artifact_ids=selected,
                workflow_state=workflow,
            )
        return self._ask(args, selected)

    def _selected_workflow(self, selected):
        task_refs = [ref for ref in selected if self.workspace.get_artifact(ref).kind == 'task']
        if not task_refs:
            return self._base_workflow
        missing = [ref for ref in task_refs if ref not in self._workflow_snapshots]
        if missing:
            raise ValueError('任务候选没有对应的服务器工作流快照')
        # Snapshots are cumulative and created in call order.  The latest selected
        # task effect contains all earlier selected transitions; rejecting a
        # skipped earlier task avoids committing an accidental intermediate diff.
        latest = task_refs[-1]
        selected_state = deepcopy(self._workflow_snapshots[latest])
        changed = self._workflow_changed_task_ids(self._base_workflow, selected_state)
        selected_task_ids = {self.workspace.get_artifact(ref).payload['task_ref'] for ref in task_refs}
        if changed - selected_task_ids:
            raise ValueError('终止选择遗漏了支撑工作流变化的任务候选')
        return selected_state

    @staticmethod
    def _workflow_changed_task_ids(before, after):
        left = {row['id']: row for row in before['tasks']}
        right = {row['id']: row for row in after['tasks']}
        return {task_id for task_id in set(left) | set(right) if left.get(task_id) != right.get(task_id)}

    def _apply_item_completion(self, args, selected):
        # Free-form item strings aid model bookkeeping only. Workflow writes are
        # performed through manage_task, whose task artifacts bind effect refs.
        if any(self.workspace.get_artifact(ref).kind == 'task' for ref in selected):
            return

    def _ask(self, args, selected):
        current_workflow = deepcopy(self.workflow)
        self.workflow = self._selected_workflow(selected)
        task_ref = args.get('task_ref')
        # A child needs_input may have created a server-owned task solely for
        # this terminal question. The question itself is the Store-verifiable
        # support for that new task, so it need not have a selected task artifact.
        if task_ref and task_ref not in {row['id'] for row in self.workflow['tasks']}:
            if task_ref in {row['id'] for row in current_workflow['tasks']}:
                self.workflow = current_workflow
        if not task_ref:
            task_ref = self.workspace.reserve_task_id()
            self.workflow, task_ref = add_task(
                self.workflow, goal=self.request_context.body or args['question'],
                source_message_ids=[self.request_context.message_id], effect_kind='read',
                authorization_refs=[self.request_context.message_id],
                basis={
                    'requirements_revision': self.request_context.requirements_revision,
                    'plan_basis': self.request_context.plan_basis.to_dict()
                    if self.request_context.plan_basis.has_content else None,
                }, task_id=task_ref,
            )
            self.workspace.set_workflow_candidate(self.workflow)
            self.workspace.allow_task(task_ref)
        elif task_ref not in {row['id'] for row in self.workflow['tasks']}:
            raise ValueError('追问任务引用不可见')
        pending_message_id = self.workspace.reserve_message_id()
        self.workflow, raw = set_active_question(
            self.workflow, task_id=task_ref, text=args['question'],
            answer_target=args['answer_target'], assistant_message_id=pending_message_id,
            source_message_ids=[self.request_context.message_id],
        )
        self.workspace.set_workflow_candidate(self.workflow)
        question = TerminalQuestion(
            raw['id'], task_ref, raw['text'], FrozenDict(raw['answer_target']),
            tuple(raw['source_message_ids']),
        )
        return self.workspace.finish(
            '', selected_artifact_ids=selected, workflow_state=self.workflow, question=question,
        )

    def _capability_question(self, namespace, text, answer_target=None):
        task_ref = self.request_context.conversation_task_id
        if not task_ref:
            task_ref = self.workspace.reserve_task_id()
            self.workflow, task_ref = add_task(
                self.workflow, goal=self.request_context.body or text, effect_kind='read',
                source_message_ids=[self.request_context.message_id],
                authorization_refs=[self.request_context.message_id],
                task_id=task_ref,
            )
            self.workspace.set_workflow_candidate(self.workflow)
            self.workspace.allow_task(task_ref)
        return TerminalQuestion(
            f'question-{uuid4().hex}', task_ref, text,
            FrozenDict(answer_target or {'namespace': namespace, 'field': 'clarification'}),
            (self.request_context.message_id,),
        )

    def _can_finish_prose(self):
        return not self.workspace.artifacts and self.workspace.workflow_candidate is None

    def _confirm_fast_path(self):
        output = self._confirm({'plan_ref': self.request_context.plan_basis.plan_id})
        return self.workspace.finish(selected_artifact_ids=[output['artifact_ref']], workflow_state=self.workflow)

    def _capability_fast_path(self):
        name = 'draft_itinerary' if self.execution_context.entry == 'planning' else 'revise_itinerary'
        args = ({'task_ref': self.request_context.conversation_task_id, 'requirements_ref': None,
                 'constraints': self.request_context.body}
                if name == 'draft_itinerary' else
                {'task_ref': self.request_context.conversation_task_id,
                 'plan_ref': self.request_context.plan_basis.plan_id,
                 'requirements_ref': None, 'scope': self.execution_context.scope or 'all',
                 'instruction': self.request_context.body,
                 'target_item_id': self.execution_context.target_item_id})
        try:
            result = self._run_capability(name, args)
            if result.status == CapabilityStatus.NEEDS_REQUIREMENTS:
                requirements = self._run_capability('prepare_requirements', {
                    'task_ref': self.request_context.conversation_task_id,
                    'message_ref': self.request_context.message_id,
                    'answer_question_ref': None,
                })
                requirement_ref = requirements.artifact_refs[-1] if requirements.artifact_refs else None
                if requirements.status == CapabilityStatus.OK and requirement_ref:
                    args['requirements_ref'] = requirement_ref
                    result = self._run_capability(name, args)
            if result.status == CapabilityStatus.OK and result.artifact_refs:
                selected = []
                for ref in result.artifact_refs:
                    artifact = self.workspace.get_artifact(ref)
                    selected.extend(item for item in artifact.depends_on if item not in selected)
                    if ref not in selected:
                        selected.append(ref)
                return self.workspace.finish(selected_artifact_ids=selected,
                                             evidence_refs=result.evidence_refs,
                                             workflow_state=self.workflow)
            if result.status == CapabilityStatus.NEEDS_INPUT:
                return self._ask({
                    'task_ref': result.input_request.conversation_task_id if result.input_request else None,
                    'question': result.input_request.text if result.input_request else result.summary,
                    'answer_target': thaw(result.input_request.answer_target)
                    if result.input_request else {'namespace': name, 'field': 'clarification'},
                }, [])
            return self.workspace.fail_closed(result.error or result.summary or '专业能力未完成。')
        except Exception as exc:
            return self.workspace.fail_closed(str(exc))

    @staticmethod
    def _assistant_message(assistant, calls):
        value = {'role': 'assistant', 'content': assistant.content or '', 'tool_calls': [
            {'id': call.id, 'type': 'function', 'function': {
                'name': call.function.name, 'arguments': call.function.arguments or '{}',
            }} for call in calls]}
        wire = getattr(assistant, '_responses_output', None)
        if wire is not None:
            value['_responses_output'] = wire
        return value

    @staticmethod
    def _tool_message(call, output):
        return {
            'role': 'tool', 'tool_call_id': call.id, 'name': call.function.name,
            'content': json.dumps(output, ensure_ascii=False, default=str),
        }


__all__ = ['TaskExecutionContext', 'TaskExecutionRuntime']
