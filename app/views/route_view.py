"""Offline, evidence-bound route metadata for prose-derived city outing cards.

The saved text and raw display remain the itinerary. Search results are only a
catalog, not an adopted route; unknown stops interrupt adjacency. Text returned
here is plain data and must be escaped by the rendering template.
"""
from dataclasses import asdict
from datetime import datetime
import json
import math
import re
from urllib.parse import urlencode

from app.models import PlaceDetails, Trip, TripPlan
from app.services.places import collect_places
from app.services.plan_display import text_hash

MODE_LABELS = {'walking': '步行', 'transit': '公共交通', 'driving': '驾车'}
NAVIGATION_MODES = {'walking': 'walk', 'transit': 'bus', 'driving': 'car'}
BASIS_NOTE = '站点为方案顺序，不是地图几何；历史路线仅为查询时估算，不证明开放、免费、门票、预约或遮蔽条件。'
REPLAN_NOTE = '高德外链用于重新规划，不代表正文已有公交路线。'


def _name(value):
    return re.sub(r'\s+', ' ', value.strip()).casefold() if isinstance(value, str) else ''


def _city(value):
    return _name(value).removesuffix('市')


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except (OverflowError, ValueError):
        return False


def _timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.utcoffset() is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _place(raw, city=''):
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
    if not all(_name(getattr(place, key)) for key in ('poi_id', 'name', 'city')):
        return None
    if place.source != '高德地图 POI' or not _timestamp(place.fetched_at):
        return None
    if city and _city(place.city) != _city(city):
        return None
    return place


def _saved_city(plan):
    snapshot = plan.requirements_snapshot if plan and isinstance(plan.requirements_snapshot, dict) else {}
    city = snapshot.get('city')
    return city if isinstance(city, str) and city.strip() else ''


def _saved_lodging(plan):
    return _place(plan.lodging_snapshot, _saved_city(plan)) if plan else None


def _current_lodging(trip):
    return _place(trip.lodging, trip.requirements.city) if _city(trip.requirements.city) else None


def _lodging_identity(place):
    return (place.poi_id, _name(place.name), place.longitude, place.latitude,
            _city(place.city), _name(place.district), _name(place.address))


def plan_basis(trip: Trip, plan: TripPlan | None) -> dict:
    """Check only saved settings/locations, without scanning evidence or cards."""
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
    saved = _saved_lodging(plan) if trip.mode == 'city' else None
    missing_lodging = trip.mode == 'city' and saved is None
    lodging_note = ' 原方案住宿依据不完整，不使用当前住宿补齐原路线。' if missing_lodging else ''
    if not complete:
        status, message = 'unknown', '原方案的需求版本或需求快照不完整，无法确认是否仍适用。'
    elif (revision != current_revision or snapshot != current
          or any(type(snapshot[key]) is not type(value) for key, value in current.items())):
        status, message = 'stale', '游玩设置或旅行需求已变化，以下为原方案的历史依据，请重新生成方案。'
    elif trip.mode == 'city':
        confirmed = _current_lodging(trip)
        if missing_lodging or confirmed is None:
            status, message = 'unknown', '无法完整核对原方案与当前已确认住宿的位置依据。'
        elif _lodging_identity(saved) != _lodging_identity(confirmed):
            status, message = 'stale', '已确认住宿发生变化，以下仅使用原方案住宿，未混入当前住宿，请重新生成方案。'
        else:
            status, message = 'fresh', '方案与当前游玩设置及已确认住宿一致，出发前仍需核实。'
    else:
        status, message = 'fresh', '方案与当前旅行需求一致，出发前仍需核实。'
    return {'basis_status': status, 'basis_message': message + lodging_note + ' ' + BASIS_NOTE}


def _catalog(evidence):
    """Reuse the POI binder, but quarantine duplicate IDs before its last-write wins."""
    candidates, identities, names = [], {}, {}
    rejected, conflicts = set(), set()
    for row in evidence:
        if not isinstance(row, dict) or row.get('tool') != 'search_places':
            continue
        result = row.get('result')
        if not isinstance(result, dict) or result.get('status') != 'available':
            continue
        values = result.get('candidates')
        for raw in values if isinstance(values, list) else []:
            place = _place(raw)
            if place is None:
                if (isinstance(raw, dict) and raw.get('source') == '高德地图 POI'
                        and isinstance(raw.get('poi_id'), str) and raw['poi_id'].strip()):
                    rejected.add(raw['poi_id'])
                continue
            key = (_city(place.city), _name(place.name))
            names.setdefault(place.poi_id, set()).add(key)
            identity = (key, place.longitude, place.latitude)
            if place.poi_id in identities and identities[place.poi_id] != identity:
                conflicts.add(place.poi_id)
            identities.setdefault(place.poi_id, identity)
            candidates.append(raw)
    by_name = {}
    for poi_id, keys in names.items():
        for key in keys:
            by_name.setdefault(key, set()).add(poi_id)
    ambiguous = {key for key, ids in by_name.items() if len(ids) > 1 or ids & conflicts}
    catalog = collect_places([{'role': 'tool', 'name': 'search_places', 'content':
        json.dumps({'status': 'available', 'candidates': candidates}, ensure_ascii=False)}])
    blocked = rejected | conflicts
    return {poi_id: PlaceDetails(**raw) for poi_id, raw in catalog.items()
            if poi_id not in blocked}, ambiguous, blocked


def _place_view(place):
    if place is None:
        return None
    # Reuse the provider's marker URI, never a URL supplied by a model or user.
    from app.tools.places import map_url
    view = {key: getattr(place, key) for key in
            ('name', 'poi_id', 'address', 'city', 'district', 'source', 'fetched_at')}
    view['map_url'] = map_url(place)
    view['copy_address'] = ' '.join(value for value in
        (place.city, place.district, place.address, place.name) if value)
    if place.opening_hours:
        view['opening_hours'] = place.opening_hours
    return view


def _conditional(item):
    values = [item.get(key) for key in ('title', 'location', 'description', 'time_text')]
    notes = item.get('notes')
    values.extend(notes if isinstance(notes, list) else [])
    return any(isinstance(value, str) and re.search(
        r'如果|若|如遇|备选|二选一|择一|任选|可选|雨天|晴天|视天气|天气允许|替代方案|或改为', value)
        for value in values)


def _match(item, catalog, ambiguous, city, text):
    if _conditional(item):
        return None, 'unknown', '这是条件或备选安排，尚未确认为实际站点；不据此拼接或导航路线。'
    location = item.get('location')
    value = item.get('title') if location is None or isinstance(location, str) and not location.strip() else location
    name = _name(value)
    # Delimiters are deliberately conservative: a longer Chinese/Latin name is
    # not evidence for its substring, nor is a guessed alias or prose rewrite.
    mentioned = bool(name and re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', text))
    if not mentioned or not city:
        return None, 'unknown', '原文或同城地点证据不足，具体地点待确认；不跨过此项推断相邻路线。'
    key = (_city(city), name)
    if key in ambiguous:
        return None, 'ambiguous', '存在同名候选或同一 POI 的冲突资料，尚不能确定具体地点。'
    matches = [place for place in catalog.values() if (_city(place.city), _name(place.name)) == key]
    if len(matches) != 1:
        return None, 'unknown', '未唯一匹配原文中的同城完整地点名称，区域建议或多地点安排不作单一站点。'
    return matches[0], 'matched', '已匹配原文中的高德 POI；地点资料不等于开放、票价或预约已核实。'


def _route_records(evidence, places):
    records = {}
    for row in evidence:
        if not isinstance(row, dict) or row.get('tool') != 'query_route':
            continue
        args, result = row.get('arguments'), row.get('result')
        if not isinstance(args, dict) or not isinstance(result, dict):
            continue
        origin_id, destination_id, mode = (args.get(key) for key in
            ('origin_poi_id', 'destination_poi_id', 'mode'))
        if not all(isinstance(value, str) for value in (origin_id, destination_id, mode)) or mode not in MODE_LABELS:
            continue
        origin, destination = places.get(origin_id), places.get(destination_id)
        if not origin or not destination:
            continue
        if any(result.get(key) != args.get(key) for key in ('origin_poi_id', 'destination_poi_id', 'mode')):
            continue
        if result.get('origin_name') != origin.name or result.get('destination_name') != destination.name:
            continue
        source, status = result.get('source'), result.get('status')
        if source != '高德路线规划' and not (source == '同一已匹配地点' and origin_id == destination_id):
            continue
        queried_at = result.get('fetched_at') if 'fetched_at' in result else row.get('queried_at')
        queried = _timestamp(queried_at)
        if queried is None:
            continue
        duration, distance = result.get('duration_minutes'), result.get('distance_meters')
        if status == 'available':
            if not _number(duration) or (duration == 0 and origin_id != destination_id):
                continue
            if distance is not None and not _number(distance):
                continue
            if source == '同一已匹配地点' and (duration != 0 or distance != 0):
                continue
        elif status == 'unavailable':
            if duration is not None or distance is not None or not isinstance(result.get('reason'), str) or not result['reason'].strip():
                continue
        else:
            continue
        key = (origin_id, destination_id)
        value = {'mode': mode, 'status': status, 'duration': duration, 'distance': distance,
                 'source': source, 'queried_at': queried_at}
        if mode == 'transit' and status == 'available':
            value['transit_steps'] = result.get('transit_steps') or []
        modes = records.setdefault(key, {})
        previous = modes.get(mode)
        if previous is None or queried > previous[0]:
            modes[mode] = (queried, value)
        elif queried == previous[0] and value != previous[1]:
            # Two contradictory results at the same query time are not a choice.
            modes[mode] = (queried, None)
    return records


def _navigation_url(origin, destination, mode):
    return 'https://uri.amap.com/navigation?' + urlencode({
        'from': f'{origin.longitude},{origin.latitude},{origin.name}',
        'to': f'{destination.longitude},{destination.latitude},{destination.name}',
        'mode': NAVIGATION_MODES[mode], 'policy': 1, 'coordinate': 'gaode', 'callnative': 0,
    })


def _transit_view(raw_steps):
    """Project saved provider instructions without joining alternatives into transfers."""
    steps, summaries, has_ride = [], [], False
    for raw in raw_steps if isinstance(raw_steps, list) else []:
        if not isinstance(raw, dict):
            raw = {'kind': 'unknown'}
        if raw.get('kind') == 'walking':
            values = raw.get('instructions')
            instructions = [value for value in values if isinstance(value, str) and value.strip()] if isinstance(values, list) else []
            steps.append({'kind': 'walking', 'instructions': instructions,
                          **{key: raw.get(key) if _number(raw.get(key)) else None
                             for key in ('distance_meters', 'duration_minutes')}})
        elif raw.get('kind') == 'ride' and isinstance(raw.get('lines'), list):
            lines = []
            for line in raw['lines']:
                if not isinstance(line, dict) or not isinstance(line.get('name'), str) or not line['name'].strip():
                    continue
                value = {key: line.get(key) if isinstance(line.get(key), str) and line[key].strip() else None
                         for key in ('name', 'departure_stop', 'arrival_stop', 'direction')}
                value.update({key: line.get(key) if _number(line.get(key)) else None
                              for key in ('via_stop_count', 'duration_minutes')})
                lines.append(value)
            if lines:
                steps.append({'kind': 'ride', 'lines': lines, 'transfer': has_ride,
                              **{key: raw.get(key) if isinstance(raw.get(key), str) else None
                                 for key in ('entrance', 'exit')}})
                names = ' 或 '.join(line['name'] for line in lines)
                summaries.append('（' + names + '）' if len(lines) > 1 else names)
                has_ride = True
                continue
            steps.append({'kind': 'unknown'})
            summaries.append('待核实路段')
            has_ride = False
        else:
            steps.append({'kind': 'unknown'})
            summaries.append('待核实路段')
            has_ride = False
    return steps, ' → '.join(summaries) if any(step['kind'] == 'ride' for step in steps) else ''


def _leg(origin, destination, origin_name, destination_name, records):
    leg = {'origin': origin.name if origin else origin_name,
           'destination': destination.name if destination else destination_name,
           'status': 'unknown', 'summary': '相邻地点或原方案住宿尚未确认，交通耗时待核实。',
           'source': '', 'queried_at': '', 'navigation_url': None,
           'transit_steps': [], 'line_summary': '', 'missing_transit_details': False}
    if not origin or not destination:
        return leg
    modes = records.get((origin.poi_id, destination.poi_id), {})
    record = next(iter(modes.values()))[1] if len(modes) == 1 else None
    mode = record['mode'] if record else 'transit'
    leg['navigation_url'] = _navigation_url(origin, destination, mode)
    if record is None and origin.poi_id == destination.poi_id:
        leg.update(source='同一已匹配地点', summary='同一已匹配地点，无需地点间接驳；未估算场地内部步行。' + ' ' + REPLAN_NOTE)
        return leg
    if record is None:
        reason = '有多种历史交通方式或冲突结果，未确认本段方式。' if modes else '未找到同方向、同方式的可信历史路线，交通耗时待核实。'
        leg['summary'] = reason + ' ' + REPLAN_NOTE
        return leg
    leg.update(status=record['status'], source=record['source'], queried_at=record['queried_at'])
    if record['status'] == 'unavailable':
        leg['summary'] = MODE_LABELS[mode] + ' · 历史查询未获得可用路线，耗时待核实；高德外链用于重新规划。'
        return leg
    if mode == 'transit':
        leg['transit_steps'], leg['line_summary'] = _transit_view(record.get('transit_steps'))
        leg['missing_transit_details'] = not leg['transit_steps'] and origin.poi_id != destination.poi_id
    summary = f"{MODE_LABELS[mode]} · 查询时约{record['duration']:g}分钟"
    distance = record['distance']
    if distance is not None:
        amount = f'{distance / 1000:.1f}'.rstrip('0').rstrip('.') + '公里' if distance >= 1000 else f'{distance:g}米'
        summary += ' · ' + amount
    leg['summary'] = summary
    return leg


def _display_days(plan):
    if (not plan or not isinstance(plan.text, str) or not plan.text.strip()
            or not plan.display_source_hash or plan.display_source_hash != text_hash(plan.text)
            or not isinstance(plan.display, dict)):
        return []
    days = plan.display.get('days')
    if not isinstance(days, list) or any(not isinstance(day, dict) or not isinstance(day.get('items'), list) for day in days):
        return []
    return days


def _stop_label(item):
    for key in ('location', 'title'):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return '地点待确认'


def build_route_view(trip: Trip, plan: TripPlan | None) -> dict:
    """Derive city card metadata from saved evidence, without I/O or mutation."""
    view = {'is_city': trip.mode == 'city', **plan_basis(trip, plan),
            'current_lodging': None, 'lodging': None, 'days': []}
    if not view['is_city']:
        return view
    saved = _saved_lodging(plan)
    view['current_lodging'] = _place_view(_current_lodging(trip))
    view['lodging'] = _place_view(saved)
    days = _display_days(plan)
    if not days:
        return view
    evidence = plan.evidence if isinstance(plan.evidence, list) else []
    catalog, ambiguous, blocked = _catalog(evidence)
    # A saved lodging is an explicit confirmed anchor, not another searched stop.
    # Conflicting search metadata cannot overwrite it or validate a changed route.
    route_places = dict(catalog)
    if saved:
        searched = catalog.get(saved.poi_id)
        if saved.poi_id in blocked or searched and _lodging_identity(saved) != _lodging_identity(searched):
            route_places.pop(saved.poi_id, None)
        else:
            route_places[saved.poi_id] = saved
    records = _route_records(evidence, route_places)
    city = _saved_city(plan) or (saved.city if saved else '')
    text = _name(plan.text)
    for day in days:
        items, stops = [], []
        previous, previous_name = saved, saved.name if saved else '原方案住宿待确认'
        for raw in day['items']:
            item = raw if isinstance(raw, dict) else {}
            if item.get('category') == 'transport':
                items.append({'place': None, 'place_status': 'not_applicable',
                    'message': '交通条目保留原文，不作为实体站点。', 'incoming': None})
                continue
            label = _stop_label(item)
            place, status, message = _match(item, catalog, ambiguous, city, text)
            items.append({'place': _place_view(place), 'place_status': status, 'message': message,
                          'incoming': _leg(previous, place, previous_name, label, records)})
            stops.append(label)
            previous, previous_name = place, label
        return_leg = _leg(previous, saved, previous_name, saved.name if saved else '原方案住宿待确认', records) if stops else None
        view['days'].append({'items': items, 'return_leg': return_leg, 'stops': stops})
    return view
