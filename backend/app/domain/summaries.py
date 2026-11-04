"""Date-only display projection; archived text is never current requirements."""
from datetime import date


def legacy_summaries(date_range=''):
    return {'summaries': {'date_range': date_range}, 'fallback': {'date_range': True}}


def trip_summaries(requirements, legacy_metadata=None, mode='city'):
    metadata = legacy_metadata or {}
    r = requirements
    date_range = ''
    if r.start_date and r.end_date:
        try:
            start, end = date.fromisoformat(r.start_date), date.fromisoformat(r.end_date)
            date_range = f'{start.year}年{start.month}月{start.day}日 - {end.year}年{end.month}月{end.day}日'
        except (ValueError, TypeError):
            pass
    if not date_range:
        if r.day_count:
            date_range = f'{r.day_count}天同城游 · 日期待补充' if mode == 'city' else f'{r.day_count}天 · 日期待定'
        else:
            old = metadata.get('summaries', {}).get('date_range')
            date_range = old if metadata.get('fallback', {}).get('date_range') and isinstance(old, str) and old.strip() else ('日期与天数待补充' if mode == 'city' else '时间待定')
    return {'date_range': date_range}
