"""Route hydration and clock checks in the Agent planning pipeline."""
import json
import re
from copy import deepcopy
from dataclasses import asdict

from .models import RouteDetails
from .places import collect_places, resolve_place
from .tools.places import search_places
from .tools.routes import lookup_route

MODE_LABELS = {'walking': '步行', 'transit': '公交', 'driving': '驾车'}


def seed_places(plan):
    catalog = {}
    if plan:
        for day in plan.days:
            for item in day.items:
                if item.place:
                    candidate = asdict(item.place)
                    candidate.pop('selected_at', None)
                    catalog[candidate['poi_id']] = candidate
    return catalog


def merge_target(raw, existing, target_id):
    if not target_id or not existing:
        return raw
    from .plan_validation import PlanValidationError
    matches = [item for day in raw['days'] for item in day['items'] if item.get('id') == target_id]
    if len(matches) != 1:
        raise PlanValidationError('模型未准确定位单项调整目标，必须保留原条目 ID')
    merged = asdict(existing)
    for day in merged['days']:
        day['items'] = [deepcopy(matches[0]) if item['id'] == target_id else item for item in day['items']]
    return merged


def _endpoint(name, city, catalog, cache):
    if not name or not city:
        return None
    key = ('endpoint', name, city)
    if key not in cache:
        budget = cache.get('_budget')
        if budget is not None:
            budget.external()
        result = search_places({'city': city, 'keywords': name})
        found = collect_places([{'role': 'tool', 'name': 'search_places', 'content': json.dumps(result)}])
        exact = [candidate for candidate in found.values()
                 if candidate['name'].strip() == name.strip()
                 and candidate['city'].removesuffix('市') == city.removesuffix('市')]
        cache[key] = exact[0] if len(exact) == 1 else None
        catalog.update(found)
    return cache[key]


def _pending(origin, destination, mode, reason):
    return asdict(RouteDetails(origin_name=origin, destination_name=destination, mode=mode, reason=reason))


def _leg(origin, destination, mode, catalog, cache, origin_name='', destination_name=''):
    if not origin or not destination:
        return _pending(origin_name or (origin['name'] if origin else '地点待确认'),
                        destination_name or (destination['name'] if destination else '地点待确认'), mode,
                        '具体地点未匹配，交通耗时待核实；需要时由 Agent 在聊天中确认。')
    result = lookup_route({'origin_poi_id': origin['poi_id'], 'destination_poi_id': destination['poi_id'], 'mode': mode}, catalog, cache)
    return {**asdict(RouteDetails(origin_poi_id=origin['poi_id'], origin_name=origin['name'],
                                   destination_poi_id=destination['poi_id'], destination_name=destination['name'], mode=mode)), **result}


def _travel_mode(items, fallback):
    modes = [item.get('travel_mode', 'transit') for item in items if item.get('category') == 'transport']
    return next((mode for mode in modes if mode in ('transit', 'driving')), modes[0] if modes else fallback)


def prepare_routes(raw, trip, catalog, cache, existing=None):
    """Only tool-derived metadata enters raw; model route fields are overwritten."""
    old = {item.id: item for day in existing.days for item in day.items} if existing else {}
    pending = [text for text in raw.get('pending_items', []) if not text.startswith('路线待核实：')]
    overnight = None
    for day_index, day in enumerate(raw['days']):
        stops = []
        positions = {}
        for position, item in enumerate(day['items']):
            positions[id(item)] = position
            item.pop('incoming_route', None)
            item.pop('departure_route', None)
            previous = old.get(item.get('id'))
            place = resolve_place(item, catalog)
            if not place and previous and item.get('place_scope') != 'area' and all(item.get(k) == getattr(previous, k) for k in ('title', 'location', 'category')):
                place = previous.place
            if place:
                item['poi_id'] = place.poi_id
            else:
                item['poi_id'] = ''
            mode = item.get('travel_mode', 'transit')
            item['travel_mode'] = mode if mode in MODE_LABELS else 'transit'
            if item.get('category') == 'rest' and item.get('place_scope') != 'area' and not place:
                start, end = _window(item.get('time'))
                if start is None or end is None:
                    pending.append('路线待核实：休息时段不完整，尚不能完整判断相邻地点的交通时间窗口。')
            if item.get('category') != 'transport' and (item.get('category') != 'rest' or place or item.get('place_scope') == 'area'):
                stops.append((item, catalog.get(place.poi_id) if place else None))
        for (previous, origin), (item, destination) in zip(stops, stops[1:]):
            item['travel_mode'] = _travel_mode(day['items'][positions[id(previous)] + 1:positions[id(item)]], item['travel_mode'])
            item['incoming_route'] = _leg(origin, destination, item['travel_mode'], catalog, cache,
                                          previous['location'], item['location'])
        if stops and day_index == 0 and trip:
            item, destination = stops[0]
            item['travel_mode'] = _travel_mode(day['items'][:positions[id(item)]], item['travel_mode'])
            origin = _endpoint(trip.requirements.arrival_location, trip.requirements.city, catalog, cache)
            item['incoming_route'] = _leg(origin, destination, item['travel_mode'], catalog, cache,
                                          trip.requirements.arrival_location or '抵达地点待确认', item['location'])
        elif stops and day_index > 0:
            item, destination = stops[0]
            item['travel_mode'] = _travel_mode(day['items'][:positions[id(item)]], item['travel_mode'])
            item['incoming_route'] = _leg(overnight, destination, item['travel_mode'], catalog, cache,
                                          overnight['name'] if overnight else '当日出发地点待确认', item['location'])
        if stops and day_index == len(raw['days']) - 1 and trip:
            item, origin = stops[-1]
            destination = _endpoint(trip.requirements.departure_location, trip.requirements.city, catalog, cache)
            mode = _travel_mode(day['items'][positions[id(item)] + 1:], item['travel_mode'])
            item['departure_route'] = _leg(origin, destination, mode, catalog, cache,
                                           item['location'], trip.requirements.departure_location or '离开地点待确认')
        for stop_index, (item, _) in enumerate(stops):
            start, end = _window(item.get('time'))
            incoming = item.get('incoming_route')
            if incoming and incoming['status'] == 'available':
                if stop_index:
                    previous_end = _window(stops[stop_index - 1][0].get('time'))[1]
                elif day_index == 0 and trip:
                    previous_end = _window(trip.requirements.arrival_time)[0]
                else:
                    starts = [_window(value.get('time'))[0] for value in day['items'][:positions[id(item)]] if value.get('category') == 'transport']
                    previous_end = min((value for value in starts if value is not None), default=None)
                if start is None or previous_end is None:
                    pending.append(f'路线待核实：到「{item["title"]}」的交通已有估算，但活动开始/上一站结束或抵达时间不完整，尚不能判断能否按时到达。')
            if item.get('departure_route', {}).get('status') == 'available' and (end is None or not trip or not trip.requirements.departure_time):
                pending.append(f'路线待核实：最后一站结束或离开时刻不完整，尚不能判断离城接驳是否来得及。')
            for field in ('incoming_route', 'departure_route'):
                route = item.get(field)
                if not route: continue
                if route['status'] != 'available':
                    pending.append(f"路线待核实：{route['origin_name']} → {route['destination_name']}。{route['reason']}")
        accommodation_ids = {item.get('poi_id') for item in day['items'] if item.get('category') == 'accommodation' and item.get('poi_id')}
        overnight = stops[-1][1] if stops and stops[-1][1] and stops[-1][1]['poi_id'] in accommodation_ids else None
    raw['pending_items'] = list(dict.fromkeys(pending))
    risks = list(raw.get('risks', []))
    partial = '部分地点、交通路段或时间尚待核实，当前行程尚未完成全部路线可行性检查。'
    risks = [risk for risk in risks if risk != partial]
    if any(text.startswith('路线待核实：') for text in pending):
        risks.append(partial)
    raw['risks'] = risks
    return raw


def _window(value):
    match = re.fullmatch(r'(\d{1,2}:\d{2})(?:\s*[-—–~～至]\s*(\d{1,2}:\d{2}))?', str(value or '').strip().replace('：', ':'))
    if not match: return None, None
    def clock(text):
        hour, minute = map(int, text.split(':'))
        return hour * 60 + minute if 0 <= hour <= 23 and 0 <= minute <= 59 else None
    return clock(match[1]), clock(match[2]) if match[2] else None


def route_time_errors(raw, trip=None):
    errors = []
    for number, day in enumerate(raw.get('days', []), 1):
        previous = None
        blocked = 0
        transport_minutes = 0
        transport_present = False
        transport_known = True
        transport_start_bound = None
        for item in day['items']:
            if item.get('category') == 'transport':
                transport_present = True
                transport_start, transport_end = _window(item.get('time'))
                if transport_start is not None:
                    transport_start_bound = transport_start if transport_start_bound is None else min(transport_start_bound, transport_start)
                if transport_start is not None and transport_end is not None:
                    transport_minutes += max(0, transport_end - transport_start)
                else:
                    transport_known = False
                continue
            if item.get('category') == 'rest' and item.get('place_scope') != 'area' and not item.get('poi_id') and not item.get('place'):
                if item.get('category') == 'rest':
                    start, end = _window(item.get('time'))
                    if start is not None and end is not None: blocked += max(0, end - start)
                continue
            route = item.get('incoming_route')
            start, end = _window(item.get('time'))
            if route and route.get('status') == 'available' and route.get('duration_minutes') is not None:
                bound = None
                if previous:
                    _, bound = _window(previous.get('time'))
                    expected_origin = previous.get('poi_id') or (previous.get('place') or {}).get('poi_id')
                    if expected_origin != route.get('origin_poi_id'):
                        errors.append(f'第 {number} 天「{item["title"]}」路线起点与前一地点不一致')
                elif trip and number == 1:
                    bound, _ = _window(trip.requirements.arrival_time)
                elif number > 1:
                    bound = transport_start_bound
                expected_destination = item.get('poi_id') or (item.get('place') or {}).get('poi_id')
                if expected_destination != route.get('destination_poi_id'):
                    errors.append(f'第 {number} 天「{item["title"]}」路线终点与安排地点不一致')
                if transport_present and transport_known and transport_minutes < route['duration_minutes']:
                    errors.append(f'第 {number} 天前往「{item["title"]}」交通条目仅预留 {transport_minutes} 分钟，真实路线预计 {route["duration_minutes"]} 分钟')
                if bound is not None and start is not None and start - bound - blocked < route['duration_minutes']:
                    errors.append(f'第 {number} 天到「{item["title"]}」{MODE_LABELS.get(route["mode"], route["mode"])}预计 {route["duration_minutes"]} 分钟，当前可用时间 {start - bound - blocked} 分钟；请调整时间、地点或交通方式')
            previous, blocked = item, 0
            transport_minutes, transport_present, transport_known = 0, False, True
            transport_start_bound = None
            departure = item.get('departure_route')
            if departure and departure.get('status') == 'available' and trip and number == len(raw['days']):
                expected_origin = item.get('poi_id') or (item.get('place') or {}).get('poi_id')
                if expected_origin != departure.get('origin_poi_id'):
                    errors.append('离城路线起点与最后一站不一致')
                if trip.requirements.departure_location and departure.get('destination_name') != trip.requirements.departure_location.strip():
                    errors.append('离城路线终点与当前离开地点不一致')
                deadline, _ = _window(trip.requirements.departure_time)
                item_index = next(i for i, value in enumerate(day['items']) if value is item)
                tail = [value for value in day['items'][item_index + 1:] if value.get('category') == 'transport']
                windows = [_window(value.get('time')) for value in tail]
                if windows and all(start is not None and finish is not None for start, finish in windows):
                    allocated = sum(max(0, finish - start) for start, finish in windows)
                    if allocated < departure['duration_minutes']:
                        errors.append(f'离城交通条目仅预留 {allocated} 分钟，真实路线预计 {departure["duration_minutes"]} 分钟')
                tail_rest = [_window(value.get('time')) for value in day['items'][item_index + 1:] if value.get('category') == 'rest']
                rest_minutes = sum(max(0, finish - start) for start, finish in tail_rest if start is not None and finish is not None)
                if end is not None and deadline is not None and deadline - end - rest_minutes < departure['duration_minutes']:
                    errors.append(f'最后一站「{item["title"]}」到离开地点预计 {departure["duration_minutes"]} 分钟，距离离开仅有 {deadline - end - rest_minutes} 分钟')
    return errors


def route_from_raw(raw):
    return RouteDetails(**raw) if isinstance(raw, dict) else None
