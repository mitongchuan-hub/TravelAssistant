"""Planning prerequisites and dates, derived from the durable requirements only."""
from datetime import date, timedelta


def city_outing_brief(trip):
    """Require a dated outing and a consistent, explicit day count."""
    if missing := required_information(trip):
        return None, '旧旅行创建资料不完整，无法规划或重排行程，请新建完整旅行。仍可维护需求正文和已有文稿。'
    p = trip.requirements
    count = p.day_count
    dates = None
    if p.start_date and p.end_date:
        try:
            first, last = date.fromisoformat(p.start_date), date.fromisoformat(p.end_date)
            count = (last - first).days + 1
            if count != p.day_count:
                return None, '需要确认：游玩天数与起止日期不一致。'
            if not 1 <= count <= 365:
                raise ValueError()
            dates = [(first + timedelta(days=i)).isoformat() for i in range(count)]
        except ValueError:
            return None, '需要确认：游玩日期有冲突，请检查起止日期。'
    elif p.start_date or p.end_date:
        return None, '需要确认：请同时提供起止日期，或者都留空。'
    if not 1 <= count <= 365:
        return None, '需要确认：游玩天数应在1到365天之间。'
    return {
        'mode': 'city', 'city': p.city, 'dates': dates,
        'transport_mode': p.transport_mode,
        'start_date': p.start_date or None, 'end_date': p.end_date or None,
        'day_count': count, 'day_labels': [f'第{i+1}天' for i in range(count)],
        'overnight_count': max(0, count - 1),
        'lodging': trip.lodging,
        'time_basis': '住宿所在城市当地时间', 'requirements_revision': p.revision,
        'note': '每天从同一住宿出发并返回，按已确认日期与天数安排。'
                '按实际路线给出建议时段，不把建议当作已确认时刻。'
                '住宿作为路线起终点，住宿费用和跨城交通不计入默认游玩费用。',
    }, None


def required_information(trip):
    missing = []
    if not trip or not trip.requirements.lodging_address.strip():
        missing.append('住宿地址')
    if not trip or not trip.lodging or not trip.requirements.city.strip():
        missing.append('住宿位置确认（请通过完整创建表单新建旅行）')
    if not trip or not trip.requirements.start_date or not trip.requirements.end_date:
        missing.append('出行日期（开始日期及结束日期）')
    if not trip or not trip.requirements.day_count:
        missing.append('游玩天数')
    if not trip or trip.requirements.transport_mode not in {'driving', 'transit'}:
        missing.append('出行方式')
    return missing


def planning_brief(trip, *, require_all=False):
    return city_outing_brief(trip)
