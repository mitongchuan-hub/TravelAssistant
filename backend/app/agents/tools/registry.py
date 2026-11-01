"""工具的参数、校验与处理函数共享一个注册定义。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Callable

from jsonschema import Draft202012Validator

from app.services.requirements import BASIC_FIELDS, LIST_FIELDS, PROFILE_FIELDS
from app.agents.tools.routes import ROUTE_TOOL_SPEC, lookup_route
from app.agents.tools.places import PLACE_TOOL_SPEC, search_places
from app.agents.tools.food import FOOD_TOOL_SPEC, search_food
from app.agents.tools.weather import WEATHER_TOOL_SPEC, query_weather


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable
    terminal: bool = False

    def model_spec(self) -> dict:
        return {'type': 'function', 'function': {
            'name': self.name, 'description': self.description,
            'parameters': deepcopy(self.parameters)}}

    def validate(self, arguments: dict) -> None:
        Draft202012Validator(self.parameters).validate(arguments)

    def execute(self, arguments: dict):
        self.validate(arguments)
        return self.handler(arguments)


def _object(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required),
            'additionalProperties': False}


_TEXT = {'type': 'string'}
_READ_PARAMETERS = _object({
    'section': {'type': 'string', 'enum': ['requirements', 'messages', 'ideas', 'plan', 'evidence']},
    'query': _TEXT, 'offset': {'type': 'integer', 'minimum': 0},
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
}, ['section'])
_REQUIREMENTS = {key: _TEXT for key in sorted(PROFILE_FIELDS | BASIC_FIELDS)}
_REQUIREMENTS.update({key: {'type': ['integer', 'null']} for key in ('day_count', 'travelers')})
_REQUIREMENTS.update({key: {**_object({
    'add': {'type': 'array', 'items': _TEXT},
    'remove': {'type': 'array', 'items': _TEXT},
}), 'minProperties': 1} for key in LIST_FIELDS})
_REQUIREMENTS['transport_mode'] = {'type': 'string', 'enum': ['driving', 'transit', 'unspecified']}
_REQUIREMENTS['budget_scope'] = {'type': 'string', 'enum': ['total', 'per_person', 'unspecified']}
_IDEA_FIELDS = {'kind': {'type': 'string', 'enum': ['地点', '预算', '节奏', '禁忌', '待归类']},
                'title': {'type': 'string', 'minLength': 1, 'maxLength': 300},
                'body': {'type': 'string', 'minLength': 1, 'maxLength': 2000}}
_OUTCOMES = {'type': 'array', 'items': _object({
    'idea_ref': _TEXT,
    'status': {'type': 'string', 'enum': ['scheduled', 'conflict', 'needs_input', 'excluded']},
    'reason': {'type': 'string', 'minLength': 1},
}, ['idea_ref', 'status', 'reason'])}
SUBMIT_PARAMETERS = _object({
    'user_quote': {'type': 'string', 'description': '逐字引用本轮完整用户原文，不能摘录、改写或省略。'},
    'requirements_patch': {**_object(_REQUIREMENTS), 'minProperties': 1,
                           'description': '只提交实际变化字段；列表仅用add/remove，不替换未提到的旧项。'},
    'lodging': _object({'poi_id': _TEXT}, ['poi_id']),
    'ideas': {'type': 'array', 'minItems': 1, 'maxItems': 20, 'items': {'oneOf': [
        _object({'action': {'const': 'create'}, **_IDEA_FIELDS}, ['action', 'kind', 'title', 'body']),
        _object({'action': {'const': 'update'}, 'idea_id': _TEXT,
                 'changes': {**_object(_IDEA_FIELDS), 'minProperties': 1}}, ['action', 'idea_id', 'changes']),
        _object({'action': {'const': 'delete'}, 'idea_id': _TEXT}, ['action', 'idea_id']),
    ]}},
    'itinerary': {'oneOf': [
        _object({'action': {'const': 'replace'}, 'text': {'type': 'string', 'minLength': 1},
                 'idea_outcomes': _OUTCOMES}, ['action', 'text', 'idea_outcomes']),
        _object({'action': {'const': 'edit_text'},
                 'operation': {'type': 'string', 'enum': ['append', 'replace']},
                 'old_text': {'type': ['string', 'null']},
                 'context': {'type': ['string', 'null']}, 'new_text': _TEXT},
                ['action', 'operation', 'old_text', 'new_text']),
        _object({'action': {'const': 'confirm'}}, ['action']),
        _object({'action': {'const': 'clear'}}, ['action']),
    ]},
    'presentation': _object({'style_instruction': {'type': 'string', 'minLength': 1}}, ['style_instruction']),
    'next_question': {'type': 'string', 'minLength': 1},
}, ['user_quote'])
SUBMIT_PARAMETERS['anyOf'] = [{'required': [key]} for key in
                             ('requirements_patch', 'lodging', 'ideas', 'itinerary', 'presentation')]


def _route(arguments, *, place_catalog=None, route_cache=None):
    return lookup_route(arguments, place_catalog or {}, route_cache if route_cache is not None else {})


def _weather(arguments, *, place_catalog=None, route_cache=None):
    return query_weather(arguments, cache=route_cache)


def _plain(handler):
    return lambda arguments, **kwargs: handler(arguments)


# 外部工具也从同一个绑定生成模型定义和执行校验。
_EXTERNAL = tuple(ToolSpec(spec['function']['name'], spec['function']['description'],
                           deepcopy(spec['function']['parameters']), handler)
                  for spec, handler in (
                      (WEATHER_TOOL_SPEC, _weather), (FOOD_TOOL_SPEC, _plain(search_food)),
                      (PLACE_TOOL_SPEC, _plain(search_places)), (ROUTE_TOOL_SPEC, _route)))


def tool_specs() -> list[dict]:
    return [item.model_spec() for item in _EXTERNAL]


def run_tool(name: str, arguments: dict, *, place_catalog=None, route_cache=None) -> dict:
    spec = next((item for item in _EXTERNAL if item.name == name), None)
    if spec is None:
        raise ValueError('未知查询工具')
    spec.validate(arguments)
    return spec.handler(arguments, place_catalog=place_catalog, route_cache=route_cache)


def agent_tool_registry(*, read_context, submit_changes=None, query, readonly=False) -> dict[str, ToolSpec]:
    """共享参数合同；只读入口不注册终止写入能力。"""
    specs = [ToolSpec('read_context', '按需读取需求、完整消息、想法、当前行程或服务器查询依据。',
                      _READ_PARAMETERS, read_context)]
    specs.extend(ToolSpec(item.name, item.description, item.parameters,
                          lambda args, name=item.name: query(name, args)) for item in _EXTERNAL)
    if not readonly:
        specs.append(ToolSpec('submit_changes',
            '一次提交本轮授权的全部变更候选并立即结束；校验成功后由服务器原子保存。'
            '至少一项操作，支持独立精确编辑、确认、清空和样式；不在调用后继续回复。',
            SUBMIT_PARAMETERS, submit_changes, terminal=True))
    return {item.name: item for item in specs}


def agent_tool_specs(*, readonly=False) -> tuple[dict, ...]:
    registry = agent_tool_registry(read_context=lambda args: None,
                                   submit_changes=lambda args: None, query=lambda name, args: None,
                                   readonly=readonly)
    return tuple(item.model_spec() for item in registry.values())
