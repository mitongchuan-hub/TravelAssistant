"""The original itinerary renders Markdown without executing embedded HTML."""
from pathlib import Path

from jinja2 import Environment

from app.views.chat_presentation import chat_reply_view


def render_original(text):
    source = Path('app/templates/workspace.html').read_text(encoding='utf-8')
    start = source.index('<div class="text-itinerary-content"')
    fragment = source[start:source.index('</div>', start) + len('</div>')]
    env = Environment(autoescape=True)
    env.filters['chat_reply_view'] = chat_reply_view
    return env.from_string(fragment).render(plan={'text': text})


def test_original_renders_headings_bold_lists_tables_and_separator():
    text = '## Day 1\n\n**Breakfast**\n\n- Buns\n\n---\n\n| Item | Cost |\n| --- | --- |\n| Food | Unknown |'
    html = render_original(text)
    assert '<h2>Day 1</h2>' in html
    assert '<strong>Breakfast</strong>' in html
    assert '<li>Buns</li>' in html
    assert '<table>' in html and '<td>Unknown</td>' in html
    assert '<hr' in html
    assert '**Breakfast**' not in html


def test_original_escapes_html_and_rejects_script_links():
    html = render_original('<script>alert(1)</script>\n\n[click](javascript:alert(1))')
    assert '<script>' not in html
    assert '&lt;script&gt;' in html
    assert 'href="javascript:' not in html
