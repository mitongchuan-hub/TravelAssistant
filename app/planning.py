"""Planning prerequisites and dates, derived from the durable requirements only."""
from datetime import date


def planning_brief(trip):
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
