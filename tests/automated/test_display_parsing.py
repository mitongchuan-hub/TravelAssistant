"""Safe single-document parsing preserves quoted source text without repairs."""
import pytest
import yaml
from app.plan_display import parse_display_output, validate_display
from test_agent_roles import display_data


def test_yaml_block_text_preserves_quotes_newlines_and_date_strings():
    data = display_data()
    data['overview'] = '点餐时明确“不要辣、微辣也不要”，做法待确认。\n原文换行保留。'
    raw = yaml.safe_dump(data, allow_unicode=True)
    result = validate_display(parse_display_output(raw, 'yaml'))
    assert result == data
    parsed = parse_display_output('date: 2026-11-14\ntext: |-\n  点餐时明确"不要辣、微辣也不要"，做法待确认。', 'yaml')
    assert parsed['date'] == '2026-11-14'
    assert parsed['text'] == '点餐时明确"不要辣、微辣也不要"，做法待确认。'


@pytest.mark.parametrize('raw', [
    'field: first\nfield: second',
    'a: &x [1]\nb: *x',
    '!!python/object/apply:os.system ["echo unsafe"]',
    'a: one\n---\na: two',
    'true: value',
    '```yaml\na: value\n```\nextra prose',
])
def test_yaml_rejects_ambiguous_or_unsafe_documents(raw):
    with pytest.raises((ValueError, yaml.YAMLError)):
        parse_display_output(raw, 'yaml')


def test_single_fence_is_accepted_but_duplicate_json_fields_are_rejected():
    assert parse_display_output('```json\n{"text":"完整原句"}\n```') == {'text': '完整原句'}
    assert parse_display_output('```yaml\ntext: 完整原句\n```', 'yaml') == {'text': '完整原句'}
    with pytest.raises(ValueError):
        parse_display_output('{"text":"前句","text":"后句"}')
