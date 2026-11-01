"""Agent terminal tool: validate one complete change package without saving it."""
from copy import deepcopy

from app.domain.requirements import BASIC_FIELDS, LIST_FIELDS, PROFILE_FIELDS
from app.domain.changes import prepare_changes
from app.application.contracts import ExecutionResult


def _object(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required),
            'additionalProperties': False}


_TEXT = {'type': 'string'}
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
    'next_question': {'type': 'string', 'minLength': 1},
}, ['user_quote'])
SUBMIT_PARAMETERS['anyOf'] = [{'required': [key]} for key in
                             ('requirements_patch', 'lodging', 'ideas', 'itinerary')]


SUBMIT_CHANGES_TOOL_SPEC = {
    'type': 'function',
    'function': {
        'name': 'submit_changes',
        'description': (
            '一次提交本轮授权的全部变更候选并立即结束；校验成功后由服务器原子保存。'
            '至少一项操作，支持独立精确编辑、确认和清空；不在调用后继续回复。'),
        'parameters': SUBMIT_PARAMETERS,
    },
}


def submit_changes(arguments, *, request_context, execution_context, evidence, budget):
    """Use server query records only; persistence remains in the commit workflow."""
    budget.remaining()
    payload = prepare_changes(arguments, request_context, execution_context, deepcopy(evidence))
    budget.remaining()
    return ExecutionResult(changes=payload)
