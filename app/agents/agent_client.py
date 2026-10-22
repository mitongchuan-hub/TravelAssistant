from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import monotonic
from copy import deepcopy

from dotenv import load_dotenv

from app.models import AgentCard
from app.tools import run_tool
from app.agents.model_transport import create_completion, response_storage_enabled, visible_params, wire_api


APP_DIR = Path(__file__).resolve().parents[2]
load_dotenv(APP_DIR / ".env", override=False)
logger = logging.getLogger(__name__)


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


VALID_IDEA_KINDS = {"地点", "预算", "节奏", "禁忌", "待归类"}


@dataclass
class ConversationTrace:
    request_id: str
    started_at: str
    trip_id: str
    user_body: str
    events: list[str] = field(default_factory=list)
    records: list[dict] = field(default_factory=list, repr=False)
    written: bool = field(default=False, repr=False)

    def add(self, title: str, value: object) -> None:
        if isinstance(value, str):
            content = value
        else:
            content = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        self.events.append(f"\n{'=' * 20} {title} {'=' * 20}\n{content}\n")
        self.records.append({"title": title, "value": value})

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
def record_agent_run(trace: ConversationTrace, *, error: BaseException | None = None) -> None:
    from app.persistence import insert_agent_run
    insert_agent_run(os.getenv("CITY_DB_PATH", ".cache/cityassistant.sqlite3"), _agent_run_row(trace, error))


def _agent_run_row(trace: ConversationTrace, error: BaseException | None) -> dict:
    records = trace.records
    output = None
    for record in reversed(records):
        if record["title"] == "最终模型原始回复" and isinstance(record["value"], str):
            output = record["value"]
            break
    model = None
    for record in records:
        value = record["value"]
        if not isinstance(value, dict):
            continue
        if record["title"] == "模型请求" and value.get("model"):
            model = value["model"]
        elif record["title"] == "切换备用模型" and value.get("to"):
            model = value["to"]
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
    tool_calls = 0
    for record in records:
        if record["title"] == "共享调用预算" and isinstance(record["value"], dict):
            tool_calls = int(record["value"].get("tool_calls") or 0)
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


def is_llm_enabled() -> bool:
    disabled = os.getenv("TRAVEL_AGENT_DISABLE_LLM", "").strip().lower()
    if disabled in {"1", "true", "yes", "on"} or os.getenv("PYTEST_CURRENT_TEST"):
        return False
    return bool(os.getenv("OPENAI_API_KEY"))


def generate_task_proposal(request_context, execution_context, *, cancelled=None):
    """Run the task-execution strategy and return only a sealed proposal.

    Strategy selection and persistence stay in Store.  A fallback retries only
    the model request that failed; it never reconstructs or replays the workspace.
    """
    from app.agents.agent_protocol import RequestContext
    from app.agents.agent_runtime import TurnBudget
    from app.workflow.task_context import normalize_workflow
    from app.agents.task_runtime import TaskExecutionContext, TaskExecutionRuntime
    from app.workflow.turn_workspace import TurnWorkspace

    if not isinstance(request_context, RequestContext):
        raise ValueError('request_context必须由服务器创建')
    if not isinstance(execution_context, TaskExecutionContext):
        raise ValueError('execution_context类型不正确')
    trace = ConversationTrace(
        request_id=request_context.request_id,
        started_at=datetime.now().isoformat(timespec='seconds'),
        trip_id=request_context.trip_id,
        user_body=request_context.body,
    )
    trace.add('输入上下文', {
        'strategy': request_context.strategy, 'entry': request_context.entry,
        'request_id': request_context.request_id, 'message_id': request_context.message_id,
        'trip_id': request_context.trip_id,
        'requirements_revision': request_context.requirements_revision,
        'workflow_revision': request_context.workflow_revision,
        'sequence': request_context.sequence, 'epoch': request_context.epoch,
    })
    if not is_llm_enabled():
        workflow = normalize_workflow(getattr(execution_context.trip, 'workflow_state', {}),
                                      messages=execution_context.messages,
                                      plan_basis=request_context.plan_basis,
                                      requirements_revision=request_context.requirements_revision)
        workspace = TurnWorkspace(request_context, workflow_state=workflow,
            authorized_message_ids=[getattr(row, 'id', '') for row in execution_context.messages
                                    if getattr(row, 'id', '')],
            visible_task_ids=[row['id'] for row in workflow['tasks']])
        proposal = workspace.fail_closed('模型服务当前不可用，本次未生成或修改行程。')
        trace.add('Task Execution结果', {'failed': True, 'reason': proposal.reason})
        trace.write()
        return proposal

    client = None
    runtime = None
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=os.getenv('OPENAI_API_KEY'),
            base_url=os.getenv('OPENAI_BASE_URL') or None,
            timeout=float(os.getenv('OPENAI_TIMEOUT_SECONDS', '180.0')),
            max_retries=0,
        )
        primary_model = os.getenv('OPENAI_MODEL') or os.getenv('MODEL_ID') or 'qwen-plus'
        fallback_model = os.getenv('OPENAI_FALLBACK_MODEL', 'qwen-plus').strip()

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

        budget = TurnBudget(cancelled=cancelled)
        runtime = TaskExecutionRuntime(
            client, primary_model, request_context, execution_context,
            request=request_current, trace=trace, budget=budget,
        )
        proposal = runtime.run()
        trace.add('Task Execution结果', {
            'proposal_id': proposal.id, 'failed': proposal.failed,
            'reason': proposal.reason, 'selected_artifact_ids': proposal.selected_artifact_ids,
            'usage': budget.usage(),
        })
        return proposal
    except Exception as exc:
        trace.write(exc)
        # If execution started, its workspace is authoritative: preserve only
        # explicitly ready independent effects instead of reconstructing a turn.
        if runtime is not None:
            try:
                return runtime.workspace.fail_closed('本次任务执行未完成，当前需求和行程已保留。')
            except WorkspaceError:
                raise
        workflow = normalize_workflow(getattr(execution_context.trip, 'workflow_state', {}),
                                      messages=execution_context.messages,
                                      plan_basis=request_context.plan_basis,
                                      requirements_revision=request_context.requirements_revision)
        workspace = TurnWorkspace(request_context, workflow_state=workflow,
            authorized_message_ids=[getattr(row, 'id', '') for row in execution_context.messages
                                    if getattr(row, 'id', '')],
            visible_task_ids=[row['id'] for row in workflow['tasks']])
        return workspace.fail_closed('本次任务执行未完成，当前需求和行程已保留。')
    finally:
        if client is not None and callable(getattr(client, 'close', None)):
            client.close()
        trace.write()


def generate_plan_display(source_text: str, source_dates, trip_id: str, *, cancelled=None, retry_allowed=None) -> dict:
    """Convert only saved prose with an independent budget; never run a business loop."""
    from app.agents.agent_runtime import TravelAgentRuntime, TurnBudget
    from app.services.plan_display import text_hash
    trace = ConversationTrace(uuid.uuid4().hex[:12], datetime.now().isoformat(timespec='seconds'), trip_id, source_text)
    client = None
    source_hash = text_hash(source_text)
    try:
        if not is_llm_enabled():
            raise RuntimeError('模型服务当前不可用，完整文字已保留')
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'), base_url=os.getenv('OPENAI_BASE_URL') or None,
            timeout=float(os.getenv('OPENAI_TIMEOUT_SECONDS', '180')), max_retries=0)
        budget = TurnBudget(max_models=2, max_tools=0, max_external=0,
            seconds=float(os.getenv('TRAVEL_DISPLAY_TIMEOUT_SECONDS', '180')), cancelled=cancelled)
        runtime = TravelAgentRuntime(client, os.getenv('OPENAI_MODEL') or os.getenv('MODEL_ID') or 'qwen-plus',
            {'source_dates': deepcopy(source_dates), 'cancelled': cancelled,
             'display_retry_allowed': retry_allowed or (lambda: True)}, trace=trace,
            cache={'_budget': budget}, request=_create_chat_completion)
        reply = runtime.convert_display(AgentReply(source_text, None, [], plan_text=source_text))
        return {'display': reply.display, 'display_source_hash': source_hash,
                'display_status': reply.display_status, **runtime.cache.get('display_error', {})}
    except Exception as exc:
        detail = str(exc)
        for key, value in os.environ.items():
            if value and len(value) > 8 and any(word in key for word in ('KEY', 'TOKEN', 'PASSWORD')):
                detail = detail.replace(value, '[redacted]')
        trace.add('展示转换', {'status': 'fallback', 'error_type': type(exc).__name__, 'reason': detail[:300],
                                'source_hash': source_hash})
        return {'display': None, 'display_source_hash': source_hash, 'display_status': 'fallback',
                'error_type': type(exc).__name__, 'reason': detail[:300]}
    finally:
        if client is not None and callable(getattr(client, 'close', None)):
            client.close()
        trace.write()










def _is_rate_limit_error(exc: Exception) -> bool:
    return type(exc).__name__ == "RateLimitError" or getattr(exc, "status_code", None) == 429




def _looks_like_unsupported_tools(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(word in text for word in ("tool", "function", "unsupported", "not support"))


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

    api = wire_api()
    params: dict[str, object] = {"model": model, "messages": messages}
    rejection_key = (str(getattr(client, 'base_url', '')) + '#' + api, model, reasoning_effort or '')
    rejections = budget.reasoning_rejections if budget is not None else set()
    reasoning_status = 'provider_default'
    if reasoning_effort is not None:
        if rejection_key in rejections:
            reasoning_status = 'provider_default_after_rejection'
        else:
            params['reasoning_effort'] = reasoning_effort
            reasoning_status = 'requested'
    # The main execution loop is a routing/coordination pass: the endpoint only
    # honors the chat-template switch (a bare enable_thinking parameter is ignored
    # server-side). Env var OPENAI_MAIN_ENABLE_THINKING=true restores default thinking.
    if (agent_role == 'main_execution'
            and os.getenv('OPENAI_MAIN_ENABLE_THINKING', 'false').strip().lower() != 'true'):
        params['extra_body'] = {'chat_template_kwargs': {'enable_thinking': False}}
    if tools:
        params["tools"] = tools
    if response_format:
        params["response_format"] = response_format
    while True:
        try:
            if budget is not None:
                budget.model()
                params['timeout'] = min(float(os.getenv('OPENAI_TIMEOUT_SECONDS', '180')), budget.remaining())
            if trace:
                trace.add('模型请求', {'model': model, 'agent_role': agent_role, 'params': visible_params(params),
                                     'wire_api': api, 'store': response_storage_enabled(),
                                     'reasoning': {'requested': reasoning_effort, 'sent': params.get('reasoning_effort'),
                                                   'status': reasoning_status}})
            started = monotonic()
            response = create_completion(client, params, api)
            usage = getattr(response, 'usage', None)
            if budget is not None:
                budget.record_usage(getattr(usage, 'prompt_tokens', None),
                                    getattr(usage, 'completion_tokens', None))
            if trace:
                message = response.choices[0].message
                details = getattr(usage, 'completion_tokens_details', None)
                trace.add("模型响应", {
                    'agent_role': agent_role,
                    'seconds': round(monotonic() - started, 3),
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
    # A generic tools/schema error mentioning effort is not enough to drop it.
    conflicting_parameter = parameter and parameter not in effort_parameters
    return bool(mentions_effort and rejects and not conflicting_parameter)


def _looks_like_unsupported_response_format(text: str) -> bool:
    if 'invalid_json_schema' in text:
        return False
    mentions = any(term in text for term in ('response_format', 'text.format', 'json_schema', 'structured output'))
    rejects = any(term in text for term in ('not supported', 'unsupported', 'not support', 'unknown parameter',
        'unrecognized', 'unrecognised', 'invalid parameter', 'supported values', 'not available'))
    return mentions and rejects


def _parse_idea_cards(raw_cards: object) -> list[dict]:
    if not isinstance(raw_cards, list):
        return []

    idea_cards = []
    for raw_card in raw_cards[:3]:
        if not isinstance(raw_card, dict):
            continue
        kind = str(raw_card.get("kind") or "待归类").strip()
        if kind not in VALID_IDEA_KINDS:
            kind = "待归类"
        title = str(raw_card.get("title") or "新的旅行想法").strip()[:24]
        body = str(raw_card.get("body") or title).strip()[:120]
        status = str(raw_card.get("status") or "已整理").strip()[:16]
        if title and body:
            idea_cards.append({"kind": kind, "title": title, "body": body, "status": status})
    return idea_cards



