"""Exact demo fixtures and lifecycle boundaries of the pure seed factories."""
from dataclasses import asdict

from app.domain.models import User
from app.storage import demo_data
from app.storage import store as store_module


USERS = {
    'u1': User('u1', '林夏', '林'),
    'agent': User('agent', '旅行规划 Agent', 'AI'),
}
ROTATIONS = ['-1.4deg', '1deg', '0.6deg', '-0.8deg', '1.3deg']


def expected_messages():
    user = {'id': 'u1', 'name': '林夏', 'initials': '林'}
    agent = {'id': 'agent', 'name': '旅行规划 Agent', 'initials': 'AI'}
    return [
        {'id': 'm1', 'sender': user, 'sender_type': 'user',
         'body': '我 8 月底想去杭州，安排两晚，节奏别太赶。', 'created_at': '19:10',
         'status': 'sent', 'agent_card': None, 'execution_progress': {}},
        {'id': 'm2', 'sender': agent, 'sender_type': 'agent',
         'body': '收到，我先按轻松的 3 天 2 晚来整理。', 'created_at': '19:11',
         'status': 'sent', 'execution_progress': {}, 'agent_card': {
             'kind': 'requirement-summary', 'title': '旅行框架已经记下',
             'summary': '杭州、两晚、轻松节奏已经进入计划约束。',
             'bullets': ['8 月底出发', '安排两晚', '避免行程太赶', '还需要预算和必去地点'],
             'actions': [{'label': '查看旅行想法', 'target': 'board'}]}},
        {'id': 'm3', 'sender': user, 'sender_type': 'user',
         'body': '我想去西湖和茶园，预算控制在人均 1500 左右。', 'created_at': '19:12',
         'status': 'sent', 'agent_card': None, 'execution_progress': {}},
        {'id': 'm4', 'sender': agent, 'sender_type': 'agent',
         'body': '信息够用了，我生成了第一版行程。', 'created_at': '19:16',
         'status': 'sent', 'execution_progress': {}, 'agent_card': {
             'kind': 'itinerary-draft', 'title': '杭州 3 天 2 晚初版行程',
             'summary': '西湖、龙井茶园、运河街区，整体偏轻松。',
             'bullets': ['每天 2-3 个主要安排', '下午保留休息时间', '预算按人均 1500 控制'],
             'actions': [{'label': '查看行程', 'target': 'itinerary'}]}},
    ]


def expected_ideas():
    return [
        {'id': 'idea-place-1', 'kind': '地点', 'title': '西湖傍晚散步',
         'body': '第一天傍晚去断桥和白堤，避开中午高温，也留出自由时间。',
         'author': '你', 'status': '已采纳', 'rotation': '-1.4deg'},
        {'id': 'idea-place-2', 'kind': '地点', 'title': '龙井茶园',
         'body': '安排半天慢一点的茶园体验，不要只打卡拍照。',
         'author': '对话', 'status': '候选', 'rotation': '1deg'},
        {'id': 'idea-budget-1', 'kind': '预算', 'title': '人均 1500 左右',
         'body': '住宿、餐饮和交通都按轻松但不奢侈来筛选。',
         'author': '对话', 'status': '约束', 'rotation': '0.6deg'},
        {'id': 'idea-pace-1', 'kind': '节奏', 'title': '不要每天太赶',
         'body': '每天最多 2-3 个主要安排，下午保留休息窗口。',
         'author': '你', 'status': '已采纳', 'rotation': '-0.8deg'},
        {'id': 'idea-taboo-1', 'kind': '禁忌', 'title': '避免连续长距离移动',
         'body': 'Agent 提醒：如果茶园、运河街区和西湖分散安排，会和轻松旅行目标冲突。',
         'author': 'Agent', 'status': '冲突提醒', 'rotation': '1.3deg'},
    ]


def expected_plan():
    item_defaults = {
        'category': 'activity', 'meal': None, 'place': None, 'place_scope': 'unknown',
        'travel_mode': 'transit', 'incoming_route': None, 'departure_route': None,
    }
    return {
        'id': 'plan-v1', 'title': '杭州 3 天 2 晚轻松行程', 'status': '草案',
        'days': [
            {'id': 'day-1', 'label': 'Day 1', 'date': '', 'theme': '', 'items': [
                {**item_defaults, 'id': 'item-1', 'time': '14:00', 'title': '抵达杭州并入住',
                 'location': '湖滨商圈附近', 'duration': '1.5 小时',
                 'reason': '方便晚间步行到西湖，也便于第二天出发。',
                 'notes': '住宿预算还需要继续确认。', 'satisfies': ['交通便利', '轻松节奏']},
                {**item_defaults, 'id': 'item-2', 'time': '17:00', 'title': '西湖傍晚散步',
                 'location': '断桥 - 白堤', 'duration': '2 小时',
                 'reason': '符合轻松节奏，也满足西湖需求。',
                 'notes': '避开中午高温和人流。', 'satisfies': ['西湖', '轻松节奏']},
            ]},
            {'id': 'day-2', 'label': 'Day 2', 'date': '', 'theme': '', 'items': [
                {**item_defaults, 'id': 'item-3', 'time': '10:00', 'title': '龙井茶园',
                 'location': '龙井村', 'duration': '3 小时',
                 'reason': '满足茶园偏好，适合慢节奏体验。',
                 'notes': '建议提前确认是否需要预约体验。', 'satisfies': ['茶园体验', '预算可控']},
            ]},
        ],
        'preparation': {'clothing': [], 'accommodation': []}, 'overview': '',
        'constraints_met': [], 'pending_items': [], 'risks': [], 'text': '', 'evidence': [],
        'requirements_revision': None, 'display': None, 'display_source_hash': '',
        'display_status': 'not_requested', 'display_style': '',
        'requirements_snapshot': {}, 'lodging_snapshot': {},
        'requirements_basis_complete': True, 'requirements_legacy_stale': False,
    }


def test_seed_factories_preserve_entire_demo_content():
    assert [asdict(row) for row in demo_data.seed_messages(USERS)] == expected_messages()
    assert asdict(demo_data.seed_plan()) == expected_plan()


def test_factories_make_fresh_nested_objects_on_every_call():
    messages = demo_data.seed_messages(USERS)
    messages[0].body = 'changed'
    messages[0].execution_progress['steps'] = [{'detail': 'changed'}]
    messages[1].agent_card.bullets.append('changed')
    messages[1].agent_card.actions.clear()
    messages.pop()
    plan = demo_data.seed_plan()
    other_plan = demo_data.seed_plan()
    assert plan is not other_plan
    assert plan.days[0] is not other_plan.days[0]
    assert plan.days[0].items[0] is not other_plan.days[0].items[0]
    plan.days[0].items[0].satisfies.append('changed')
    plan.days[0].items.pop()
    plan.days.pop()
    plan.preparation.clothing.append('changed')
    plan.preparation.accommodation.append('changed')
    for value in (plan.constraints_met, plan.pending_items, plan.risks, plan.evidence):
        value.append('changed')
    plan.requirements_snapshot['city'] = 'changed'
    plan.lodging_snapshot['name'] = 'changed'
    assert [asdict(row) for row in demo_data.seed_messages(USERS)] == expected_messages()
    assert asdict(demo_data.seed_plan()) == expected_plan()


def make_store(**kwargs):
    return store_module.DemoStore(display_worker_enabled=False, **kwargs)


def test_two_stores_do_not_share_mutable_demo_state():
    first = make_store(persistence_enabled=False)
    second = make_store(persistence_enabled=False)
    first.messages['trip-hangzhou'][1].agent_card.bullets.clear()
    first.messages['trip-hangzhou'][1].agent_card.actions.clear()
    first.messages['trip-hangzhou'][0].execution_progress['steps'] = []
    first.messages['trip-hangzhou'].pop()
    first.trips['trip-hangzhou'].requirements.text = ''
    first.plans['trip-hangzhou'].days[0].items[0].satisfies.clear()
    first.plans['trip-hangzhou'].days[0].items.clear()
    first.plans['trip-hangzhou'].preparation.clothing.append('changed')
    first.plans['trip-hangzhou'].requirements_snapshot['city'] = 'changed'
    first.trips['trip-hangzhou'].requirements.lodging_address = 'changed'
    assert [asdict(row) for row in second.messages['trip-hangzhou']] == expected_messages()
    assert second.trips['trip-hangzhou'].requirements.text == '想去西湖，节奏轻松。'
    assert asdict(second.plans['trip-hangzhou']) == expected_plan()
    assert second.trips['trip-hangzhou'].requirements.lodging_address == ''


def test_seed_inputs_are_resolved_at_call_time(monkeypatch):
    replacement = {
        'u1': User('other-user', '另一个旅行者', '另'),
        'agent': User('other-agent', '另一个助手', '助'),
    }
    messages = demo_data.seed_messages(replacement)
    assert messages[0].sender is replacement['u1']
    assert messages[1].sender is replacement['agent']
    rotations = ['a', 'b', 'c', 'd', 'e']
    monkeypatch.setattr(store_module, 'USERS', replacement)
    store = make_store(persistence_enabled=False)
    assert store.trips['trip-hangzhou'].owner is replacement['u1']
    assert [row.sender for row in store.messages['trip-hangzhou']] == [
        replacement['u1'], replacement['agent'], replacement['u1'], replacement['agent']]


def test_restored_persisted_state_takes_precedence_over_demo_seeds(tmp_path, monkeypatch):
    # Snapshot restoration must run after all seeds, not be overwritten by a factory.
    monkeypatch.setattr(store_module, 'USERS', dict(USERS))
    path = str(tmp_path / 'demo-state.sqlite3')
    original = make_store(db_path=path, persistence_enabled=True)
    from app.workflow.counters import normalize_workflow as empty_workflow
    original.trips['trip-hangzhou'].workflow_state = empty_workflow({})
    original.trips['trip-hangzhou'].destination = '保留的目的地'
    original.messages['trip-hangzhou'][0].body = '保留的消息'
    original.messages['trip-hangzhou'].pop()
    original.trips['trip-hangzhou'].requirements.text = '保留需求'
    original.plans['trip-hangzhou'].title = '保留的行程'
    original.plans['trip-hangzhou'].days.clear()
    original.plans['trip-hangzhou'].text = '保留的正文'
    original._persist()
    expected = {
        'trip': asdict(original.trips['trip-hangzhou']),
        'messages': [asdict(row) for row in original.messages['trip-hangzhou']],
        'ideas': original.trips['trip-hangzhou'].requirements.text,
        'plan': asdict(original.plans['trip-hangzhou']),
    }
    restored = make_store(db_path=path, persistence_enabled=True)
    assert asdict(restored.trips['trip-hangzhou']) == expected['trip']
    assert [asdict(row) for row in restored.messages['trip-hangzhou']] == expected['messages']
    assert restored.trips['trip-hangzhou'].requirements.text == expected['ideas']
    assert asdict(restored.plans['trip-hangzhou']) == expected['plan']
