"""Model nodes and three business agents sharing one bounded tool loop."""
from dataclasses import dataclass
import os

from .prompts import load_prompt
from .tools import tool_specs
from .requirements import PROFILE_FIELDS, BASIC_FIELDS, LIST_FIELDS


def tool(name, description, properties, required=()):
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties, 'required': list(required),
                       'additionalProperties': False}}}


READ_CONTEXT = tool('read_context', '读取本旅行需求、聊天、想法、当前行程或真实查询依据。', {
    'section': {'type': 'string', 'enum': ['requirements', 'messages', 'ideas', 'plan', 'evidence']},
    'query': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0},
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
}, ['section'])

changes = {key: {'type': 'string'} for key in (PROFILE_FIELDS | BASIC_FIELDS) - LIST_FIELDS - {'travelers', 'day_count'}}
changes.update({key: {'type': ['integer', 'null']} for key in ('travelers', 'day_count')})
changes.update({key: {'type': 'object', 'additionalProperties': False, 'properties': {
    'add': {'type': 'array', 'items': {'type': 'string'}},
    'remove': {'type': 'array', 'items': {'type': 'string'}}}} for key in LIST_FIELDS})
changes['budget_amount'] = {'type': 'string', 'pattern': r'^\d+(?:\.\d{1,2})?$',
    'description': '纯数字金额文本，例如2600，不含元、人均或其他单位。'}
changes['budget_scope'] = {'type': 'string', 'enum': ['total', 'per_person', 'unspecified']}
UPDATE_REQUIREMENTS = tool('update_requirements', '在本轮快照中整理用户明确的新需求。正确参数格式：{"user_quote":"完整用户原话","changes":{"travelers":2,"budget_amount":"2600","budget_scope":"total"}}。user_quote在顶层，与changes并列，不放入changes。列表使用add/remove，系统最终保存。', {
    'user_quote': {'type': 'string', 'description': '顶层参数，与changes并列，值是最后一条用户消息的完整原文，不是上下文JSON。'},
    'changes': {'type': 'object', 'properties': changes, 'additionalProperties': False},
    'idea_cards': {'type': 'array', 'items': {'type': 'object', 'properties': {
        'kind': {'type': 'string'}, 'title': {'type': 'string'}, 'body': {'type': 'string'},
        'status': {'type': 'string'}}, 'required': ['kind', 'title', 'body'], 'additionalProperties': False}},
}, ['user_quote', 'changes'])
CHECK_REQUIREMENTS = tool('check_requirements', '已审阅整条最新原话后，检查五组必填需求与日期窗口。生成前必须执行；信息不足则系统结束本轮并提出补充问题。只补充需求时不自动生成。', {
    'user_quote': {'type': 'string'},
}, ['user_quote'])
INTENT_SCHEMA = {'type': 'object', 'additionalProperties': False,
    'properties': {
        'intent': {'type': 'string', 'enum': ['chat', 'query', 'planning', 'revision']},
        'action': {'type': 'string', 'enum': ['reply', 'update_requirements', 'generate', 'modify', 'explain', 'confirm', 'cancel']},
        'scope': {'type': 'string', 'enum': ['', 'item', 'all', 'explain']},
        'user_quote': {'type': 'string'},
        'question': {'type': ['string', 'null']},
    }, 'required': ['intent', 'action', 'scope', 'user_quote', 'question']}


@dataclass(frozen=True)
class AgentRole:
    name: str
    prompt: str
    tools: tuple = ()
    response_format: dict | None = None
    max_steps: int = 8
    reasoning_effort: str = 'low'


REASONING_EFFORTS = {'default', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}


def role_reasoning_effort(role, scope=''):
    value = os.getenv(f'OPENAI_REASONING_EFFORT_{role.name.upper()}', '').strip().lower()
    if not value and role.name in {'chat', 'intent'}:
        value = os.getenv('OPENAI_REASONING_EFFORT_MAIN', '').strip().lower()
    value = value or os.getenv('OPENAI_REASONING_EFFORT', '').strip().lower()
    value = value or ('low' if role.name == 'revision' and scope == 'explain' else role.reasoning_effort)
    if value not in REASONING_EFFORTS:
        raise ValueError(f'Invalid reasoning effort for {role.name}: {value}')
    return None if value == 'default' else value


def roles():
    rules = load_prompt('agents/travel_rules.txt')
    read = (READ_CONTEXT,)
    facts = tuple(tool_specs())
    requirements = (UPDATE_REQUIREMENTS, CHECK_REQUIREMENTS)
    return {
        'main': AgentRole('main', load_prompt('agents/main.txt'), response_format={
            'type': 'json_schema', 'json_schema': {'name': 'travel_intent', 'strict': True, 'schema': INTENT_SCHEMA}}),
        'chat': AgentRole('chat', '你是旅行助手。用简体中文自然回应闲聊。没有工具，不声称查询、更新需求或保存行程。'),
        'research': AgentRole('research', load_prompt('agents/research.txt') + '\n\n' + rules, read + facts, max_steps=6),
        'planning': AgentRole('planning', load_prompt('agents/planning.txt') + '\n\n' + rules, read + requirements + facts, max_steps=10, reasoning_effort='medium'),
        'revision': AgentRole('revision', load_prompt('agents/revision.txt') + '\n\n' + rules,
            read + requirements + facts, max_steps=10, reasoning_effort='medium'),
        'display': AgentRole('display', load_prompt('agents/display.txt')),
    }
