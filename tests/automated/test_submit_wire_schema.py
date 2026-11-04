"""Wire compatibility must not weaken local save validation."""
import pytest
from jsonschema import ValidationError

from app.agents.tools.registry import ToolSpec, agent_tool_registry, agent_tool_specs


def save_registry(handler=lambda args: args):
    return agent_tool_registry(read_context=lambda args: {}, save_trip_changes=handler,
                               query=lambda name, args: {})


def test_registered_save_is_the_only_terminal_tool_and_has_no_old_alias():
    registry = save_registry()
    assert len(registry) == 10
    assert 'submit_changes' not in registry
    assert [name for name, spec in registry.items() if spec.terminal] == ['save_trip_changes']
    spec = registry['save_trip_changes']
    assert spec.name == spec.model_spec()['function']['name'] == 'save_trip_changes'
    assert spec.description == (
        '保存用户明确要求的旅行需求或行程修改，支持记录、修改、删除需求，以及生成、编辑、确认、清空行程。'
        '一次提交本轮全部获授权的修改并结束本轮，最终保存结果以服务器回执为准。仅查询或讨论时不调用。')
    assert [item['function']['name'] for item in agent_tool_specs()] == list(registry)


def test_save_wire_omits_root_anyof_without_mutating_validation_schema():
    spec = save_registry()['save_trip_changes']
    wire = spec.model_spec()['function']['parameters']
    assert 'anyOf' not in wire
    assert 'anyOf' in spec.parameters
    assert wire['required'] == ['user_quote']
    assert wire['properties'] == spec.parameters['properties']
    assert wire['additionalProperties'] is False
    with pytest.raises(ValidationError):
        spec.execute({})
    with pytest.raises(ValidationError):
        spec.execute({'user_quote': '记下来'})
    assert spec.execute({'user_quote': '清空需求正文', 'requirements_text': ''})['requirements_text'] == ''
    wire['properties']['user_quote']['type'] = 'integer'
    assert spec.parameters['properties']['user_quote']['type'] == 'string'


def test_other_tools_keep_their_root_combinators():
    schema = {'type': 'object', 'anyOf': [{'required': ['a']}, {'required': ['b']}],
              'properties': {'a': {'type': 'string'}, 'b': {'type': 'string'}}}
    spec = ToolSpec('another_tool', '', schema, lambda args: args)
    assert spec.model_spec()['function']['parameters'] == schema
