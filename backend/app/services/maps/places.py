"""Shared POI parsing and compatibility page search."""
import math
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from app.services.maps import amap_mcp


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def normalize_pois(payload, city=''):
    """Only provider coordinates can create a routable place; search summaries may lack them."""
    payload = payload if isinstance(payload, dict) else {}
    queried = datetime.now(timezone.utc).isoformat(timespec='seconds')
    records = payload.get('pois', []) if isinstance(payload, dict) else []
    if isinstance(payload, dict) and payload.get('id'):
        records = [payload]
    candidates, seen = [], set()
    for poi in records if isinstance(records, list) else []:
        if not isinstance(poi, dict):
            continue
        poi_id, name = _text(poi.get('id')), _text(poi.get('name'))
        location = _text(poi.get('location')).split(',')
        if not poi_id or not name or poi_id in seen or len(location) != 2:
            continue
        try:
            longitude, latitude = map(float, location)
            if not math.isfinite(longitude) or not math.isfinite(latitude) or not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
                continue
        except ValueError:
            continue
        seen.add(poi_id)
        candidate = {'poi_id': poi_id, 'name': name, 'address': _text(poi.get('address')),
            'longitude': longitude, 'latitude': latitude, 'city': _text(poi.get('city') or poi.get('cityname')) or city,
            'district': _text(poi.get('adname')), 'source': '高德地图 POI', 'fetched_at': queried}
        if _text(poi.get('type')):
            candidate['place_type'] = _text(poi['type'])
        hours = _text(poi.get('opentime2')) or _text(poi.get('open_time'))
        if hours:
            candidate['opening_hours'] = hours
        candidates.append(candidate)
    return {'status': 'available' if candidates else 'unavailable', 'city': city,
            'candidates': candidates[:10], 'total_matched': payload.get('total_matched', len(candidates)),
            'source': '高德地图 POI', 'transport': 'amap_mcp',
            **{key: _text(payload[key]) for key in ('rating', 'cost')
               if payload.get('id') and _text(payload.get(key))}}


def search_places(arguments):
    """Legacy settings endpoint: search and fetch details without an LLM round trip."""
    city, keywords = _text(arguments.get('city')), _text(arguments.get('keywords'))
    unavailable = {'status': 'unavailable', 'candidates': []}
    if not keywords or len(city) > 100 or len(keywords) > 300:
        return {**unavailable, 'reason': '请填写具体地点名称或住宿地址。'}
    if not amap_mcp.configured():
        return {**unavailable, 'reason': '地点查询暂未开通，当前地点仍待核实。'}
    try:
        payload = amap_mcp.search_pois(city, keywords)
        if not isinstance(payload, dict) or str(payload.get('status', '1')) != '1' or str(payload.get('infocode', '10000')) != '10000':
            return {**unavailable, 'reason': '地点服务暂时无法提供结果，请稍后再试。'}
        result = normalize_pois(payload, city)
        if not result['candidates']:
            return {**result, 'reason': '没有找到带有效坐标的候选地点，请换一个更具体的名称；当前地点仍待核实。'}
        return {**result, 'keywords': keywords,
                'note': '请核对并选择具体地点，重名不自动猜测。地点匹配不保证未来开放、票价或预约。'}
    except (amap_mcp.AmapMCPError, TimeoutError, OSError, ValueError, TypeError):
        return {**unavailable, 'reason': '地点服务暂时不可用，原行程保持不变，请稍后再试。'}


def query_time_label(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return '时间待核实'
        local = parsed.astimezone(timezone(timedelta(hours=8)))
        return f'{local.year}年{local.month}月{local.day}日 {local:%H:%M}（北京时间）'
    except (ValueError, TypeError):
        return '时间待核实'


def map_url(place):
    return 'https://uri.amap.com/marker?' + urlencode({
        'position': f'{place.longitude},{place.latitude}', 'name': place.name,
        'src': 'TravelAssistant', 'coordinate': 'gaode', 'callnative': 0})
