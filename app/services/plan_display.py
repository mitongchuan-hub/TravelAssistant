"""Display data is a version-bound derivative of the original itinerary text."""
from copy import deepcopy
from datetime import date, timedelta
from hashlib import sha256
import json
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / 'schemas' / 'itinerary-display-v1.json').read_text(encoding='utf-8'))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


def text_hash(text):
    return sha256(text.encode('utf-8')).hexdigest()


def dates_from_snapshot(snapshot):
    first, last = snapshot.get('start_date'), snapshot.get('end_date')
    if not first or not last:
        return None
    first, last = date.fromisoformat(first), date.fromisoformat(last)
    return [(first + timedelta(days=i)).isoformat() for i in range((last-first).days+1)]


def migrate_stored_display(data):
    """Upgrade explicitly versioned old records without relaxing new responses."""
    result = deepcopy(data)
    if isinstance(result, dict) and result.get('schema_version') == '1.0':
        result.setdefault('weather', [])
        result['schema_version'] = '1.1'
    return result


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


def parse_display_output(raw, output_format='json'):
    """Parse one document; YAML block scalars avoid JSON quote truncation."""
    text = raw.strip()
    if text.startswith('```'):
        lines = text.splitlines()
        allowed = {'```', '```json'} if output_format == 'json' else {'```', '```yaml', '```yml'}
        if len(lines) < 3 or lines[0].strip() not in allowed or lines[-1].strip() != '```':
            raise ValueError('展示输出须为单一完整文档')
        text = '\n'.join(lines[1:-1])
    if output_format == 'json':
        def unique_fields(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError('展示字段不能重复')
                result[key] = value
            return result
        return json.loads(text, object_pairs_hook=unique_fields)
    if output_format != 'yaml':
        raise ValueError('未知展示输出格式')
    import yaml

    class DisplayLoader(yaml.SafeLoader):
        # Dates remain textual source facts, not Python date objects.
        yaml_implicit_resolvers = {key: [(tag, pattern) for tag, pattern in rules
                                         if tag != 'tag:yaml.org,2002:timestamp']
                                   for key, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()}

        def compose_node(self, parent, index):
            if self.check_event(yaml.AliasEvent):
                raise ValueError('展示输出不接受YAML别名')
            if getattr(self.peek_event(), 'tag', None) is not None:
                raise ValueError('展示输出不接受YAML标签')
            return super().compose_node(parent, index)

        def construct_mapping(self, node, deep=False):
            result = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, str) or key in result:
                    raise ValueError('展示字段须为不重复的文本键')
                result[key] = self.construct_object(value_node, deep=deep)
            return result

    try:
        return yaml.load(text, Loader=DisplayLoader)
    except yaml.YAMLError as exc:
        raise ValueError('展示YAML格式无效：' + str(exc)[:500]) from None


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
