"""Pure schema-nine conversion; historical SQL task payloads are never inputs."""
from copy import deepcopy

from app.workflow.counters import normalize_workflow

# Frozen source contracts: never infer old completeness from the current model.
V8_KEYS = {'start_date', 'end_date', 'city', 'day_count', 'travelers', 'budget_amount',
           'budget_scope', 'budget_coverage', 'transport_mode', 'must_visit', 'avoid',
           'dietary', 'pace', 'lodging_address', 'outing_start_time', 'outing_end_time', 'revision'}
V7_REMOVED = {'arrival_time', 'departure_time', 'arrival_location', 'departure_location'}
V7_KEYS = V8_KEYS | V7_REMOVED
OPTIONAL_FIELDS = {'travelers', 'budget_amount', 'budget_scope', 'budget_coverage',
                   'must_visit', 'avoid', 'dietary', 'pace', 'outing_start_time', 'outing_end_time'}
REMOVED = OPTIONAL_FIELDS | V7_REMOVED


def migrate_trip(raw):
    raw = deepcopy(raw)
    metadata = raw.get('legacy_metadata') or {}
    old_date = raw.pop('date_range', metadata.get('summaries', {}).get('date_range', ''))
    fallback = metadata.get('fallback', {}).get('date_range', bool(old_date))
    # Optional archives and opaque workflow archives have no current authority.
    raw['legacy_metadata'] = ({'summaries': {'date_range': old_date},
                               'fallback': {'date_range': fallback}} if old_date else {})
    for key in ('budget', 'style', 'note'):
        raw.pop(key, None)
    requirements = raw.get('requirements')
    if not isinstance(requirements, dict):
        requirements = {}
        raw['requirements'] = requirements
    for key in REMOVED:
        requirements.pop(key, None)
    raw['workflow_state'] = normalize_workflow(raw.get('workflow_state'))
    return raw


def migrate_plan(raw, requirements=None, source_version=8):
    raw = deepcopy(raw)
    snapshot = raw.get('requirements_snapshot')
    source_keys = V7_KEYS if source_version < 8 else V8_KEYS
    complete = isinstance(snapshot, dict) and set(snapshot) == source_keys
    raw['requirements_basis_complete'] = bool(raw.get('requirements_basis_complete', complete) and complete)
    # A removed mismatch was stale before migration and must remain so afterwards.
    removed_stale = isinstance(snapshot, dict) and isinstance(requirements, dict) and any(
        key in snapshot and (snapshot[key] != requirements.get(key, '')
                             or type(snapshot[key]) is not type(requirements.get(key, '')))
        for key in REMOVED)
    raw['requirements_legacy_stale'] = bool(raw.get('requirements_legacy_stale', False) or removed_stale)
    if isinstance(snapshot, dict):
        for key in REMOVED:
            snapshot.pop(key, None)
    return raw


def migrate_snapshot(snapshot):
    version = int(snapshot.get('schema_version', 1))
    if version >= 10:
        return deepcopy(snapshot)
    result = deepcopy(snapshot)
    if version < 9:
        result['trips'] = {key: migrate_trip(raw) for key, raw in result.get('trips', {}).items()}
        result['plans'] = {key: migrate_plan(raw, snapshot.get('trips', {}).get(key, {}).get('requirements'), version)
                           for key, raw in result.get('plans', {}).items()}
    for trip_id, plan in result.get('plans', {}).items():
        basis = plan.get('requirements_snapshot')
        current = result.get('trips', {}).get(trip_id, {}).get('requirements', {})
        old_keys = {'start_date', 'end_date', 'city', 'day_count', 'transport_mode', 'lodging_address', 'revision'}
        if (isinstance(basis, dict) and set(basis) == old_keys
                and plan.get('requirements_basis_complete', True)
                and type(plan.get('requirements_revision')) is int
                and type(basis.get('revision')) is int
                and basis['revision'] == plan['requirements_revision']
                and (plan['requirements_revision'] != current.get('revision')
                     or any(basis[key] != current.get(key) or type(basis[key]) is not type(current.get(key))
                            for key in old_keys))):
            plan['requirements_legacy_stale'] = True
    cards_by_trip = result.pop('idea_cards', {})
    for trip_id, trip in result.get('trips', {}).items():
        requirements = trip.setdefault('requirements', {})
        parts = [requirements.get('text', '')]
        for card in cards_by_trip.get(trip_id, []):
            # Preserve every original textual field in array order, including
            # duplicates and status annotations. The backup keeps the raw shape.
            parts.append('\n'.join(str(card[key]) for key in ('kind', 'title', 'body', 'author', 'status')
                                   if key in card and card[key] is not None))
        requirements['text'] = '\n\n'.join(part for part in parts if part != '')
    for plan in result.get('plans', {}).values():
        if not isinstance(plan.get('requirements_snapshot'), dict) or 'text' not in plan['requirements_snapshot']:
            plan['requirements_basis_complete'] = False
    for row in result.get('agent_undo', {}).values():
        row['entries'] = []
        row['version'] = int(row.get('version', 0)) + 1
        row.pop('migration_invalid', None)
    result['schema_version'] = 10
    return result
