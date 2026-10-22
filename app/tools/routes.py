"""AMap route estimates between trusted POIs, never model coordinates."""
import math
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from app.tools import amap_mcp

ROUTE_TOOL_SPEC = {'type': 'function', 'function': {
    'name': 'query_route',
    'description': '查询两个可信 POI 之间的步行、公共交通或驾车路线。公共交通返回所选方案的 transit_steps，保留地铁/公交线路、上下车站、可用方向、步行和换乘顺序；同一乘车段的 lines 为可选线路，不是连续换乘。transit_detail_status 为 available/partial/missing；missing 时只有总耗时和距离，不能当作完整公交线路。只接受真实 POI ID，不查询火车票。未来路况、班次和候车不保证，缺少的明细保持待核实。',
    'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {
        'origin_poi_id': {'type': 'string'}, 'destination_poi_id': {'type': 'string'},
        'mode': {'type': 'string', 'enum': ['walking', 'transit', 'driving']}},
        'required': ['origin_poi_id', 'destination_poi_id', 'mode']}}}


def _fetch_json(endpoint, params):
    return amap_mcp.route(endpoint, params)


def _text(value):
    if isinstance(value, str):
        return value.strip() or None
    return None


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _minutes(value):
    number = _number(value)
    return math.ceil(number / 60) if number is not None else None


def _stop(value):
    return _text(value.get('name')) if isinstance(value, dict) else None


def _has_data(value):
    """Provider placeholders may be nested objects containing only empty fields."""
    if isinstance(value, dict):
        return any(_has_data(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_data(item) for item in value)
    if isinstance(value, str):
        return value.strip() not in {'', '0'}
    return bool(value)


def _transit_steps(path):
    """Keep provider order and alternatives; never infer a missing station or direction."""
    segments = path.get('segments')
    steps = []
    for segment in segments if isinstance(segments, list) else []:
        if not isinstance(segment, dict):
            steps.append({'kind': 'unknown'})
            continue
        walking = segment.get('walking')
        if isinstance(walking, dict):
            raw_steps = walking.get('steps')
            instructions = [_text(step.get('instruction')) for step in
                            (raw_steps if isinstance(raw_steps, list) else []) if isinstance(step, dict)]
            instructions = [text for text in instructions if text]
            distance, duration = _number(walking.get('distance')), _minutes(walking.get('duration'))
            if instructions or distance or duration:
                steps.append({'kind': 'walking', 'instructions': instructions,
                              'distance_meters': distance, 'duration_minutes': duration})
            elif _has_data(walking):
                steps.append({'kind': 'unknown'})
        elif _has_data(walking):
            steps.append({'kind': 'unknown'})
        bus = segment.get('bus')
        raw_lines = bus.get('buslines') if isinstance(bus, dict) else None
        lines = []
        for line in raw_lines if isinstance(raw_lines, list) else []:
            if not isinstance(line, dict) or not _text(line.get('name')):
                continue
            count = _number(line.get('via_num'))
            direction = _text(line.get('direction'))
            # Some providers encode a binary direction flag rather than a destination.
            if direction in {'0', '1'}:
                direction = None
            lines.append({'name': _text(line['name']),
                          'departure_stop': _stop(line.get('departure_stop')),
                          'arrival_stop': _stop(line.get('arrival_stop')),
                          'direction': direction,
                          'via_stop_count': int(count) if count is not None and count.is_integer() else None,
                          'duration_minutes': _minutes(line.get('duration'))})
        if lines:
            steps.append({'kind': 'ride', 'lines': lines,
                          'entrance': _stop(segment.get('entrance')),
                          'exit': _stop(segment.get('exit'))})
        elif _has_data(bus):
            steps.append({'kind': 'unknown'})
        if any(_has_data(segment.get(key)) for key in ('railway', 'taxi')):
            steps.append({'kind': 'unknown'})
    return steps


def transit_detail_status(steps):
    """Only a ride with boarding and alighting stops is a usable transit line."""
    if not isinstance(steps, list) or not steps:
        return 'missing'
    for step in steps:
        if not isinstance(step, dict) or step.get('kind') != 'ride':
            continue
        for line in step.get('lines') or []:
            if (isinstance(line, dict) and line.get('name')
                    and line.get('departure_stop') and line.get('arrival_stop')):
                return 'available'
    return 'partial'


def query_route(origin, destination, mode='transit'):
    result = {'origin_poi_id': origin['poi_id'], 'origin_name': origin['name'],
              'destination_poi_id': destination['poi_id'], 'destination_name': destination['name'],
              'mode': mode, 'status': 'unavailable', 'duration_minutes': None, 'distance_meters': None,
              'source': '高德路线规划', 'fetched_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'reason': '路线待核实。'}
    if mode not in ('walking', 'transit', 'driving'):
        return {**result, 'reason': '交通方式待确认。'}
    for place in (origin, destination):
        try:
            lon, lat = float(place['longitude']), float(place['latitude'])
            if not math.isfinite(lon) or not math.isfinite(lat) or not -180 <= lon <= 180 or not -90 <= lat <= 90:
                raise ValueError()
        except (TypeError, ValueError, KeyError):
            return {**result, 'reason': '地点坐标待核实。'}
    if origin['poi_id'] == destination['poi_id']:
        return {**result, 'status': 'available', 'duration_minutes': 0, 'distance_meters': 0,
                'source': '同一已匹配地点', 'reason': '同一地点，未计场地内部步行。'}
    if not amap_mcp.configured():
        return {**result, 'reason': '路线查询暂未开通，交通耗时待核实。'}
    params = {'origin': f"{origin['longitude']},{origin['latitude']}",
              'destination': f"{destination['longitude']},{destination['latitude']}"}
    endpoint = {'walking': 'walking', 'driving': 'driving', 'transit': 'transit/integrated'}[mode]
    if mode == 'transit':
        if not origin.get('city') or not destination.get('city'):
            return {**result, 'reason': '公交查询需要已匹配地点的城市。'}
        params.update(city=origin['city'], cityd=destination['city'], strategy=0)
    try:
        payload = _fetch_json(endpoint, params)
        if not isinstance(payload, dict) or str(payload.get('status', '1')) != '1' or str(payload.get('infocode', '10000')) != '10000':
            return {**result, 'reason': '路线服务权限、配额或请求暂不可用，耗时待核实。'}
        route = payload.get('route')
        paths = route.get('transits' if mode == 'transit' else 'paths', []) if isinstance(route, dict) else []
        options = []
        for path in paths if isinstance(paths, list) else []:
            if not isinstance(path, dict): continue
            try:
                duration = float(path.get('duration'))
                if not math.isfinite(duration) or duration <= 0: continue
                distance_value = path.get('distance')
                if distance_value in (None, '', []):
                    distance_value = route.get('distance')
                distance = float(distance_value) if distance_value not in (None, '', []) else None
                if distance is not None and (not math.isfinite(distance) or distance < 0): continue
            except (ValueError, TypeError):
                continue
            options.append((duration, distance, path))
        if not options:
            return {**result, 'reason': '没有找到有效路线，耗时待核实；请调整交通方式或地点。'}
        parsed = []
        for duration_value, distance_value, path in options:
            steps = _transit_steps(path) if mode == 'transit' else []
            parsed.append((duration_value, distance_value, steps))
        if mode == 'transit':
            # Prefer the provider's first plan that carries usable line details;
            # an estimate without stops must not hide a later complete plan.
            selected = (next((item for item in parsed if transit_detail_status(item[2]) == 'available'), None)
                        or next((item for item in parsed if transit_detail_status(item[2]) == 'partial'), None)
                        or parsed[0])
        else:
            selected = parsed[0]
        duration, distance, transit_steps = selected
        result.update(status='available', duration_minutes=math.ceil(duration / 60),
                      distance_meters=round(distance) if distance is not None else None,
                      reason='高德MCP查询时的预计耗时，未来路况与班次可能变化；未额外计候车、安检或场地内部步行。')
        if mode == 'transit':
            detail_status = transit_detail_status(transit_steps)
            result['transit_steps'] = transit_steps
            result['transit_detail_status'] = detail_status
            if detail_status == 'missing':
                result['reason'] = (result['reason'].rstrip('。')
                    + '；线路明细未返回，无法给出具体线路、站点和换乘，仅可参考总耗时和距离。')
            elif detail_status == 'partial':
                result['reason'] = (result['reason'].rstrip('。')
                    + '；部分线路字段缺失，已返回内容仍待核实。')
        return result
    except (amap_mcp.AmapMCPError, HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError):
        return {**result, 'reason': '路线服务暂不可用，交通耗时仍待核实。'}


def lookup_route(arguments, catalog, cache):
    origin = catalog.get(arguments.get('origin_poi_id'))
    destination = catalog.get(arguments.get('destination_poi_id'))
    mode = arguments.get('mode', 'transit')
    if not origin or not destination:
        return {'status': 'unavailable', 'reason': '请先查询并匹配两个具体地点，不接受模型自报坐标。'}
    key = (origin['poi_id'], destination['poi_id'], origin['longitude'], origin['latitude'],
           destination['longitude'], destination['latitude'], mode)
    if key not in cache:
        if sum(isinstance(k, tuple) and len(k) == 7 for k in cache) >= 32:
            return {'status': 'unavailable', 'reason': '本轮路线查询数量已达上限，剩余路段待核实。'}
        budget = cache.get('_budget')
        if budget is not None and origin['poi_id'] != destination['poi_id']:
            budget.external()
        budget = cache.get('_budget')
        with amap_mcp.budget_scope(budget):
            cache[key] = query_route(origin, destination, mode)
    return cache[key]
