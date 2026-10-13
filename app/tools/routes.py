"""AMap route estimates between trusted POIs, never model coordinates."""
import math
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from . import amap_mcp

ROUTE_TOOL_SPEC = {'type': 'function', 'function': {
    'name': 'query_route',
    'description': '查询已通过 search_places 获取的两个 POI 之间的步行、公交或驾车预计耗时。只接受真实 POI ID；不查询火车票。未来路况、班次、候车和安检时间不保证，失败保持待核实。',
    'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {
        'origin_poi_id': {'type': 'string'}, 'destination_poi_id': {'type': 'string'},
        'mode': {'type': 'string', 'enum': ['walking', 'transit', 'driving']}},
        'required': ['origin_poi_id', 'destination_poi_id', 'mode']}}}


def _fetch_json(endpoint, params):
    return amap_mcp.route(endpoint, params)


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
            options.append((duration, distance))
        if not options:
            return {**result, 'reason': '没有找到有效路线，耗时待核实；请调整交通方式或地点。'}
        duration, distance = options[0]
        return {**result, 'status': 'available', 'duration_minutes': math.ceil(duration / 60),
                'distance_meters': round(distance) if distance is not None else None,
                'reason': '高德MCP查询时的预计耗时，未来路况与班次可能变化；未额外计候车、安检或场地内部步行。'}
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
