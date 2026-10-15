"""Planning prerequisites and dates, derived from the durable requirements only."""
from datetime import date


def required_information(trip):
    p = trip.requirements if trip else None
    if not p:
        return ['旅行城市', '起止日期', '抵离时间和地点', '旅行人数', '预算金额和口径']
    missing = []
    for valid, label in (
        (p.city.strip(), '旅行城市'),
        (p.start_date and p.end_date, '具体起止日期'),
        (p.arrival_time and p.departure_time, '抵达和离开时间'),
        (p.arrival_location.strip() and p.departure_location.strip(), '抵达和离开地点'),
        (p.travelers, '旅行人数'),
        (p.budget_amount and p.budget_scope in {'total', 'per_person'}, '预算金额及总额/人均口径'),
        (p.budget_coverage.strip(), '预算包含的费用范围'),
    ):
        if not valid:
            missing.append(label)
    return missing


def planning_brief(trip, *, require_all=False):
    profile = trip.requirements if trip else None
    if not profile or not profile.city.strip():
        return None, '需要补充：这次旅行去哪个城市？'
    if not profile.start_date or not profile.end_date:
        return None, '需要补充：计划哪天抵达、哪天离开？请给出具体日期。'
    try:
        start, end = date.fromisoformat(profile.start_date), date.fromisoformat(profile.end_date)
        if end < start:
            raise ValueError('日期倒置')
    except ValueError:
        return None, '需要确认：抵达和离开日期存在冲突，请确认有效的起止日期。'
    nights = (end - start).days
    if nights >= 365:
        return None, '需要确认：旅行日期跨度超过365天，请确认起止日期。'
    if nights == 0 and profile.arrival_time and profile.departure_time and profile.departure_time <= profile.arrival_time:
        return None, '需要确认：当天离开时间应晚于抵达时间，请确认抵离时刻。'
    if require_all and (missing := required_information(trip)):
        return None, '需要补充：请补齐' + '、'.join(missing) + '。已有需求已保留。'
    return {
        'city': profile.city,
        'start_date': start.isoformat(), 'end_date': end.isoformat(),
        'dates': [date.fromordinal(start.toordinal() + i).isoformat() for i in range(nights + 1)],
        'day_count': nights + 1, 'overnight_count': nights,
        'arrival': {'time': profile.arrival_time or None, 'location': profile.arrival_location or None},
        'departure': {'time': profile.departure_time or None, 'location': profile.departure_location or None},
        'time_basis': '旅游城市当地时间',
        'requirements_revision': profile.revision,
        'note': '跨午夜次数不代表已预订酒店；当天往返不住宿。未知抵离时刻保持未知，不自行填充。',
    }, None
