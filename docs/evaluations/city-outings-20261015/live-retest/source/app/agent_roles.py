"""Model nodes and three business agents sharing one bounded tool loop."""
from copy import deepcopy
from dataclasses import dataclass
import os

from .prompts import load_prompt
from .tools import tool_specs
from .requirements import PROFILE_FIELDS, BASIC_FIELDS, LIST_FIELDS


def tool(name, description, properties, required=()):
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties, 'required': list(required),
                       'additionalProperties': False}}}


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
    'start_date': '本轮明确的抵达日期，YYYY-MM-DD；有明确日期不能只填day_count。',
    'end_date': '本轮明确的离开日期，YYYY-MM-DD；跨月按原话补全月份与已明确年份，不改变日期。',
    'arrival_time': '抵达旅游城市的当地时刻，HH:MM。',
    'departure_time': '离开旅游城市的当地时刻，HH:MM。',
    'arrival_location': '本轮明确的抵达车站、机场或地点原名，不代表已核实。',
    'departure_location': '本轮明确的离开车站、机场或地点原名。',
    'budget_coverage': '预算包含和排除的费用范围，保留原话的限定；沿用时不覆盖。',
    'pace': '本轮明确的节奏偏好，不替代抵离日期和时间。',
}.items():
    changes[key]['description'] = description
changes['travelers']['description'] = '本轮明确的旅行总人数，包含已明确同行成人/儿童/老人；不能遗漏。'
changes['day_count']['description'] = '仅天数表达；已有起止日期时按日期计算，不能代替start_date/end_date。'
changes['budget_amount'] = {'type': 'string',
    'description': '纯数字金额文本，不含单位；未提供且有效需求也未知时为空字符串。金额有效性由本地统一校验。'}
changes['budget_scope'] = {'type': 'string', 'enum': ['total', 'per_person', 'unspecified'],
    'description': '总额/两人合计用total，每人/人均用per_person，未明确才unspecified；与budget_amount分别填写。'}
UPDATE_REQUIREMENTS = tool('update_requirements', '在本轮快照中整理用户明确的新需求。changes完整包含五组标量：城市、起止日期、抵离时间地点、人数、预算金额口径费用范围。未改变的值沿用，未知文本为空字符串、人数为null。明确的列表、pace节奏和note备注也须提交，不能只在回复中采用；列表仅用add/remove。user_quote是与changes并列的顶层参数，逐字引用完整最新原话。工具ready的最新结果优先初始输入，除纠正遗漏或错误外不重复提交相同需求。', {
    'user_quote': {'type': 'string', 'description': '顶层参数，与changes并列，值是最后一条用户消息的完整原文，不是上下文JSON。'},
    'changes': {'type': 'object', 'properties': changes, 'additionalProperties': False,
                'required': ['city', 'start_date', 'end_date', 'arrival_time', 'departure_time',
                             'arrival_location', 'departure_location', 'travelers',
                             'budget_amount', 'budget_scope', 'budget_coverage']},
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
INTENT_SCHEMA = {'type': 'object', 'additionalProperties': False,
    'properties': {
        'intent': {'type': 'string', 'enum': ['chat', 'query', 'planning', 'revision']},
        'action': {'type': 'string', 'enum': ['reply', 'update_requirements', 'update_and_query', 'generate', 'modify', 'explain', 'confirm', 'cancel']},
        'scope': {'type': 'string', 'enum': ['', 'item', 'all', 'explain']},
        'user_quote': {'type': 'string'},
        'question': {'type': ['string', 'null']},
    }, 'required': ['intent', 'action', 'scope', 'user_quote', 'question']}


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
    if not value and role.name in {'chat', 'intent'}:
        value = os.getenv('OPENAI_REASONING_EFFORT_MAIN', '').strip().lower()
    value = value or os.getenv('OPENAI_REASONING_EFFORT', '').strip().lower()
    value = value or ('low' if role.name == 'revision' and scope == 'explain' else role.reasoning_effort)
    if value not in REASONING_EFFORTS:
        raise ValueError(f'Invalid reasoning effort for {role.name}: {value}')
    return None if value == 'default' else value


def roles(mode='travel'):
    city = mode == 'city'
    rules = load_prompt('agents/city_rules.txt' if city else 'agents/travel_rules.txt')
    read = (READ_CONTEXT,)
    facts = tuple(tool_specs())
    update = UPDATE_REQUIREMENTS
    if city:
        update = deepcopy(UPDATE_REQUIREMENTS)
        function = update['function']
        function['description'] = ('保存用户明确的新游玩设置，未知可选字段保持未知，不追问抵离车站、人数或预算。'
            '完整回传city、lodging_address、start_date/end_date、day_count、outing_start_time/outing_end_time、'
            'travelers、budget_amount/budget_scope/budget_coverage；沿用未改值。列表只用add/remove。'
            'user_quote逐字引用完整最新原话。更换住宿地址后须在游玩设置重新确认地图位置。')
        function['parameters']['properties']['changes']['required'] = [
            'city', 'lodging_address', 'start_date', 'end_date', 'day_count', 'outing_start_time', 'outing_end_time',
            'travelers', 'budget_amount', 'budget_scope', 'budget_coverage']
        properties = function['parameters']['properties']['changes']['properties']
        for key in ('arrival_time', 'departure_time', 'arrival_location', 'departure_location', 'date_range', 'style'):
            properties.pop(key, None)
        properties['start_date']['description'] = '可选的游玩开始日期，未知为空；不是到达城市日期。'
        properties['end_date']['description'] = '可选的游玩结束日期，未知为空；不是离开城市日期。'
        properties['lodging_address']['description'] = '用户提供的住宿地址；不是车站，不默认更换住宿。'
        properties['outing_start_time']['description'] = '每天从住处出门的时刻，未知为空。'
        properties['outing_end_time']['description'] = '每天返回住处的时刻，未知为空。'
    requirements = (update, CHECK_REQUIREMENTS)
    return {
        'main': AgentRole('main', load_prompt('agents/main.txt') + ('\n当前为同城游玩：住宿是唯一起点，日期、人数和预算均可选。不要因这些可选信息未知而澄清或阻止明确的生成请求。' if city else ''), response_format={
            'type': 'json_schema', 'json_schema': {'name': 'travel_intent', 'strict': True, 'schema': INTENT_SCHEMA}}),
        'chat': AgentRole('chat', '你是旅行助手。用简体中文自然回应闲聊，也可回答当前已保存旅行需求的问题。requirements是当前保存状态，包含requirements字段和trip备注；它优先于conversation中的旧值、欢迎文案或缺项提示。已填写的信息直接使用，不重复索要；必去地点、忌口和节奏等可选偏好不作为回复前提。字段未记录时如实说明，不猜未知。没有工具，不声称查询、更新需求或保存行程；涉及新需求或生成、修改、外部资料查询的请求应由对应业务流程处理。'),
        'research': AgentRole('research', load_prompt('agents/research.txt') + '\n\n' + rules, read + facts, max_steps=6),
        'planning': AgentRole('planning', load_prompt('agents/city_planning.txt' if city else 'agents/planning.txt') + '\n\n' + rules
            + '\n\n' + load_prompt('agents/city_examples.txt' if city else 'agents/planning_examples.txt'), read + requirements + facts, max_steps=10, reasoning_effort='medium'),
        'revision': AgentRole('revision', load_prompt('agents/city_revision.txt' if city else 'agents/revision.txt') + '\n\n' + rules,
            read + requirements + (EDIT_PLAN_TEXT,) + facts, max_steps=10, reasoning_effort='medium'),
        'display': AgentRole('display', load_prompt('agents/display.txt')),
    }
