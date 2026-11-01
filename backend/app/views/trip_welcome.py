"""Create a welcome message from the new trip's saved requirements."""
from app.domain.models import AgentCard


def trip_welcome(trip):
    profile = trip.requirements
    place = trip.lodging
    if not place:
        body = '你好！直接告诉我这趟旅行的想法就好。需要安排路线时，请告诉我住宿全名和所在城市、开始日期、游玩天数，以及自驾还是公交地铁；我会在聊天中与你核对。'
        return body, AgentCard('requirement-summary', '旅行助手', '通过聊天安排旅行。', [], [])
    records = [f"- 住宿：{place.get('name') or profile.lodging_address}",
               f"- 位置：{profile.city} · {place.get('address', '')}",
               f"- 游玩：{str(profile.day_count) + '天' if profile.day_count else '天数待补充'}，每天从住宿出发并返回"]
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
    return body, AgentCard('requirement-summary', '住宿位置已确认', '在聊天中告诉我下一步安排。', [], [])
