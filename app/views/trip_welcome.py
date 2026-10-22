"""Create a welcome message from the new trip's saved requirements."""
from app.models import AgentCard, AgentAction


def trip_welcome(trip):
    profile = trip.requirements
    place = trip.lodging
    records = [f"- 住宿：{place.get('name') or profile.lodging_address}",
               f"- 位置：{profile.city} · {place.get('address', '')}",
               f"- 游玩：{profile.day_count or 1}天，每天从住宿出发并返回"]
    if profile.start_date:
        records.append(f'- 日期：{profile.start_date} 至 {profile.end_date}')
    if profile.outing_start_time or profile.outing_end_time:
        records.append(f"- 每天时间：{profile.outing_start_time or '出门时间可选'} → {profile.outing_end_time or '返回时间可选'}")
    if profile.travelers:
        records.append(f'- 人数：{profile.travelers}人')
    if profile.budget_amount:
        scope = '人均' if profile.budget_scope == 'per_person' else '总额'
        records.append(f'- 预算：{scope}{profile.budget_amount}元；{profile.budget_coverage}')
    body = '同城行程已经建好。\n\n' + '\n'.join(records)
    body += '\n\n可以直接说“生成路线”。出行日期和游玩天数为必填；人数和预算可选，也可以补充必去地点、忌口或节奏。'
    return body, AgentCard('requirement-summary', '住宿位置已确认', '可以直接生成同城路线。', [],
                           [AgentAction('生成路线', 'board')])
