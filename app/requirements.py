"""Validation and updates for the traveler's durable requirements."""
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
import re

from .models import Trip, TripRequirements

LIST_FIELDS = {"must_visit", "avoid", "dietary"}
BASIC_FIELDS = {"destination", "date_range", "style", "note"}
PROFILE_FIELDS = {
    "start_date", "end_date", "arrival_time", "departure_time", "day_count", "travelers", "budget_amount",
    "city", "arrival_location", "departure_location",
    "budget_scope", "budget_coverage", "pace", "lodging_address", "outing_start_time", "outing_end_time", *LIST_FIELDS,
}


def initial_requirements(budget: str, style: str) -> TripRequirements:
    requirements = TripRequirements(pace=style.strip() if style.strip() != "风格待定" else "")
    # Only normalize unambiguous amounts. Keep other legacy text on Trip.
    match = re.fullmatch(r"\s*(人均|每人|总额|总预算)?\s*(\d+(?:\.\d{1,2})?)\s*(?:元|人民币)?\s*", budget)
    if match:
        requirements.budget_amount = match.group(2)
        requirements.budget_scope = (
            "per_person" if match.group(1) in {"人均", "每人"}
            else "total" if match.group(1) else "unspecified"
        )
    return requirements


def clean_list(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("每类需求请填写不超过 100 条文本")
    result = []
    for item in value:
        if not isinstance(item, str) or len(item.strip()) > 300:
            raise ValueError("每条需求请填写不超过 300 字的文本")
        item = item.strip()
        if item and item not in result:
            result.append(item)
    return result


def _optional_int(value: object, label: str, maximum: int) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise ValueError(f"{label}请填写正整数")
    number = int(value)
    if not 1 <= number <= maximum:
        raise ValueError(f"{label}应在 1 到 {maximum} 之间")
    return number


def update_requirements(trip: Trip, changes: dict) -> Trip:
    """Validate the full candidate before changing any live state."""
    if not isinstance(changes, dict) or changes.keys() - BASIC_FIELDS - PROFILE_FIELDS:
        raise ValueError("需求字段不正确，请刷新后重试")
    updated = deepcopy(trip)
    profile = updated.requirements
    for key, value in changes.items():
        if key in LIST_FIELDS:
            setattr(profile, key, clean_list(value))
        elif key in {"day_count", "travelers"}:
            setattr(profile, key, _optional_int(value, "旅行天数" if key == "day_count" else "人数", 365 if key == "day_count" else 100))
        else:
            if not isinstance(value, str):
                raise ValueError("需求请填写文本，未知信息可以留空")
            if len(value.strip()) > (2000 if key == "note" else 300):
                raise ValueError("需求文字过长，请缩短后保存")
            setattr(updated if key in BASIC_FIELDS else profile, key, value.strip())
    for key, label in (("arrival_time", "抵达时间"), ("departure_time", "离开时间"),
                       ("outing_start_time", "每天出门时间"), ("outing_end_time", "每天返回时间")):
        value = getattr(profile, key)
        if value and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError(f"{label}请填写有效的 HH:MM 时间")
    if not updated.destination:
        raise ValueError("请填写目的地或旅行主题")
    if bool(profile.start_date) != bool(profile.end_date):
        raise ValueError("请同时填写出发日期和结束日期，或都留空")
    if profile.start_date:
        try:
            start, end = date.fromisoformat(profile.start_date), date.fromisoformat(profile.end_date)
        except ValueError as exc:
            raise ValueError("请填写有效的旅行日期") from exc
        if end < start:
            raise ValueError("结束日期不能早于出发日期")
        if end == start and profile.arrival_time and profile.departure_time and profile.departure_time <= profile.arrival_time:
            raise ValueError("当天离开时间必须晚于抵达时间")
        days = (end - start).days + 1
        if days > 365:
            raise ValueError("旅行日期跨度不能超过 365 天")
        if "day_count" in changes and profile.day_count is not None and profile.day_count != days:
            raise ValueError("旅行天数与起止日期不一致")
        profile.day_count = days
        updated.date_range = f"{start.year}年{start.month}月{start.day}日 - {end.year}年{end.month}月{end.day}日"
    elif {"start_date", "end_date", "day_count"} & changes.keys():
        if profile.day_count:
            updated.date_range = f"{profile.day_count}天 · 日期待定"
        elif trip.requirements.start_date or trip.requirements.day_count:
            updated.date_range = "时间待定"
    updated.date_range = updated.date_range or "时间待定"
    if profile.budget_scope not in {"unspecified", "per_person", "total"}:
        raise ValueError("请选择人均预算、总预算或待确认")
    if profile.budget_amount:
        try:
            amount = Decimal(profile.budget_amount)
        except InvalidOperation as exc:
            raise ValueError("预算金额请用纯数字文本，例如2600，不含元或其他单位") from exc
        if not amount.is_finite() or amount <= 0 or amount > 100000000 or amount.as_tuple().exponent < -2:
            raise ValueError("预算应为正数，最多保留两位小数")
        profile.budget_amount = format(amount, "f")
        prefix = {"per_person": "人均", "total": "总预算", "unspecified": "预算"}[profile.budget_scope]
        updated.budget = f"{prefix} {profile.budget_amount} 元" + ("（口径待确认）" if profile.budget_scope == "unspecified" else "")
    elif trip.requirements.budget_amount:
        updated.budget = "预算待定"
    if "pace" in changes:
        updated.style = profile.pace or "风格待定"
    elif "style" in changes:
        profile.pace = updated.style if updated.style != "风格待定" else ""
    if updated.mode == 'city':
        if profile.outing_start_time and profile.outing_end_time and profile.outing_end_time <= profile.outing_start_time:
            raise ValueError('每天返回时间必须晚于出门时间；跨午夜游玩请分别安排')
        if (profile.lodging_address != trip.requirements.lodging_address
                or profile.city != trip.requirements.city):
            updated.lodging = {}
        if not profile.start_date:
            profile.day_count = profile.day_count or 1
            updated.date_range = f'{profile.day_count}天同城游 · 日期可选'
        if profile.budget_amount and profile.budget_scope == 'unspecified':
            raise ValueError('填写预算后请选择人均或总额口径')
    profile.revision = trip.requirements.revision + 1
    return updated


def chat_changes(patch: dict, user_body: str, trip: Trip) -> dict:
    """Require evidence from this message; list edits are explicit add/remove."""
    if not isinstance(patch, dict) or set(patch) != {"changes", "evidence"}:
        raise ValueError("需求更新缺少本轮依据")
    evidence, changes = patch["evidence"], patch["changes"]
    if not isinstance(evidence, str) or not evidence.strip() or evidence.strip() not in user_body:
        raise ValueError("需求更新没有对应本轮消息")
    if not isinstance(changes, dict) or changes.keys() - BASIC_FIELDS - PROFILE_FIELDS:
        raise ValueError("需求更新字段不正确")
    changes = deepcopy(changes)
    for key in LIST_FIELDS & changes.keys():
        operations = changes[key]
        if not isinstance(operations, dict) or set(operations) - {"add", "remove"}:
            raise ValueError("需求列表应使用添加或移除操作")
        add, remove = clean_list(operations.get("add", [])), clean_list(operations.get("remove", []))
        if set(add) & set(remove):
            raise ValueError("同一需求不能同时添加和移除")
        previous = getattr(trip.requirements, key)
        if set(remove) - set(previous):
            raise ValueError("要移除的需求已变化，请重新确认")
        changes[key] = clean_list([item for item in previous if item not in remove] + add)
    return changes
