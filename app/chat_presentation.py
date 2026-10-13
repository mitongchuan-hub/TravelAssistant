"""Read-only chat presentation: safe Markdown, complete previews and time breaks."""
from .models import AgentAction
from markdown_it import MarkdownIt
from markupsafe import Markup
import re

_MARKDOWN = MarkdownIt('commonmark', {'html': False, 'breaks': True}).enable('table').disable('image')


def chat_reply_view(body):
    text = body or ''
    long = len(text) > 900 or len(text.splitlines()) > 18
    preview = text
    if long:
        tokens = _MARKDOWN.parse(text)
        # Take the first whole paragraph, including preceding headings. Never
        # cut inside a sentence or ask a model for another summary.
        block = next((tokens[i + 1] for i, token in enumerate(tokens[:-1])
                      if token.type == 'paragraph_open' and tokens[i + 1].map), None)
        if block is None:
            block = next((token for token in tokens if token.map and token.type in {'fence', 'code_block', 'inline'}), None)
        if block:
            preview = '\n'.join(text.splitlines()[:block.map[1]])
        # A single long paragraph stays fully readable rather than disappearing
        # behind an expansion which would duplicate the whole reply.
        long = preview != text
    return {'html': Markup(_MARKDOWN.render(text)), 'preview': Markup(_MARKDOWN.render(preview)), 'long': long}


def chat_time_break(message, previous=None):
    current = message.created_at or ''
    if not previous:
        return current
    before = previous.created_at or ''
    pattern = r'^(\d{1,2}):(\d{2})$'
    first, last = re.fullmatch(pattern, current), re.fullmatch(pattern, before)
    if first and last:
        delta = (int(first[1])*60 + int(first[2]) - int(last[1])*60 - int(last[2])) % 1440
        return current if delta >= 15 else ''
    return current if current != before else ''


def chat_actions(card):
    if not card:
        return []
    allowed, seen, result = {'itinerary', 'generate', 'board'}, set(), []
    for action in card.actions:
        if action.target in allowed and action.target not in seen:
            seen.add(action.target)
            result.append(action)
    if card.kind in {'itinerary-draft', 'revision'} and 'itinerary' not in seen:
        result.insert(0, AgentAction('查看当前行程', 'itinerary'))
    return result


def chat_supplemental(body, card):
    if not card:
        return []
    def key(value):
        return re.sub(r'\s+', '', value).rstrip('。.!！')
    existing = {key(body or '')}
    for token in _MARKDOWN.parse(body or ''):
        if token.type == 'inline':
            existing.add(key(''.join(child.content for child in token.children or [])))
    result = []
    for bullet in card.bullets:
        normalized = key(bullet)
        if normalized and normalized not in existing:
            existing.add(normalized)
            result.append(bullet)
    return result
