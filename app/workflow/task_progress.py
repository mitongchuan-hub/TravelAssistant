"""Small, public execution history derived only from real runtime events."""
from copy import deepcopy
from time import time


MODEL_LABELS = {
    'main_execution': '理解你的请求',
    'main_reply_review': '核对回复与当前行程',
    'requirements_capability': '整理游玩需求',
    'research_capability': '分析查询资料',
    'planning_capability': '规划游玩路线',
    'revision_capability': '调整行程安排',
}
TOOL_LABELS = {
    'read_context': '读取旅行信息', 'query_weather': '查询天气',
    'search_places': '检索地点', 'search_food': '查找餐饮', 'query_route': '查询交通路线',
    'check_requirements': '检查出行需求', 'update_requirements': '整理需求变更',
    'edit_plan_text': '调整行程文字',
}
MAX_STEPS = 80


def clock_ms():
    return int(time() * 1000)


def new_progress(state='running', *, timestamp=None):
    stamp = clock_ms() if timestamp is None else timestamp
    return dict(state=state, started_at=stamp, updated_at=stamp, duration_ms=0,
                revision=0, omitted=0, steps=[])


def _detail(name, arguments):
    if not isinstance(arguments, dict):
        return ''
    if name == 'read_context':
        return {'requirements': '出行需求', 'plan': '当前行程', 'evidence': '已查询资料',
                'messages': '聊天记录', 'ideas': '想法墙'}.get(arguments.get('section'), '')
    if name not in {'query_weather', 'search_places', 'search_food'}:
        return ''
    # Only these user-facing query fields may leave the internal trace.
    values = [arguments.get(key) for key in ('city', 'keywords', 'keyword', 'query', 'start_date', 'end_date')]
    return ' · '.join(dict.fromkeys(value.strip()[:80] for value in values
                                   if isinstance(value, str) and value.strip()))[:180]


def advance_progress(progress, title, value, *, timestamp=None):
    if not progress or progress.get('state') not in {'queued', 'running'} or not isinstance(value, dict):
        return None
    if title in {'模型请求', '模型响应'}:
        role = value.get('agent_role')
        if role not in MODEL_LABELS:
            return None
        key, kind, label = f'model:{role}', 'model', MODEL_LABELS[role]
        if role == 'main_execution' and any(step['key'] == key for step in progress['steps']):
            label = '整理结果与下一步'
        start, detail = title == '模型请求', ''
    elif title in {'工具开始', '工具执行'}:
        name = value.get('name')
        if name not in TOOL_LABELS:
            return None
        key, kind, label = f"tool:{value.get('role')}:{name}", 'tool', TOOL_LABELS[name]
        start, detail = title == '工具开始', _detail(name, value.get('arguments'))
    else:
        return None
    stamp = clock_ms() if timestamp is None else timestamp
    result = deepcopy(progress)
    steps = result['steps']
    if start:
        if len(steps) >= MAX_STEPS:
            index = next((i for i, row in enumerate(steps) if row['status'] != 'running'), None)
            if index is None:
                return None
            steps.pop(index)
            result['omitted'] += 1
        steps.append(dict(id=result['revision'] + 1, key=key, kind=kind, label=label, detail=detail,
                          status='running', started_at=stamp, duration_ms=0))
    else:
        step = next((row for row in reversed(steps) if row['key'] == key and row['status'] == 'running'), None)
        if step is None:
            return None
        output = value.get('result')
        status = output.get('status') if isinstance(output, dict) else None
        if value.get('error_type') or status in {'failed', 'error'}:
            step['status'] = 'failed'
        elif status in {'unavailable', 'needs_input', 'budget_exhausted', 'prerequisite_needed'}:
            step['status'] = 'unavailable'
        else:
            step['status'] = 'succeeded'
        step['duration_ms'] = max(0, stamp - step['started_at'])
    result.update(state='running', updated_at=stamp, revision=result['revision'] + 1)
    return result


def finish_progress(progress, state, *, timestamp=None):
    if not progress:
        return {}
    stamp = clock_ms() if timestamp is None else timestamp
    result = deepcopy(progress)
    for step in result['steps']:
        if step['status'] == 'running':
            step.update(status='failed' if state == 'failed' else 'stopped',
                        duration_ms=max(0, stamp - step['started_at']))
    result.update(state=state, updated_at=stamp, duration_ms=max(0, stamp - result['started_at']),
                  revision=result['revision'] + 1)
    return result
