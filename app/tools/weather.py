from __future__ import annotations

import json
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


WEATHER_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "query_weather",
        "description": "查询旅行目的地在指定日期的天气预报。日期必须使用 YYYY-MM-DD；无法查询时返回待确认原因。",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "city": {"type": "string", "description": "目的地城市，例如杭州"},
                "date": {"type": "string", "description": "日期，格式 YYYY-MM-DD；不确定时可以省略"},
            },
            "required": ["city"],
        },
    },
}

_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_WEATHER_CODES = {
    0: "晴",
    1: "大致晴",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "阵雨",
    81: "中阵雨",
    82: "强阵雨",
    95: "雷雨",
    96: "雷雨伴冰雹",
    99: "强雷雨伴冰雹",
}


def query_weather(arguments: dict) -> dict:
    """Return a small, model-friendly weather result without raising network errors."""
    city = str(arguments.get("city") or "").strip()
    requested_date = str(arguments.get("date") or "").strip()
    if not city:
        return {"status": "unavailable", "reason": "缺少目的地城市，天气待确认。"}

    if requested_date:
        try:
            date.fromisoformat(requested_date)
        except ValueError:
            return {
                "status": "unavailable",
                "city": city,
                "reason": "日期不是 YYYY-MM-DD 格式，天气待确认。",
            }

    try:
        place = _fetch_json(_GEOCODING_URL, {
            "name": city,
            "count": 1,
            "language": "zh",
            "format": "json",
        })
        results = place.get("results") or []
        if not results:
            return {"status": "unavailable", "city": city, "reason": "没有找到对应城市，天气待确认。"}
        location = results[0]
        forecast = _fetch_json(_FORECAST_URL, {
            "latitude": location["latitude"],
            "longitude": location["longitude"],
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto",
            "forecast_days": 16,
        })
    except (KeyError, TypeError, ValueError, HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return {
            "status": "unavailable",
            "city": city,
            "reason": "天气服务暂时不可用，行程中的天气需要出发前再次确认。",
        }

    daily = forecast.get("daily") or {}
    dates = daily.get("time") or []
    if not requested_date:
        preview = []
        for preview_index, preview_date in enumerate(dates[:3]):
            preview_max = _value_at(daily.get("temperature_2m_max"), preview_index)
            preview_min = _value_at(daily.get("temperature_2m_min"), preview_index)
            preview_rain = _value_at(daily.get("precipitation_probability_max"), preview_index)
            preview.append({
                "date": preview_date,
                "condition": _WEATHER_CODES.get(_value_at(daily.get("weather_code"), preview_index), "天气情况待确认"),
                "temperature_max_c": preview_max,
                "temperature_min_c": preview_min,
                "precipitation_probability_max": preview_rain,
                "clothing_advice": _clothing_advice(preview_max, preview_min, preview_rain),
            })
        return {
            "status": "available",
            "city": location.get("name") or city,
            "country": location.get("country") or "",
            "timezone": forecast.get("timezone") or "",
            "forecast_range": [dates[0], dates[-1]] if dates else [],
            "forecast_preview": preview,
            "reason": "未指定具体日期，以上温度和穿衣建议仅供近期参考，请在日期确定后再次查询。",
        }
    if requested_date not in dates:
        return {
            "status": "unavailable",
            "city": location.get("name") or city,
            "requested_date": requested_date,
            "forecast_range": [dates[0], dates[-1]] if dates else [],
            "reason": "日期超出当前预报范围，天气待确认。",
        }

    index = dates.index(requested_date)
    code = _value_at(daily.get("weather_code"), index)
    temperature_max = _value_at(daily.get("temperature_2m_max"), index)
    temperature_min = _value_at(daily.get("temperature_2m_min"), index)
    precipitation_probability = _value_at(daily.get("precipitation_probability_max"), index)
    return {
        "status": "available",
        "city": location.get("name") or city,
        "requested_date": requested_date,
        "timezone": forecast.get("timezone") or "",
        "condition": _WEATHER_CODES.get(code, "天气情况待确认"),
        "temperature_max_c": temperature_max,
        "temperature_min_c": temperature_min,
        "precipitation_probability_max": precipitation_probability,
        "clothing_advice": _clothing_advice(temperature_max, temperature_min, precipitation_probability),
        "source": "Open-Meteo",
    }


def _clothing_advice(temperature_max: object, temperature_min: object, precipitation_probability: object) -> str:
    if isinstance(temperature_max, (int, float)) and temperature_max >= 35:
        advice = "轻薄透气衣物，注意遮阳、补水，减少正午长时间户外活动"
    elif isinstance(temperature_max, (int, float)) and temperature_max >= 28:
        advice = "短袖等轻薄衣物，建议准备遮阳用品"
    elif isinstance(temperature_min, (int, float)) and temperature_min <= 10:
        advice = "保暖外套或羽绒层，早晚注意防寒"
    elif isinstance(temperature_min, (int, float)) and temperature_min <= 18:
        advice = "长袖和薄外套，早晚可能偏凉"
    else:
        advice = "按春秋季分层穿衣，准备一件方便增减的外套"
    if isinstance(precipitation_probability, (int, float)) and precipitation_probability >= 50:
        advice += "；降雨概率较高，带折叠伞或轻便雨衣"
    return advice


def _fetch_json(url: str, params: dict) -> dict:
    request = Request(
        f"{url}?{urlencode(params)}",
        headers={"User-Agent": "TravelAssistant/1.0"},
        method="GET",
    )
    with urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("weather response is not an object")
    return payload


def _value_at(values: object, index: int):
    if isinstance(values, list) and index < len(values):
        return values[index]
    return None
