"""Model nodes and four business agents sharing one bounded tool loop."""
from copy import deepcopy
from dataclasses import dataclass
import os

from app.prompts import load_prompt
from app.tools import tool_specs
from app.services.requirements import PROFILE_FIELDS, BASIC_FIELDS, LIST_FIELDS


def tool(name, description, properties, required=()):
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties, 'required': list(required),
                       'additionalProperties': False}}}


def strict_tool(name, description, properties, required=()):
    """A provider hint plus a schema that is still validated locally before use."""
    spec = tool(name, description, properties, required)
    spec['function']['strict'] = True
    return spec


READ_CONTEXT = tool('read_context', '读取本旅行需求、聊天、想法、当前行程或真实查询依据。', {
    'section': {'type': 'string', 'enum': ['requirements', 'messages', 'ideas', 'plan', 'evidence']},
    'query': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0},
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
}, ['section'])

changes = {key: {'type': 'string'} for key in sorted((PROFILE_FIELDS | BASIC_FIELDS) - LIST_FIELDS - {'travelers', 'day_count'})}
changes.update({key: {'type': ['integer', 'null']} for key in ('travelers', 'day_count')})
changes.update({key: {'type': 'object', 'additionalProperties': False, 'properties': {
    'add': {'type': 'array', 'items': {'type': 'string'}},
    'remove': {'type': 'array', 'items': {'type': 'string'}}}} for key in sorted(LIST_FIELDS)})
for key, description in {
    'city': '本轮明确的旅游目的地城市。',
    'start_date': '必填的游玩开始日期，YYYY-MM-DD。',
    'end_date': '游玩结束日期，YYYY-MM-DD，由开始日期和天数确定。',
    'budget_coverage': '预算包含和排除的费用范围，保留原话的限定；沿用时不覆盖。',
    'pace': '本轮明确的游玩节奏偏好。',
}.items():
    changes[key]['description'] = description
changes['travelers']['description'] = '本轮明确的旅行总人数，包含已明确同行成人/儿童/老人；不能遗漏。'
changes['day_count']['description'] = '仅天数表达；已有起止日期时按日期计算，不能代替start_date/end_date。'
changes['budget_amount'] = {'type': 'string',
    'description': '纯数字金额文本，不含单位；未提供且有效需求也未知时为空字符串。金额有效性由本地统一校验。'}
changes['budget_scope'] = {'type': 'string', 'enum': ['total', 'per_person', 'unspecified'],
    'description': '总额/两人合计用total，每人/人均用per_person，未明确才unspecified；与budget_amount分别填写。'}
changes['transport_mode'] = {'type': 'string', 'enum': ['driving', 'transit', 'unspecified'],
    'description': '市内交通方式：用户明确自驾/开车用driving，公交地铁/公共交通用transit，未明确才unspecified。'}
UPDATE_REQUIREMENTS = tool('update_requirements', '在本轮快照中整理用户明确的新游玩设置。未改变的值沿用，未知文本为空字符串、人数为null。明确的列表、pace节奏和note备注也须提交；列表仅用add/remove。user_quote逐字引用完整最新原话。', {
    'user_quote': {'type': 'string', 'description': '顶层参数，与changes并列，值是最后一条用户消息的完整原文，不是上下文JSON。'},
    'changes': {'type': 'object', 'properties': changes, 'additionalProperties': False,
                'required': ['city', 'lodging_address', 'start_date', 'end_date', 'day_count',
                             'outing_start_time', 'outing_end_time', 'travelers',
                             'budget_amount', 'budget_scope', 'budget_coverage', 'transport_mode']},
    'idea_cards': {'type': 'array', 'items': {'type': 'object', 'properties': {
        'kind': {'type': 'string'}, 'title': {'type': 'string'}, 'body': {'type': 'string'},
        'status': {'type': 'string'}}, 'required': ['kind', 'title', 'body'], 'additionalProperties': False}},
}, ['user_quote', 'changes'])
CHECK_REQUIREMENTS = tool('check_requirements', '审阅完整最新原话后，检查当前有效需求和日期窗口。生成且本轮尚未检查时调用；requirements_checked=true或本轮update_requirements已返回ready且含有效travel_window时复用检查，不重复调用。生成缺少必填则结束本轮并提出补充问题；只记录、查询和已有行程局部修改不因此要求全部规划字段齐全，不自动生成。', {
    'user_quote': {'type': 'string'},
}, ['user_quote'])
EDIT_PLAN_TEXT = tool('edit_plan_text', '执行用户明确指定文字的精确编辑，保留其他原文。append在全文末尾追加一行new_text，程序只补必需的一个换行；new_text须为单行。replace替换唯一匹配的old_text（重名须提供足够上下文）。new_text须逐字来自最新用户原话，不能生成或润色。先check_requirements。', {
    'user_quote': {'type': 'string'},
    'operation': {'type': 'string', 'enum': ['append', 'replace']},
    'old_text': {'type': ['string', 'null']},
    'context': {'type': ['string', 'null'], 'description': '替换文字重复时，提供在当前原文中唯一出现且只含一次old_text的上下文；上下文其他文字保持不变。'},
    'new_text': {'type': 'string'},
}, ['user_quote', 'operation', 'old_text', 'new_text'])


# Execution tool schemas are copied
# into each request in deterministic order and validated again by TaskExecutionRuntime.
EXECUTION_READ_CONTEXT = strict_tool('read_context', '按需读取当前旅行的需求、任务、想法或证据；section=plan 返回当前行程完整正文，可核实某个想法是否已安排。', {
    'section': {'type': 'string', 'enum': ['requirements', 'messages', 'ideas', 'plan', 'evidence', 'tasks']},
    'query': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0},
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
}, ['section'])
EXECUTION_MANAGE_TASK = strict_tool('manage_task', '跟踪复杂事项、状态和跨轮追问；完成取消或挂起须显式采用候选。', {
    'operation': {'type': 'string', 'enum': ['create', 'complete', 'suspend', 'resume', 'cancel', 'adopt']},
    'task_ref': {'type': ['string', 'null']}, 'goal': {'type': 'string'},
    'effect_kind': {'type': 'string', 'enum': ['read', 'requirements', 'itinerary', 'confirmation', 'mixed']},
    'item': {'type': 'string'}, 'effect_refs': {'type': 'array', 'items': {'type': 'string'}},
    'unresolved_items': {'type': 'array', 'items': {'type': 'string'}},
}, ['operation', 'effect_kind'])
EXECUTION_MANAGE_TODOS = strict_tool('manage_todos', '维护本轮多事项清单；add新增待处理项，update按返回的id更新，不删除其他项。非pending状态必须说明结果或原因；done仅表示本轮处理完毕，不代表候选已保存。', {
    'operation': {'type': 'string', 'enum': ['add', 'update']},
    'items': {'type': 'array', 'minItems': 1, 'maxItems': 20, 'items': {
        'type': 'object', 'additionalProperties': False,
        'properties': {
            'id': {'type': ['string', 'null']},
            'title': {'type': 'string', 'minLength': 1, 'maxLength': 200},
            'status': {'type': 'string', 'enum': ['pending', 'done', 'needs_input', 'unresolved']},
            'reason': {'type': 'string', 'maxLength': 1000},
        }, 'required': ['id', 'title', 'status', 'reason'],
    }},
}, ['operation', 'items'])
EXECUTION_PREPARE_REQUIREMENTS = strict_tool('prepare_requirements', '提出正式需求候选；活动追问答案仍只按 answer_target 使用。', {
    'task_ref': {'type': ['string', 'null']}, 'message_ref': {'type': 'string'},
    'answer_question_ref': {'type': ['string', 'null']},
    'effect': {'type': 'string', 'enum': ['independent', 'conditional', 'hypothetical']},
    'condition': {'type': 'string', 'enum': ['satisfied', 'not_satisfied', 'unknown']},
    'condition_evidence_refs': {'type': 'array', 'items': {'type': 'string'}},
    'condition_predicate': {'anyOf': [
        {'type': 'null'},
        {'type': 'object', 'properties': {
            'kind': {'type': 'string', 'enum': ['weather_precipitation_at_least']},
            'city': {'type': 'string'}, 'date': {'type': 'string'},
            'threshold': {'type': 'integer', 'minimum': 0, 'maximum': 100},
        }, 'required': ['kind', 'city', 'date', 'threshold'], 'additionalProperties': False},
    ]},
}, ['message_ref', 'effect', 'condition'])
EXECUTION_RESEARCH = strict_tool('research', '查询具体问题，返回可支持下一步的证据；不写正式需求或行程。', {
    'task_ref': {'type': ['string', 'null']}, 'question': {'type': 'string'},
    'answer_question_ref': {'type': ['string', 'null']},
    'context_refs': {'type': 'array', 'items': {'type': 'string'}},
    'hypothetical': {'type': 'boolean'},
    'evidence_kind': {'type': 'string', 'enum': ['weather', 'place', 'food', 'route', 'other']},
}, ['question', 'hypothetical', 'evidence_kind'])
EXECUTION_DRAFT_ITINERARY = strict_tool('draft_itinerary', '生成完整文字行程候选；使用有效需求引用和条件证据。', {
    'task_ref': {'type': ['string', 'null']}, 'requirements_ref': {'type': ['string', 'null']},
    'constraints': {'type': 'string'},
    'effect': {'type': 'string', 'enum': ['independent', 'conditional', 'hypothetical']},
    'condition': {'type': 'string', 'enum': ['satisfied', 'not_satisfied', 'unknown']},
    'condition_predicate': {'anyOf': [
        {'type': 'null'},
        {'type': 'object', 'properties': {
            'kind': {'type': 'string', 'enum': ['weather_precipitation_at_least']},
            'city': {'type': 'string'}, 'date': {'type': 'string'},
            'threshold': {'type': 'integer', 'minimum': 0, 'maximum': 100},
        }, 'required': ['kind', 'city', 'date', 'threshold'], 'additionalProperties': False},
    ]},
    'condition_evidence_refs': {'type': 'array', 'items': {'type': 'string'}},
}, [])
EXECUTION_REVISE_ITINERARY = strict_tool('revise_itinerary', '按当前行程引用和授权范围生成修改候选；支持精确编辑和条件证据。', {
    'task_ref': {'type': ['string', 'null']}, 'plan_ref': {'type': 'string'},
    'requirements_ref': {'type': ['string', 'null']},
    'scope': {'type': 'string', 'enum': ['item', 'all']}, 'instruction': {'type': 'string'},
    'target_item_id': {'type': ['string', 'null']},
    'effect': {'type': 'string', 'enum': ['independent', 'conditional', 'hypothetical']},
    'condition': {'type': 'string', 'enum': ['satisfied', 'not_satisfied', 'unknown']},
    'condition_predicate': {'anyOf': [
        {'type': 'null'},
        {'type': 'object', 'properties': {
            'kind': {'type': 'string', 'enum': ['weather_precipitation_at_least']},
            'city': {'type': 'string'}, 'date': {'type': 'string'},
            'threshold': {'type': 'integer', 'minimum': 0, 'maximum': 100},
        }, 'required': ['kind', 'city', 'date', 'threshold'], 'additionalProperties': False},
    ]},
    'condition_evidence_refs': {'type': 'array', 'items': {'type': 'string'}},
}, ['plan_ref', 'scope', 'instruction', 'effect', 'condition', 'condition_predicate'])
EXECUTION_CONFIRM_ITINERARY = strict_tool('confirm_itinerary', '确认用户已看到且依据未变的当前行程。', {
    'plan_ref': {'type': 'string'},
}, ['plan_ref'])
EXECUTION_ASK_USER = strict_tool('ask_user', '提出一个关键问题；可同时采用不依赖答案的候选。', {
    'task_ref': {'type': ['string', 'null']}, 'question': {'type': 'string'},
    'answer_target': {'type': 'object', 'properties': {
        'namespace': {'type': 'string'}, 'field': {'type': 'string'},
    }, 'required': ['namespace', 'field'], 'additionalProperties': False},
    'adopt_refs': {'type': 'array', 'items': {'type': 'string'}},
    'completed_items': {'type': 'array', 'items': {'type': 'string'}},
    'unresolved_items': {'type': 'array', 'items': {'type': 'string'}},
}, ['question', 'answer_target'])
EXECUTION_FINISH_TURN = strict_tool('finish_turn', '结构化结束本轮，只采用显式候选引用。', {
    'reply': {'type': 'string'},
    'adopt_refs': {'type': 'array', 'items': {'type': 'string'}},
    'completed_items': {'type': 'array', 'items': {'type': 'string'}},
    'unresolved_items': {'type': 'array', 'items': {'type': 'string'}},
}, ['reply'])
COMPLETE_CAPABILITY = strict_tool('complete_capability', '专业能力的结构化终止合同，返回候选摘要或结构化缺项。', {
    'status': {'type': 'string', 'enum': ['ok', 'needs_input', 'needs_requirements', 'conflict', 'unavailable', 'failed']},
    'summary': {'type': 'string'},
    'artifact_text': {'type': ['string', 'null']},
    'question': {'type': ['string', 'null']},
    'answer_target': {'anyOf': [
        {'type': 'null'},
        {'type': 'object', 'properties': {
            'namespace': {'type': 'string'}, 'field': {'type': 'string'},
        }, 'required': ['namespace', 'field'], 'additionalProperties': False},
    ]},
    'completed_items': {'type': 'array', 'items': {'type': 'string'}},
    'unresolved_items': {'type': 'array', 'items': {'type': 'string'}},
    'idea_outcomes': {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False,
        'properties': {
            'idea_ref': {'type': 'string'},
            'status': {'type': 'string', 'enum': ['scheduled', 'conflict', 'needs_input', 'excluded']},
            'reason': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
        }, 'required': ['idea_ref', 'status', 'reason'],
    }, 'description': '完整规划成功时逐项覆盖全部想法引用；scheduled的reason逐字摘录新行程中的对应安排，其他状态说明具体冲突、缺项或不适用原因。'},
}, ['status', 'summary', 'artifact_text', 'question', 'answer_target', 'completed_items', 'unresolved_items'])
EXECUTION_TOOLS = (EXECUTION_READ_CONTEXT, EXECUTION_MANAGE_TASK, EXECUTION_MANAGE_TODOS, EXECUTION_PREPARE_REQUIREMENTS,
                   EXECUTION_RESEARCH, EXECUTION_DRAFT_ITINERARY,
                   EXECUTION_REVISE_ITINERARY, EXECUTION_CONFIRM_ITINERARY, EXECUTION_ASK_USER,
                   EXECUTION_FINISH_TURN)


@dataclass(frozen=True)
class AgentRole:
    name: str
    prompt: str
    tools: tuple = ()
    response_format: dict | None = None
    max_steps: int = 8
    reasoning_effort: str = 'low'


REASONING_EFFORTS = {'default', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}


def role_reasoning_effort(role, scope=''):
    value = os.getenv(f'OPENAI_REASONING_EFFORT_{role.name.upper()}', '').strip().lower()
    if not value and role.name.endswith('_capability'):
        base = role.name.removesuffix('_capability')
        value = os.getenv(f'OPENAI_REASONING_EFFORT_{base.upper()}', '').strip().lower()
    if not value and role.name == 'main_execution':
        value = os.getenv('OPENAI_REASONING_EFFORT_MAIN', '').strip().lower()
    value = value or os.getenv('OPENAI_REASONING_EFFORT', '').strip().lower()
    value = value or ('low' if role.name == 'revision' and scope == 'explain' else role.reasoning_effort)
    if value not in REASONING_EFFORTS:
        raise ValueError(f'Invalid reasoning effort for {role.name}: {value}')
    return None if value == 'default' else value


def _city_requirements_tools():
    update = deepcopy(UPDATE_REQUIREMENTS)
    function = update['function']
    function['description'] = ('保存用户明确的新游玩设置，未知可选字段保持未知，不追问抵离车站、人数或预算。'
        '完整回传city、lodging_address、start_date/end_date、day_count、outing_start_time/outing_end_time、'
        'travelers、budget_amount/budget_scope/budget_coverage、transport_mode；沿用未改值。列表只用add/remove。'
        'user_quote逐字引用完整最新原话。更换住宿地址后须在游玩设置重新确认地图位置。')
    function['parameters']['properties']['changes']['required'] = [
        'city', 'lodging_address', 'start_date', 'end_date', 'day_count', 'outing_start_time', 'outing_end_time',
        'travelers', 'budget_amount', 'budget_scope', 'budget_coverage', 'transport_mode']
    properties = function['parameters']['properties']['changes']['properties']
    for key in ('arrival_time', 'departure_time', 'arrival_location', 'departure_location', 'date_range', 'style'):
        properties.pop(key, None)
    properties['start_date']['description'] = '必填的游玩开始日期，缺少时待补充；不是到达城市日期。'
    properties['end_date']['description'] = '游玩结束日期，由开始日期和天数确定；不是离开城市日期。'
    properties['lodging_address']['description'] = '用户提供的住宿地址；不是车站，不默认更换住宿。'
    properties['outing_start_time']['description'] = '每天从住处出门的时刻，未知为空。'
    properties['outing_end_time']['description'] = '每天返回住处的时刻，未知为空。'
    return update, CHECK_REQUIREMENTS

def execution_roles(mode='city'):
    """The single task_execution framework's role bundle."""
    if mode != 'city':
        raise ValueError('Unsupported trip mode')
    requirements = _city_requirements_tools()
    return {
        'main_execution': AgentRole('main_execution', load_prompt('agents/main.txt'),
                                    EXECUTION_TOOLS, max_steps=12, reasoning_effort='low'),
        'requirements_capability': AgentRole('requirements_capability',
            load_prompt('agents/requirements.txt'), (READ_CONTEXT,) + requirements + (COMPLETE_CAPABILITY,),
            max_steps=6),
        'research_capability': AgentRole('research_capability',
            load_prompt('agents/research.txt'),
            (READ_CONTEXT,) + tuple(tool_specs()) + (COMPLETE_CAPABILITY,), max_steps=6),
        'planning_capability': AgentRole('planning_capability',
            load_prompt('agents/planning.txt'),
            (READ_CONTEXT, requirements[1]) + tuple(tool_specs()) + (COMPLETE_CAPABILITY,),
            max_steps=10, reasoning_effort='medium'),
        'revision_capability': AgentRole('revision_capability',
            load_prompt('agents/revision.txt'),
            (READ_CONTEXT, requirements[1], EDIT_PLAN_TEXT) + tuple(tool_specs()) + (COMPLETE_CAPABILITY,),
            max_steps=10, reasoning_effort='medium'),
    }


def roles(mode='city'):
    """Tool validation views and the display node; reuse current capability prompts."""
    configs = execution_roles(mode)
    read = (READ_CONTEXT,)
    facts = tuple(tool_specs())
    requirements = _city_requirements_tools()
    return {
        'requirements': AgentRole('requirements', configs['requirements_capability'].prompt, read + requirements, max_steps=6),
        'research': AgentRole('research', configs['research_capability'].prompt, read + facts, max_steps=6),
        'planning': AgentRole('planning', configs['planning_capability'].prompt, read + requirements + facts, max_steps=10, reasoning_effort='medium'),
        'revision': AgentRole('revision', configs['revision_capability'].prompt,
            read + requirements + (EDIT_PLAN_TEXT,) + facts, max_steps=10, reasoning_effort='medium'),
        'display': AgentRole('display', load_prompt('agents/display.txt')),
    }
