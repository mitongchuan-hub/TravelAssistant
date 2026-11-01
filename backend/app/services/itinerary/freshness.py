"""Saved-plan freshness and shared location validation, without I/O or views."""
from dataclasses import asdict
from datetime import datetime
import math
import re

from app.models import PlaceDetails, Trip, TripPlan


BASIS_NOTE = '站点为方案顺序，不是地图几何；历史路线仅为查询时估算，不证明开放、免费、门票、预约或遮蔽条件。'


def normalize_name(value):
    return re.sub(r'\s+', ' ', value.strip()).casefold() if isinstance(value, str) else ''


def normalize_city(value):
    return normalize_name(value).removesuffix('市')


def parse_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.utcoffset() is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def validated_place(raw, city=''):
    if not isinstance(raw, dict):
        return None
    # bool is a number in Python, but never a coordinate or a POI identifier.
    for key, limit in (('longitude', 180), ('latitude', 90)):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            if not math.isfinite(value) or not -limit <= value <= limit:
                return None
        except (OverflowError, ValueError):
            return None
    try:
        place = PlaceDetails(**raw)
    except (ValueError, TypeError):
        return None
    fields = ('poi_id', 'name', 'address', 'city', 'district', 'source', 'fetched_at',
              'selected_at', 'place_type', 'opening_hours')
    if any(not isinstance(getattr(place, key), str) for key in fields):
        return None
    if not all(normalize_name(getattr(place, key)) for key in ('poi_id', 'name', 'city')):
        return None
    if place.source != '高德地图 POI' or not parse_timestamp(place.fetched_at):
        return None
    if city and normalize_city(place.city) != normalize_city(city):
        return None
    return place


def saved_city(plan):
    snapshot = plan.requirements_snapshot if plan and isinstance(plan.requirements_snapshot, dict) else {}
    city = snapshot.get('city')
    return city if isinstance(city, str) and city.strip() else ''


def saved_lodging(plan):
    return validated_place(plan.lodging_snapshot, saved_city(plan)) if plan else None


def current_lodging(trip):
    return validated_place(trip.lodging, trip.requirements.city) if normalize_city(trip.requirements.city) else None


def lodging_identity(place):
    return (place.poi_id, normalize_name(place.name), place.longitude, place.latitude,
            normalize_city(place.city), normalize_name(place.district), normalize_name(place.address))


def plan_freshness(trip: Trip, plan: TripPlan | None) -> dict:
    """Check saved settings/locations, not the protocol's plan identity basis."""
    if not plan or not plan.has_content:
        return {'basis_status': 'empty', 'basis_message': '尚无行程方案。'}
    current = asdict(trip.requirements)
    snapshot = plan.requirements_snapshot
    revision = plan.requirements_revision
    current_revision = trip.requirements.revision
    complete = (isinstance(snapshot, dict) and snapshot.keys() == current.keys()
                and isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0
                and isinstance(current_revision, int) and not isinstance(current_revision, bool)
                and type(snapshot.get('revision')) is int and snapshot['revision'] == revision)
    saved = saved_lodging(plan) if trip.mode == 'city' else None
    missing_lodging = trip.mode == 'city' and saved is None
    lodging_note = ' 原方案住宿依据不完整，不使用当前住宿补齐原路线。' if missing_lodging else ''
    if not complete:
        status, message = 'unknown', '原方案的需求版本或需求快照不完整，无法确认是否仍适用。'
    elif (revision != current_revision or snapshot != current
          or any(type(snapshot[key]) is not type(value) for key, value in current.items())):
        status, message = 'stale', '游玩设置或旅行需求已变化，以下为原方案的历史依据，请重新生成方案。'
    elif trip.mode == 'city':
        confirmed = current_lodging(trip)
        if missing_lodging or confirmed is None:
            status, message = 'unknown', '无法完整核对原方案与当前已确认住宿的位置依据。'
        elif lodging_identity(saved) != lodging_identity(confirmed):
            status, message = 'stale', '已确认住宿发生变化，以下仅使用原方案住宿，未混入当前住宿，请重新生成方案。'
        else:
            status, message = 'fresh', '方案与当前游玩设置及已确认住宿一致，出发前仍需核实。'
    else:
        status, message = 'fresh', '方案与当前旅行需求一致，出发前仍需核实。'
    return {'basis_status': status, 'basis_message': message + lodging_note + ' ' + BASIS_NOTE}
