"""工具的参数、校验与处理函数共享一个注册定义。"""
from __future__ import annotations

import json
from pathlib import Path
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable

from jsonschema import Draft202012Validator

from app.agents.tools.read_context import READ_CONTEXT_TOOL_SPEC
from app.agents.tools.read_skill import READ_SKILL_TOOL_SPEC, read_skill as local_read_skill
from app.agents.tools.save_trip_changes import SAVE_TRIP_CHANGES_TOOL_SPEC
from app.services.maps import amap_mcp, amap_rest
from app.agents.tools.weather import WEATHER_TOOL_SPEC, query_weather


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable
    terminal: bool = False

    def model_spec(self) -> dict:
        parameters = deepcopy(self.parameters)
        if self.name == 'save_trip_changes':
            # The configured llama.cpp endpoint emits {} with a root anyOf.
            # Keep the full schema for local validation; only project the wire schema.
            parameters.pop('anyOf', None)
        return {'type': 'function', 'function': {
            'name': self.name, 'description': self.description,
            'parameters': parameters}}

    def validate(self, arguments: dict) -> None:
        Draft202012Validator(self.parameters).validate(arguments)
        if self.name.startswith('maps_direction_'):
            for side in ('origin', 'destination'):
                if not arguments.get(side) and not arguments.get(side + '_ref'):
                    raise ValueError('路线每端必须提供可信中心坐标或服务器端点ref')

    def execute(self, arguments: dict):
        self.validate(arguments)
        return self.handler(arguments)


# Search keeps native MCP schemas. Detail/routes have local fixed REST contracts;
# optional endpoint refs are resolved by the runtime, never sent to MCP validation.
_NATIVE = json.loads(Path(__file__).with_name('amap_tools.json').read_text(encoding='utf-8'))
_EXTERNAL = tuple(ToolSpec(item['name'], item['description'], item['parameters'],
                          lambda args, name=item['name']: (amap_rest.call(args) if name == 'maps_search_detail'
                                                         else amap_rest.route(name, args) if name.startswith('maps_direction_')
                                                         else amap_mcp.call(name, args))) for item in _NATIVE)
_EXTERNAL += (ToolSpec(WEATHER_TOOL_SPEC['function']['name'], WEATHER_TOOL_SPEC['function']['description'],
                      WEATHER_TOOL_SPEC['function']['parameters'], query_weather),)


def run_tool(name: str, arguments: dict, *, place_catalog=None, route_cache=None) -> dict:
    spec = next((item for item in _EXTERNAL if item.name == name), None)
    if spec is None:
        raise ValueError('未知查询工具')
    spec.validate(arguments)
    return query_weather(arguments, cache=route_cache) if name == 'query_weather' else spec.handler(arguments)


def agent_tool_registry(*, read_context, save_trip_changes, query, read_skill=local_read_skill) -> dict[str, ToolSpec]:
    """Register native queries plus local read and terminal-submit actions."""
    read = READ_CONTEXT_TOOL_SPEC['function']
    submit = SAVE_TRIP_CHANGES_TOOL_SPEC['function']
    skill = READ_SKILL_TOOL_SPEC['function']
    specs = [ToolSpec(read['name'], read['description'], read['parameters'], read_context),
             ToolSpec(skill['name'], skill['description'], skill['parameters'], read_skill)]
    specs.extend(ToolSpec(item.name, item.description, item.parameters,
                          lambda args, name=item.name: query(name, args)) for item in _EXTERNAL)
    specs.append(ToolSpec(submit['name'], submit['description'], submit['parameters'],
                          save_trip_changes, terminal=True))
    return {item.name: item for item in specs}


def agent_tool_specs() -> tuple[dict, ...]:
    registry = agent_tool_registry(read_context=lambda args: None,
                                   save_trip_changes=lambda args: None, query=lambda name, args: None)
    return tuple(item.model_spec() for item in registry.values())
