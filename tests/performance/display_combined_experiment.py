"""Single-request display candidate with distinct clothing, lodging and weather."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
from uuid import uuid4
from jsonschema import Draft202012Validator

from app import agent_client
from app.agent_runtime import TravelAgentRuntime, TurnBudget, AgentBudgetError
from app.plan_display import display_format, validate_display, text_hash

VERSION = '1.1-weather-experiment'
PROMPT = Path(__file__).parent / 'prompts/display_combined_weather.txt'


def combined_format(dates):
    base = display_format(dates)['json_schema']['schema']
    nullable = {'type': ['string', 'null'], 'minLength': 1}
    weather_fields = {
        'date_text': {**nullable, 'description': '原文天气日期/范围或模糊日期，保留如10月14日前后；不能扩成未给日期的逐日预报。'},
        'summary': {**nullable, 'description': '原文明示的天气描述，未给则null。'},
        'temperature_text': {**nullable, 'description': '原文气温与范围，保留约、单位及未知；不补数值。'},
        'precipitation_text': {**nullable, 'description': '原文降水概率，保留约、最高、≤等；未给则null。'},
        'queried_at_text': {**nullable, 'description': '原文查询时刻，如10月11日；未给则null。'},
        'notes': {'type': 'array', 'items': {'type': 'string', 'minLength': 1},
                  'description': '原文预报性质与复核提醒；不放衣物建议，不新增预报。'},
    }
    base['properties']['schema_version'] = {'type': 'string', 'const': VERSION}
    base['properties']['preparation']['properties']['clothing']['description'] = (
        '只提取原文衣物/装备建议；不据天气补雨具，不放预报或核对事项。原文无建议才[]。')
    base['properties']['weather'] = {'type': 'array', 'items': {'type': 'object',
        'additionalProperties': False, 'required': list(weather_fields), 'properties': weather_fields},
        'description': '原文已有天气预报，不是额外查询；仅保留已给日期，未给则[]。'}
    # Place the previously missed sections before the long daily list.
    order = ['schema_version', 'title', 'preparation', 'weather', 'overview', 'budget_note', 'pending_items', 'risks', 'days']
    base['properties'] = {key: base['properties'][key] for key in order}
    base['required'] = order
    return {'type': 'json_schema', 'json_schema': {'name': 'display_combined_weather_experiment', 'strict': True, 'schema': base}}


def validate_combined(data, dates):
    Draft202012Validator(combined_format(dates)['json_schema']['schema']).validate(data)
    common = deepcopy(data)
    common['schema_version'] = '1.1'
    validated = validate_display(common, dates)
    validated['schema_version'] = VERSION
    return validated


def generate_combined_display(source_text, source_dates, trip_id, *, cancelled=None, retry_allowed=None):
    trace = agent_client.ConversationTrace(uuid4().hex[:12], datetime.now().isoformat(timespec='seconds'), trip_id, source_text)
    client, budget = None, None
    result = {'display': None, 'display_status': 'fallback', 'display_source_hash': text_hash(source_text),
              'experiment_strategy': 'combined_weather_single_request'}
    try:
        if not agent_client.is_llm_enabled():
            raise RuntimeError('模型服务当前不可用，完整文字已保留')
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'), base_url=os.getenv('OPENAI_BASE_URL') or None,
                        timeout=float(os.getenv('OPENAI_TIMEOUT_SECONDS', '180')), max_retries=0)
        budget = TurnBudget(max_models=1, max_tools=0, max_external=0,
            seconds=float(os.getenv('TRAVEL_DISPLAY_TIMEOUT_SECONDS', '180')), cancelled=cancelled)
        budget.remaining()
        if retry_allowed is not None and not retry_allowed():
            raise AgentBudgetError('前台业务优先，实验未发起提取')
        runtime = TravelAgentRuntime(client, os.getenv('OPENAI_MODEL') or os.getenv('MODEL_ID') or 'qwen-plus',
            {'source_dates': deepcopy(source_dates), 'cancelled': cancelled}, trace=trace,
            cache={'_budget': budget}, request=agent_client._create_chat_completion)
        runtime.configs['display'] = replace(runtime.configs['display'], prompt=PROMPT.read_text(encoding='utf-8'))
        raw = runtime.model_node('display', {'source_text': source_text, 'source_dates': source_dates},
                                 response_format=combined_format(source_dates))
        result['raw_output'] = raw
        display = validate_combined(json.loads(raw), source_dates)
        budget.remaining()
        result.update(display=display, display_status='ready')
    except Exception as exc:
        detail = str(exc)
        for key, value in os.environ.items():
            if value and len(value)>8 and any(word in key for word in ('KEY', 'TOKEN', 'PASSWORD')):
                detail = detail.replace(value, '[redacted]')
        result.update(error_type=type(exc).__name__, reason=detail[:500])
        trace.add('合并提取实验失败', {'error_type': type(exc).__name__, 'reason': detail[:500]})
    finally:
        result['sdk_requests'] = budget.model_calls if budget else 0
        if client is not None and callable(getattr(client, 'close', None)):
            client.close()
        trace.write()
    return result
