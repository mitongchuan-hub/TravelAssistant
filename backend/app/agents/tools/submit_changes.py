"""Agent terminal tool: validate one complete change package without saving it."""
from copy import deepcopy

from app.domain.changes import prepare_changes
from app.application.contracts import ExecutionResult


def _object(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required),
            'additionalProperties': False}


_TEXT = {'type': 'string'}
SUBMIT_PARAMETERS = _object({
    'user_quote': {'type': 'string', 'description': '逐字引用本轮完整用户原文，不能摘录、改写或省略。'},
    'requirements_text': {'type': 'string', 'description': '更新后的完整需求正文；保留无关旧要求，空串表示明确清空。'},
    'itinerary': {'oneOf': [
        _object({'action': {'const': 'replace'}, 'text': {'type': 'string', 'minLength': 1}}, ['action', 'text']),
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
                             ('requirements_text', 'itinerary')]


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
