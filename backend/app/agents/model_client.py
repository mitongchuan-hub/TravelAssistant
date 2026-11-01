# 固定模型请求、单请求限流备用与预算/响应轨迹。
from __future__ import annotations

import logging
import os
import uuid
from time import monotonic

from app.core.config import model_timeout
from app.agents.model_transport import create_completion, response_storage_enabled, visible_params, wire_api
from app.agents.trace import ConversationTrace

logger = logging.getLogger(__name__)


# 原样发送固定模型参数，记录单次请求的预算和轨迹；参数拒绝直接交给调用方。
def request_completion(
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
    fallback_model: str | None = None,
    fallback_scope: str = 'current_model_request',
) -> object:
    """Send fixed parameters; retry only this request once on HTTP 429, within budget."""
    for attempt in range(2):
        # 基础参数在 Chat Completions 与 Responses 两种线协议间共用。
        api = wire_api()
        params: dict[str, object] = {"model": model, "messages": messages}
        # 固定端点按配置发送思考档位；服务拒绝时直接报错，不隐式改用默认值。
        reasoning_status = 'provider_default'
        if reasoning_effort is not None:
            params['reasoning_effort'] = reasoning_effort
            reasoning_status = 'requested'
        # 仅 Chat Completions 支持此聊天模板开关；Responses 不发送。
        # 规划默认保留服务端思考行为，只有显式 true/false 才覆盖。
        thinking = os.getenv('OPENAI_MAIN_ENABLE_THINKING', '').strip().lower()
        if api == 'chat_completions' and agent_role == 'travel_agent' and thinking in {'true', 'false'}:
            params['extra_body'] = {'chat_template_kwargs': {'enable_thinking': thinking == 'true'}}
        if tools:
            params["tools"] = tools
        if response_format:
            params["response_format"] = response_format
        # 任务 ID 保持稳定，每次调用使用独立 UUID 关联开始、结束和响应事件。
        model_request_id = uuid.uuid4().hex
        request_id = trace.request_id if trace else None
        request_index = None
        effective_timeout = None
        remaining_before = None
        if budget is not None:
            try:
                budget.model()
                configured_timeout = model_timeout()
                remaining_before = budget.remaining()
                effective_timeout = min(configured_timeout, remaining_before)
                params['timeout'] = effective_timeout
                request_index = budget.model_calls
            except Exception as exc:
                if trace:
                    trace.add('模型请求未开始', {
                        'request_id': request_id, 'model_request_id': model_request_id, 'model': model,
                        'agent_role': agent_role, 'exception_type': type(exc).__name__,
                        'reason': '模型预算检查未通过',
                    })
                raise
        else:
            request_index = 1
        request_meta = {
            'request_id': request_id, 'model_request_id': model_request_id,
            'request_index': request_index,
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
            rate_limited = (type(exc).__name__ == 'RateLimitError'
                            or getattr(exc, 'status_code', None) == 429)
            if attempt or not rate_limited or not fallback_model or fallback_model == model:
                raise
            if fallback_scope == 'display_model_request':
                switch = {'from': model, 'to': fallback_model, 'scope': fallback_scope,
                          'error_type': type(exc).__name__, 'status_code': getattr(exc, 'status_code', None)}
            else:
                logger.warning('Primary model request was rate limited; retrying that request on fallback')
                switch = {'from': model, 'to': fallback_model, 'scope': fallback_scope, 'reason': str(exc)}
            if trace:
                trace.add('切换备用模型', switch)
            model = fallback_model
            continue
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
                'request_id': request_id, 'model_request_id': model_request_id,
                'request_index': request_index,
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
