"""Generated guides must remain portable documents without active content."""
from html import unescape

import pytest

from app.services.itinerary.plan_display import validate_display


DATES = ['2026-10-28']
HTML = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<title>北京攻略</title><style>body {color: #112d4e}</style></head>'
        '<body><h1>北京攻略</h1><p>10月28日 09:00 去香山，票价待核实。</p>'
        '<details><summary>交通</summary><p>地铁耗时约一小时。</p></details></body></html>')


def test_html_guide_preserves_source_content_and_dates():
    from app.services.itinerary.guide_html import build_guide_display

    result = build_guide_display('```html\n' + HTML + '\n```', DATES)
    assert result['schema_version'] == '2.0'
    assert result['title'] == '北京攻略'
    assert result['html'] == HTML
    assert result['dates'] == DATES
    assert validate_display(result, DATES) == result
    with pytest.raises(ValueError):
        validate_display(result, ['2026-10-29'])


@pytest.mark.parametrize('content', [
    '<script>alert(1)</script>', '<img src="https://example.com/x">',
    '<p onclick="alert(1)">去香山</p>', '<a href="javascript:alert(1)">导航</a>',
    '<a href="/workspace">导航</a>', '<iframe src="about:blank"></iframe>',
    '<form action="https://example.com"><input name="secret"></form>',
    '<svg onload="alert(1)"></svg>', '<meta http-equiv="refresh" content="0;url=https://example.com">',
    '<link rel="stylesheet" href="https://example.com/x.css">',
    '<style>@import "https://example.com/x.css";</style>',
])
def test_active_or_remote_content_is_rejected(content):
    from app.services.itinerary.guide_html import build_guide_display

    with pytest.raises(ValueError):
        build_guide_display(HTML.replace('</body>', content + '</body>'), DATES)


@pytest.mark.parametrize('raw', ['', '<h1>攻略</h1>', HTML[:-7], HTML + HTML,
                                     HTML.replace('北京攻略</h1>', '北京攻略</h2>')])
def test_incomplete_or_multiple_documents_are_rejected(raw):
    from app.services.itinerary.guide_html import build_guide_display

    with pytest.raises(ValueError):
        build_guide_display(raw, DATES)


def test_download_document_enforces_csp_and_safe_navigation():
    from app.services.itinerary.guide_html import build_guide_display, guide_document

    source = HTML.replace('</body>', '<a href="https://uri.amap.com/marker?x=1&amp;y=2">地图</a></body>')
    document = guide_document(build_guide_display(source, DATES))
    assert 'Content-Security-Policy' in document
    assert "default-src 'none'" in unescape(document)
    assert 'target="_blank"' in document
    assert 'noopener noreferrer' in document
    assert '票价待核实' in document


def test_export_declares_utf8_before_any_author_content():
    from app.services.itinerary.guide_html import build_guide_display, guide_document

    source = '<!doctype html><html><head><title>中文攻略</title></head><body><p>西湖</p></body></html>'
    document = guide_document(build_guide_display(source))
    assert '<meta charset="utf-8">' in document
    assert document.index('charset="utf-8"') < document.index('<title>')
    assert document.index('Content-Security-Policy') < document.index('<title>')


@pytest.mark.parametrize('source', [
    '<!doctype html><html><body><p>西湖</p></body><head><title>攻略</title></head></html>',
    HTML.replace('<body>', '<body><style>p{color:red}</style>'),
    HTML.replace('charset="utf-8"', 'charset="windows-1252"'),
    HTML.replace('color: #112d4e', 'background-image:image-set("https://example.com/x" 1x)'),
])
def test_security_metadata_and_styles_stay_in_valid_head(source):
    from app.services.itinerary.guide_html import build_guide_display

    with pytest.raises(ValueError):
        build_guide_display(source)


def test_display_runtime_generates_html_and_repairs_once():
    from app.agents.agent_client import AgentReply
    import json
    from types import SimpleNamespace

    from app.agents.budget import TurnBudget
    from app.agents.display_runtime import DisplayRuntime

    outputs = iter(['<h1>截断的页面', HTML])
    payloads = []

    def request(client, model, messages, **kwargs):
        assert kwargs['agent_role'] == 'display'
        assert kwargs['tools'] is None
        assert not kwargs.get('response_format')
        kwargs['budget'].model()
        payloads.append(json.loads(messages[-1]['content']))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=next(outputs), tool_calls=[]))])

    runtime = DisplayRuntime(None, 'offline', DATES, request=request,
                             budget=TurnBudget(max_models=2), style_instruction='简洁手账风')
    result = runtime.convert_display(AgentReply('行程', None, [], plan_text='09:00 香山，票价待核实。'))
    assert result.display_status == 'ready'
    assert result.display['html'] == HTML
    assert len(payloads) == 2
    assert payloads[0]['style_instruction'] == '简洁手账风'
    assert 'validation_error' in payloads[1]
