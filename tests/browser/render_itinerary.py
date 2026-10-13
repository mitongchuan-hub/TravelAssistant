"""Render UI fixtures without starting the app, logging in, or writing a database."""
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jinja2 import Environment, FileSystemLoader, select_autoescape
from app.models import User, Trip, TripDay, TripItem, TripPlan, MealRecommendation
from app.presentation import day_date_label, timeline_time_parts

root = Path(__file__).resolve().parents[2]
output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=True)
env = Environment(loader=FileSystemLoader(root / 'app/templates'), autoescape=select_autoescape())
env.filters.update(day_date_label=day_date_label, timeline_time_parts=timeline_time_parts)
env.globals.update(url_for=lambda name, path: '/static/' + path.lstrip('/'), static_version='fixture')
owner = User('fixture-user', '旅行者', '旅')
trip = Trip('fixture-trip', '济南旅行', '2026年10月2日—8日', '人均1500', '轻松', '', owner, '草案', '')
days = []
for number in range(7):
    items = []
    for index, category in enumerate(['activity', 'transport', 'meal', 'rest', 'accommodation']):
        title = ['大明湖散步', '步行前往曲水亭街', '本地午餐', '午后休息', '办理入住'][index]
        if number == 0 and index == 0:
            title = '大明湖散步与老城慢游：一条很长的安排标题（保留完整说明）'
        meal = MealRecommendation('午餐', '把子肉与时蔬', '鲁菜', '约30元/人', '待确认') if category == 'meal' else None
        items.append(TripItem(f'item-{number}-{index}', ['09:00-11:30', '11:30—12:00', '12:00-13:00', '下午', '15:30之后'][index],
                              title, '济南老城 / 大明湖附近', '1小时', '根据体力安排，减少往返。',
                              '这里保留完整备注，可在详情中阅读。' * 8, ['慢游'], category, meal))
    days.append(TripDay(f'day-{number}', f'Day {number+1}', items,
                       (date(2026, 10, 2) + timedelta(days=number)).isoformat(),
                       '老城泉水与美食慢游，较长主题也应自然换行'))
plans = {
    'itinerary': TripPlan('fixture-plan', '计划', '草案', days, overview='围绕老城慢游', pending_items=['返程方式']),
    'empty': TripPlan('empty', '未生成', '未生成'),
    'single': TripPlan('single', '单日', '草案', [TripDay('single', 'Day 1', [
        TripItem('bare-item', '时间待定', '安排尚待补充', '地点待定', '待定', '', ''),
    ])]),
}
for name, plan in plans.items():
    html = env.get_template('workspace.html').render(
        tab='itinerary', request=SimpleNamespace(query_params={}), trip=trip, plan=plan,
        current_user=owner, messages=[], idea_cards=[],
        metrics={'plan_day_count': len(plan.days), 'plan_item_count': sum(len(day.items) for day in plan.days)},
    )
    (output / f'{name}.html').write_text(html, encoding='utf-8')
print(output)
