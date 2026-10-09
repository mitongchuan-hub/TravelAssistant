"""One role runner: model -> tools (including delegation) -> model -> result.

This module works on a private trip snapshot. Only Store may apply its results.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
import json
from time import monotonic
from datetime import datetime, timezone

from .agent_loop import AgentLoop, AgentLoopState, LoopAction, LoopObservation, LOOP_COMPLETED, LOOP_WAITING_USER
from .agent_roles import DELEGATIONS, roles, role_reasoning_effort
from .requirements import chat_changes, update_requirements, LIST_FIELDS
from .places import collect_places
from .planning import planning_brief


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
    reasoning_rejections: set[tuple[str, str, str]] = field(default_factory=set)

    def __post_init__(self):
        self.deadline = monotonic() + self.seconds

    def remaining(self):
        if self.cancelled and self.cancelled():
            raise AgentBudgetError('本次任务已由新消息替代。')
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
        self.original_trip = deepcopy(self.snapshot.get('trip'))
        self.trip = self.snapshot.get('trip')
        self.plan = self.snapshot.get('plan')
        self.user_body = self.snapshot.get('user_body', '')
        self.catalog = catalog if catalog is not None else {}
        self.cache = cache if cache is not None else {}
        self.budget = self.cache.setdefault('_budget', TurnBudget(cancelled=self.snapshot.get('cancelled')))
        self.request = request
        self.configs = roles()
        self.patch = {}
        self.changes = {}
        self.ideas = []
        self.candidate = None
        self.version_action = {}
        self.versions_read = False
        self.last_response = None
        self.failure = None
        self.delegations = 0
        self.evidence = []
        self.requirements_checked = False
        self.entry_role = None
        self.used_saved_evidence = False

    def event(self, name, value):
        if self.trace:
            self.trace.add(name, value)

    def run(self, role='main', task='', *, scope='', target_item_id=None):
        try:
            self.entry_role = role
            result = self.run_role(role, task or self.user_body, scope=scope, target_item_id=target_item_id)
            self.budget.remaining()
            if self.failure and not self.candidate:
                result = self.failure
            if self.candidate:
                # The narrator cannot overwrite the validated child artifact or its explanation.
                result = self.candidate
            result = replace(result, requirements_patch=self.patch, verified_places=self.catalog,
                             idea_cards=self.ideas, orchestrated=True, version_action=self.version_action)
            self.cache['requirements_patch'] = self.patch
            self.event('Agent 返回结果', asdict(result))
            self.cache['final_reply'] = result
            return self.last_response
        finally:
            self.cache['requirements_patch'] = self.patch
            self.cache['idea_cards'] = self.ideas
            self.event('共享调用预算', {
                'model_calls': self.budget.model_calls, 'tool_calls': self.budget.tool_calls,
                'external_calls': self.budget.external_calls,
            })

    def _initial_messages(self, role, task, scope, target_item_id):
        payload = {'task': task, 'user_message': self.user_body, 'scope': scope,
                   'target_item_id': target_item_id,
                   'current_date': self.snapshot.get('current_date'),
                   'conversation': self.snapshot.get('recent_chat', [])}
        if role != 'main':
            # Hard constraints are always complete; optional history and plans are read on demand.
            payload['requirements'] = self.read_context({'section': 'requirements'})
        if role in {'planning', 'revision'}:
            payload['travel_window'], _ = planning_brief(self.trip)
        prompt = self.configs[role].prompt
        if role == 'revision' and scope == 'explain':
            prompt += '\n本次是只读解释模式，直接输出解释，不交付新行程、不新增版本。'
        return [{'role': 'system', 'content': prompt},
                {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]

    def prepare_write(self, role):
        # Every conversational plan/revision write must synchronize requirements once.
        # A page plan request already operates on saved requirements; page revision
        # feedback may contain new constraints and goes through the same prerequisite.
        if role == 'revision' and (not self.plan or not self.plan.has_content):
            return replace(self.text_reply('需要补充：当前还没有可修改的行程，请先生成方案。'), needs_input=True)
        needs_sync = self.entry_role == 'main' or role == 'revision' and self.entry_role == 'revision'
        if needs_sync and not self.requirements_checked:
            started = monotonic()
            reply = self.run_role('requirements', self.user_body)
            self.event('行程写入前需求同步', {'role': role, 'seconds': round(monotonic() - started, 3),
                                       'needs_input': reply.needs_input, 'failed': reply.failed,
                                       'requirements_staged': bool(self.patch)})
            if reply.needs_input or reply.failed:
                return reply
        if role != 'planning':
            return None  # Legacy/current plan edits can preserve unspecified dates.
        brief, question = planning_brief(self.trip)
        self.event('规划入口检查', {'travel_window': brief, 'question': question})
        if question:
            return replace(self.text_reply(question), needs_input=True)
        return None

    def run_role(self, role, task, *, scope='', target_item_id=None):
        from . import agent_client as api
        if role == 'planning' or role == 'revision' and scope != 'explain':
            stopped = self.prepare_write(role)
            if stopped:
                return stopped
        config = self.configs[role]
        effort = role_reasoning_effort(config, scope)
        specs = list(config.tools)
        if role == 'revision' and scope == 'explain':
            specs = [s for s in specs if s['function']['name'] == 'read_context']
        allowed = {s['function']['name'] for s in specs}
        messages = self._initial_messages(role, task, scope, target_item_id)
        state = AgentLoopState(self.trace.request_id if self.trace else 'turn', role, 'working',
                               goal=task, max_steps=config.max_steps, max_tool_calls=self.budget.max_tools)
        result, errors = [], []
        repairs = 0
        tools_supported = True

        def decide(current):
            nonlocal tools_supported
            # Only requirements have an internal data contract. Travel plans
            # remain prose throughout generation, editing, display and storage.
            response_format = config.response_format if repairs else None
            kwargs = {'tools': specs if specs and tools_supported else None, 'trace': self.trace, 'budget': self.budget,
                      'reasoning_effort': effort, 'agent_role': role}
            if response_format:
                kwargs['response_format'] = response_format
            try:
                response = self.request(self.client, self.model, messages, **kwargs)
            except Exception as exc:
                if type(exc).__name__ == 'BadRequestError' and api._looks_like_unsupported_tools(exc) and tools_supported:
                    tools_supported = False
                    # A provider without tools can converse, but cannot fabricate a delegated write.
                    messages.append({'role': 'system', 'content': '当前服务不支持工具。只能回复已有信息或说明无法执行；不得声称已更新需求、查询事实或保存方案。'})
                    if role != 'main':
                        messages.append({'role': 'user', 'content': json.dumps(self.read_context({'section': 'plan'}), ensure_ascii=False)})
                    kwargs['tools'] = None
                    response = self.request(self.client, self.model, messages, **kwargs)
                else:
                    raise
            self.last_response = response
            assistant = response.choices[0].message
            calls = list(getattr(assistant, 'tool_calls', None) or [])
            if calls:
                return LoopAction('tools', {'message': assistant, 'calls': calls})
            return LoopAction('finish', {'content': (assistant.content or '').strip()})

        def execute(action, current):
            nonlocal repairs
            try:
                self.budget.remaining()  # Reject late model responses before staging any mutation.
                if action.name == 'tools':
                    calls = action.payload['calls']
                    self.budget.tools(len(calls))  # Reserve the entire batch before any tool executes.
                    assistant_message = {'role': 'assistant', 'content': getattr(action.payload['message'], 'content', None) or '',
                                         'tool_calls': [{'id': call.id, 'type': 'function', 'function': {
                                             'name': call.function.name, 'arguments': call.function.arguments or '{}'}} for call in calls]}
                    wire_output = getattr(action.payload['message'], '_responses_output', None)
                    if wire_output is not None:
                        assistant_message['_responses_output'] = wire_output
                    messages.append(assistant_message)
                    direct = None
                    for call in calls:
                        name = call.function.name
                        started = monotonic()
                        try:
                            args = json.loads(call.function.arguments or '{}')
                            if not isinstance(args, dict):
                                raise ValueError('工具参数必须为对象')
                            if name not in allowed:
                                raise ValueError('当前角色不允许使用此工具')
                            if role == 'main' and len(calls) > 1:
                                raise ValueError('主 Agent 每次只委派或执行一个动作；有依赖的任务必须串行')
                            output, direct = self.execute_tool(role, name, args)
                        except (ValueError, TypeError, KeyError) as exc:
                            output = {'status': 'unavailable', 'reason': str(exc)}
                        tool_message = {'role': 'tool', 'tool_call_id': call.id, 'name': name,
                                        'content': json.dumps(output, ensure_ascii=False)}
                        messages.append(tool_message)
                        self.event('角色工具执行', {'role': role, 'name': name, 'arguments': call.function.arguments,
                                                  'seconds': round(monotonic() - started, 3), 'result': output})
                    if direct is not None:
                        result.append(direct)
                    return LoopObservation(tool_calls=len(calls), done=direct is not None,
                                           wait_for_user=bool(direct and direct.needs_input))
                content = action.payload['content']
                try:
                    reply = self.finish_role(role, content, scope, target_item_id)
                except (ValueError, TypeError) as exc:
                    if role != 'requirements' or repairs >= 1:
                        raise
                    repairs += 1
                    messages.extend([
                        {'role': 'assistant', 'content': content},
                        {'role': 'user', 'content': '需求更新未通过检查：' + str(exc) + '。仅修正本轮用户明确表达的需求变更；信息不明确时返回 needs_input。'},
                    ])
                    self.event('需求更新校验', {'attempt': repairs, 'error': str(exc)})
                    return LoopObservation(next_phase='repairing', data={'issue': str(exc)})
                result.append(reply)
                return LoopObservation(done=True, wait_for_user=reply.needs_input)
            except Exception as exc:
                errors.append(exc)
                raise

        def safe_decide(current):
            try:
                return decide(current)
            except Exception as exc:
                errors.append(exc)
                raise

        outcome = AgentLoop(state, safe_decide, execute, allowed_actions={'tools', 'finish'}).run()
        self.event('Agent Loop 状态', {'mode': role, 'phase': outcome.phase, 'status': outcome.status,
                                    'steps': outcome.step_count, 'tool_calls': outcome.tool_call_count,
                                    'stop_reason': outcome.stop_reason, 'history': outcome.history})
        if errors:
            raise errors[-1]
        if outcome.status not in {LOOP_COMPLETED, LOOP_WAITING_USER} or not result:
            raise AgentBudgetError(outcome.stop_reason or 'Agent 未完成本次任务。')
        return result[-1]

    def read_context(self, args):
        section = args.get('section')
        basis = {'requirements_revision': self.trip.requirements.revision if self.trip else None,
                 'plan_id': self.plan.id if self.plan else None}
        if section == 'requirements':
            return {'basis': basis, 'requirements': asdict(self.trip.requirements) if self.trip else {},
                    'trip': {key: getattr(self.trip, key) for key in ('destination', 'date_range', 'budget', 'style', 'note')} if self.trip else {}}
        if section == 'plan':
            return {'basis': basis, 'plan': asdict(self.plan) if self.plan else None}
        if section == 'evidence':
            self.used_saved_evidence = True
            saved = deepcopy(self.plan.evidence) if self.plan else []
            return {'basis': basis, 'places': deepcopy(self.catalog), 'results': saved + deepcopy(self.evidence),
                    'saved_results': saved, 'turn_results': deepcopy(self.evidence),
                    'note': '记录查询的来源、时间和适用范围；有结果不代表开放、预约、价格或过敏原均已核实。',
                    'routes': [deepcopy(v) for k, v in self.cache.items() if isinstance(k, tuple) and len(k) == 7]}
        if section not in {'messages', 'ideas', 'versions'}:
            raise ValueError('未知上下文类型')
        key = {'messages': 'messages', 'ideas': 'idea_cards', 'versions': 'plan_versions'}[section]
        entries = [asdict(item) for item in self.snapshot.get(key, []) if section != 'messages' or item.status == 'sent']
        if section == 'versions' and args.get('version_id'):
            entries = [entry for entry in entries if entry['id'] == args['version_id']]
        elif section == 'versions':
            entries = [{k: item[k] for k in ('id', 'label', 'title', 'status', 'change_summary')} for item in entries]
        query = args.get('query', '')
        if not isinstance(query, str):
            raise ValueError('检索词必须为文本')
        if query:
            entries = [item for item in entries if query.casefold() in json.dumps(item, ensure_ascii=False).casefold()]
        offset, limit = args.get('offset', 0), args.get('limit', 20)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('分页参数无效')
        end = offset + limit
        return {'basis': basis, 'items': entries[offset:end], 'total': len(entries),
                'next_offset': end if end < len(entries) else None, 'query': query,
                'note': '本次检索结果；无匹配不代表历史从未记录。' if query else ''}

    def execute_tool(self, role, name, args):
        started = monotonic()
        output = None
        error = None
        try:
            output, direct = self._execute_tool(role, name, args)
            return output, direct
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            self.event('工具执行', {'role': role, 'name': name, 'arguments': args,
                                    'seconds': round(monotonic() - started, 3),
                                    'result': output, 'error_type': error})

    def _execute_tool(self, role, name, args):
        self.budget.remaining()
        from . import agent_client as api
        if name == 'read_context':
            return self.read_context(args), None
        if name == 'read_versions':
            self.versions_read = True
            return {'current_plan': {'id': self.plan.id, 'title': self.plan.title, 'status': self.plan.status} if self.plan else None,
                    **self.read_context({'section': 'versions', 'limit': 100})}, None
        if name == 'change_version':
            self.check_quote(args)
            if not self.versions_read or self.candidate or self.patch or self.version_action:
                raise ValueError('版本操作须先读取目标，且不能与本轮其他写入混合')
            op, target = args.get('operation'), args.get('version_id')
            if op == 'confirm' and self.plan and self.plan.has_content and target == self.plan.id:
                pass
            elif op == 'restore' and any(v.id == target for v in self.snapshot.get('plan_versions', [])):
                pass
            else:
                raise ValueError('目标版本不存在或不明确')
            self.version_action = {'operation': op, 'version_id': target}
            reply = self.text_reply('当前行程已确认。' if op == 'confirm' else '指定历史版本已恢复为新草案，原有待核实事项继续保留。')
            return {'status': 'ready', 'operation': op, 'target': target}, reply
        if name in DELEGATIONS:
            if self.version_action or self.candidate:
                raise ValueError('本轮已有待保存结果，请先结束当前轮次')
            self.check_quote(args)
            if not isinstance(args.get('task'), str) or not args['task'].strip():
                raise ValueError('请提供明确委派任务')
            if type(args.get('finish_turn')) is not bool:
                raise ValueError('finish_turn 必须为布尔值')
            child = DELEGATIONS[name]
            scope = args.get('scope', '')
            if child == 'revision' and scope not in {'explain', 'item', 'all'}:
                raise ValueError('修改范围必须为 explain/item/all')
            if self.delegations >= 4:
                raise AgentBudgetError('本轮委派次数已达上限。')
            self.delegations += 1
            self.event('委派子 Agent', {'agent': child, 'task': args['task'], 'scope': scope, 'user_quote': args['user_quote']})
            try:
                reply = self.run_role(child, args['task'], scope=scope)
            except (AgentBudgetError, ValueError) as exc:
                reply = replace(self.text_reply(f'本次任务未完成：{exc}。原行程已保留。'), failed=True)
                self.failure = reply
            if reply.plan_text:
                self.candidate = reply
                self.failure = None
            output = {'status': 'failed' if reply.failed else ('needs_input' if reply.needs_input else ('ready' if reply.plan_text or child == 'requirements' and self.patch else 'answered')),
                      'body': reply.body, 'has_plan': bool(reply.plan_text), 'requirements_staged': bool(self.patch),
                      'note': '候选写入在本轮结束时由系统检查版本并保存。'}
            return output, reply if args['finish_turn'] or reply.needs_input or reply.failed else None
        # External tools are only reachable from the role's advertised whitelist.
        if name != 'query_route':
            self.budget.external()
        started = monotonic()
        output = (api.run_tool(name, args, place_catalog=self.catalog, route_cache=self.cache)
                  if name in {'query_route', 'query_weather'} else api.run_tool(name, args))
        seconds = round(monotonic() - started, 3)
        message = {'role': 'tool', 'name': name, 'content': json.dumps(output, ensure_ascii=False)}
        self.catalog.update(collect_places([message]))
        self.evidence.append({'tool': name, 'arguments': deepcopy(args), 'result': deepcopy(output),
                              'queried_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'seconds': seconds})
        return output, None

    def check_quote(self, args):
        quote = args.get('user_quote')
        if not isinstance(quote, str) or not quote.strip() or quote not in self.user_body:
            raise ValueError('动作必须引用最新用户消息的连续原文')

    def finish_role(self, role, content, scope, target_item_id):
        from . import agent_client as api
        if not content:
            raise ValueError('Empty model response')
        if role in {'main', 'research'} or role == 'revision' and scope == 'explain':
            return self.text_reply(content)
        if role == 'requirements':
            data = api._load_json(content)
            reply = self.text_reply(data.get('body', ''))
            if data.get('status') == 'needs_input':
                return replace(reply, needs_input=True)
            patch = data.get('requirements_patch', {})
            if patch:
                if not self.trip:
                    raise ValueError('缺少旅行需求上下文')
                changes = chat_changes(patch, self.user_body, self.trip)
                if changes:
                    updated = update_requirements(self.trip, changes)
                    self.changes.update(changes)
                    aggregate = deepcopy(self.changes)
                    for key in LIST_FIELDS & aggregate.keys():
                        original = getattr(self.original_trip.requirements, key)
                        final = getattr(updated.requirements, key)
                        aggregate[key] = {'add': [v for v in final if v not in original], 'remove': [v for v in original if v not in final]}
                    self.patch = {'changes': aggregate, 'evidence': self.user_body}
                    self.trip = updated
            self.ideas.extend(api._parse_idea_cards(data.get('idea_cards')))
            self.requirements_checked = True
            return reply
        if content.startswith(('需要补充：', '需要补充:', '需要确认：', '需要确认:')):
            return replace(self.text_reply(content), needs_input=True)
        from .models import AgentCard, AgentAction
        self.event('文字行程', {'role': role, 'scope': scope, 'text': content})
        saved_evidence = self.plan.evidence if self.plan and (role == 'revision' or self.used_saved_evidence) else []
        reply = replace(self.text_reply(content), plan_text=content,
                        evidence=deepcopy(saved_evidence + self.evidence), next_action='show_plan',
                        card=AgentCard('revision' if role == 'revision' else 'itinerary-draft',
                                       '行程调整' if role == 'revision' else '行程草案',
                                       content[:120], [], [AgentAction('查看行程', 'itinerary')]))
        self.candidate = reply
        return reply

    @staticmethod
    def text_reply(body):
        from .agent_client import AgentReply
        from .models import AgentCard, AgentAction
        if not isinstance(body, str) or not body.strip():
            raise ValueError('回复缺少正文')
        return AgentReply(body.strip(), AgentCard('requirement-summary', '旅行助手', body.strip()[:120], [],
                          [AgentAction('继续聊天', 'chat')]), [])
