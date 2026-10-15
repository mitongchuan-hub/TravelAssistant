"""Review whether matched trip anchors have actual directional route evidence."""


def critical_route_review(trip, catalog, evidence, remaining):
    """Check arrival/core and core/departure without inventing a route order.

    Only unique exact names are anchors. Ambiguous or unqueried locations do
    not become a guessed POI. Any core attraction can be the first/last stop;
    the agent chooses using its actual itinerary. Failed attempts count as
    attempts, never as successful travel-time evidence.
    """
    if not trip:
        return []
    profile = trip.requirements

    def anchor(name):
        matches = [place for place in catalog.values()
                   if place.get('name', '').strip().casefold() == name.strip().casefold()]
        return matches[0] if name.strip() and len(matches) == 1 else None

    core = [place for name in profile.must_visit if (place := anchor(name))]
    if not core:
        return []
    result = []
    for purpose, origins, destinations in (
        ('抵达接驳', [anchor(profile.arrival_location)], core),
        ('返程接驳', core, [anchor(profile.departure_location)]),
    ):
        origins = [place for place in origins if place]
        destinations = [place for place in destinations if place]
        if not origins or not destinations:
            continue
        pairs = {(origin['poi_id'], destination['poi_id'])
                 for origin in origins for destination in destinations}
        if any(origin == destination for origin, destination in pairs):
            continue
        attempts = [row for row in evidence if row.get('tool') == 'query_route'
                    and (row.get('arguments', {}).get('origin_poi_id'),
                         row.get('arguments', {}).get('destination_poi_id')) in pairs
                    and row.get('arguments', {}).get('mode', 'transit') in {'walking', 'transit', 'driving'}]
        status = 'not_attempted' if remaining > 0 else 'budget_exhausted'
        if attempts:
            status = 'available' if any(row.get('result', {}).get('status') == 'available'
                                        for row in attempts) else 'unavailable'
        result.append({'purpose': purpose, 'status': status,
                       'origins': [{'poi_id': place['poi_id'], 'name': place['name']} for place in origins],
                       'destinations': [{'poi_id': place['poi_id'], 'name': place['name']} for place in destinations]})
    return result
