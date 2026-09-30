from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


FOOD_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "search_food",
        "description": (
            "查询国内城市的美食推荐：按菜系或关键词搜索真实门店，"
            "返回评分最高、人均消费、地址、营业时间和联系电话。"
            "只适用于中国境内城市；无结果或无可用门店评分时返回待确认原因。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "city": {"type": "string", "description": "国内城市，例如杭州、成都"},
                "keywords": {
                    "type": "string",
                    "description": "菜系、菜品或餐厅特征，例如火锅、烤鸭、本地小吃；不确定时可以省略，省略后返回该城市评分靠前的综合餐厅",
                },
                "max_results": {
                    "type": "integer",
                    "description": "返回门店数量，1 到 10，默认 5",
                },
            },
            "required": ["city"],
        },
    },
}

_AMAP_PLACE_TEXT_URL = "https://restapi.amap.com/v3/place/text"
_FETCH_PAGE_SIZE = 20
_MAX_RESULTS = 10


def search_food(arguments: dict) -> dict:
    """Return top-rated dining POIs for a city without raising network errors."""
    city = str(arguments.get("city") or "").strip()
    keywords = str(arguments.get("keywords") or "").strip()
    max_results = _clamp_max_results(arguments.get("max_results"))
    if not city:
        return {"status": "unavailable", "reason": "缺少目的地城市，美食推荐待确认。"}

    amap_key = os.getenv("AMAP_MAP_KEY", "").strip()
    if not amap_key:
        return {
            "status": "unavailable",
            "city": city,
            "reason": "未配置高德地图 key（AMAP_MAP_KEY），美食推荐待确认。",
        }

    params = {
        "key": amap_key,
        "city": city,
        "citylimit": "true",
        "extensions": "all",
        "page_size": _FETCH_PAGE_SIZE,
    }
    if keywords:
        params["keywords"] = keywords
    else:
        params["types"] = "050000"

    try:
        payload = _fetch_json(params)
    except (ValueError, HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
        return {
            "status": "unavailable",
            "city": city,
            "keywords": keywords or None,
            "reason": "美食服务暂时不可用，门店推荐需要出发前再次确认。",
        }

    if str(payload.get("status")) != "1" or payload.get("infocode") != "10000":
        return {
            "status": "unavailable",
            "city": city,
            "keywords": keywords or None,
            "reason": f"高德地图查询失败（{payload.get('info') or '未知错误'}），美食推荐待确认。",
        }

    candidates = [
        poi for poi in (payload.get("pois") or [])
        if isinstance(poi, dict) and str(poi.get("name") or "").strip()
    ]
    rated = [
        poi for poi in candidates
        if _to_float((poi.get("biz_ext") or {}).get("rating")) is not None
    ]
    rated.sort(key=lambda poi: _to_float((poi.get("biz_ext") or {}).get("rating")) or 0.0, reverse=True)
    top_pois = rated[:max_results]

    if not top_pois:
        return {
            "status": "unavailable",
            "city": city,
            "keywords": keywords or None,
            "total_matched": len(candidates),
            "reason": "该城市下没有找到带评分的门店，美食推荐基于通用知识，具体门店建议出发前核实。",
        }

    return {
        "status": "available",
        "city": city,
        "keywords": keywords or None,
        "total_matched": len(candidates),
        "recommendations": [_summarize_poi(poi) for poi in top_pois],
        "source": "高德地图 POI",
        "note": "评分来自高德用户评价，人均单位为元/人；热门门店建议提前电话或在线确认排队情况。",
    }


def _summarize_poi(poi: dict) -> dict:
    biz_ext = poi.get("biz_ext") or {}
    cost = _to_float(biz_ext.get("cost"))
    return {
        "name": str(poi.get("name") or "").strip(),
        "rating": biz_ext.get("rating") or None,
        "cost_per_person_cny": cost,
        "address": str(poi.get("address") or "").strip() or None,
        "district": str(poi.get("adname") or "").strip() or None,
        "business_area": str(poi.get("business_area") or "").strip() or None,
        "cuisine_tags": str(poi.get("atag") or poi.get("keytag") or "").strip() or None,
        "open_time": str(biz_ext.get("opentime2") or biz_ext.get("open_time") or "").strip() or None,
        "tel": str(poi.get("tel") or "").strip() or None,
    }


def _clamp_max_results(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 5
    return max(1, min(number, _MAX_RESULTS))


def _to_float(value: object):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_json(params: dict) -> dict:
    request = Request(
        f"{_AMAP_PLACE_TEXT_URL}?{urlencode(params)}",
        headers={"User-Agent": "TravelAssistant/1.0"},
        method="GET",
    )
    with urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("amap response is not an object")
    return payload
