"""Safe chat formatting changes the view only, never source or facts."""
from copy import deepcopy
from types import SimpleNamespace
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
