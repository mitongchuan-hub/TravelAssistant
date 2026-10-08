"""AMap POI search. Provider metadata is kept separate from model text."""
import json
import math
import os
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PLACE_TOOL_SPEC = {
    'type': 'function',
    'function': {
        'name': 'search_places',
        'description': '按明确的中国境内城市与地点名称查询景点、餐厅、酒店、车站或机场候选。返回地址、坐标、来源和查询时间。Agent 结合旅行语境匹配候选，无法区分的重名地点通过聊天确认，查到地点不代表已确认营业、开放或预约。区域级推荐不要假装具体门店。',
        'parameters': {'type': 'object', 'additionalProperties': False,
            'properties': {'city': {'type': 'string'}, 'keywords': {'type': 'string'}},
            'required': ['city', 'keywords']},
    },
}


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _fetch_json(params):
    request = Request('https://restapi.amap.com/v3/place/text?' + urlencode(params), headers={'User-Agent': 'TravelAssistant/1.0'})
    with urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode('utf-8'))


def search_places(arguments):
    city, keywords = _text(arguments.get('city')), _text(arguments.get('keywords'))
    if not city or not keywords or len(city) > 100 or len(keywords) > 200:
        return {'status': 'unavailable', 'reason': '请填写具体城市和地点名称。', 'candidates': []}
    key = os.getenv('AMAP_MAP_KEY', '').strip()
    if not key or key == 'your_amap_web_service_key':
        return {'status': 'unavailable', 'reason': '地点查询暂未开通，当前地点仍待核实。', 'candidates': []}
    try:
        payload = _fetch_json({'key': key, 'city': city, 'keywords': keywords, 'citylimit': 'true', 'extensions': 'all', 'offset': 10, 'page': 1})
        if not isinstance(payload, dict) or str(payload.get('status')) != '1' or str(payload.get('infocode')) != '10000':
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
            candidates.append({'poi_id': poi_id, 'name': name, 'address': _text(poi.get('address')),
                'longitude': longitude, 'latitude': latitude, 'city': _text(poi.get('cityname')) or city,
                'district': _text(poi.get('adname')), 'source': '高德地图 POI', 'fetched_at': queried})
        if not candidates:
            return {'status': 'unavailable', 'reason': '没有找到带有效坐标的候选地点，请换一个更具体的名称；当前地点仍待核实。', 'candidates': []}
        return {'status': 'available', 'city': city, 'keywords': keywords, 'candidates': candidates[:10],
                'source': '高德地图 POI', 'note': 'Agent 按旅行语境匹配具体地点，无法区分时通过聊天确认。坐标来自高德；营业、开放和预约情况仍需另行核实。'}
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError):
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
