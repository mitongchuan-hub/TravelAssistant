"""Display data is a version-bound derivative of the original itinerary text."""
from copy import deepcopy
from datetime import date, timedelta
from hashlib import sha256
import json
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker

SCHEMA = json.loads((Path(__file__).parent / 'schemas' / 'itinerary-display-v1.json').read_text(encoding='utf-8'))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


def text_hash(text):
    return sha256(text.encode('utf-8')).hexdigest()


def dates_from_snapshot(snapshot):
    first, last = snapshot.get('start_date'), snapshot.get('end_date')
    if not first or not last:
        return None
    first, last = date.fromisoformat(first), date.fromisoformat(last)
    return [(first + timedelta(days=i)).isoformat() for i in range((last-first).days+1)]


def validate_display(data, dates=None):
    VALIDATOR.validate(data)
    result = deepcopy(data)
    days = result['days']
    if dates is not None:
        if len(days) != len(dates):
            raise ValueError('展示天数与行程日期不一致')
        for day, expected in zip(days, dates):
            if day['date'] is None:
                day['date'] = expected
            elif day['date'] != expected:
                raise ValueError('展示日期与行程依据不一致')
    return result


def display_format(dates=None):
    # Some structured-output providers reject sibling keywords on $ref.
    # Expand the small, acyclic schema without changing its local validation contract.
    def expand(value):
        if isinstance(value, list):
            return [expand(v) for v in value]
        if not isinstance(value, dict):
            return value
        if '$ref' in value:
            name = value['$ref'].removeprefix('#/$defs/')
            value = {**deepcopy(SCHEMA['$defs'][name]), **{k: v for k, v in value.items() if k != '$ref'}}
        return {k: expand(v) for k, v in value.items() if k not in {'$schema', '$id', '$defs'}}
    schema = expand(SCHEMA)
    if dates is not None:
        if not dates or len(set(dates)) != len(dates):
            raise ValueError('展示日期窗口为空或重复')
        for value in dates:
            date.fromisoformat(value)
        days = schema['properties']['days']
        days.update(minItems=len(dates), maxItems=len(dates))
        days['items']['properties']['date'] = {'type': 'string', 'enum': list(dates),
            'description': '按source_dates顺序对应日期；只映射日期，不新增活动。'}
    return {'type': 'json_schema', 'json_schema': {'name': 'itinerary_display_v1', 'strict': True, 'schema': schema}}
