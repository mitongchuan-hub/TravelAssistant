"""Presentation retains travel conditions and price qualifiers while grouping fields."""
from copy import deepcopy

from jinja2 import Environment, FileSystemLoader, select_autoescape
import pytest

from app.views.display_presentation import display_view
from app.views.presentation import day_date_label


def sample():
    return dict(days=[dict(date='2026-10-24', label='第一天', theme='', items=[dict(
        category='transport', title='前往公园', time_text=None,
        description='乘28路或28路区间；若临时停运，先确认替代路线。下车后步行120米。',
        location='测试站', duration_text='约30分钟', cost_text='参考3元/人，待核实',
        notes=['保留候车余量。'])])], preparation=dict(clothing=[], accommodation=[]),
        weather=[], pending_items=[], risks=[])


def render(data):
    env = Environment(loader=FileSystemLoader('app/templates'), autoescape=select_autoescape())
    env.filters['day_date_label'] = day_date_label
    return env.get_template('partials/display_itinerary.html').render(
        display=display_view(data), plan=dict(requirements_snapshot={}))


def test_transport_steps_retain_every_condition_without_mutating_saved_data():
    data = sample()
    original = deepcopy(data)
    item = display_view(data)['days'][0]['items'][0]
    assert item['description_parts'] == [
        '乘28路或28路区间；', '若临时停运，先确认替代路线。', '下车后步行120米。']
    assert ''.join(item['description_parts']) == item['description']
    assert data == original


def test_render_exposes_price_qualifiers_and_escapes_content():
    data = sample()
    data['days'][0]['items'][0]['cost_text'] = '参考3元/人，待核实<script>alert(1)</script>'
    html = render(data)
    assert '参考3元/人，待核实&lt;script&gt;' in html
    assert '<script>alert' not in html
    assert '若临时停运，先确认替代路线。' in html
    assert '费用未提供，不代表免费' not in html


def test_missing_prices_are_explained_once_without_filling_zero():
    data = sample()
    data['days'][0]['items'][0]['cost_text'] = None
    data['days'][0]['items'].append(deepcopy(data['days'][0]['items'][0]))
    html = render(data)
    assert html.count('费用未提供，不代表免费') == 1
    assert '0元' not in html


def test_transport_excerpt_quotes_complete_ride_and_preserves_full_instructions():
    data = sample()
    item = data['days'][0]['items'][0]
    item['description'] = '步行约100米至甲站。地铁2号线，甲站上车，乙站下车；B口出站。'
    view = display_view(data)['days'][0]['items'][0]
    assert view['transport_preview_parts'] == ['地铁2号线，甲站上车，乙站下车；']
    html = render(data)
    assert '步行、进出口与完整说明' in html
    assert '步行约100米至甲站。' in html
    assert 'B口出站。' in html


@pytest.mark.parametrize('description', [
    '若公交停运，改乘地铁。地铁2号线，甲站上车，乙站下车。步行约100米至入口。',
    '步行100米至甲站，遇大雨改乘出租车。地铁2号线，甲站上车，乙站下车。',
    '步行100米至甲站，末班车22点。地铁2号线，甲站上车，乙站下车。',
    '步行100米至甲站（遇大雨改乘出租车）。地铁2号线，甲站上车，乙站下车。',
])
def test_conditional_transport_stays_fully_visible(description):
    data = sample()
    data['days'][0]['items'][0]['description'] = description
    view = display_view(data)['days'][0]['items'][0]
    assert view['transport_preview_parts'] == []
    assert 'class="display-transport-detail"' not in render(data)


@pytest.mark.parametrize('context', [
    '步行100米至甲站（遇大雨改乘出租车）。',
    'A口进站（末班车22点）。',
])
def test_unclassified_parenthetical_context_remains_in_visible_preview(context):
    data = sample()
    data['days'][0]['items'][0]['description'] = (
        '步行100米至入口。' + context + '地铁2号线，甲站上车，乙站下车。')
    view = display_view(data)['days'][0]['items'][0]
    assert context in view['transport_preview_parts']
