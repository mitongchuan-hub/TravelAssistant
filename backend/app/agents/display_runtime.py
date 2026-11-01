"""Tool-free conversion of saved itinerary prose into a validated HTML guide."""
# 展示转换独立于业务执行器，只读取已保存正文，不查询事实或修改行程。
from copy import deepcopy
import json
import os
from time import monotonic

from app.agents.roles import display_role, role_reasoning_effort
from app.agents.budget import TurnBudget
from app.services.itinerary.guide_html import build_guide_display
from app.services.itinerary.plan_display import text_hash


def redact_error(error: Exception) -> str:
    """Hide configured credentials before tracing or returning provider errors."""
    detail = str(error)
    for key, value in os.environ.items():
        if value and any(word in key.upper() for word in ('KEY', 'TOKEN', 'PASSWORD', 'SECRET')):
            detail = detail.replace(value, '[redacted]')
    return detail


class DisplayRuntime:
    DEFAULT_STYLE = '轻松、愉悦'

    def __init__(self, client, model, source_dates, *, request, trace=None,
                 budget=None, retry_allowed=None):
        self.client, self.model, self.request, self.trace = client, model, request, trace
        self.snapshot = {'source_dates': deepcopy(source_dates),
                         'display_retry_allowed': retry_allowed if retry_allowed is not None else (lambda: True)}
        self.budget = budget if budget is not None else TurnBudget(max_models=2, max_tools=0, max_external=0)

    def event(self, name, value):
        if self.trace:
            self.trace.add(name, value)

    # 每次转换都使用展示角色配置，且绝不向模型开放工具。
    def model_node(self, role, payload):
        config = display_role()
        messages = [{'role': 'system', 'content': config.prompt},
                    {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
        started = monotonic()
        try:
            response = self.request(self.client, self.model, messages, tools=None,
                response_format=config.response_format, trace=self.trace, budget=self.budget,
                reasoning_effort=role_reasoning_effort(config), agent_role='display')
            self.budget.remaining()
            message = response.choices[0].message
            if getattr(message, 'tool_calls', None):
                raise ValueError('无工具模型节点不能调用工具')
            content = (message.content or '').strip()
            if not content:
                raise ValueError('Empty model response')
            return content
        finally:
            self.event('Workflow 模型节点', {'node': role, 'seconds': round(monotonic() - started, 3)})

    # HTML/CSP/日期校验不通过时，预算和代次允许才携带校验原因纠错一次。
    def convert_display(self, source_text: str) -> dict:
        source_hash = text_hash(source_text)
        try:
            dates = self.snapshot['source_dates']
            payload = {'source_text': source_text, 'source_dates': dates,
                       'display_style': self.DEFAULT_STYLE, 'output_format': 'html'}
            raw = self.model_node('display', payload)
            try:
                display = build_guide_display(raw, dates)
            except ValueError as invalid:
                retry_allowed = self.snapshot.get('display_retry_allowed', lambda: True)
                if self.budget.remaining() < 60 or self.budget.model_calls >= self.budget.max_models or not retry_allowed():
                    raise
                self.budget.check_current()
                reason = invalid.message if hasattr(invalid, 'message') else str(invalid)
                self.event('展示纠错', {'error_type': type(invalid).__name__, 'reason': reason[:300]})
                payload.update(previous_output=raw, validation_error=reason[:500],
                    correction_instruction='修正 HTML 结构及被拒绝内容，返回完整 HTML。仅使用内联 CSS，'
                    '所有标签显式闭合；保留原文活动顺序、交通、条件、预算与未知，不新增业务事实。')
                raw = self.model_node('display', payload)
                display = build_guide_display(raw, dates)
            self.event('展示转换', {'status': 'ready', 'source_hash': source_hash})
            return {'display': display, 'display_source_hash': source_hash, 'display_status': 'ready'}
        except Exception as exc:
            self.budget.check_current()
            detail = redact_error(exc)
            self.event('展示转换', {'status': 'fallback', 'error_type': type(exc).__name__,
                'status_code': getattr(exc, 'status_code', None), 'source_hash': source_hash})
            return {'display': None, 'display_source_hash': source_hash, 'display_status': 'fallback',
                    'error_type': type(exc).__name__, 'reason': detail[:500]}
