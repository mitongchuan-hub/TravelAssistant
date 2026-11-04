"""Agent terminal tool: validate one complete change package without saving it."""
from copy import deepcopy

from app.domain.changes import prepare_changes
from app.application.contracts import ExecutionResult


def _object(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required),
            'additionalProperties': False}


SAVE_TRIP_CHANGES_PARAMETERS = _object({
    'user_quote': {'type': 'string', 'description': '逐字引用本轮完整用户原文（包括不受理部分），用于服务器授权核对；不能摘录、改写或省略。'},
    'requirements_text': {'type': 'string', 'description': '明确记录、新增、修改或删除需求时，提供更新后的完整需求正文，不是增量；保留本轮未授权更改的旧要求，仅明确清空时传空串。不修改固定创建资料。'},
    'itinerary': {'description': '仅在明确授权生成、修改、确认或清空行程时使用；只查询、解释或澄清时省略。', 'oneOf': [
        _object({'action': {'const': 'replace', 'description': '明确授权生成或普通修改行程时，用完整正文替换。'},
                 'text': {'type': 'string', 'minLength': 1, 'description': '生成或修改后的完整中文Markdown行程正文，不是局部增量。'}}, ['action', 'text']),
        _object({'action': {'const': 'edit_text', 'description': '仅按用户给出的原文精确追加或替换，不生成或润色其他文字。'},
                 'operation': {'type': 'string', 'enum': ['append', 'replace'], 'description': 'append追加明确给出的单行文字；replace替换唯一匹配的原文。'},
                 'old_text': {'type': ['string', 'null'], 'description': 'replace时为当前行程中待替换的原文；append时为空。'},
                 'context': {'type': ['string', 'null'], 'description': '原文重名时提供唯一定位上下文，且其中仅含一次old_text；无需消歧时省略。'},
                 'new_text': {'type': 'string', 'description': '逐字取自本轮用户原话的追加或替换文字，不自行润色。'}},
                ['action', 'operation', 'old_text', 'new_text']),
        _object({'action': {'const': 'confirm', 'description': '用户明确确认其已看到的当前方案时使用。'}}, ['action']),
        _object({'action': {'const': 'clear', 'description': '仅明确独立清空整个行程时使用；取消某个景点不等于清空。'}}, ['action']),
    ]},
    'next_question': {'type': 'string', 'minLength': 1, 'description': '随实际变更附带的一个具体追问；只需澄清时直接回复，不为提问提交空操作。'},
}, ['user_quote'])
SAVE_TRIP_CHANGES_PARAMETERS['anyOf'] = [{'required': [key]} for key in
                             ('requirements_text', 'itinerary')]


SAVE_TRIP_CHANGES_TOOL_SPEC = {
    'type': 'function',
    'function': {
        'name': 'save_trip_changes',
        'description': (
            '保存用户明确要求的旅行需求或行程修改，支持记录、修改、删除需求，以及生成、编辑、确认、清空行程。'
            '一次提交本轮全部获授权的修改并结束本轮，最终保存结果以服务器回执为准。仅查询或讨论时不调用。'),
        'parameters': SAVE_TRIP_CHANGES_PARAMETERS,
    },
}


def save_trip_changes(arguments, *, request_context, execution_context, evidence, budget):
    """Use server query records only; persistence remains in the commit workflow."""
    budget.remaining()
    payload = prepare_changes(arguments, request_context, execution_context, deepcopy(evidence))
    budget.remaining()
    return ExecutionResult(changes=payload)
