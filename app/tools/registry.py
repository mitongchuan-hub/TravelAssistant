from __future__ import annotations

from .food import FOOD_TOOL_SPEC, search_food
from .weather import WEATHER_TOOL_SPEC, query_weather


TOOL_SPECS = [WEATHER_TOOL_SPEC, FOOD_TOOL_SPEC]
TOOL_HANDLERS = {
    "query_weather": query_weather,
    "search_food": search_food,
}


def tool_specs() -> list[dict]:
    return list(TOOL_SPECS)


def run_tool(name: str, arguments: dict) -> dict:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return {"status": "unavailable", "reason": f"未知工具：{name}"}
    return handler(arguments)
