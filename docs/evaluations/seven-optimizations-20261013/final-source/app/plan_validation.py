"""Deterministic checks for known dates and explicit local clock times."""
from datetime import date, timedelta
import re
from dataclasses import asdict

from .models import Trip, TripPlan


class PlanValidationError(ValueError):
    pass


def _clock(value: str) -> int:
    hour, minute = map(int, value.split(':'))
    if hour > 23 or minute > 59:
        raise ValueError('无效时刻')
    return hour * 60 + minute


def validate_plan(plan: dict | TripPlan, trip: Trip) -> None:
    raw = asdict(plan) if isinstance(plan, TripPlan) else plan
    days = raw.get('days', [])
    errors = []
    profile = trip.requirements
    expected = []
    if profile.start_date and profile.end_date:
        first, last = date.fromisoformat(profile.start_date), date.fromisoformat(profile.end_date)
        expected = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    if profile.day_count and len(days) != profile.day_count:
        errors.append(f'需要安排 {profile.day_count} 天，实际为 {len(days)} 天')
    actual_dates = []
    for number, day in enumerate(days, 1):
        label = f'第 {number} 天'
        day_date = str(day.get('date') or '').strip()
        known_date = None
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', day_date):
            try:
                known_date = date.fromisoformat(day_date)
                actual_dates.append(day_date)
            except ValueError:
                errors.append(f'{label}日期无效：{day_date}')
        elif expected or re.search(r'\d', day_date):
            errors.append(f'{label}日期应为有效的 YYYY-MM-DD：{day_date or "未填写"}')
        lower = upper = None
        if known_date and day_date == profile.start_date and profile.arrival_time:
            lower = _clock(profile.arrival_time)
        if known_date and day_date == profile.end_date and profile.departure_time:
            upper = _clock(profile.departure_time)
        if not expected:
            if number == 1 and profile.arrival_time:
                lower = _clock(profile.arrival_time)
            if number == len(days) and profile.departure_time:
                upper = _clock(profile.departure_time)
        previous_start = None
        previous_end = None
        previous_title = ''
        for item in day.get('items', []):
            title = str(item.get('title') or '未命名安排')
            value = str(item.get('time') or '').strip().replace('：', ':')
            match = re.fullmatch(r'(\d{1,2}:\d{2})(?:\s*[-—–~至]\s*(\d{1,2}:\d{2}))?', value)
            if not match:
                if re.search(r'\d{1,2}:\d{2}', value):
                    errors.append(f'{label}「{title}」时间格式不明确：{value}，请使用 HH:MM 或 HH:MM-HH:MM，无法确定时保留待确认')
                continue
            try:
                start = _clock(match.group(1))
                end = _clock(match.group(2)) if match.group(2) else start
            except ValueError:
                errors.append(f'{label}「{title}」时刻无效：{value}')
                continue
            if match.group(2) and end <= start:
                errors.append(f'{label}「{title}」结束时间必须晚于开始时间；跨夜住宿请只标入住时刻')
            if lower is not None and start < lower:
                errors.append(f'{label}「{title}」安排在抵达时间 {profile.arrival_time} 之前')
            if upper is not None and end > upper:
                errors.append(f'{label}「{title}」安排超过离开时间 {profile.departure_time}')
            if previous_start is not None and start < previous_start:
                errors.append(f'{label}「{title}」时间顺序早于前一安排「{previous_title}」')
            elif previous_end is not None and start < previous_end:
                errors.append(f'{label}「{title}」与前一安排「{previous_title}」时间重叠')
            previous_start = start
            if previous_end is None or end >= previous_end:
                previous_end, previous_title = max(start, end), title
    if len(actual_dates) != len(set(actual_dates)):
        errors.append('行程存在重复日期')
    if actual_dates != sorted(actual_dates):
        errors.append('行程日期没有按先后顺序排列')
    if expected and actual_dates != expected:
        missing = sorted(set(expected) - set(actual_dates))
        extra = sorted(set(actual_dates) - set(expected))
        errors.append('行程日期未覆盖抵离区间' + (f'，缺少：{"、".join(missing)}' if missing else '') + (f'，超出：{"、".join(extra)}' if extra else ''))
    from .routes import route_time_errors
    errors.extend(route_time_errors(raw, trip))
    if errors:
        raise PlanValidationError('；'.join(dict.fromkeys(errors)))
