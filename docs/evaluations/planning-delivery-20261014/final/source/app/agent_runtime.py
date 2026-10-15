"""Four intent routes; three business agents share one loop and tool executor."""
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import json
import os
from time import monotonic
from jsonschema import Draft202012Validator

from .agent_loop import AgentLoop, AgentLoopState, LoopAction, LoopObservation, LOOP_COMPLETED, LOOP_WAITING_USER
from .agent_roles import roles, role_reasoning_effort, INTENT_SCHEMA
from .requirements import chat_changes, update_requirements, LIST_FIELDS
from .planning import planning_brief, required_information
from .planning_evidence import critical_route_review
from .planning_review import REVIEW_SCHEMA, review_issues
from .places import collect_places
from jsonschema import ValidationError
from .plan_display import validate_display, display_format, text_hash, parse_display_output


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

    def model(self):
        self.remaining()
        if self.model_calls >= self.max_models:
            raise AgentBudgetError('本次模型调用次数已达上限，原行程已保留。')
        self.model_calls += 1

    def tools(self, count):
        self.remaining()
        if self.tool_calls + count > self.max_tools:
            raise AgentBudgetError('本次工具调用次数已达上限，未执行超额工具。')
        self.tool_calls += count

    def external(self):
        self.remaining()
        if self.external_calls >= self.max_external:
            raise AgentBudgetError('本次外部查询次数已达上限，剩余事实待核实。')
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
        self.configs = roles()
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

    def event(self, name, value):
        if self.trace:
            self.trace.add(name, value)

    def run(self, role='main', task='', *, scope='', target_item_id=None):
        self.entry_role = role
        try:
            if role == 'main':
                route = self.classify()
                self.action, scope = route['action'], route['scope']
                self.event('意图识别结果', route)
                role = {'chat': 'chat', 'query': 'research', 'planning': 'planning', 'revision': 'revision'}[route['intent']]
                if route['question']:
                    result = replace(self.text_reply(route['question']), needs_input=True,
                        workflow_state=deepcopy(self.snapshot.get('workflow_state', getattr(self.trip, 'workflow_state', {}))))
                elif self.action == 'cancel':
                    result = replace(self.text_reply('已暂停规划，已保存的旅行需求和行程继续保留。'), workflow_state={})
                else:
                    result = self.execute_route(role, scope, target_item_id)
            else:
                self.action = 'generate' if role == 'planning' else ('explain' if scope == 'explain' else 'modify') if role == 'revision' else 'reply'
                result = self.execute_route(role, scope, target_item_id)
            self.budget.remaining()
            business_response = self.last_response
            if result.plan_text:
                result = replace(result, display=None, display_source_hash=text_hash(result.plan_text), display_status='pending')
            self.budget.check_current()
            if result.workflow_state is None:
                state = deepcopy(self.snapshot.get('workflow_state', getattr(self.original_trip, 'workflow_state', {})))
                if role == 'planning':
                    state = {'awaiting_generation': True, 'question': result.body} if self.action == 'generate' and result.needs_input else {}
                elif role == 'revision' and self.action != 'explain':
                    state = {}
                result = replace(result, workflow_state=state)
            result = replace(result, requirements_patch=self.patch, verified_places=self.catalog,
                             idea_cards=self.ideas, orchestrated=True)
            self.cache['final_reply'] = result
            self.event('Workflow 返回结果', asdict(result))
            return business_response
        finally:
            self.cache['requirements_patch'] = self.patch
            self.cache['idea_cards'] = self.ideas
            self.event('共享调用预算', {'model_calls': self.budget.model_calls,
                'tool_calls': self.budget.tool_calls, 'external_calls': self.budget.external_calls})

    def classify(self):
        payload = {'user_message': self.user_body, 'conversation': self.snapshot.get('recent_chat', []),
            'current_date': self.snapshot.get('current_date'), 'has_plan': bool(self.plan and self.plan.has_content),
            'workflow_state': self.snapshot.get('workflow_state', getattr(self.trip, 'workflow_state', {})),
            'requirements': self.read_context({'section': 'requirements'}),
            'missing_requirements': required_information(self.trip)}
        data = json.loads(self.model_node('main', payload))
        Draft202012Validator(INTENT_SCHEMA).validate(data)
        allowed = {'chat': {'reply'}, 'query': {'reply'}, 'planning': {'update_requirements', 'update_and_query', 'generate', 'cancel'},
                   'revision': {'modify', 'explain', 'confirm'}}
        if data['action'] not in allowed[data['intent']]:
            raise ValueError('意图与动作不一致')
        self.check_quote(data)
        if data['intent'] == 'revision' and data['action'] == 'modify' and data['scope'] not in {'item', 'all'}:
            raise ValueError('修改范围必须明确')
        return data

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

    def execute_route(self, role, scope, target_item_id):
        if role == 'chat':
            return self.text_reply(self.model_node('chat', {'user_message': self.user_body,
                'conversation': self.snapshot.get('recent_chat', []),
                'current_date': self.snapshot.get('current_date'),
                'requirements': self.read_context({'section': 'requirements'}),
                'missing_requirements': required_information(self.trip)}))
        if role == 'revision' and (not self.plan or not self.plan.has_content):
            return replace(self.text_reply('需要补充：当前没有可修改的行程，请先生成方案。'), needs_input=True)
        if role == 'revision' and self.action == 'confirm':
            return replace(self.text_reply('当前行程已确认，后续修改会更新当前方案。'), confirm_current=True)
        if role == 'planning' and self.entry_role != 'main':
            self.requirements_checked = True
            _, question = planning_brief(self.trip, require_all=True)
            if question:
                return replace(self.text_reply(question), needs_input=True)
        return self.run_role(role, self.user_body, scope=scope, target_item_id=target_item_id)

    def _initial_messages(self, role, task, scope, target_item_id):
        brief, _ = planning_brief(self.trip)
        payload = {'user_message': self.user_body, 'action': self.action,
            'scope': scope, 'target_item_id': target_item_id, 'current_date': self.snapshot.get('current_date'),
            'conversation': self.snapshot.get('recent_chat', []),
            'requirements': self.read_context({'section': 'requirements'}),
            'requirements_checked': self.requirements_checked, 'travel_window': brief,
            'external_query_budget': {'remaining': self.budget.max_external - self.budget.external_calls,
                                      'search_max_calls': 6}}
        if task != self.user_body:
            payload['task'] = task
        return [{'role': 'system', 'content': self.configs[role].prompt},
                {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]

    def run_role(self, role, task, *, scope='', target_item_id=None):
        if role not in {'research', 'planning', 'revision'}:
            raise ValueError('只有查询、规划和修改使用Agent Loop')
        config = self.configs[role]
        specs = list(config.tools)
        if role == 'planning' and self.entry_role == 'planning':
            specs = [s for s in specs if s['function']['name'] != 'update_requirements']
        if role == 'revision' and self.action == 'explain':
            specs = [s for s in specs if s['function']['name'] == 'read_context']
        if role == 'planning' and self.action == 'update_requirements':
            specs = [s for s in specs if s['function']['name'] in {'read_context', 'update_requirements', 'check_requirements'}]
        allowed = {s['function']['name'] for s in specs}
        started = monotonic()
        self.route_review_enabled = role == 'planning' and self.action == 'generate'
        messages = self._initial_messages(role, task, scope, target_item_id)
        state = AgentLoopState(self.trace.request_id if self.trace else 'turn', role, 'working',
            goal=task, max_steps=config.max_steps, max_tool_calls=self.budget.max_tools)
        results, errors = [], []
        review_retries = 0
        route_review_retries = 0
        delivery_review_retries = 0

        def decide(current):
            response = self.request(self.client, self.model, messages, tools=specs, trace=self.trace,
                budget=self.budget, reasoning_effort=role_reasoning_effort(config, scope), agent_role=role)
            self.last_response = response
            assistant = response.choices[0].message
            calls = list(getattr(assistant, 'tool_calls', None) or [])
            return LoopAction('tools', {'message': assistant, 'calls': calls}) if calls else LoopAction('finish', {'content': (assistant.content or '').strip()})

        def execute(action, current):
            nonlocal review_retries, route_review_retries, delivery_review_retries
            self.budget.remaining()
            if action.name == 'tools':
                calls = action.payload['calls']
                self.budget.tools(len(calls))
                assistant = action.payload['message']
                msg = {'role': 'assistant', 'content': assistant.content or '', 'tool_calls': [
                    {'id': c.id, 'type': 'function', 'function': {'name': c.function.name, 'arguments': c.function.arguments or '{}'}} for c in calls]}
                wire = getattr(assistant, '_responses_output', None)
                if wire is not None:
                    msg['_responses_output'] = wire
                messages.append(msg)
                direct = None
                for call in calls:
                    name = call.function.name
                    if direct is not None:
                        output = {'status': 'skipped', 'reason': '本轮已经结束，未执行后续工具'}
                    else:
                        try:
                            args = json.loads(call.function.arguments or '{}')
                            if not isinstance(args, dict) or name not in allowed:
                                raise ValueError('当前角色不允许使用此工具或参数无效')
                            output, direct = self.execute_tool(role, name, args)
                        except (ValueError, TypeError, KeyError) as exc:
                            output = {'status': 'unavailable', 'reason': str(exc)}
                    messages.append({'role': 'tool', 'tool_call_id': call.id, 'name': name, 'content': json.dumps(output, ensure_ascii=False)})
                if direct:
                    results.append(direct)
                return LoopObservation(tool_calls=len(calls), done=direct is not None, wait_for_user=bool(direct and direct.needs_input))
            content = action.payload['content']
            needs_review = role == 'planning' or role == 'revision' and self.action == 'modify'
            asks = content.startswith(('需要补充：', '需要补充:', '需要确认：', '需要确认:'))
            if needs_review and not self.requirements_checked and not asks:
                if review_retries >= 1:
                    raise ValueError('未完成本轮需求审阅，未保存行程')
                review_retries += 1
                # 中途提醒使用 user 角色：部分服务端（如 llama.cpp）要求 system 消息只能在开头
                messages.extend([{'role': 'assistant', 'content': content}, {'role': 'user', 'content': '请先审阅整条user_message，执行update_requirements或check_requirements；完成后再交付结果。'}])
                return LoopObservation(next_phase='requirements')
            if self.route_review_enabled and not asks:
                review = self.planning_route_review()
                pending = [row for row in review if row['status'] == 'not_attempted']
                if pending:
                    self.event('关键接驳复核', {'routes': review, 'attempt': route_review_retries + 1})
                    if route_review_retries >= 1:
                        raise ValueError('已匹配的关键接驳尚未查询，未保存新行程')
                    route_review_retries += 1
                    instruction = {'route_review': review, 'instruction':
                        '先查询not_attempted的关键接驳，再重新交付完整方案；每项的候选起终点由实际主线选择，'
                        '两个方向分别查询。原草稿未保存，不能把没有查询写成查询失败。查询失败或预算不足时'
                        '如实给条件草案，不填写未知通勤或酒店到达时刻。用户明确禁止外查时先说明限制并确认取舍。'}
                    messages.extend([{'role': 'assistant', 'content': content},
                                     {'role': 'user', 'content': json.dumps(instruction, ensure_ascii=False)}])
                    return LoopObservation(next_phase='routes')
            review_delivery = self.route_review_enabled or role == 'revision' and scope == 'all'
            if review_delivery and not asks:
                issues = self.review_draft(content)
                if issues:
                    if delivery_review_retries >= 1:
                        result = replace(self.text_reply('这版行程的安排尚未核对通过，暂未保存。'
                            '已填写的需求和现有行程保留，可以稍后重新生成。'), failed=True)
                        results.append(result)
                        return LoopObservation(done=True)
                    delivery_review_retries += 1
                    messages.extend([{'role': 'assistant', 'content': content}, {'role': 'user', 'content':
                        json.dumps({'delivery_issues': issues, 'instruction':
                            '草稿尚未保存。依据当前需求和查询结果修正这些问题，再交付完整方案；'
                            '只能修改有问题的安排，不能删掉用户约束、餐次或必要条件以规避检查。'
                            '预算使用明确不重叠上限，未知酒店不套用景点路线。概览自然简短，'
                            '不要在正文展示审查过程或内部规则。必要时在现有预算内补查。'}, ensure_ascii=False)}])
                    return LoopObservation(next_phase='delivery_review')
            result = self.finish_role(role, content, scope, target_item_id)
            results.append(result)
            return LoopObservation(done=True, wait_for_user=result.needs_input)

        def safe_decide(current):
            try:
                return decide(current)
            except Exception as exc:
                errors.append(exc)
                raise

        def safe_execute(action, current):
            try:
                return execute(action, current)
            except Exception as exc:
                errors.append(exc)
                raise

        outcome = AgentLoop(state, safe_decide, safe_execute, allowed_actions={'tools', 'finish'}).run()
        self.event('Agent Loop 状态', {'mode': role, 'seconds': round(monotonic() - started, 3), 'status': outcome.status, 'steps': outcome.step_count,
            'tool_calls': outcome.tool_call_count, 'stop_reason': outcome.stop_reason, 'history': outcome.history})
        if errors:
            raise errors[-1]
        if outcome.status not in {LOOP_COMPLETED, LOOP_WAITING_USER} or not results:
            raise AgentBudgetError(outcome.stop_reason or 'Agent 未完成本次任务')
        return results[-1]

    def review_draft(self, content):
        brief, _ = planning_brief(self.trip)
        saved = self.plan.evidence if self.plan and self.used_saved_evidence else []
        payload = {'user_message': self.user_body, 'requirements': self.read_context({'section': 'requirements'}),
                   'travel_window': brief, 'evidence': deepcopy(saved + self.evidence),
                   'output_schema': REVIEW_SCHEMA, 'draft': content}
        business_response = self.last_response
        try:
            review = parse_display_output(self.model_node('review', payload), 'json')
            issues = review_issues(review, self.trip)
            self.event('行程交付复核', {'source_hash': text_hash(content), 'review': review, 'issues': issues})
            return issues
        finally:
            # The request caller must receive the business reply, not the review JSON.
            self.last_response = business_response

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
                'trip': {k: getattr(self.trip, k) for k in ('destination', 'date_range', 'budget', 'style', 'note')} if self.trip else {}}
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
            if role not in {'planning', 'revision'} or self.action in {'explain', 'confirm'}:
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
            return {'status': 'edited', 'operation': args['operation']}, self.finish_role('revision', text, '', None)
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
            # Search includes up to five MCP detail calls. Decline an operation
            # before starting it when its bounded cost cannot fit the turn.
            required_calls = 6 if name in {'search_places', 'search_food'} else 1
            if self.budget.max_external - self.budget.external_calls < required_calls:
                return {'status': 'budget_exhausted', 'reason': '本轮剩余外部查询预算不足，请使用已有依据并保留未知。'}, None
            self.budget.external()
        started = monotonic()
        from .tools.amap_mcp import budget_scope
        with budget_scope(self.budget):
            output = api.run_tool(name, args, place_catalog=self.catalog, route_cache=self.cache) if name in {'query_route', 'query_weather'} else api.run_tool(name, args)
        seconds = round(monotonic() - started, 3)
        self.catalog.update(collect_places([{'role': 'tool', 'name': name, 'content': json.dumps(output, ensure_ascii=False)}]))
        if name in {'search_places', 'search_food', 'query_weather'} and output.get('status') == 'available':
            self.external_results[cache_key] = deepcopy(output)
        self.evidence.append({'tool': name, 'arguments': deepcopy(args), 'result': deepcopy(output),
            'queried_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'seconds': seconds})
        return self.planning_tool_output(name, output), None

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
        if role == 'research' or role == 'planning' and self.action in {'update_requirements', 'update_and_query'} or role == 'revision' and self.action != 'modify':
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
