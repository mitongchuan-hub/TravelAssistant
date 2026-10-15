"""Structured semantic review plus independent budget upper-bound arithmetic."""
from decimal import Decimal
from jsonschema import Draft202012Validator

CHECK_LABELS = {
    'requirements': '用户约束完整性',
    'route_evidence': '交通起终点、方向与方式对应',
    'schedule': '时间窗口及活动衔接',
    'lodging': '住宿与寄存条件',
    'meals': '餐次及逐餐费用',
    'budget': '预算分项与可行性条件',
    'facts': '其他事实依据',
    'delivery': '简短概览与自然表达',
}

REVIEW_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'status': {'type': 'string', 'enum': ['pass', 'revise']},
        'issues': {'type': 'array', 'items': {'type': 'string'}},
        'checks': {'type': 'object', 'additionalProperties': False,
                   'properties': {key: {'type': 'boolean'} for key in CHECK_LABELS},
                   'required': list(CHECK_LABELS)},
        'budget_items': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False,
            'properties': {'label': {'type': 'string'},
                           'upper_total_cny': {'type': 'number', 'minimum': 0},
                           'included': {'type': 'boolean'}},
            'required': ['label', 'upper_total_cny', 'included'],
        }},
    },
    'required': ['status', 'issues', 'checks', 'budget_items'],
}


def review_issues(review, trip):
    Draft202012Validator(REVIEW_SCHEMA).validate(review)
    issues = list(review['issues'])
    for key, passed in review['checks'].items():
        if not passed:
            issues.append(CHECK_LABELS[key] + '尚未通过复核。')
    if review['status'] == 'revise' and not issues:
        issues.append('交付复核未通过，请重新核对需求和查询依据。')
    included = [Decimal(str(item['upper_total_cny'])) for item in review['budget_items'] if item['included']]
    if not all(value.is_finite() for value in included):
        raise ValueError('预算复核金额必须为有限数值')
    if trip and trip.requirements.budget_amount:
        profile = trip.requirements
        maximum = Decimal(profile.budget_amount)
        if profile.budget_scope == 'per_person':
            maximum *= profile.travelers
        total = sum(included, Decimal(0))
        if total > maximum:
            issues.append(f'分项上界按全程全部人数合计为{total}元，超过有效总预算{maximum}元；请重新分配互不重叠的上限。')
    return issues
