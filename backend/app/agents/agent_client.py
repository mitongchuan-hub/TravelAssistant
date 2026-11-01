# 负责创建旅行智能体请求、记录执行轨迹，并统一处理模型调用与兼容性降级。
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic

from app.core import config
from app.core.config import database_path, model_name, fallback_model_name, model_timeout, display_timeout
from app.core.resources import borrow_client
from app.agents.model_transport import create_completion, response_storage_enabled, visible_params, wire_api


# 配置由入口显式初始化；模块只创建自己的日志器。
logger = logging.getLogger(__name__)


# 收集单次会话的可展示事件和持久化记录，并负责最终落库。
@dataclass
class ConversationTrace:
    # 当前任务的稳定请求标识，用于把进度、模型调用和运行记录关联起来。
    request_id: str
    # 本轮执行的开始时间，用于计算整轮耗时并写入运行记录。
    started_at: str
    # 当前任务所属旅行的标识，用于保存运行记录和校验数据归属。
    trip_id: str
    # 用户本轮发送的原始消息，用于审计上下文和故障排查。
    user_body: str
    # 按发生顺序保存模型、工具和任务生命周期事件；不直接暴露给前端。
    records: list[dict] = field(default_factory=list, repr=False)
    # 标记运行记录是否已经落库，避免正常路径和异常路径重复写入。
    written: bool = field(default=False, repr=False)
    # 可选的实时进度回调；有值时把新增事件转发给任务进度持久化层。
    on_progress: object = field(default=None, repr=False)

    # 保存结构化事件，并尽力通知实时进度回调。
    def add(self, title: str, value: object) -> None:
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
    # 当前传输层使用“模型响应”记录正文和工具调用。
    output = None
    for record in reversed(records):
        if record['title'] != '模型响应' or not isinstance(record['value'], dict):
            continue
        response = record['value']
        if isinstance(response.get('content'), str) and response['content'].strip():
            output = response['content']
        elif response.get('tool_calls'):
            output = json.dumps(response['tool_calls'], ensure_ascii=False)
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
    # 执行器在结束时记录整轮预算，不再依赖旧工作流事件名称。
    tool_calls = 0
    for record in reversed(records):
        if record['title'] == 'Task Runtime预算' and isinstance(record['value'], dict):
            tool_calls = int(record['value'].get('tool_calls') or 0)
            break
    process = [
        {"title": record["title"], "value": record["value"]}
        for record in records
        if record["title"] != "输入上下文"
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
    if not config.llm_enabled():
        result = ExecutionResult.failure('模型服务当前不可用，本次未生成或修改行程。')
        trace.add('Task Execution结果', {'failed': True, 'reason': result.reason})
        trace.write()
        return result

    try:
        # 主模型和限流备用模型均由环境配置，备用仅重试当前失败请求。
        primary_model = model_name()
        fallback_model = fallback_model_name()

        # 对当前模型请求包一层限流降级，不重放已经执行的工具。
        def request_current(client_arg, model, messages, **kwargs):
            try:
                return _create_chat_completion(client_arg, model, messages, **kwargs)
            except Exception as exc:
                rate_limited = (type(exc).__name__ == 'RateLimitError'
                                or getattr(exc, 'status_code', None) == 429)
                if not rate_limited or not fallback_model or fallback_model == model:
                    raise
                logger.warning('Primary model request was rate limited; retrying that request on fallback')
                trace.add('切换备用模型', {'from': model, 'to': fallback_model,
                                            'scope': 'current_model_request', 'reason': str(exc)})
                return _create_chat_completion(client_arg, fallback_model, messages, **kwargs)

        # 借用应用级连接池；预算、轨迹和执行状态仍每轮独立。
        with borrow_client() as client:
            # 执行器共享整轮预算，返回回复和可选变更包。
            budget = (TurnBudget(max_models=None, max_tools=None, max_external=None, cancelled=cancelled)
                      if request_context.entry == 'main' else TurnBudget(cancelled=cancelled))
            runtime = TaskExecutionRuntime(
                client, primary_model, request_context, execution_context,
                request=request_current, trace=trace, budget=budget,
            )
            result = runtime.run()
            trace.add('Task Execution结果', {
                'failed': result.failed,
                'reason': result.reason, 'has_changes': result.changes is not None,
                'usage': budget.usage(),
            })
            return result
    except Exception as exc:
        trace.write(exc)
        return ExecutionResult.failure('本次任务执行未完成，当前需求和行程已保留。')
    finally:
        trace.write()


# 使用独立的无工具预算将已保存行程文本转换为 HTML 攻略展示，失败时保留文字回退信息。
def generate_plan_display(source_text: str, source_dates, trip_id: str, *, cancelled=None, retry_allowed=None) -> dict:
    """Convert only saved prose with an independent budget; never run a business loop."""
    from app.agents.budget import TurnBudget
    from app.agents.display_runtime import DisplayRuntime, redact_error
    from app.services.itinerary.plan_display import text_hash
    trace = ConversationTrace(uuid.uuid4().hex[:12], datetime.now().isoformat(timespec='seconds'), trip_id, source_text)
    try:
        if not config.llm_enabled():
            raise RuntimeError('模型服务当前不可用，完整文字已保留')
        with borrow_client() as client:
            # 展示转换最多调用两次模型，明确禁止工具和外部访问。
            budget = TurnBudget(max_models=2, max_tools=0, max_external=0,
                seconds=display_timeout(), cancelled=cancelled)
            primary_model = model_name()
            fallback_model = fallback_model_name()

            def request_display(client_arg, model, messages, **kwargs):
                try:
                    return _create_chat_completion(client_arg, model, messages, **kwargs)
                except Exception as exc:
                    status_code = getattr(exc, 'status_code', None)
                    rate_limited = (type(exc).__name__ == 'RateLimitError' or status_code == 429)
                    if (not rate_limited or not fallback_model or fallback_model == model):
                        raise
                    trace.add('切换备用模型', {
                        'from': model, 'to': fallback_model, 'scope': 'display_model_request',
                        'error_type': type(exc).__name__, 'status_code': status_code,
                    })
                    return _create_chat_completion(client_arg, fallback_model, messages, **kwargs)

            runtime = DisplayRuntime(client, primary_model,
                source_dates, retry_allowed=retry_allowed,
                trace=trace, budget=budget, request=request_display)
            return runtime.convert_display(source_text)
    except Exception as exc:
        # 写入回退原因前遮蔽环境中的密钥、令牌和密码值；正常路径由 runtime 只计算一次哈希。
        source_hash = text_hash(source_text)
        detail = redact_error(exc)
        trace.add('展示转换', {'status': 'fallback', 'error_type': type(exc).__name__,
                                'status_code': getattr(exc, 'status_code', None),
                                'source_hash': source_hash})
        return {'display': None, 'display_source_hash': source_hash, 'display_status': 'fallback',
                'error_type': type(exc).__name__, 'reason': detail[:300]}
    finally:
        trace.write()


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
    # 固定端点按配置发送思考档位；服务拒绝时直接报错，不隐式改用默认值。
    reasoning_status = 'provider_default'
    if reasoning_effort is not None:
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
            # 仅保留响应格式的兼容处理，不自动移除思考参数。
            text = str(exc).lower()
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


# 识别端点整体不支持结构化响应格式的错误，但保留具体模式校验错误。
def _looks_like_unsupported_response_format(text: str) -> bool:
    if 'invalid_json_schema' in text:
        return False
    mentions = any(term in text for term in ('response_format', 'text.format', 'json_schema', 'structured output'))
    rejects = any(term in text for term in ('not supported', 'unsupported', 'not support', 'unknown parameter',
        'unrecognized', 'unrecognised', 'invalid parameter', 'supported values', 'not available'))
    return mentions and rejects
