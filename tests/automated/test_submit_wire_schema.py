"""Wire compatibility must not weaken local submit validation."""
import pytest
from jsonschema import ValidationError

from app.agents.tools.registry import ToolSpec
from app.agents.tools.submit_changes import SUBMIT_PARAMETERS


def test_submit_wire_omits_root_anyof_without_mutating_validation_schema():
    spec = ToolSpec('submit_changes', '提交至少一项实际变更', SUBMIT_PARAMETERS, lambda args: args, terminal=True)
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
