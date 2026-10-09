from __future__ import annotations

from .routes import ROUTE_TOOL_SPEC, lookup_route
from .places import PLACE_TOOL_SPEC, search_places
from .food import FOOD_TOOL_SPEC, search_food
from .weather import WEATHER_TOOL_SPEC, query_weather


TOOL_SPECS = [WEATHER_TOOL_SPEC, FOOD_TOOL_SPEC, PLACE_TOOL_SPEC, ROUTE_TOOL_SPEC]
TOOL_HANDLERS = {
    "search_places": search_places,
    "query_weather": query_weather,
    "search_food": search_food,
}


def tool_specs() -> list[dict]:
    return list(TOOL_SPECS)


def run_tool(name: str, arguments: dict, *, place_catalog=None, route_cache=None) -> dict:
    if name == 'query_weather':
        return query_weather(arguments, cache=route_cache)
    if name == 'query_route':
        return lookup_route(arguments, place_catalog or {}, route_cache if route_cache is not None else {})
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return {"status": "unavailable", "reason": f"未知工具：{name}"}
    return handler(arguments)
