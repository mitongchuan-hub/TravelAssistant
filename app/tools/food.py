from __future__ import annotations

import json
import math
from urllib.error import HTTPError, URLError
from app.tools import amap_mcp


FOOD_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "search_food",
        "description": (
            "通过高德MCP查询国内城市的真实餐饮候选及详情，"
            "对本次最多5家已获取详情且有评分的候选按评分排序，返回已提供的人均、地址和营业时间。"
            "缺失评分、电话、营业或人均保留未知；不保证全城最高评分、实时营业或忌口安全。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "city": {"type": "string", "description": "国内城市，例如杭州、成都"},
                "keywords": {
                    "type": "string",
                    "description": "菜系、菜品或餐厅特征，例如火锅、烤鸭、本地小吃；省略时搜索餐厅候选，只在本次有评分的详情中排序",
                },
                "max_results": {
                    "type": "integer",
                    "description": "返回门店数量，1 到 5，默认 5",
                },
            },
            "required": ["city"],
        },
    },
}

_MAX_RESULTS = 5


def search_food(arguments: dict) -> dict:
    """Return top-rated dining POIs for a city without raising network errors."""
    city = str(arguments.get("city") or "").strip()
    keywords = str(arguments.get("keywords") or "").strip()
    max_results = _clamp_max_results(arguments.get("max_results"))
    if not city:
        return {"status": "unavailable", "reason": "缺少目的地城市，美食推荐待确认。"}

    if not amap_mcp.configured():
        return {
            "status": "unavailable",
            "city": city,
            "reason": "未配置高德MCP key（AMAP_MAPS_API_KEY 或 AMAP_MAP_KEY），美食推荐待确认。",
        }

    params = {'city': city, 'keywords': keywords or '餐厅', 'max_results': max_results}

    try:
        payload = _fetch_json(params)
    except (amap_mcp.AmapMCPError, ValueError, HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
        return {
            "status": "unavailable",
            "city": city,
            "keywords": keywords or None,
            "reason": "美食服务暂时不可用，门店推荐需要出发前再次确认。",
        }

    if str(payload.get("status", "1")) != "1" or payload.get("infocode", "10000") != "10000":
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
        if _to_float(poi.get('rating') or (poi.get("biz_ext") or {}).get("rating")) is not None
    ]
    rated.sort(key=lambda poi: _to_float(poi.get('rating') or (poi.get("biz_ext") or {}).get("rating")) or 0.0, reverse=True)
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
        "total_matched": payload.get('total_matched', len(candidates)),
        "recommendations": [_summarize_poi(poi) for poi in top_pois],
        "source": "高德地图 POI", "transport": "amap_mcp",
        "note": "评分排序仅针对本次已获取详情的候选，非全城排名。人均为元/人参考，缺失信息待核实；营业、电话、忌口和排队仍需确认。",
    }


def _summarize_poi(poi: dict) -> dict:
    biz_ext = poi.get("biz_ext") or {}
    cost = _to_float(poi.get('cost') or biz_ext.get("cost"))
    return {
        "name": str(poi.get("name") or "").strip(),
        "rating": poi.get('rating') or biz_ext.get("rating") or None,
        "cost_per_person_cny": cost,
        "address": str(poi.get("address") or "").strip() or None,
        "district": str(poi.get("adname") or "").strip() or None,
        "business_area": str(poi.get("business_area") or "").strip() or None,
        "cuisine_tags": str(poi.get("atag") or poi.get("keytag") or "").strip() or None,
        "open_time": str(poi.get('opentime2') or poi.get('open_time') or biz_ext.get("opentime2") or biz_ext.get("open_time") or "").strip() or None,
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
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


def _fetch_json(params: dict) -> dict:
    return amap_mcp.search_pois(params['city'], params['keywords'], params.get('max_results', 5))
