"""Agent 执行入口：负责装配、启动和收尾，不实现具体执行循环。

负责的工作：
- 提供主 Agent 入口 generate_task_result，接收可信请求与业务数据快照。
- 提供攻略展示入口 generate_plan_display，接收已保存的正文与日期。
- 检查模型启用状态，读取主模型和备用模型配置，绑定统一模型请求函数。
- 为每轮执行创建轨迹和预算，借用应用级共享模型客户端。
- 启动对应 Runtime，返回执行结果，处理失败回退并最终保存运行轨迹。

职责边界：
- 模型与工具循环由 travel_runtime.py 负责，HTML 转换和纠错由 display_runtime.py 负责。
- 模型请求、超时和限流备用由 model_client.py 负责，协议转换由 model_transport.py 负责。
- 轨迹收集与落库由 trace.py 负责，最终业务变更校验与保存由 workflow/turn_commit.py 负责。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from functools import partial

from app.core import config
from app.core.config import model_name, fallback_model_name, display_timeout
from app.core.model_resources import borrow_client
from app.agents.model_client import request_completion
from app.agents.trace import ConversationTrace


# 运行任务执行器，直接返回回复和可选的完整变更包。
def generate_task_result(request_context, execution_context, *, cancelled=None, on_progress=None):
    """Return a direct execution result; Store owns revalidation and persistence.

    Each failed HTTP 429 request gets at most one fallback retry within budget,
    not one retry per whole turn; already executed tools are never replayed.
    """
    from app.application.contracts import RequestContext
    from app.agents.budget import TurnBudget
    from app.agents.inputs.execution import TaskExecutionContext
    from app.agents.travel_runtime import TaskExecutionRuntime
    from app.application.contracts import ExecutionResult

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

        request_current = partial(request_completion, fallback_model=fallback_model,
                                  fallback_scope='current_model_request')

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

            request_display = partial(request_completion, fallback_model=fallback_model,
                                      fallback_scope='display_model_request')

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
