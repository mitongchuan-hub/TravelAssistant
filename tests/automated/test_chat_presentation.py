"""Safe chat formatting changes the view only, never source or facts."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from app.chat_presentation import chat_reply_view, chat_time_break, chat_actions
from app.models import AgentCard, AgentAction


def test_markdown_formats_text_but_html_scripts_and_unsafe_links_are_inert():
    source = '# 旅行安排\n\n**慢慢走**，先休息。\n\n- 带水\n- 带伞\n\n<script>alert(1)</script>\n\n[点此](javascript:alert(1))\n\n![图片](https://example.com/track.png)'
    view = chat_reply_view(source)
    html = str(view['html'])
    assert '<h1>旅行安排</h1>' in html and '<strong>慢慢走</strong>' in html and '<ul>' in html
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert 'href="javascript:' not in html and '<img' not in html
    assert source.endswith('track.png)')


def test_long_reply_preview_ends_at_whole_paragraph_and_full_body_remains():
    first = '# 行程\n\n这是完整开头。条件：费用待核实，不保证不超预算。'
    source = first + '\n\n' + '\n\n'.join(f'## 第{i}段\n\n午后休息，保留原文第{i}项。' for i in range(25))
    view = chat_reply_view(source)
    assert view['long']
    assert '不保证不超预算。' in str(view['preview']) and '第24项' not in str(view['preview'])
    assert '第24项' in str(view['html']) and source.startswith(first)
    assert not chat_reply_view('这是一个完整长段落。'*100)['long']


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_plan_preview_keeps_whole_source_overview_budget_and_conditions(newline):
    overview = '# 两日行程\n\n推荐主线：首日老街，次日休息后返程。\n\n- 预算：两人总额1000元，游船先作可选。\n- 需确认：酒店位置和入街票价。'
    source = (overview + '\n\n---\n\n## 第1天\n\n' + '完整原文，保留餐次与未知条件。\n' * 50).replace('\n', newline)
    before = source
    card = AgentCard('itinerary-draft', '行程', '', [], [])
    view = chat_reply_view(source, card)
    assert view['long'] and view['overview']
    assert '游船先作可选' in str(view['preview']) and '酒店位置和入街票价' in str(view['preview'])
    assert '第1天' not in str(view['preview']) and '第1天' in str(view['html'])
    assert str(view['html']).count('保留餐次与未知条件') == 50
    assert source == before


def test_nested_separators_and_other_card_kinds_do_not_define_plan_overview():
    source = '完整开头。\n\n> 引用\n>\n> ---\n\n' + '后续内容。\n' * 50
    card = AgentCard('revision', '调整', '', [], [])
    assert not chat_reply_view(source, card)['overview']
    ordinary = '完整开头。\n\n另一段含预算条件。\n\n---\n\n' + '完整后文。\n' * 50
    assert '预算条件' not in str(chat_reply_view(ordinary)['preview'])
    assert '预算条件' not in str(chat_reply_view(ordinary, AgentCard('missing-info', '', '', [], []))['preview'])


def test_time_separator_uses_recorded_time_not_invented_dates():
    previous = SimpleNamespace(created_at='10:01')
    assert chat_time_break(SimpleNamespace(created_at='10:03'), previous) == ''
    assert chat_time_break(SimpleNamespace(created_at='10:20'), previous) == '10:20'
    assert chat_time_break(SimpleNamespace(created_at='00:05'), SimpleNamespace(created_at='23:58')) == ''
    assert chat_time_break(SimpleNamespace(created_at='刚刚')) == '刚刚'


def test_actions_only_show_distinct_business_steps_without_mutating_card():
    card = AgentCard('requirement-summary', '摘要', '重复文字', ['重复文字'], [
        AgentAction('继续聊天', 'chat'), AgentAction('查看行程', 'itinerary'),
        AgentAction('再看行程', 'itinerary'), AgentAction('无效', 'javascript:')])
    before = deepcopy(card)
    assert [action.target for action in chat_actions(card)] == ['itinerary']
    assert card == before and chat_actions(None) == []
