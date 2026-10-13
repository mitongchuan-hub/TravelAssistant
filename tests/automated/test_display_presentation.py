from copy import deepcopy
import pytest
from app.display_presentation import display_view, coverage_detail
from test_agent_roles import display_data


def activity(**extra):
    return {'title': '西湖散步', 'time_text': '11:00左右', 'location': '西湖', 'duration_text': None,
            'description': '西湖散步。', 'cost_text': None, 'category': 'activity', 'notes': [], **extra}


def test_exact_field_duplicates_reduce_without_mutating_source_or_saved_cards():
    data = display_data(['2026-11-14'])
    data['days'][0]['items'] = [activity(notes=['11:00左右', '开放待核实', '开放待核实。'])]
    data['preparation']['clothing'] = [
        {'title': '轻便雨具', 'body': '轻便雨具。'}, {'title': '轻便雨具', 'body': '轻便雨具。'}]
    data['pending_items'] = ['门票待核实', '门票待核实。']
    data['risks'] = ['门票待核实', '不吃辣，点餐再确认']
    before = deepcopy(data)
    view = display_view(data)
    assert view['days'][0]['items'][0]['description'] is None
    assert view['days'][0]['items'][0]['notes'] == ['开放待核实']
    assert view['preparation']['clothing'] == [{'title': '轻便雨具', 'body': None}]
    assert view['risks'] == ['不吃辣，点餐再确认']
    assert data == before


@pytest.mark.parametrize('description,note', [
    ('不需要预约', '需要预约'),
    ('预算800元不含住宿', '含住宿'),
    ('候车缓冲约40分钟（估算）', '通勤40分钟'),
    ('建议最晚15:00前出发', '最晚15:00前出发'),
    ('10月14日前后18–26°C', '10月14日18–26°C'),
    ('人均≤130元', '人均130元'),
])
def test_partial_or_differently_qualified_statements_are_never_fuzzy_deduplicated(description, note):
    data = display_data(['2026-11-14'])
    data['days'][0]['items'] = [activity(description=description, notes=[note])]
    assert display_view(data)['days'][0]['items'][0]['notes'] == [note]


def test_reminders_on_different_days_and_activities_stay_local():
    data = display_data()
    for day in data['days']:
        day['items'] = [activity(notes=['不吃辣，点餐时再次确认'])]
    view = display_view(data)
    assert all(day['items'][0]['notes'] == ['不吃辣，点餐时再次确认'] for day in view['days'])


def test_common_weather_query_and_note_are_shown_once_but_date_specific_remains():
    data = display_data()
    data['weather'] = [{'date_text': date, 'summary': '阴', 'temperature_text': '18–25°C',
        'precipitation_text': '≤12%', 'queried_at_text': '10月11日',
        'notes': ['出行前一周再次确认', '14日预约待核实'] if i==0 else ['出行前一周再次确认']}
        for i,date in enumerate(['10月14日', '10月15日'])]
    before = deepcopy(data)
    view = display_view(data)
    assert view['weather_query'] == '10月11日'
    assert view['weather_notes'] == ['出行前一周再次确认']
    assert view['weather'][0]['notes'] == ['14日预约待核实']
    assert view['weather'][1]['notes'] == []
    assert all(row['queried_at_text'] is None for row in view['weather'])
    assert data == before


def test_missing_weather_in_legacy_cards_is_supported_and_source_stays_unchanged():
    data = display_data()
    del data['weather']
    view = display_view(data)
    assert view['weather'] == [] and 'weather' not in data
    assert display_view(None) is None


def test_weather_only_metadata_moves_to_shared_area_with_date_and_qualifiers_intact():
    data = display_data()
    row = {'date_text': '全程', 'summary': None, 'temperature_text': None,
           'precipitation_text': None, 'queried_at_text': '10月11日',
           'notes': ['以上为出发前预报，出行前一周再次确认']}
    data['weather'] = [row]
    view = display_view(data)
    assert view['weather'] == []
    assert view['weather_metadata'] == [row]
    assert data['weather'] == [row]


def test_budget_coverage_only_omitted_for_whole_matching_clause():
    assert coverage_detail('总预算800元。含餐饮，不含车票。', '含餐饮，不含车票') is None
    assert coverage_detail('总预算800元，不含住宿。', '含住宿') == '含住宿'
    assert coverage_detail('含餐饮。', '含餐饮，不含车票') == '含餐饮，不含车票'
