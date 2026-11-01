"""Portable, passive HTML guides; generated markup never enters the app DOM."""
from html import escape
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit


GUIDE_CSP = ("default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; "
             "img-src 'none'; font-src 'none'; connect-src 'none'; object-src 'none'; "
             "frame-src 'none'; base-uri 'none'; form-action 'none'")
GUIDE_SANDBOX = 'sandbox allow-popups allow-popups-to-escape-sandbox'
MAX_HTML_BYTES = 512 * 1024
_TAGS = set(('html head title meta style body main article section header footer nav aside '
             'h1 h2 h3 h4 h5 h6 p div span a ul ol li dl dt dd table caption thead tbody tfoot '
             'tr th td colgroup col strong em b i u s small mark sub sup time address '
             'blockquote q cite pre code br hr details summary abbr').split())
_VOID = {'meta', 'br', 'hr', 'col'}
_ATTRS = {'class', 'id', 'style', 'title', 'lang', 'dir', 'role', 'hidden', 'tabindex',
          'datetime', 'open', 'colspan', 'rowspan', 'scope', 'start', 'reversed', 'type',
          'value', 'href', 'target', 'rel', 'charset', 'name', 'content'}


def _check_css(value):
    if re.search(r'url\s*\(|@import|image-set\s*\(|expression\s*\(|-moz-binding|behavior\s*:|\\', value, re.I):
        raise ValueError('网页样式只允许自包含 CSS，不能加载外部资源或执行代码')


class _GuideParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.seen = set()
        self.title = []
        self.body_text = []
        self.output = []

    def handle_decl(self, decl):
        if decl.lower() != 'doctype html' or self.seen:
            raise ValueError('网页须为一个 HTML5 文档')
        self.output.append('<!doctype html>')

    def handle_starttag(self, tag, attrs):
        if tag not in _TAGS:
            raise ValueError(f'网页不允许标签：{tag}')
        if tag == 'head' and 'body' in self.seen or tag == 'body' and 'head' not in self.seen:
            raise ValueError('网页 head 必须在 body 之前')
        if tag in {'title', 'style', 'meta'} and self.stack != ['html', 'head']:
            raise ValueError('网页标题、样式和元数据必须位于 head 中')
        if tag in {'html', 'head', 'body', 'title'}:
            if tag in self.seen:
                raise ValueError(f'网页含重复的 {tag}')
            self.seen.add(tag)
        expected = {'html': [], 'head': ['html'], 'body': ['html'], 'title': ['html', 'head']}
        if tag in expected and self.stack != expected[tag]:
            raise ValueError(f'网页 {tag} 结构不完整')
        if not self.stack and tag != 'html':
            raise ValueError('网页须包含完整 html/head/body')
        if tag not in {'html', 'head', 'body', 'title', 'meta', 'style'} and 'body' not in self.stack:
            raise ValueError('网页正文必须放在 body 内')
        values = {}
        for key, value in attrs:
            if key in values or (key not in _ATTRS and not key.startswith(('aria-', 'data-'))):
                raise ValueError(f'网页不允许属性：{key}')
            value = value or ''
            if key == 'style':
                _check_css(value)
            if key == 'href':
                parsed = urlsplit(value)
                if not value.startswith('#') and (parsed.scheme != 'https' or not parsed.hostname
                                                   or parsed.username or parsed.password):
                    raise ValueError('导航只允许页内锚点或 HTTPS 链接')
                if tag != 'a':
                    raise ValueError('只有链接可以使用 href')
            values[key] = value
        if tag == 'meta' and not ('charset' in values or values.get('name', '').lower() == 'viewport'):
            raise ValueError('网页仅允许字符集和视口 meta')
        if tag == 'meta' and 'charset' in values and values['charset'].lower() not in {'utf-8', 'utf8'}:
            raise ValueError('网页字符集必须为 UTF-8')
        if tag == 'a' and urlsplit(values.get('href', '')).scheme == 'https':
            values.update(target='_blank', rel='noopener noreferrer')
        self.output.append('<' + tag + ''.join(f' {key}="{escape(value, quote=True)}"'
                                               for key, value in values.items()) + '>')
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _VOID or not self.stack or self.stack[-1] != tag:
            raise ValueError(f'网页标签未正确闭合：{tag}')
        self.stack.pop()
        self.output.append(f'</{tag}>')

    def handle_data(self, data):
        if self.stack and self.stack[-1] == 'style':
            _check_css(data)
            self.output.append(data)
            return
        if 'title' in self.stack:
            self.title.append(data)
        if 'body' in self.stack:
            self.body_text.append(data)
        if not self.stack and data.strip():
            raise ValueError('HTML 文档外不能包含文字')
        self.output.append(escape(data, quote=False))

    def finish(self, source):
        if not isinstance(source, str) or not source.strip() or len(source.encode('utf-8')) > MAX_HTML_BYTES:
            raise ValueError('网页为空或超过 512 KiB')
        self.feed(source)
        self.close()
        if self.stack or self.seen != {'html', 'head', 'body', 'title'} or not ''.join(self.body_text).strip():
            raise ValueError('网页须含完整 html/head/title/body 和可读正文，所有标签必须闭合')
        return self


def build_guide_display(raw, dates=None):
    if not isinstance(raw, str):
        raise ValueError('网页输出须为 HTML 文本')
    source = raw.strip()
    if source.startswith('```'):
        lines = source.splitlines()
        if len(lines) < 3 or lines[0].strip().lower() not in {'```html', '```'} or lines[-1].strip() != '```':
            raise ValueError('网页须为单一完整 HTML 文档')
        source = '\n'.join(lines[1:-1]).strip()
    parser = _GuideParser().finish(source)
    return {'schema_version': '2.0', 'html': source,
            'title': ''.join(parser.title).strip() or '旅行攻略', 'dates': list(dates or [])}


def validate_guide_display(data, dates=None):
    if not isinstance(data, dict) or data.get('schema_version') != '2.0':
        raise ValueError('网页展示合同无效')
    if not isinstance(data.get('dates'), list) or not all(isinstance(d, str) for d in data['dates']):
        raise ValueError('网页日期合同无效')
    result = build_guide_display(data.get('html'), data['dates'])
    if dates is not None and result['dates'] != list(dates):
        raise ValueError('网页日期与行程依据不一致')
    return result


def guide_document(data):
    validated = validate_guide_display(data)
    parser = _GuideParser().finish(validated['html'])
    document = ''.join(parser.output)
    meta = '<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="' + escape(GUIDE_CSP, quote=True) + '">'
    return re.sub(r'(<head\b[^>]*>)', lambda match: match[0] + meta, document, count=1, flags=re.I)
