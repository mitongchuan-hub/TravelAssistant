"""Structured semantic review plus independent budget upper-bound arithmetic."""
from decimal import Decimal
from jsonschema import Draft202012Validator

REVIEW_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'status': {'type': 'string', 'enum': ['pass', 'revise']},
        'issues': {'type': 'array', 'items': {'type': 'string'}},
        'budget_items': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False,
            'properties': {'label': {'type': 'string'},
                           'upper_total_cny': {'type': 'number', 'minimum': 0},
                           'included': {'type': 'boolean'}},
            'required': ['label', 'upper_total_cny', 'included'],
        }},
    },
    'required': ['status', 'issues', 'budget_items'],
}


def review_issues(review, trip):
    Draft202012Validator(REVIEW_SCHEMA).validate(review)
    issues = list(review['issues'])
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
