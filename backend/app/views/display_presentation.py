"""Presentation-only reduction of exact repetitions in prose-derived cards.

Never mutate the saved display/source or infer that differently worded conditions
mean the same thing. Local activity reminders are not deduplicated across days.
"""
from collections import Counter
from copy import deepcopy
import re


def key(text):
    return re.sub(r'\s+', '', text or '').strip('。；;.!！？?')


def statements(text):
    # Keep commas, parentheses and numeric qualifiers together. They may bind a
    # condition to a value, so partial matches are not sufficient for removal.
    return {key(s) for s in re.split(r'[。；;！？!?\n]+', text or '') if key(s)}


def unique_texts(values, covered=()):
    seen = set(covered)
    result = []
    for text in values or []:
        normalized = key(text)
        if normalized and normalized not in seen:
            result.append(text)
            seen.add(normalized)
    return result


def unique_entries(entries):
    result, seen = [], set()
    for entry in entries or []:
        identity = (key(entry.get('title')), key(entry.get('body')))
        if identity in seen:
            continue
        item = deepcopy(entry)
        if item.get('body') and key(item['body']) == key(item['title']):
            item['body'] = None
        result.append(item)
        seen.add(identity)
    return result


def display_view(data):
    if not data:
        return None
    view = deepcopy(data)
    view['pending_items'] = unique_texts(view.get('pending_items'))
    view['risks'] = unique_texts(view.get('risks'), {key(s) for s in view['pending_items']})
    view['has_unprovided_cost'] = any(not item.get('cost_text')
        or key(item['cost_text']) == key('费用未提供，不代表免费')
        for day in view['days'] for item in day['items'])
    for name in ('clothing', 'accommodation'):
        view['preparation'][name] = unique_entries(view['preparation'].get(name))
    for day in view['days']:
        for item in day['items']:
            if key(item.get('cost_text')) == key('费用未提供，不代表免费'):
                item['cost_text'] = None
            description = item.get('description')
            if key(description) and key(description) == key(item.get('title')):
                item['description'] = None
            represented = set()
            for name in ('title', 'time_text', 'description'):
                represented.update(statements(item.get(name)))
            for name in ('location', 'duration_text', 'cost_text'):
                value = item.get(name)
                if value and key(value) in represented:
                    item[name] = None
                else:
                    represented.update(statements(value))
            item['notes'] = unique_texts(item.get('notes'), represented)
            # Sentence boundaries only: retain alternatives, qualifiers and every
            # instruction rather than attempting to infer a new route from prose.
            item['description_parts'] = [part.strip() for part in re.split(
                r'(?<=[。；])\s*|\n+', item.get('description') or '') if part.strip()
            ] if item.get('category') == 'transport' else []
            parts = item['description_parts']
            rides = [part for part in parts if '上车' in part and '下车' in part
                     and re.search(r'地铁|号线|\d+路', part)]
            # Quote complete ride clauses, without synthesizing a new route.
            # Only strict walking/entrance clauses can be folded; every other
            # clause stays in the preview, including parenthetical context.
            walk_or_gate = (
                r'(?:出站)?步行(?:约)?\d+(?:\.\d+)?(?:公里|米)'
                r'(?:[（(]约?\d+(?:\.\d+)?分钟[）)])?'
                r'(?:(?:至|到|进入)[A-Za-z0-9\u4e00-\u9fff·]*(?:站(?:换乘)?|入口|门|公园))?[。；]?'
                r'|[A-Z][A-Za-z0-9\u4e00-\u9fff·]*口(?:进|出)站[。；]?')
            conditional = re.search(
                r'如果|若|如遇|备选|可选|择一|任选|雨天|晴天|替代|注意|必须|禁止|建议|需|不|勿|仅|只',
                item.get('description') or '')
            preview = [part for part in parts if not re.fullmatch(walk_or_gate, part)]
            item['transport_preview_parts'] = preview if (
                rides and not conditional and len(preview) < len(parts)) else []

    # Identical query dates and notes apply across all weather rows and are shown
    # once underneath. Date-specific information stays with its own forecast.
    weather = view.get('weather') or []
    shared_query = None
    if len(weather)>1 and weather[0].get('queried_at_text') and all(
            key(row.get('queried_at_text')) == key(weather[0]['queried_at_text']) for row in weather):
        shared_query = weather[0]['queried_at_text']
        for row in weather:
            row['queried_at_text'] = None
    counts = Counter(normalized for row in weather for normalized in {key(s) for s in row.get('notes', [])})
    shared_keys = {normalized for normalized, count in counts.items() if len(weather)>1 and count == len(weather)}
    shared_notes = unique_texts([s for row in weather for s in row.get('notes', []) if key(s) in shared_keys])
    for row in weather:
        row['notes'] = unique_texts(row.get('notes'), shared_keys)
    view['weather_metadata'] = [row for row in weather if not any(row.get(name) for name in
        ('summary', 'temperature_text', 'precipitation_text'))]
    view['weather'] = [row for row in weather if any(row.get(name) for name in
        ('summary', 'temperature_text', 'precipitation_text'))]
    view['weather_query'] = shared_query
    view['weather_notes'] = shared_notes
    return view


def coverage_detail(budget_note, coverage):
    """Omit the exact coverage clause only when the budget already states it."""
    if key(coverage) and key(coverage) in statements(budget_note):
        return None
    return coverage
