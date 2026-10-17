"""Shared tool executor and professional capabilities for the task_execution framework."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import json
import os
from time import monotonic
from jsonschema import Draft202012Validator

from .agent_roles import roles, execution_roles, role_reasoning_effort
from .requirements import chat_changes, update_requirements, LIST_FIELDS
from .planning import planning_brief, required_information
from .planning_evidence import critical_route_review
from .places import collect_places
from jsonschema import ValidationError
from .plan_display import validate_display, display_format, text_hash, parse_display_output
from .tasks import validate_display_target_metadata


_QUOTE_FONTS = str.maketrans({'“': '"', '”': '"', '‘': "'", '’': "'", '＂': '"', '＇': "'"})


def _reference_text(value):
    """Only quotation glyphs vary; words, numbers and whitespace stay exact."""
    return value.translate(_QUOTE_FONTS)


def _unique_reference(source, fragment):
    reference, original = _reference_text(fragment), _reference_text(source)
    first = original.find(reference)
    if not reference or first < 0 or original.find(reference, first + 1) >= 0:
        raise ValueError('原文目标须唯一匹配；请提供足够上下文')
    return source[first:first + len(fragment)]


class AgentBudgetError(RuntimeError):
    pass


@dataclass
class TurnBudget:
    max_models: int = 16
    max_tools: int = 64
    max_external: int = 32
    seconds: float = 240
    cancelled: object = None
    model_calls: int = 0
    tool_calls: int = 0
    external_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_token_limit: int | None = None
    reasoning_rejections: set = field(default_factory=set)

    def __post_init__(self):
        self.deadline = monotonic() + self.seconds

    def check_current(self):
        if self.cancelled and self.cancelled():
            raise AgentBudgetError('本次任务已由新消息替代。')

    def remaining(self):
        self.check_current()
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise AgentBudgetError('本次任务已达到时间上限，原行程已保留。')
        return remaining

    @property
    def total_tokens(self):
        return self.input_tokens + self.output_tokens

    def reserve(self, *, models=0, tools=0, external=0, seconds=0.0):
        """Check that work plus a caller-declared finishing reserve can still fit."""
        remaining = self.remaining()
        if self.model_calls + models > self.max_models:
            raise AgentBudgetError('本次模型调用次数已达上限，原行程已保留。')
        if self.tool_calls + tools > self.max_tools:
            raise AgentBudgetError('本次工具调用次数已达上限，未执行超额工具。')
        if self.external_calls + external > self.max_external:
            raise AgentBudgetError('本次外部查询次数已达上限，剩余事实待核实。')
        if remaining <= seconds:
            raise AgentBudgetError('本次任务剩余时间不足以安全执行并收尾。')

    def model(self):
        self.reserve(models=1)
        if self.total_token_limit is not None and self.total_tokens >= self.total_token_limit:
            raise AgentBudgetError('本次模型令牌预算已用尽，未开始新的模型调用。')
        self.model_calls += 1

    def record_usage(self, input_tokens=None, output_tokens=None):
        for value, field_name in ((input_tokens, 'input_tokens'), (output_tokens, 'output_tokens')):
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                setattr(self, field_name, getattr(self, field_name) + value)
        if self.total_token_limit is not None and self.total_tokens > self.total_token_limit:
            raise AgentBudgetError('本次模型令牌预算已超出，丢弃当前结果并安全停止。')

    def usage(self):
        return {'model_calls': self.model_calls, 'tool_calls': self.tool_calls,
                'external_calls': self.external_calls, 'input_tokens': self.input_tokens,
                'output_tokens': self.output_tokens, 'total_tokens': self.total_tokens}

    @contextmanager
    def child_scope(self, *, reserve_models=1, reserve_seconds=5.0, max_models=None):
        """Temporarily bound a serial child while preserving root counters."""
        self.reserve(models=reserve_models, seconds=reserve_seconds)
        old_max_models, old_deadline = self.max_models, self.deadline
        available = old_max_models - self.model_calls - reserve_models
        if max_models is not None:
            available = min(available, max_models)
        if available <= 0:
            raise AgentBudgetError('本轮剩余模型额度只够父Agent安全收尾。')
        child_deadline = old_deadline - reserve_seconds
        if child_deadline <= monotonic():
            raise AgentBudgetError('本轮剩余时间只够父Agent安全收尾。')
        self.max_models = self.model_calls + available
        self.deadline = child_deadline
        try:
            yield self
        finally:
            self.max_models = old_max_models
            self.deadline = old_deadline

    def tools(self, count):
        if type(count) is not int or count < 0:
            raise AgentBudgetError('工具调用计数无效。')
        self.reserve(tools=count)
        self.tool_calls += count

    def external(self):
        self.reserve(external=1)
        self.external_calls += 1


class TravelAgentRuntime:
    def __init__(self, client, model, snapshot, *, trace=None, catalog=None, cache=None, request=None):
        self.client, self.model, self.trace = client, model, trace
        self.snapshot = deepcopy(snapshot)
        self.trip = self.snapshot.get('trip')
        self.original_trip = deepcopy(self.trip)
        self.plan = self.snapshot.get('plan')
        self.user_body = self.snapshot.get('user_body', '')
        self.catalog = catalog if catalog is not None else {}
        self.cache = cache if cache is not None else {}
        self.budget = self.cache.setdefault('_budget', TurnBudget(cancelled=self.snapshot.get('cancelled')))
        self.request = request
        self.configs = roles(self.trip.mode if self.trip else 'travel')
        saved_places = collect_places([
            {'role': 'tool', 'name': 'search_places', 'content': json.dumps(row['result'], ensure_ascii=False)}
            for row in (self.plan.evidence if self.plan else [])
            if isinstance(row, dict) and row.get('tool') == 'search_places' and isinstance(row.get('result'), dict)
        ])
        self._merge_places(saved_places)
        self.patch, self.changes, self.ideas = {}, {}, []
        self.evidence = []
        # Successful read-only results are reusable only inside this runtime.
        self.external_results = {}
        self.requirements_checked = False
        self.requirement_completion_attempts = 0
        self.used_saved_evidence = False
        self.route_review_enabled = False
        self.last_response = None
        self.action = 'reply'
        self.entry_role = None
        self.display_target = None

    def _merge_places(self, places):
        self.catalog.update(deepcopy(places))
        if self.trip and self.trip.mode == 'city':
            city = self.trip.requirements.city.removesuffix('市')
            for poi_id, place in list(self.catalog.items()):
                place_city = place.get('city') if isinstance(place, dict) else None
                if not isinstance(place_city, str) or place_city.removesuffix('市') != city:
                    del self.catalog[poi_id]
            # Current confirmed lodging wins over a historical result with the same ID.
            if self.trip.lodging:
                self.catalog[self.trip.lodging['poi_id']] = deepcopy(self.trip.lodging)

    def event(self, name, value):
        if self.trace:
            self.trace.add(name, value)

    def model_node(self, role, payload, *, response_format=None):
        config = self.configs[role]
        messages = [{'role': 'system', 'content': config.prompt},
                    {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
        started = monotonic()
        try:
            response = self.request(self.client, self.model, messages, tools=None,
                response_format=response_format or config.response_format, trace=self.trace, budget=self.budget,
                reasoning_effort=role_reasoning_effort(config), agent_role=role)
            self.budget.remaining()
            self.last_response = response
            message = response.choices[0].message
            if getattr(message, 'tool_calls', None):
                raise ValueError('无工具模型节点不能调用工具')
            content = (message.content or '').strip()
            if not content:
                raise ValueError('Empty model response')
            return content
        finally:
            self.event('Workflow 模型节点', {'node': role, 'seconds': round(monotonic() - started, 3)})

    def run_capability(self, capability, task, *, workspace, requirements_ref=None,
                       scope='', target_item_id=None, display_target=None,
                       conversation_task_id=None, question_factory=None, candidate_policy=None):
        """Execute one professional capability without finalizing the parent turn.

        This intentionally does not call ``run(role=...)``: per-call state is
        reset here while the parent workspace, budget, cache and evidence remain
        shared.  Planning/revision cannot write requirements in this path.
        """
        if capability not in {'prepare_requirements', 'research', 'draft_itinerary', 'revise_itinerary'}:
            raise ValueError('未知专业能力')
        role = {'prepare_requirements': 'requirements', 'research': 'research',
                'draft_itinerary': 'planning', 'revise_itinerary': 'revision'}[capability]
        previous = (self.entry_role, self.action, self.display_target, self.requirements_checked,
                    self.route_review_enabled, deepcopy(self.patch), deepcopy(self.changes), deepcopy(self.ideas),
                    deepcopy(self.evidence), self.used_saved_evidence)
        self.entry_role = 'task_execution'
        self.action = {'prepare_requirements': 'update_requirements', 'research': 'reply',
                       'draft_itinerary': 'generate', 'revise_itinerary': 'modify'}[capability]
        self.display_target = validate_display_target_metadata(self.plan, display_target) if display_target else None
        self.requirements_checked = capability in {'draft_itinerary', 'revise_itinerary'}
        self.route_review_enabled = False
        self.patch, self.changes, self.ideas, self.evidence = {}, {}, [], []
        self.used_saved_evidence = False
        try:
            return self._run_capability_loop(capability, role, task, workspace=workspace,
                                             requirements_ref=requirements_ref, scope=scope,
                                             target_item_id=target_item_id,
                                             conversation_task_id=conversation_task_id,
                                             question_factory=question_factory,
                                             candidate_policy=candidate_policy or {})
        finally:
            (self.entry_role, self.action, self.display_target, self.requirements_checked,
             self.route_review_enabled, self.patch, self.changes, self.ideas,
             self.evidence, self.used_saved_evidence) = previous

    def _run_capability_loop(self, capability, legacy_role, task, *, workspace,
                             requirements_ref=None, scope='', target_item_id=None,
                             conversation_task_id=None, question_factory=None, candidate_policy=None):
        from .agent_protocol import CapabilityResult, CapabilityStatus
        config = execution_roles(self.trip.mode if self.trip else 'travel')[f'{legacy_role}_capability']
        specs = list(config.tools)
        allowed = {spec['function']['name']: spec for spec in specs}
        payload = {'capability_request': task, 'user_message': self.user_body,
                   'request_message_ref': workspace.context.message_id,
                   'conversation_task_ref': workspace.context.conversation_task_id,
                   'requirements_ref': requirements_ref, 'scope': scope,
                   'target_item_id': target_item_id,
                   'current_date': self.snapshot.get('current_date'),
                   'requirements': self.read_context({'section': 'requirements'}),
                   'plan_basis': workspace.context.plan_basis.to_dict(),
                   'external_query_budget': {'remaining': self.budget.max_external - self.budget.external_calls,
                                             'search_max_calls': 6}}
        if self.display_target is not None:
            payload['display_target'] = deepcopy(self.display_target)
        messages = [{'role': 'system', 'content': config.prompt},
                    {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
        repeated_failures, calls_seen = {}, 0
        for _ in range(config.max_steps):
            self.budget.reserve(models=1)
            response = self.request(self.client, self.model, messages, tools=specs, trace=self.trace,
                budget=self.budget, reasoning_effort=role_reasoning_effort(config, scope), agent_role=config.name)
            self.last_response = response
            assistant = response.choices[0].message
            calls = list(getattr(assistant, 'tool_calls', None) or [])
            if not calls:
                # Capability prose is never a completion contract.
                key = ('missing_complete_capability', (assistant.content or '').strip()[:200])
                repeated_failures[key] = repeated_failures.get(key, 0) + 1
                if repeated_failures[key] > 1:
                    return workspace.observe(CapabilityResult(CapabilityStatus.FAILED, capability,
                        error='专业能力未使用结构化完成合同'))
                messages.extend([self._assistant_wire_message(assistant, ()), {'role': 'user',
                    'content': '请仅调用一个工具；完成时必须调用complete_capability，不要输出正文。'}])
                continue
            if len(calls) != 1:
                calls_seen += len(calls)
                self.budget.tools(len(calls))
                messages.append(self._assistant_wire_message(assistant, calls))
                for call in calls:
                    messages.append({'role': 'tool', 'tool_call_id': call.id, 'name': call.function.name,
                                     'content': json.dumps({'status': 'unavailable',
                                        'reason': '专业能力每个响应只能调用一个工具，所有并行调用均未执行'}, ensure_ascii=False)})
                if calls_seen > 4:
                    return workspace.observe(CapabilityResult(CapabilityStatus.FAILED, capability,
                        error='专业能力重复返回多个工具调用'))
                continue
            call = calls[0]
            self.budget.tools(1)
            messages.append(self._assistant_wire_message(assistant, calls))
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or '{}')
                if not isinstance(args, dict) or name not in allowed:
                    raise ValueError('当前专业能力不允许此工具')
                Draft202012Validator(allowed[name]['function']['parameters']).validate(args)
                if name == 'complete_capability':
                    return self._complete_capability(capability, args, workspace,
                        conversation_task_id=conversation_task_id,
                        question_factory=question_factory,
                        candidate_policy=candidate_policy or {})
                output, direct = self.execute_tool(legacy_role, name, args)
                if direct is not None:
                    # Legacy early termination becomes an observation, never a parent finish.
                    if direct.needs_input:
                        if question_factory is None:
                            raise ValueError('专业能力追问缺少父级任务绑定器')
                        question = question_factory(capability, direct.body)
                        return workspace.observe(CapabilityResult(CapabilityStatus.NEEDS_INPUT,
                            capability, direct.body, input_request=question))
                    artifact_refs = ()
                    if direct.plan_text:
                        dependencies = tuple(item.id for item in workspace.artifacts
                            if item.kind == 'requirements' and workspace.invalidation_reason(item.id) is None)[-1:]
                        artifact = workspace.register_artifact('itinerary', {
                            'text': direct.plan_text, 'evidence': direct.evidence or [],
                            'preserve_plan_basis': direct.preserve_plan_basis,
                        }, source_capability=capability, conversation_task_id=conversation_task_id,
                           depends_on=dependencies,
                           effect=candidate_policy.get('effect', 'independent'),
                           condition=candidate_policy.get('condition', 'satisfied'),
                           evidence_refs=tuple(candidate_policy.get('evidence_refs', ())))
                        artifact_refs = (artifact.id,)
                    return workspace.observe(CapabilityResult(CapabilityStatus.OK, capability,
                                                              direct.body, artifact_refs=artifact_refs))
            except (ValueError, TypeError, KeyError, ValidationError) as exc:
                output = {'status': 'unavailable', 'reason': str(exc)}
                key = (name, json.dumps(args if 'args' in locals() else {}, sort_keys=True, ensure_ascii=False), str(exc))
                repeated_failures[key] = repeated_failures.get(key, 0) + 1
                if repeated_failures[key] > 1:
                    return workspace.observe(CapabilityResult(CapabilityStatus.FAILED, capability,
                        error='专业能力重复了相同的无效调用'))
            messages.append({'role': 'tool', 'tool_call_id': call.id, 'name': name,
                             'content': json.dumps(output, ensure_ascii=False, default=str)})
        return workspace.observe(CapabilityResult(CapabilityStatus.FAILED, capability,
                                                   error='专业能力达到步数上限'))

    @staticmethod
    def _assistant_wire_message(assistant, calls):
        message = {'role': 'assistant', 'content': assistant.content or '', 'tool_calls': [
            {'id': call.id, 'type': 'function', 'function': {
                'name': call.function.name, 'arguments': call.function.arguments or '{}'}} for call in calls]}
        wire = getattr(assistant, '_responses_output', None)
        if wire is not None:
            message['_responses_output'] = wire
        return message

    def _complete_capability(self, capability, args, workspace, *, conversation_task_id=None,
                             question_factory=None, candidate_policy=None):
        from .agent_protocol import CapabilityResult, CapabilityStatus
        status = CapabilityStatus(args['status'])
        summary = args['summary'].strip()
        candidate_policy = candidate_policy or {}
        effect = candidate_policy.get('effect', 'independent')
        condition = candidate_policy.get('condition', 'satisfied')
        condition_evidence = tuple(candidate_policy.get('evidence_refs', ()))
        artifact_refs, evidence_refs = [], []
        if capability == 'prepare_requirements' and self.patch:
            artifact = workspace.register_artifact('requirements', {
                'normalized_changes': deepcopy(self.changes), 'raw_patch': deepcopy(self.patch),
                'idea_cards': deepcopy(self.ideas),
            }, source_capability=capability, conversation_task_id=conversation_task_id,
               effect=effect, condition=condition, evidence_refs=condition_evidence)
            artifact_refs.append(artifact.id)
        elif capability in {'draft_itinerary', 'revise_itinerary'} and args.get('artifact_text'):
            dependencies = [item.id for item in workspace.artifacts
                            if item.kind == 'requirements' and workspace.invalidation_reason(item.id) is None]
            artifact = workspace.register_artifact('itinerary', {
                'text': args['artifact_text'].strip(), 'evidence': deepcopy(self.evidence),
                'preserve_plan_basis': False,
            }, source_capability=capability, conversation_task_id=conversation_task_id,
               depends_on=dependencies[-1:], evidence_refs=condition_evidence,
               effect=effect, condition=condition)
            artifact_refs.append(artifact.id)
        if capability == 'research' and (summary or self.evidence):
            evidence_kind = 'other'
            tools_used = {row.get('tool') for row in self.evidence if isinstance(row, dict)}
            if tools_used == {'query_weather'}:
                evidence_kind = 'weather'
            elif tools_used and tools_used <= {'search_places'}:
                evidence_kind = 'place'
            elif tools_used and tools_used <= {'search_food'}:
                evidence_kind = 'food'
            elif tools_used and tools_used <= {'query_route'}:
                evidence_kind = 'route'
            evidence = workspace.register_evidence({'summary': summary, 'results': deepcopy(self.evidence),
                'evidence_kind': evidence_kind}, source_capability=capability,
                conversation_task_id=conversation_task_id)
            evidence_refs.append(evidence.id)
        question = None
        if status == CapabilityStatus.NEEDS_INPUT:
            if question_factory is None:
                raise ValueError('专业能力追问缺少父级任务绑定器')
            target = args.get('answer_target') or {'namespace': capability, 'field': 'clarification'}
            question = question_factory(capability, args.get('question') or summary, target)
        return workspace.observe(CapabilityResult(status, capability, summary,
            tuple(artifact_refs), tuple(evidence_refs), tuple(args.get('completed_items') or ()),
            tuple(args.get('unresolved_items') or ()), question,
            summary if status == CapabilityStatus.FAILED else None))

    def planning_route_review(self):
        return critical_route_review(self.trip, self.catalog, self.evidence,
                                     self.budget.max_external - self.budget.external_calls)

    def planning_tool_output(self, name, output):
        if self.route_review_enabled and name == 'search_places':
            return dict(output, route_review=self.planning_route_review())
        return output

    def read_context(self, args):
        section = args.get('section')
        basis = {'requirements_revision': self.trip.requirements.revision if self.trip else None, 'plan_id': self.plan.id if self.plan else None}
        if section == 'requirements':
            return {'basis': basis, 'requirements': asdict(self.trip.requirements) if self.trip else {},
                'trip': {k: getattr(self.trip, k) for k in ('destination', 'date_range', 'budget', 'style', 'note', 'mode', 'lodging')} if self.trip else {}}
        if section == 'plan':
            return {'basis': basis, 'plan': asdict(self.plan) if self.plan else None}
        if section == 'evidence':
            self.used_saved_evidence = True
            saved = deepcopy(self.plan.evidence) if self.plan else []
            return {'basis': basis, 'places': deepcopy(self.catalog), 'results': saved + deepcopy(self.evidence),
                'saved_results': saved, 'turn_results': deepcopy(self.evidence),
                'routes': [deepcopy(v) for k, v in self.cache.items() if isinstance(k, tuple) and len(k) == 7]}
        if section not in {'messages', 'ideas'}:
            raise ValueError('未知上下文类型')
        key = {'messages': 'messages', 'ideas': 'idea_cards'}[section]
        entries = [asdict(v) for v in self.snapshot.get(key, []) if section != 'messages' or v.status == 'sent']
        query = args.get('query', '')
        if not isinstance(query, str):
            raise ValueError('检索词必须为文本')
        if query:
            entries = [v for v in entries if query.casefold() in json.dumps(v, ensure_ascii=False).casefold()]
        offset, limit = args.get('offset', 0), args.get('limit', 20)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('分页参数无效')
        end = offset + limit
        return {'basis': basis, 'items': entries[offset:end], 'total': len(entries), 'next_offset': end if end < len(entries) else None, 'query': query}

    def execute_tool(self, role, name, args):
        started, output, error = monotonic(), None, None
        try:
            output, direct = self._execute_tool(role, name, args)
            return output, direct
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            self.event('工具执行', {'role': role, 'name': name, 'arguments': args,
                'seconds': round(monotonic() - started, 3), 'result': output, 'error_type': error})

    def _execute_tool(self, role, name, args):
        self.budget.remaining()
        from . import agent_client as api
        if name == 'read_context':
            return self.read_context(args), None
        if name in {'update_requirements', 'check_requirements'}:
            if role not in {'requirements', 'planning', 'revision'} or self.action in {'explain', 'confirm'}:
                raise ValueError('当前动作不允许更新需求')
            self.check_quote(args, whole=True)
            if name == 'update_requirements':
                if role == 'planning' and self.entry_role == 'planning':
                    raise ValueError('页面生成只读取已保存需求，不接受需求变更')
                if not self.trip:
                    raise ValueError('缺少旅行需求')
                changes = chat_changes({'changes': args['changes'], 'evidence': self.user_body}, self.user_body, self.trip)
                if changes:
                    updated = update_requirements(self.trip, changes)
                    self.changes.update(changes)
                    aggregate = deepcopy(self.changes)
                    for key in LIST_FIELDS & aggregate.keys():
                        original, final = getattr(self.original_trip.requirements, key), getattr(updated.requirements, key)
                        aggregate[key] = {'add': [v for v in final if v not in original], 'remove': [v for v in original if v not in final]}
                    self.patch = {'changes': aggregate, 'evidence': self.user_body}
                    self.trip = updated
                    self._merge_places({})
                self.ideas.extend(api._parse_idea_cards(args.get('idea_cards')))
            self.requirements_checked = True
            brief, question = planning_brief(self.trip, require_all=role == 'planning' and self.action == 'generate')
            if role == 'planning' and self.action == 'generate' and question:
                if name == 'update_requirements' and changes and self.requirement_completion_attempts == 0:
                    self.requirement_completion_attempts += 1
                    return {'status': 'review_needed', 'missing_requirements': required_information(self.trip),
                            'instruction': '再核对整条user_message，把已经明确提供但遗漏的字段补入changes；未提供才向用户提问，不得猜测。'}, None
                return {'status': 'needs_input', 'question': question}, replace(self.text_reply(question), needs_input=True)
            return {'status': 'ready', 'requirements': self.read_context({'section': 'requirements'}), 'travel_window': brief}, None
        if name == 'edit_plan_text':
            if role != 'revision' or self.action != 'modify' or not self.requirements_checked:
                raise ValueError('精确编辑仅用于已审阅需求的行程修改')
            if self.display_target is not None:
                self._validate_scoped_exact_edit(args)
            if not self.plan or not self.plan.text:
                raise ValueError('当前没有可精确编辑的行程原文')
            self.check_quote(args, whole=True)
            spec = next(s for s in self.configs[role].tools if s['function']['name'] == name)
            try:
                Draft202012Validator(spec['function']['parameters']).validate(args)
            except ValidationError as exc:
                raise ValueError('精确编辑参数格式不符：' + exc.message) from None
            replacement, old = args['new_text'], args['old_text']
            if replacement and replacement not in self.user_body:
                replacement = _unique_reference(self.user_body, replacement)
            if args['operation'] == 'append':
                if old not in (None, '') or not replacement:
                    raise ValueError('追加操作不接受替换目标，追加文字不能为空')
                if replacement.splitlines() != [replacement]:
                    raise ValueError('追加一行只接受单行文字；多行修改须指定原文替换范围')
                separator = '' if self.plan.text.endswith('\n') else '\n'
                text = self.plan.text + separator + replacement
            else:
                context = args.get('context')
                if not old:
                    raise ValueError('替换操作须提供原文目标')
                target = _unique_reference(self.plan.text, old if context is None else context)
                original = _unique_reference(target, old)
                text = self.plan.text.replace(target, target.replace(original, replacement, 1), 1)
            self.event('精确文字编辑', {'operation': args['operation'], 'source_hash': text_hash(self.plan.text),
                                     'result_hash': text_hash(text)})
            reply = self.finish_role('revision', text, '', None)
            return {'status': 'edited', 'operation': args['operation']}, replace(reply, preserve_plan_basis=True)
        if role == 'planning':
            if self.action not in {'generate', 'update_and_query'} or not self.requirements_checked:
                raise ValueError('规划前须审阅本轮需求，当前不能执行外部查询')
            if self.action == 'generate':
                _, question = planning_brief(self.trip, require_all=True)
                if question:
                    return {'status': 'needs_input', 'question': question}, replace(self.text_reply(question), needs_input=True)
        if role == 'revision' and (self.action != 'modify' or not self.requirements_checked):
            raise ValueError('当前动作不允许外部查询；修改前须审阅本轮需求')
        if name not in {s['function']['name'] for s in self.configs[role].tools}:
            raise ValueError('当前角色不允许使用此工具')
        if (self.trip and self.trip.mode == 'city' and role in {'planning', 'revision'}
                and self.action in {'generate', 'modify'}):
            city = self.trip.requirements.city.removesuffix('市')
            if name in {'search_places', 'search_food'}:
                requested_city = args.get('city') or self.trip.requirements.city
                if not isinstance(requested_city, str) or requested_city.removesuffix('市') != city:
                    return {'status': 'unavailable', 'reason': '同城路线只在已确认住宿所在城市安排，请使用该城市查询。'}, None
                args = dict(args, city=requested_city)
            if name == 'query_route':
                endpoints = [self.catalog.get(args.get(key)) for key in ('origin_poi_id', 'destination_poi_id')]
                if any(p and p.get('city', '').removesuffix('市') != city for p in endpoints):
                    return {'status': 'unavailable', 'reason': '该路段超出住宿所在城市，请选择同城活动。'}, None
        if role == 'planning' and self.trip and self.trip.mode == 'city' and name == 'query_weather' and not self.trip.requirements.start_date:
            return {'status': 'unavailable', 'reason': '游玩日期未定，不查询或套用今天的天气；请按通用路线安排。'}, None
        if self.route_review_enabled and name == 'search_food':
            pending = [row for row in self.planning_route_review() if row['status'] == 'not_attempted']
            if pending:
                return {'status': 'prerequisite_needed', 'route_review': pending,
                        'instruction': '先查询关键抵达/返程接驳，再查询餐饮候选，避免无关推荐耗尽预算。'}, None
        cache_key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
        if name in {'search_places', 'search_food', 'query_weather'} and cache_key in self.external_results:
            self.event('查询缓存复用', {'tool': name, 'scope': 'current_turn'})
            return self.planning_tool_output(name, deepcopy(self.external_results[cache_key])), None
        if name != 'query_route':
            # Search includes up to five MCP detail calls. Weather does two actual
            # HTTP calls on a cold cache; both are counted inside weather_scope.
            required_calls = 6 if name in {'search_places', 'search_food'} else 2 if name == 'query_weather' else 1
            if self.budget.max_external - self.budget.external_calls < required_calls:
                return {'status': 'budget_exhausted', 'reason': '本轮剩余外部查询预算不足，请使用已有依据并保留未知。'}, None
            if name != 'query_weather':
                self.budget.external()
        started = monotonic()
        from .tools.amap_mcp import budget_scope
        from .tools.weather import budget_scope as weather_scope
        if name == 'query_weather':
            with weather_scope(self.budget):
                output = api.run_tool(name, args, place_catalog=self.catalog, route_cache=self.cache)
        else:
            with budget_scope(self.budget):
                output = api.run_tool(name, args, place_catalog=self.catalog, route_cache=self.cache) if name == 'query_route' else api.run_tool(name, args)
        seconds = round(monotonic() - started, 3)
        self._merge_places(collect_places([{'role': 'tool', 'name': name, 'content': json.dumps(output, ensure_ascii=False)}]))
        if name in {'search_places', 'search_food', 'query_weather'} and output.get('status') == 'available':
            self.external_results[cache_key] = deepcopy(output)
        self.evidence.append({'tool': name, 'arguments': deepcopy(args), 'result': deepcopy(output),
            'queried_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'seconds': seconds})
        return self.planning_tool_output(name, output), None

    def _validate_scoped_exact_edit(self, args):
        """Conservatively confine a page-target exact edit to trusted target text."""
        if args.get('operation') != 'replace':
            raise ValueError('页面站点目标不接受无法定位范围的全文追加，请在聊天中明确原文范围')
        target = self.display_target or {}
        anchors = [value for key, value in target.items()
                   if key in {'title', 'location', 'time_text', 'day_label', 'date'}
                   and isinstance(value, str) and value.strip()]
        old_text = args.get('old_text') or ''
        context = args.get('context') or old_text
        if not any(anchor in context or anchor in old_text for anchor in anchors):
            raise ValueError('精确编辑目标没有包含当前展示站点的可信标题、位置或时间，拒绝跨范围修改')
        if context and self.plan and context not in self.plan.text:
            raise ValueError('页面目标对应原文范围已变化，请刷新后重试')

    def check_quote(self, args, whole=False):
        quote = args.get('user_quote')
        if not isinstance(quote, str) or not quote.strip():
            raise ValueError('user_quote须引用本轮用户原话')
        reference, original = _reference_text(quote), _reference_text(self.user_body)
        if reference not in original or whole and reference != original:
            raise ValueError('user_quote必须是顶层参数，与changes并列，值为最后一条用户消息原文；需求工具须引用完整原话，不能引用上下文JSON')

    def finish_role(self, role, content, scope, target_item_id):
        if not content:
            raise ValueError('Empty model response')
        if content.startswith(('需要补充：', '需要补充:', '需要确认：', '需要确认:')):
            return replace(self.text_reply(content), needs_input=True)
        if role in {'requirements', 'research'} or role == 'planning' and self.action == 'update_and_query' or role == 'revision' and self.action != 'modify':
            return self.text_reply(content)
        if role == 'planning' and self.action == 'generate':
            _, question = planning_brief(self.trip, require_all=True)
            if question:
                return replace(self.text_reply(question), needs_input=True)
        from .models import AgentCard, AgentAction
        saved = self.plan.evidence if self.plan and (role == 'revision' or self.used_saved_evidence) else []
        self.event('文字行程', {'role': role, 'scope': scope, 'text': content})
        return replace(self.text_reply(content), plan_text=content, evidence=deepcopy(saved + self.evidence), next_action='show_plan',
            card=AgentCard('revision' if role == 'revision' else 'itinerary-draft', '行程调整' if role == 'revision' else '行程草案', content[:120], [], [AgentAction('查看行程', 'itinerary')]))

    def convert_display(self, result):
        source_hash = text_hash(result.plan_text)
        try:
            if 'source_dates' in self.snapshot:
                dates = self.snapshot['source_dates']
            else:
                brief, _ = planning_brief(self.trip)
                dates = brief['dates'] if brief else None
            output_format = os.getenv('TRAVEL_DISPLAY_OUTPUT_FORMAT', 'yaml').strip().lower()
            if output_format not in {'yaml', 'json'}:
                raise ValueError('展示输出格式配置须为yaml或json')
            response_format = display_format(dates) if output_format == 'json' else None
            payload = {'source_text': result.plan_text, 'source_dates': dates, 'output_format': output_format}
            if output_format == 'yaml':
                payload['output_schema'] = display_format(dates)['json_schema']['schema']
            raw = self.model_node('display', payload, response_format=response_format)
            try:
                display = validate_display(parse_display_output(raw, output_format), dates)
            except (json.JSONDecodeError, ValueError, ValidationError) as invalid:
                retry_allowed = self.snapshot.get('display_retry_allowed', lambda: True)
                if self.budget.remaining() < 60 or self.budget.model_calls >= self.budget.max_models or not retry_allowed():
                    raise
                self.budget.check_current()
                reason = invalid.message if hasattr(invalid, 'message') else str(invalid)
                self.event('展示纠错', {'error_type': type(invalid).__name__, 'reason': reason[:300]})
                payload.update(previous_output=raw, validation_error=reason[:500],
                    correction_instruction='仅修正结构和日期。逐日对照原文，保留预约、饮食禁忌、预算口径、未知和估算。'
                    '不得为凑天数编造或复制活动；原文缺少某日时items为空并说明原文未提供安排。')
                raw = self.model_node('display', payload, response_format=response_format)
                display = validate_display(parse_display_output(raw, output_format), dates)
            self.event('展示转换', {'status': 'ready', 'source_hash': source_hash})
            return replace(result, display=display, display_source_hash=source_hash, display_status='ready')
        except Exception as exc:
            self.budget.check_current()
            detail = str(exc)
            for key in ('OPENAI_API_KEY', 'AMAP_MAP_KEY'):
                secret = os.getenv(key)
                if secret:
                    detail = detail.replace(secret, '[redacted]')
            self.cache['display_error'] = {'error_type': type(exc).__name__, 'reason': detail[:500]}
            self.event('展示转换', {'status': 'fallback', 'error_type': type(exc).__name__,
                'reason': detail[:500], 'source_hash': source_hash})
            return replace(result, display=None, display_source_hash=source_hash, display_status='fallback')

    @staticmethod
    def text_reply(body):
        from .agent_client import AgentReply
        from .models import AgentCard, AgentAction
        if not isinstance(body, str) or not body.strip():
            raise ValueError('回复缺少正文')
        return AgentReply(body.strip(), AgentCard('requirement-summary', '旅行助手', body.strip()[:120], [], [AgentAction('继续聊天', 'chat')]), [])
