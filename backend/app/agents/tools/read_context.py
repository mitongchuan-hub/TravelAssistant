"""Agent tool for reading the current turn's authorized context snapshot."""
from app.agents.inputs.context import project_main_read_context


READ_CONTEXT_TOOL_SPEC = {
    'type': 'function',
    'function': {
        'name': 'read_context',
        'description': '按需读取需求、完整消息、想法、当前行程或服务器查询依据。',
        'parameters': {
            'type': 'object',
            'properties': {
                'section': {'type': 'string', 'enum': ['requirements', 'messages', 'ideas', 'plan', 'evidence']},
                'query': {'type': 'string'},
                'offset': {'type': 'integer', 'minimum': 0},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
            },
            'required': ['section'],
            'additionalProperties': False,
        },
    },
}


def read_context(arguments, *, request_context, execution_context, trip, plan, evidence):
    """Read snapshots only; never grant access to the Store or another trip."""
    section = arguments['section']
    raw = {
        'requirements': {'items': execution_context.idea_cards},
        'messages': execution_context.messages,
        'ideas': execution_context.idea_cards,
        'evidence': list(getattr(plan, 'evidence', []) or []) + evidence,
    }.get(section, {})
    return project_main_read_context(
        section, raw, trip=trip, plan=plan,
        expected_basis=request_context.plan_basis,
        current_message_id=request_context.message_id, request_args=arguments)
