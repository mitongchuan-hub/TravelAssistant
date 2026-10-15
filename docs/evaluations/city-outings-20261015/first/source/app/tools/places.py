"""AMap POI search. Provider metadata is kept separate from model text."""
import math
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from . import amap_mcp

PLACE_TOOL_SPEC = {
    'type': 'function',
    'function': {
        'name': 'search_places',
        'description': '高德MCP搜索明确地点名称或住宿地址，返回候选、详情坐标及上游提供的营业时间。城市可选；未给城市时跨城市搜索并由用户确认重名候选。查到POI不保证旅行当天开放或可订。',
        'parameters': {'type': 'object', 'additionalProperties': False,
            'properties': {'city': {'type': 'string'}, 'keywords': {'type': 'string'}},
            'required': ['keywords']},
    },
}


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _fetch_json(params):
    return amap_mcp.search_pois(params['city'], params['keywords'])


def search_places(arguments):
    city, keywords = _text(arguments.get('city')), _text(arguments.get('keywords'))
    if not keywords or len(city) > 100 or len(keywords) > 300:
        return {'status': 'unavailable', 'reason': '请填写具体地点名称或住宿地址。', 'candidates': []}
    if not amap_mcp.configured():
        return {'status': 'unavailable', 'reason': '地点查询暂未开通，当前地点仍待核实。', 'candidates': []}
    try:
        payload = _fetch_json({'city': city, 'keywords': keywords})
        if not isinstance(payload, dict) or str(payload.get('status', '1')) != '1' or str(payload.get('infocode', '10000')) != '10000':
            return {'status': 'unavailable', 'reason': '地点服务暂时无法提供结果，请稍后再试。', 'candidates': []}
        queried = datetime.now(timezone.utc).isoformat(timespec='seconds')
        candidates, seen = [], set()
        pois = payload.get('pois')
        for poi in (pois if isinstance(pois, list) else []):
            if not isinstance(poi, dict): continue
            poi_id, name = _text(poi.get('id')), _text(poi.get('name'))
            location = _text(poi.get('location')).split(',')
            if not poi_id or not name or poi_id in seen or len(location) != 2: continue
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
        if not candidates:
            return {'status': 'unavailable', 'reason': '没有找到带有效坐标的候选地点，请换一个更具体的名称；当前地点仍待核实。', 'candidates': []}
        return {'status': 'available', 'city': city, 'keywords': keywords, 'candidates': candidates[:10],
                'source': '高德地图 POI', 'transport': 'amap_mcp', 'total_matched': payload.get('total_matched', len(candidates)),
                'note': 'Agent 按旅行语境匹配具体地点，无法区分时通过聊天确认。坐标来自高德MCP详情；opening_hours为查询时提供的参考营业资料，不保证指定日期开放，未提供则未知；票价与预约仍需核实。'}
    except (amap_mcp.AmapMCPError, HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError):
        return {'status': 'unavailable', 'reason': '地点服务暂时不可用，原行程保持不变，请稍后再试。', 'candidates': []}


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
        'position': f'{place.longitude},{place.latitude}', 'name': place.name, 'src': 'TravelAssistant', 'coordinate': 'gaode', 'callnative': 0,
    })
