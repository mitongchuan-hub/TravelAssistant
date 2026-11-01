from __future__ import annotations
import json
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime
import re
from threading import RLock
from contextlib import contextmanager
from uuid import uuid4
from app.domain.models import AgentAction, AgentCard, IdeaCard, Message, Trip, TripPlan, User
from app.domain.requirements import initial_requirements, update_requirements
from app.storage.state import next_id, IDEA_WALL_LIMIT, IDEA_ROTATIONS

def _date_range_bounds(value: str) -> tuple[date, date] | None:
    text = (value or '').strip()
    chinese = re.search('(?:(\\d{4})年)?(\\d{1,2})月(\\d{1,2})日\\s*[-—至]\\s*(?:(\\d{4})年)?(?:(\\d{1,2})月)?(\\d{1,2})日', text)
    if chinese:
        start_year = int(chinese.group(1) or datetime.now().year)
        start_month = int(chinese.group(2))
        end_year = int(chinese.group(4) or start_year)
        end_month = int(chinese.group(5) or start_month)
        try:
            return (date(start_year, start_month, int(chinese.group(3))), date(end_year, end_month, int(chinese.group(6))))
        except ValueError:
            return None
    dotted = re.search('(?:(\\d{4})[./])?(\\d{1,2})[./](\\d{1,2})\\s*[-—至]\\s*(?:(\\d{4})[./])?(\\d{1,2})[./](\\d{1,2})', text)
    if dotted:
        start_year = int(dotted.group(1) or datetime.now().year)
        end_year = int(dotted.group(4) or start_year)
        try:
            return (date(start_year, int(dotted.group(2)), int(dotted.group(3))), date(end_year, int(dotted.group(5)), int(dotted.group(6))))
        except ValueError:
            return None
    return None

def _date_coverage_note(trip: Trip, plan: TripPlan) -> str:
    bounds = _date_range_bounds(trip.date_range)
    if not bounds or not plan.days:
        return ''
    start, end = bounds
    planned_dates = {day.date.strip() for day in plan.days if day.date.strip()}
    if not planned_dates:
        return f'日期范围共 {(end - start).days + 1} 天，具体安排待补充'
    missing = []
    current = start
    while current <= end:
        if current.isoformat() not in planned_dates:
            missing.append(current)
        current = date.fromordinal(current.toordinal() + 1)
    if not missing:
        return ''
    if len(missing) == 1 and missing[0] == end:
        return f'返程日（{end.month}月{end.day}日）尚未安排'
    return f'还有 {len(missing)} 天未安排'

def trip_metrics(self, trip_id: str) -> dict[str, int | str]:
    cards = self.idea_cards[trip_id]
    plan = self.plans[trip_id]
    display_days = (plan.display or {}).get('days', [])
    plan_items = sum((len(day['items']) for day in display_days)) if display_days else sum((len(day.items) for day in plan.days))
    basis = plan.requirements_snapshot
    plan_date_range = f"{basis['start_date']} — {basis['end_date']}" if basis.get('start_date') and basis.get('end_date') else self.trips[trip_id].date_range
    return {'message_count': len(self.messages[trip_id]), 'idea_count': len(cards), 'constraint_count': sum((card.kind != '待归类' for card in cards)), 'reminder_count': sum((card.kind == '禁忌' or card.status == '冲突提醒' for card in cards)), 'plan_day_count': len(display_days) or len((plan.display or {}).get('dates', [])) or len(plan.days), 'plan_date_range': plan_date_range, 'plan_item_count': plan_items, 'date_coverage_note': _date_coverage_note(self.trips[trip_id], plan)}
