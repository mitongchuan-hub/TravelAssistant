"""Read normalized business evidence while retaining native MCP results verbatim."""
ROUTE_TOOLS = {
    'maps_direction_walking': 'walking',
    'maps_direction_driving': 'driving',
    'maps_direction_transit_integrated': 'transit',
}
SEARCH_TOOLS = {'maps_text_search', 'maps_around_search'}
PLACE_TOOLS = SEARCH_TOOLS | {'maps_search_detail'}


def successful_detail_place(row):
    """Only server-saved same-ID successful raw detail grants center authority."""
    from app.services.maps.places import normalize_pois
    if not isinstance(row, dict) or row.get('tool') != 'maps_search_detail':
        return None
    args, raw = row.get('arguments'), row.get('result')
    if not isinstance(args, dict) or not isinstance(raw, dict):
        return None
    envelope = raw.get('raw')
    if isinstance(envelope, dict) and (str(envelope.get('status', '1')) != '1'
            or str(envelope.get('infocode', '10000')) != '10000'):
        return None
    poi_id = args.get('id')
    if (not isinstance(poi_id, str) or not poi_id.strip() or raw.get('id') != poi_id
            or raw.get('status') not in (None, '1', 1, 'available') or raw.get('error')):
        return None
    candidates = normalize_pois(raw)['candidates']
    return candidates[0] if candidates else None


def detail_endpoints(row):
    """Deterministic references to a visible same-ID detail, never model coordinates.

    Hash the immutable provider result, not any model-supplied reference field.
    Legacy v3/MCP details remain readable without rewriting saved evidence.
    """
    import hashlib
    import json
    import math
    place = successful_detail_place(row)
    if place is None:
        return []
    raw = row['result']
    navi = raw.get('navi') if isinstance(raw.get('navi'), dict) else raw
    identity = hashlib.sha256(json.dumps({k: v for k, v in raw.items() if k != 'navigation_endpoints'}, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode()).hexdigest()
    values = [('center', f"{place['longitude']},{place['latitude']}")]
    values.extend((kind, navi.get(field)) for kind, field in
                  [('entrance', 'entr_location'), ('exit', 'exit_location')])
    endpoints = []
    for kind, value in values:
        try:
            lon, lat = map(float, value.split(','))
            if not (math.isfinite(lon) and math.isfinite(lat)
                    and -180 <= lon <= 180 and -90 <= lat <= 90):
                continue
        except (AttributeError, TypeError, ValueError):
            continue
        binding = f"{identity}:{place['poi_id']}:{kind}:{lon}:{lat}"
        endpoints.append({'ref': 'amap-endpoint-' + hashlib.sha256(binding.encode()).hexdigest(),
            'poi_id': place['poi_id'], 'kind': kind, 'longitude': lon, 'latitude': lat,
            'evidence_ref': identity, 'name': place['name'],
            'note': '导航参考点；开放、可通行性及交通方式适用性未知。'})
    return endpoints


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
