"""Create a welcome message from the new trip's saved requirements."""
from .models import AgentCard, AgentAction
from .planning import required_information


def trip_welcome(trip):
    profile = trip.requirements
    scope = {'total': '总额', 'per_person': '人均'}.get(profile.budget_scope, '')
    records = [
        ('城市', profile.city),
        ('抵达', ' · '.join(value for value in
                            (profile.start_date, profile.arrival_time, profile.arrival_location) if value)),
        ('离开', ' · '.join(value for value in
                            (profile.end_date, profile.departure_time, profile.departure_location) if value)),
        ('人数', f'{profile.travelers}人' if profile.travelers else ''),
        ('预算', '；'.join(value for value in
                          (f'{scope}{profile.budget_amount}元' if profile.budget_amount else '',
                           profile.budget_coverage) if value)),
    ]
    details = [f'- {label}：{value}' for label, value in records if value]
    body = '旅行已经建好。'
    if details:
        body += '\n\n已记录：\n' + '\n'.join(details)
    missing = required_information(trip)
    if missing:
        body += '\n\n生成行程前，还需要补充：' + '、'.join(missing) + '。也可以先聊旅行想法。'
    else:
        body += '\n\n可以直接让我生成行程，也可以补充必去地点、忌口或旅行节奏。'
    return body, AgentCard(
        kind='missing-info' if missing else 'requirement-summary',
        title='旅行已创建' if missing else '旅行需求已记录',
        summary='生成行程前请补齐缺少的信息。' if missing else '创建时填写的信息已保存，可以直接生成行程。',
        bullets=[],
        actions=[AgentAction(label='查看旅行想法', target='board')],
    )
