"""Validation and updates for the traveler's durable basic requirements."""
from copy import deepcopy
from datetime import date
import re

from app.domain.models import Trip

BASIC_FIELDS = {"destination"}
PROFILE_FIELDS = {"start_date", "end_date", "city", "day_count", "transport_mode", "lodging_address"}


def _optional_int(value: object, label: str, maximum: int) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise ValueError(f"{label}请填写正整数")
    number = int(value)
    if not 1 <= number <= maximum:
        raise ValueError(f"{label}应在 1 到 {maximum} 之间")
    return number


def initialize_requirements(trip: Trip, changes: dict) -> Trip:
    """Validate the full candidate before changing any live state."""
    if not isinstance(changes, dict) or changes.keys() - BASIC_FIELDS - PROFILE_FIELDS:
        raise ValueError("需求字段不正确，请刷新后重试")
    updated = deepcopy(trip)
    profile = updated.requirements
    for key, value in changes.items():
        if key == "day_count":
            profile.day_count = _optional_int(value, "旅行天数", 365)
        else:
            if not isinstance(value, str):
                raise ValueError("需求请填写文本，未知信息可以留空")
            if len(value.strip()) > 300:
                raise ValueError("需求文字过长，请缩短后保存")
            setattr(updated if key in BASIC_FIELDS else profile, key, value.strip())
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
        days = (end - start).days + 1
        if days > 365:
            raise ValueError("旅行日期跨度不能超过 365 天")
        if "day_count" in changes and profile.day_count is not None and profile.day_count != days:
            raise ValueError("旅行天数与起止日期不一致")
        profile.day_count = days
    if profile.transport_mode not in {"unspecified", "driving", "transit"}:
        raise ValueError("请选择自驾或公交地铁")
    if updated.mode == 'city' and (profile.lodging_address != trip.requirements.lodging_address
                                   or profile.city != trip.requirements.city):
        updated.lodging = {}
    if {'start_date', 'end_date', 'day_count'} & changes.keys():
        updated.legacy_metadata.setdefault('fallback', {})['date_range'] = False
    profile.revision = trip.requirements.revision + 1
    return updated


MAX_REQUIREMENTS_TEXT = 20000


def validate_requirements_text(text, previous=''):
    if not isinstance(text, str):
        raise ValueError('需求正文请填写文本')
    if len(text) > MAX_REQUIREMENTS_TEXT and len(text) > len(previous):
        raise ValueError('需求正文不能超过20,000个Unicode字符，请整理或缩短正文后保存')
    return text


def update_requirements(trip: Trip, changes: dict) -> Trip:
    """Only prose remains mutable after creation, including on legacy trips."""
    if not isinstance(changes, dict) or set(changes) != {'text'}:
        raise ValueError('创建资料已固定，不能修改；如需更改请新建旅行')
    text = validate_requirements_text(changes['text'], trip.requirements.text)
    updated = deepcopy(trip)
    if text != trip.requirements.text:
        updated.requirements.text = text
        updated.requirements.revision += 1
    return updated
