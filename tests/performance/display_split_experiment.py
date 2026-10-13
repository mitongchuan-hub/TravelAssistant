"""Isolated two-request candidate; production display entry point is unchanged."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
from time import monotonic
from datetime import datetime
from uuid import uuid4
from jsonschema import Draft202012Validator

from app import agent_client
from app.agent_runtime import TravelAgentRuntime, TurnBudget, AgentBudgetError
from app.plan_display import display_format, validate_display, text_hash

PROMPTS = Path(__file__).parent / 'prompts'
GLOBAL_FIELDS = ('overview', 'budget_note', 'preparation', 'weather', 'pending_items', 'risks')
DAY_FIELDS = ('schema_version', 'title', 'days')


def stage_format(stage, dates):
    fields = GLOBAL_FIELDS if stage == 'global' else DAY_FIELDS if stage == 'days' else None
    if fields is None:
        raise ValueError('Unknown extraction stage')
    full = display_format(dates)['json_schema']['schema']
    schema = {'type': 'object', 'additionalProperties': False, 'required': list(fields),
              'properties': {key: deepcopy(full['properties'][key]) for key in fields}}
    return {'type': 'json_schema', 'json_schema': {
        'name': 'display_experiment_' + stage, 'strict': True, 'schema': schema}}


def generate_split_display(source_text, source_dates, trip_id, *, cancelled=None, retry_allowed=None):
    """Both stages read original prose; one 180s/2-SDK-call budget, no extra retries."""
    trace = agent_client.ConversationTrace(uuid4().hex[:12], datetime.now().isoformat(timespec='seconds'), trip_id, source_text)
    client = None
    budget = None
    stages = []
    result = {'display': None, 'display_status': 'fallback', 'display_source_hash': text_hash(source_text),
              'experiment_strategy': 'global_then_days', 'stages': stages}
    try:
        if not agent_client.is_llm_enabled():
            raise RuntimeError('模型服务当前不可用，完整文字已保留')
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'), base_url=os.getenv('OPENAI_BASE_URL') or None,
                        timeout=float(os.getenv('OPENAI_TIMEOUT_SECONDS', '180')), max_retries=0)
        budget = TurnBudget(max_models=2, max_tools=0, max_external=0,
                            seconds=float(os.getenv('TRAVEL_DISPLAY_TIMEOUT_SECONDS', '180')), cancelled=cancelled)
        runtime = TravelAgentRuntime(client, os.getenv('OPENAI_MODEL') or os.getenv('MODEL_ID') or 'qwen-plus',
            {'source_dates': deepcopy(source_dates), 'cancelled': cancelled}, trace=trace,
            cache={'_budget': budget}, request=agent_client._create_chat_completion)
        pieces = {}
        for stage in ('global', 'days'):
            budget.remaining()
            if retry_allowed is not None and not retry_allowed():
                raise AgentBudgetError('前台业务优先，实验停止后续提取；完整文字已保留')
            fmt = stage_format(stage, source_dates)
            runtime.configs['display'] = replace(runtime.configs['display'],
                prompt=(PROMPTS / ('display_' + stage + '.txt')).read_text(encoding='utf-8'))
            evidence = {'stage': stage, 'status': 'running'}
            stages.append(evidence)
            started = monotonic()
            trace.add('实验展示阶段开始', {'stage': stage})
            try:
                raw = runtime.model_node('display', {'source_text': source_text, 'source_dates': source_dates}, response_format=fmt)
                evidence['raw_output'] = raw
                data = json.loads(raw)
                Draft202012Validator(fmt['json_schema']['schema']).validate(data)
                pieces.update(data)
                evidence.update(status='validated', data=data)
            except Exception as exc:
                evidence.update(status='failed', error_type=type(exc).__name__)
                raise
            finally:
                evidence['seconds'] = monotonic() - started
                trace.add('实验展示阶段结束', {key: value for key, value in evidence.items() if key not in {'raw_output', 'data'}})
        display = validate_display(pieces, source_dates)
        budget.remaining()
        result.update(display=display, display_status='ready')
    except Exception as exc:
        detail = str(exc)
        for key, value in os.environ.items():
            if value and len(value) > 8 and any(word in key for word in ('KEY', 'TOKEN', 'PASSWORD')):
                detail = detail.replace(value, '[redacted]')
        result.update(error_type=type(exc).__name__, reason=detail[:500])
        trace.add('实验展示失败', {'error_type': type(exc).__name__, 'reason': detail[:500]})
    finally:
        result['sdk_requests'] = budget.model_calls if budget else 0
        if client is not None and callable(getattr(client, 'close', None)):
            client.close()
        trace.write()
    return result
