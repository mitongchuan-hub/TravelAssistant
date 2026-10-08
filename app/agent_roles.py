"""Role configuration. Every role uses the same tool-call loop."""
from dataclasses import dataclass
from copy import deepcopy
import os

from .prompts import load_prompt
from .response_schema import PLANNING_RESULT_RESPONSE_FORMAT
from .tools import tool_specs


def tool(name, description, properties, required=()):
    return {'type': 'function', 'function': {
        'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties,
                       'required': list(required), 'additionalProperties': False},
    }}


READ_CONTEXT = tool('read_context', '读取本旅行真实上下文。需求完整返回；聊天和想法可按关键词检索并分页；plan 返回当前完整计划；versions 可按 version_id 读取历史；evidence 返回已有工具事实。', {
    'section': {'type': 'string', 'enum': ['requirements', 'messages', 'ideas', 'plan', 'versions', 'evidence']},
    'query': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0},
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}, 'version_id': {'type': 'string'},
}, ['section'])

DELEGATIONS = {
    'delegate_requirements': 'requirements', 'delegate_research': 'research',
    'delegate_planning': 'planning', 'delegate_revision': 'revision',
}


def delegation(name, role):
    properties = {
        'task': {'type': 'string', 'description': '要完成的业务任务及用户授权边界'},
        'user_quote': {'type': 'string', 'description': '最新用户消息中的连续原文，必须精确匹配'},
        'finish_turn': {'type': 'boolean', 'description': '本轮只有此任务或这是最后一个任务时为 true，直接使用子 Agent 结果回复，省去汇总调用。有后续依赖任务时为 false。'},
    }
    if role == 'revision':
        properties['scope'] = {'type': 'string', 'enum': ['explain', 'item', 'all'],
                               'description': '解释；仅一项；用户明确授权全局修改'}
    return tool(name, f'委派 {role} Agent；子 Agent 自行读取上下文与完成任务，不能递归委派。',
                properties, properties.keys())


VERSION_READ = tool('read_versions', '读取当前与历史版本标识，用于明确确认或恢复目标。', {})
VERSION_CHANGE = tool('change_version', '用户明确授权后提出确认当前方案或恢复历史版本。系统最终检查后执行；不与需求或行程修改混合。', {
    'operation': {'type': 'string', 'enum': ['confirm', 'restore']},
    'version_id': {'type': 'string', 'description': 'confirm 用当前 plan.id；restore 用历史 version.id，均须先 read_versions'},
    'user_quote': {'type': 'string'},
}, ['operation', 'version_id', 'user_quote'])

PLAN_FORMAT = deepcopy(PLANNING_RESULT_RESPONSE_FORMAT)
_schema = PLAN_FORMAT['json_schema']['schema']
_schema['properties'] = {key: _schema['properties'][key] for key in ('body', 'plan')}
_schema['properties']['status'] = {'type': 'string', 'enum': ['complete', 'needs_input']}
_schema['properties']['target_item_id'] = {'type': 'string', 'description': '局部修改时填当前目标 ID，其余填空字符串'}
_schema['properties']['plan']['type'] = ['object', 'null']
_schema['required'] = ['status', 'body', 'plan', 'target_item_id']
PLAN_FORMAT['json_schema']['name'] = 'travel_agent_plan'


@dataclass(frozen=True)
class AgentRole:
    name: str
    prompt: str
    tools: tuple
    response_format: dict | None = None
    max_steps: int = 8
    reasoning_effort: str = 'low'


REASONING_EFFORTS = {'default', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}


def role_reasoning_effort(role: AgentRole, scope: str = '') -> str | None:
    """Explicit role override > global override > role default; default omits the parameter."""
    value = os.getenv(f'OPENAI_REASONING_EFFORT_{role.name.upper()}', '').strip().lower()
    if not value:
        value = os.getenv('OPENAI_REASONING_EFFORT', '').strip().lower()
    if not value:
        value = 'low' if role.name == 'revision' and scope == 'explain' else role.reasoning_effort
    if value not in REASONING_EFFORTS:
        raise ValueError(f'Invalid reasoning effort for {role.name}: {value}')
    return None if value == 'default' else value


def roles():
    read = (READ_CONTEXT,)
    facts = tuple(tool_specs())
    planning = load_prompt('agents/planning.txt')
    return {
        'main': AgentRole('main', load_prompt('agents/main.txt'),
                          tuple(delegation(n, r) for n, r in DELEGATIONS.items()) + (VERSION_READ, VERSION_CHANGE)),
        'requirements': AgentRole('requirements', load_prompt('agents/requirements.txt'), read,
                                  {'type': 'json_object'}, 4),
        'research': AgentRole('research', load_prompt('agents/research.txt'), read + facts, max_steps=6),
        'planning': AgentRole('planning', planning, read + facts, PLAN_FORMAT, reasoning_effort='medium'),
        'revision': AgentRole('revision', planning + '\n\n' + load_prompt('agents/revision.txt'), read + facts, PLAN_FORMAT,
                              reasoning_effort='medium'),
    }
