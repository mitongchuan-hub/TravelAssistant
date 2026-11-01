"""Regression tests for app.services.itinerary.plan_display helpers.

parse_display_output is a security-sensitive parser (JSON/YAML, code fences,
duplicate keys, YAML aliases/tags). dates_from_snapshot is used to bind display
days to the plan's date window; every caller already guards its ValueError, but
the pure behavior is worth pinning.
"""
import pytest

from app.services.itinerary.plan_display import (
    dates_from_snapshot, display_format, parse_display_output, text_hash,
)


# --- text_hash --------------------------------------------------------------

def test_text_hash_is_deterministic_sha256_hex():
    h = text_hash('行程正文')
    assert len(h) == 64 and h == text_hash('行程正文')
    assert h != text_hash('另一段正文')


# --- parse_display_output ---------------------------------------------------

def test_parse_plain_json():
    assert parse_display_output('{"a": 1}', 'json') == {'a': 1}


def test_parse_fenced_json_and_yaml():
    assert parse_display_output('```json\n{"a": 1}\n```', 'json') == {'a': 1}
    assert parse_display_output('```yaml\na: 1\n```', 'yaml') == {'a': 1}


def test_parse_rejects_duplicate_json_keys_at_any_depth():
    with pytest.raises(ValueError, match='不能重复'):
        parse_display_output('{"a": 1, "a": 2}', 'json')
    with pytest.raises(ValueError, match='不能重复'):
        parse_display_output('{"o": {"a": 1, "a": 2}}', 'json')


def test_parse_rejects_yaml_aliases_and_tags():
    with pytest.raises(ValueError, match='别名'):
        parse_display_output('a: &x 1\nb: *x', 'yaml')
    with pytest.raises(ValueError, match='标签'):
        parse_display_output('a: !!python/object/apply:os.system ["x"]', 'yaml')


def test_parse_rejects_non_string_yaml_keys():
    with pytest.raises(ValueError, match='文本键'):
        parse_display_output('1: x', 'yaml')


def test_parse_rejects_malformed_fence():
    with pytest.raises(ValueError, match='单一完整文档'):
        parse_display_output('```json\n{"a": 1}', 'json')  # missing closing fence


def test_parse_rejects_fence_format_mismatch():
    with pytest.raises(ValueError, match='单一完整文档'):
        parse_display_output('```json\n{"a": 1}\n```', 'yaml')


def test_parse_yaml_dates_stay_text():
    result = parse_display_output('a: 2026-01-01', 'yaml')
    assert result == {'a': '2026-01-01'} and isinstance(result['a'], str)


def test_parse_rejects_unknown_format():
    with pytest.raises(ValueError, match='未知'):
        parse_display_output('a: 1', 'toml')


# --- dates_from_snapshot ----------------------------------------------------

def test_dates_from_snapshot_inclusive_range():
    snap = {'start_date': '2026-10-01', 'end_date': '2026-10-03'}
    assert dates_from_snapshot(snap) == ['2026-10-01', '2026-10-02', '2026-10-03']


def test_dates_from_snapshot_single_day():
    assert dates_from_snapshot({'start_date': '2026-10-01', 'end_date': '2026-10-01'}) == ['2026-10-01']


@pytest.mark.parametrize('snapshot', [
    {'start_date': '', 'end_date': ''},
    {'start_date': '2026-10-01', 'end_date': ''},
    {'start_date': '', 'end_date': '2026-10-03'},
    {},
])
def test_dates_from_snapshot_incomplete_returns_none(snapshot):
    assert dates_from_snapshot(snapshot) is None


def test_dates_from_snapshot_malformed_raises_and_callers_guard():
    # Pure behavior: malformed ISO raises. All production callers wrap this in
    # try/except (main_input._snapshot_dates, store, display_tasks) -> None.
    with pytest.raises(ValueError):
        dates_from_snapshot({'start_date': 'bad', 'end_date': '2026-10-03'})


# --- display_format ---------------------------------------------------------

def test_display_format_pins_day_count_and_date_enum():
    dates = ['2026-10-01', '2026-10-02']
    fmt = display_format(dates)
    schema = fmt['json_schema']['schema']
    days = schema['properties']['days']
    assert days['minItems'] == 2 and days['maxItems'] == 2
    assert days['items']['properties']['date']['enum'] == dates


def test_display_format_rejects_empty_or_duplicate_dates():
    with pytest.raises(ValueError):
        display_format([])
    with pytest.raises(ValueError):
        display_format(['2026-10-01', '2026-10-01'])


def test_display_format_rejects_bad_date():
    with pytest.raises(ValueError):
        display_format(['not-a-date'])
