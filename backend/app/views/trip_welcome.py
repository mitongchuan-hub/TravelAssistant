"""Create a welcome message from the new trip's saved requirements."""
from app.domain.models import AgentCard


def trip_welcome(trip):
    profile = trip.requirements
    place = trip.lodging
    if not place:
        body = '这趟旧旅行缺少完整创建资料。仍可在聊天中维护需求正文和已有文稿；如需规划路线，请通过完整创建表单新建旅行。'
        return body, AgentCard('requirement-summary', '旅行助手', '通过聊天安排旅行。', [], [])
    records = [f"- 住宿：{place.get('name') or profile.lodging_address}",
               f"- 位置：{profile.city} · {place.get('address', '')}",
               f"- 游玩：{str(profile.day_count) + '天' if profile.day_count else '天数待补充'}，每天从住宿出发并返回"]
    if profile.start_date:
        records.append(f'- 日期：{profile.start_date} 至 {profile.end_date}')
    body = '同城行程已经建好。\n\n' + '\n'.join(records)
    body += '\n\n可以直接说“生成路线”。创建资料已固定，可以在聊天或需求正文中补充自由要求。'
    return body, AgentCard('requirement-summary', '住宿位置已确认', '在聊天中告诉我下一步安排。', [], [])
