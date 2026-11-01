"""单旅行 Agent 与独立展示转换节点的配置。"""
from dataclasses import dataclass
import os

from app.agents.prompts import load_prompt
from app.agents.tools.registry import agent_tool_specs


@dataclass(frozen=True)
class AgentRole:
    name: str
    prompt: str
    tools: tuple = ()
    response_format: dict | None = None
    max_steps: int | None = 16
    reasoning_effort: str = 'medium'


REASONING_EFFORTS = {'default', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}


def role_reasoning_effort(role, scope=''):
    value = os.getenv(f'OPENAI_REASONING_EFFORT_{role.name.upper()}', '').strip().lower()
    if not value and role.name == 'travel_agent':
        value = os.getenv('OPENAI_REASONING_EFFORT_MAIN', '').strip().lower()
    value = value or os.getenv('OPENAI_REASONING_EFFORT', '').strip().lower() or role.reasoning_effort
    if value not in REASONING_EFFORTS:
        raise ValueError(f'Invalid reasoning effort for {role.name}: {value}')
    return None if value == 'default' else value


def _check_mode(mode):
    if mode != 'city':
        raise ValueError('Unsupported trip mode')


def travel_role(mode='city'):
    _check_mode(mode)
    return AgentRole('travel_agent', load_prompt('main.txt'), agent_tool_specs(),
                     max_steps=16, reasoning_effort='medium')


def display_role(mode='city'):
    _check_mode(mode)
    return AgentRole('display', load_prompt('display.txt'), reasoning_effort='low')
