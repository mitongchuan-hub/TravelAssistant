# 负责创建旅行智能体请求、记录执行轨迹，并统一处理模型调用与兼容性降级。
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic


from app.models import AgentCard
from app.core.config import database_path, llm_enabled, model_name, fallback_model_name, model_timeout, display_timeout
from app.core.resources import borrow_client
from app.agents.model_transport import create_completion, response_storage_enabled, visible_params, wire_api


# 配置由入口显式初始化；模块只创建自己的日志器。
logger = logging.getLogger(__name__)


# 汇总一次智能体回复及其计划、展示和工作流等结构化结果。
@dataclass(frozen=True)
class AgentReply:
    body: str
    card: AgentCard
    idea_cards: list[dict]
    plan: dict | None = None
    next_action: str = "continue_chat"
    requirements_patch: dict = field(default_factory=dict)
    requirements_revision: int | None = None
    failed: bool = False
    expected_plan_id: str | None = None
    verified_places: dict = field(default_factory=dict)
    orchestrated: bool = False
    target_item_id: str | None = None
    confirm_current: bool = False
    expected_plan_snapshot: dict | None = None
    needs_input: bool = False
    plan_text: str = ""
    evidence: list[dict] | None = None
    display: dict | None = None
    display_source_hash: str = ""
    display_status: str = "not_requested"
    workflow_state: dict | None = None
    preserve_plan_basis: bool = False


# 收集单次会话的可展示事件和持久化记录，并负责最终落库。
@dataclass
class ConversationTrace:
    request_id: str
    started_at: str
    trip_id: str
    user_body: str
    events: list[str] = field(default_factory=list)
    records: list[dict] = field(default_factory=list, repr=False)
    written: bool = field(default=False, repr=False)
    on_progress: object = field(default=None, repr=False)

    # 同时追加文本事件和原始记录，并尽力通知实时进度回调。
    def add(self, title: str, value: object) -> None:
        if isinstance(value, str):
            content = value
        else:
            content = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        self.events.append(f"\n{'=' * 20} {title} {'=' * 20}\n{content}\n")
        self.records.append({"title": title, "value": value})
        if self.on_progress:
            try:
                self.on_progress(title, value)
            except Exception as exc:
                logger.warning('Could not update execution progress (%s)', type(exc).__name__)

    # 确保执行轨迹最多写入一次，并将落库失败限制为日志告警。
    def write(self, error: BaseException | None = None) -> None:
        if error is not None:
            self.add("调用异常", f"{type(error).__name__}: {error}")
        if self.written:
            return
        self.written = True
        try:
            record_agent_run(self, error=error)
        except Exception as exc:
            logger.warning("Could not record agent run (%s)", type(exc).__name__)


# 将轨迹整理为数据库行并写入配置的城市助手数据库。
def record_agent_run(trace: ConversationTrace, *, error: BaseException | None = None) -> None:
    from app.storage.persistence import insert_agent_run
    insert_agent_run(str(database_path()), _agent_run_row(trace, error))


# 从轨迹中提取最终输出、实际模型、用量和工具次数，生成持久化字段。
def _agent_run_row(trace: ConversationTrace, error: BaseException | None) -> dict:
    records = trace.records
    # 最后一条原始回复代表本次运行最终可记录的模型输出。
    output = None
    for record in reversed(records):
        if record["title"] == "最终模型原始回复" and isinstance(record["value"], str):
            output = record["value"]
            break
    # 模型字段随请求和备用模型切换记录更新，以反映最终使用者。
    model = None
    for record in records:
        value = record["value"]
        if not isinstance(value, dict):
            continue
        if record["title"] == "模型请求" and value.get("model"):
            model = value["model"]
        elif record["title"] == "切换备用模型" and value.get("to"):
            model = value["to"]
    # 聚合多次模型响应的输入和输出令牌统计。
    tokens_in = 0
    tokens_out = 0
    for record in records:
        if record["title"] != "模型响应":
            continue
        value = record["value"]
        usage = value.get("usage") if isinstance(value, dict) else None
        if not isinstance(usage, dict):
            continue
        if isinstance(usage.get("prompt_tokens"), int):
            tokens_in += usage["prompt_tokens"]
        if isinstance(usage.get("completion_tokens"), int):
            tokens_out += usage["completion_tokens"]
    # 共享预算记录给出整轮工具调用总数。
    tool_calls = 0
    for record in records:
        if record["title"] == "共享调用预算" and isinstance(record["value"], dict):
            tool_calls = int(record["value"].get("tool_calls") or 0)
    # 输入上下文和原始终稿已有独立字段，不在过程列表中重复保存。
    process = [
        {"title": record["title"], "value": record["value"]}
        for record in records
        if record["title"] not in {"输入上下文", "最终模型原始回复"}
    ]
    return {
        "request_id": trace.request_id,
        "trip_id": trace.trip_id or None,
        "user_input": trace.user_body,
        "process": process,
        "output": output,
        "ok": error is None,
        "error": f"{type(error).__name__}: {error}" if error is not None else None,
        "model": model,
        "tokens_in": tokens_in or None,
        "tokens_out": tokens_out or None,
        "tool_calls": tool_calls,
        "duration_ms": int((datetime.now() - datetime.fromisoformat(trace.started_at)).total_seconds() * 1000),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


# 根据显式禁用开关、测试环境和密钥可用性判断是否允许调用模型。
def is_llm_enabled() -> bool:
    return llm_enabled()


# 运行任务执行器，直接返回回复和可选的完整变更包。
def generate_task_result(request_context, execution_context, *, cancelled=None, on_progress=None):
    """Return a direct execution result; Store owns revalidation and persistence.

    Model fallback retries only the failed request, without replaying tools.
    """
    from app.workflow.execution import RequestContext
    from app.agents.budget import TurnBudget
    from app.agents.task_runtime import TaskExecutionContext, TaskExecutionRuntime
    from app.workflow.execution import ExecutionResult

    if not isinstance(request_context, RequestContext):
        raise ValueError('request_context必须由服务器创建')
    if not isinstance(execution_context, TaskExecutionContext):
        raise ValueError('execution_context类型不正确')
    # 每个请求建立独立轨迹，供进度通知和最终审计记录复用。
    trace = ConversationTrace(
        request_id=request_context.request_id,
        started_at=datetime.now().isoformat(timespec='seconds'),
        trip_id=request_context.trip_id,
        user_body=request_context.body,
        on_progress=on_progress,
    )
    trace.add('输入上下文', {
        'strategy': request_context.strategy, 'entry': request_context.entry,
        'request_id': request_context.request_id, 'message_id': request_context.message_id,
        'trip_id': request_context.trip_id,
        'requirements_revision': request_context.requirements_revision,
        'workflow_revision': request_context.workflow_revision,
        'sequence': request_context.sequence, 'epoch': request_context.epoch,
    })
    # 模型不可用时返回失败结果，不执行业务循环。
    if not is_llm_enabled():
        proposal = ExecutionResult.failure('模型服务当前不可用，本次未生成或修改行程。')
        trace.add('Task Execution结果', {'failed': True, 'reason': proposal.reason})
        trace.write()
        return proposal

    runtime = None
    try:
        # 主模型和限流备用模型均由环境配置，备用仅重试当前失败请求。
        primary_model = model_name()
        fallback_model = fallback_model_name()

        # 对当前模型请求包一层限流降级，不重建或重放任务工作区。
        def request_current(client_arg, model, messages, **kwargs):
            try:
                return _create_chat_completion(client_arg, model, messages, **kwargs)
            except Exception as exc:
                if not _is_rate_limit_error(exc) or not fallback_model or fallback_model == model:
                    raise
                logger.warning('Primary model request was rate limited; retrying that request on fallback')
                trace.add('切换备用模型', {'from': model, 'to': fallback_model,
                                            'scope': 'current_model_request', 'reason': str(exc)})
                return _create_chat_completion(client_arg, fallback_model, messages, **kwargs)

        # 借用应用级连接池；预算、轨迹和工作区仍每轮独立。
        with borrow_client() as client:
            # 执行器共享整轮预算，返回回复和可选变更包。
            budget = (TurnBudget(max_models=None, max_tools=None, max_external=None, cancelled=cancelled)
                      if request_context.entry == 'main' else TurnBudget(cancelled=cancelled))
            runtime = TaskExecutionRuntime(
                client, primary_model, request_context, execution_context,
                request=request_current, trace=trace, budget=budget,
            )
            proposal = runtime.run()
            trace.add('Task Execution结果', {
                'failed': proposal.failed,
                'reason': proposal.reason, 'has_changes': proposal.changes is not None,
                'usage': budget.usage(),
            })
            return proposal
    except Exception as exc:
        trace.write(exc)
        return ExecutionResult.failure('本次任务执行未完成，当前需求和行程已保留。')
    finally:
        trace.write()


# 使用独立的无工具预算将已保存行程文本转换为 HTML 攻略展示，失败时保留文字回退信息。
def generate_plan_display(source_text: str, source_dates, trip_id: str, *, style_instruction='', cancelled=None, retry_allowed=None) -> dict:
    """Convert only saved prose with an independent budget; never run a business loop."""
    from app.agents.budget import TurnBudget
    from app.agents.display_runtime import DisplayRuntime, redact_error
    from app.services.itinerary.plan_display import text_hash
    trace = ConversationTrace(uuid.uuid4().hex[:12], datetime.now().isoformat(timespec='seconds'), trip_id, source_text)
    source_hash = text_hash(source_text)
    try:
        if not is_llm_enabled():
            raise RuntimeError('模型服务当前不可用，完整文字已保留')
        with borrow_client() as client:
            # 展示转换最多调用两次模型，明确禁止工具和外部访问。
            budget = TurnBudget(max_models=2, max_tools=0, max_external=0,
                seconds=display_timeout(), cancelled=cancelled)
            runtime = DisplayRuntime(client, model_name(),
                source_dates, style_instruction=style_instruction, retry_allowed=retry_allowed,
                trace=trace, budget=budget, request=_create_chat_completion)
            reply = runtime.convert_display(AgentReply(source_text, None, [], plan_text=source_text))
            return {'display': reply.display, 'display_source_hash': source_hash,
                    'display_status': reply.display_status, **runtime.cache.get('display_error', {})}
    except Exception as exc:
        # 写入回退原因前遮蔽环境中的密钥、令牌和密码值。
        detail = redact_error(exc)
        trace.add('展示转换', {'status': 'fallback', 'error_type': type(exc).__name__, 'reason': detail[:300],
                                'source_hash': source_hash})
        return {'display': None, 'display_source_hash': source_hash, 'display_status': 'fallback',
                'error_type': type(exc).__name__, 'reason': detail[:300]}
    finally:
        trace.write()


# 兼容异常类型名和 HTTP 状态码两种限流信号。
def _is_rate_limit_error(exc: Exception) -> bool:
    return type(exc).__name__ == "RateLimitError" or getattr(exc, "status_code", None) == 429


# 从服务错误文本中识别工具或函数调用能力不受支持的情况。
def _looks_like_unsupported_tools(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(word in text for word in ("tool", "function", "unsupported", "not support"))


# 组装统一模型参数，执行带预算和轨迹的请求，并处理明确可降级的参数拒绝。
def _create_chat_completion(
    client,
    model: str,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    response_format: dict | None = None,
    trace: ConversationTrace | None = None,
    budget=None,
    reasoning_effort: str | None = None,
    agent_role: str | None = None,
) -> object:
    from openai import BadRequestError

    # 基础参数在 Chat Completions 与 Responses 两种线协议间共用。
    api = wire_api()
    params: dict[str, object] = {"model": model, "messages": messages}
    # 按端点、模型和档位缓存已被拒绝的思考参数，避免重复无效尝试。
    rejection_key = (str(getattr(client, 'base_url', '')) + '#' + api, model, reasoning_effort or '')
    rejections = budget.reasoning_rejections if budget is not None else set()
    reasoning_status = 'provider_default'
    if reasoning_effort is not None:
        if rejection_key in rejections:
            reasoning_status = 'provider_default_after_rejection'
        else:
            params['reasoning_effort'] = reasoning_effort
            reasoning_status = 'requested'
    # 规划默认保留服务端思考行为；只有显式配置 true/false 才覆盖聊天模板开关。
    # 该端点仅识别 chat_template_kwargs，单独传入 enable_thinking 无效。
    thinking = os.getenv('OPENAI_MAIN_ENABLE_THINKING', '').strip().lower()
    if agent_role == 'travel_agent' and thinking in {'true', 'false'}:
        params['extra_body'] = {'chat_template_kwargs': {'enable_thinking': thinking == 'true'}}
    if tools:
        params["tools"] = tools
    if response_format:
        params["response_format"] = response_format
    # 每次真实请求都记录独立生命周期，避免从任务总时长反推模型超时位置。
    attempt = 0
    while True:
        attempt += 1
        request_index = None
        request_id = f'{trace.request_id}:{attempt}' if trace else None
        request_started = None
        effective_timeout = None
        remaining_before = None
        try:
            if budget is not None:
                try:
                    budget.model()
                    configured_timeout = model_timeout()
                    remaining_before = budget.remaining()
                    effective_timeout = min(configured_timeout, remaining_before,
                                            240 if budget.max_models is None else configured_timeout)
                    params['timeout'] = effective_timeout
                    request_index = budget.model_calls
                except Exception as exc:
                    if trace:
                        trace.add('模型请求未开始', {
                            'request_id': request_id, 'attempt': attempt, 'model': model,
                            'agent_role': agent_role, 'exception_type': type(exc).__name__,
                            'reason': '模型预算检查未通过',
                        })
                    raise
            else:
                request_index = attempt
            request_meta = {
                'request_id': request_id, 'request_index': request_index, 'attempt': attempt,
                'model': model, 'agent_role': agent_role, 'wire_api': api,
                'configured_timeout': configured_timeout if budget is not None else None,
                'effective_timeout': effective_timeout,
                'remaining_budget_before': remaining_before,
            }
            if trace:
                trace.add('模型请求', {**request_meta, 'params': visible_params(params),
                                         'store': response_storage_enabled(),
                                         'reasoning': {'requested': reasoning_effort, 'sent': params.get('reasoning_effort'),
                                                       'status': reasoning_status}})
                trace.add('模型请求开始', request_meta)
            request_started = monotonic()
            try:
                response = create_completion(client, params, api)
            except Exception as exc:
                elapsed = monotonic() - request_started
                if trace:
                    trace.add('模型请求结束', {**request_meta, 'status': 'exception',
                                               'elapsed_ms': round(elapsed * 1000, 1),
                                               'exception_type': type(exc).__name__})
                raise
            elapsed = monotonic() - request_started
            if trace:
                trace.add('模型请求结束', {**request_meta, 'status': 'success',
                                           'elapsed_ms': round(elapsed * 1000, 1)})
            usage = getattr(response, 'usage', None)
            if budget is not None:
                budget.record_usage(getattr(usage, 'prompt_tokens', None),
                                    getattr(usage, 'completion_tokens', None))
            if trace:
                message = response.choices[0].message
                details = getattr(usage, 'completion_tokens_details', None)
                trace.add("模型响应", {
                    'request_id': request_id, 'request_index': request_index, 'attempt': attempt,
                    'agent_role': agent_role,
                    'seconds': round(elapsed, 3),
                    'reasoning_effort_sent': params.get('reasoning_effort'),
                    'reasoning_status': 'parameter_accepted' if 'reasoning_effort' in params else reasoning_status,
                    'usage': {
                        'prompt_tokens': getattr(usage, 'prompt_tokens', None),
                        'completion_tokens': getattr(usage, 'completion_tokens', None),
                        'reasoning_tokens': getattr(details, 'reasoning_tokens', None),
                    },
                    "content": getattr(message, "content", None),
                    "tool_calls": [
                        {
                            "id": getattr(call, "id", ""),
                            "name": getattr(getattr(call, "function", None), "name", ""),
                            "arguments": getattr(getattr(call, "function", None), "arguments", ""),
                        }
                        for call in (getattr(message, "tool_calls", None) or [])
                    ],
                })
            return response
        except BadRequestError as exc:
            # 仅对服务明确拒绝的思考或响应格式参数做单项降级重试。
            text = str(exc).lower()
            if 'reasoning_effort' in params and _rejects_reasoning_effort(exc):
                rejections.add(rejection_key)
                params.pop('reasoning_effort')
                reasoning_status = 'provider_default_after_rejection'
                logger.info('Model rejected reasoning_effort; retrying with provider default')
                if trace:
                    trace.add('思考参数降级', {'model': model, 'agent_role': agent_role,
                                           'requested': reasoning_effort, 'sent': None,
                                           'status': reasoning_status, 'reason': '服务明确拒绝该思考参数或档位；移除参数后重试一次。'})
                continue
            if "response_format" in params and _looks_like_unsupported_response_format(text):
                if isinstance(params["response_format"], dict) and params["response_format"].get("type") == "json_schema":
                    if trace:
                        trace.add("模型请求重试", {"reason": str(exc), "params": params})
                    params["response_format"] = {"type": "json_object"}
                else:
                    if trace:
                        trace.add("模型请求重试", {"reason": str(exc), "params": params})
                    params.pop("response_format", None)
                continue
            if "tools" in params and _looks_like_unsupported_tools(exc):
                raise
            raise


# 严格判断错误是否明确拒绝思考档位，避免被其他参数错误误导。
def _rejects_reasoning_effort(exc: Exception) -> bool:
    """Only drop effort when the provider explicitly rejects that parameter/value."""
    body = getattr(exc, 'body', None)
    error = body.get('error', body) if isinstance(body, dict) else {}
    error = error if isinstance(error, dict) else {}
    text = str(error.get('message') or exc).lower()
    parameter = str(error.get('param') or '').lower()
    effort_parameters = {'reasoning_effort', 'reasoning.effort'}
    mentions_effort = any(name in text for name in effort_parameters) or parameter in effort_parameters
    rejects = any(term in text for term in (
        'not supported', 'unsupported', 'not support', 'does not support', 'not allowed',
        'unknown parameter', 'unrecognized', 'unrecognised', 'unexpected keyword',
        'invalid', 'must be one of', 'only supports', 'not available', '不支持', '无效',
    )) or str(error.get('code') or '') in {'unsupported_parameter', 'unsupported_value', 'unknown_parameter', 'invalid_parameter_error'}
    # 仅在报错参数确属思考档位时降级；泛化的工具或结构错误不足以触发。
    conflicting_parameter = parameter and parameter not in effort_parameters
    return bool(mentions_effort and rejects and not conflicting_parameter)


# 识别端点整体不支持结构化响应格式的错误，但保留具体模式校验错误。
def _looks_like_unsupported_response_format(text: str) -> bool:
    if 'invalid_json_schema' in text:
        return False
    mentions = any(term in text for term in ('response_format', 'text.format', 'json_schema', 'structured output'))
    rejects = any(term in text for term in ('not supported', 'unsupported', 'not support', 'unknown parameter',
        'unrecognized', 'unrecognised', 'invalid parameter', 'supported values', 'not available'))
    return mentions and rejects
