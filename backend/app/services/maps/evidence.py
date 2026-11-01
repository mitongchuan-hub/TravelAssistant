"""Read normalized business evidence while retaining native MCP results verbatim."""
ROUTE_TOOLS = {
    'maps_direction_walking': 'walking',
    'maps_direction_driving': 'driving',
    'maps_direction_transit_integrated': 'transit',
}
PLACE_TOOLS = {'maps_text_search', 'maps_search_detail'}


def evidence_kind(row):
    tool = row.get('tool')
    if tool in ROUTE_TOOLS:
        return 'query_route'
    if tool in PLACE_TOOLS:
        return 'search_places'
    return tool  # Saved evidence from previous versions remains readable.


def evidence_result(row):
    return row.get('normalized', row.get('result', {}))


def place_messages(evidence):
    import json
    return [{'role': 'tool', 'name': 'search_places',
             'content': json.dumps(evidence_result(row), ensure_ascii=False)}
            for row in evidence if isinstance(row, dict) and evidence_kind(row) == 'search_places'
            and isinstance(evidence_result(row), dict)]
