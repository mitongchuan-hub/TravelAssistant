"""Bind Agent-selected POI IDs to real tool results; reject model coordinates."""
from datetime import datetime, timezone
import math

from app.models import PlaceDetails


def collect_places(tool_messages):
    import json
    catalog = {}
    for message in tool_messages:
        if message.get('role') != 'tool' or message.get('name') != 'search_places':
            continue
        try:
            result = json.loads(message.get('content', '{}'))
        except (ValueError, TypeError):
            continue
        if not isinstance(result, dict) or result.get('status') != 'available':
            continue
        for candidate in result.get('candidates', []):
            if not isinstance(candidate, dict): continue
            try:
                place = PlaceDetails(**candidate)
                if not place.poi_id or not place.name or place.source != '高德地图 POI': continue
                if not math.isfinite(place.longitude) or not math.isfinite(place.latitude): continue
                if not -180 <= place.longitude <= 180 or not -90 <= place.latitude <= 90: continue
            except (ValueError, TypeError):
                continue
            catalog[place.poi_id] = candidate
    return catalog


def resolve_place(raw_item, catalog):
    if raw_item.get('place_scope') == 'area':
        return None
    poi_id = raw_item.get('poi_id')
    candidate = (catalog or {}).get(poi_id) if isinstance(poi_id, str) else None
    if not candidate:
        return None
    try:
        return PlaceDetails(**candidate, selected_at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
    except (ValueError, TypeError):
        return None
