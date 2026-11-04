"""Read application-owned planning methods through fixed resource identifiers."""
from app.agents.skills import read_skill_resource

READ_SKILL_TOOL_SPEC = {
    'type': 'function',
    'function': {
        'name': 'read_skill',
        'description': '按目录读取本地规划方法；生成或实质修改行程时按需加载，不查询现实事实、不授权保存。',
        'parameters': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string', 'enum': ['travel-planning']},
                'resource': {'type': 'string', 'enum': ['main', 'evidence-and-routing', 'output-and-revision'],
                             'default': 'main'},
            },
            'required': ['name'],
            'additionalProperties': False,
        },
    },
}


def read_skill(arguments):
    return read_skill_resource(arguments['name'], arguments.get('resource', 'main'))
