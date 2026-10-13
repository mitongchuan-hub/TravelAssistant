"""Literal editing uses a general operation, never feedback keyword matching."""
from copy import deepcopy
import pytest
from app.agent_runtime import TravelAgentRuntime, TurnBudget
from app.store import DemoStore
from test_agent_roles import scripted, completion, ready_for_planning, requirements_unchanged

SOURCE = '第一天：午后散步。\n备注：带水。\n第二天：自由活动。\n备注：带水。'


def runtime(user):
    local = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    tid = 'trip-hangzhou'
    ready_for_planning(local, tid)
    local._apply_text_plan(tid, SOURCE, [{'source': '保存的依据'}])
    current = TravelAgentRuntime(object(), 'fixture', {'trip': local.trips[tid], 'plan': local.plans[tid], 'user_body': user})
    current.action = 'modify'
    current.requirements_checked = True
    return current


def args(user, operation='append', old=None, new='提醒：带伞。'):
    return dict(user_quote=user, operation=operation, old_text=old, new_text=new)


def test_append_is_exact_and_preserves_original_and_evidence():
    user = '在末尾追加一行“提醒：带伞。”，其他保持。'
    current = runtime(user)
    before = deepcopy(current.plan)
    result, reply = current.execute_tool('revision', 'edit_plan_text', args(user))
    assert reply.plan_text == SOURCE + '\n提醒：带伞。'
    assert reply.evidence == before.evidence
    assert current.plan == before and result['status'] == 'edited'
    assert current.budget.external_calls == 0


def test_replace_requires_unique_context_and_keeps_other_occurrence():
    user = '第一天备注改为“带伞。”，第二天保持。'
    current = runtime(user)
    value = args(user, 'replace', '带水。', '带伞。')
    value['context'] = '第一天：午后散步。\n备注：带水。'
    _, reply = current.execute_tool('revision', 'edit_plan_text', value)
    assert reply.plan_text == '第一天：午后散步。\n备注：带伞。\n第二天：自由活动。\n备注：带水。'


@pytest.mark.parametrize('fault', ['ambiguous', 'missing_target', 'invented_text', 'quote', 'append_target', 'empty_append', 'separator', 'unreviewed', 'explain'])
def test_invalid_edits_do_not_change_plan(fault):
    user = '在末尾追加一行“提醒：带伞。”，其他保持。'
    current = runtime(user)
    value = args(user)
    if fault == 'ambiguous': value.update(operation='replace', old_text='备注：带水。')
    if fault == 'missing_target': value.update(operation='replace', old_text='不存在的内容')
    if fault == 'invented_text': value['new_text'] = '新编的建议'
    if fault == 'quote': value['user_quote'] = '提醒：带伞。'
    if fault == 'append_target': value['old_text'] = '备注：带水。'
    if fault == 'empty_append': value['new_text'] = ''
    if fault == 'separator': value['separator'] = 'paragraph'
    if fault == 'unreviewed': current.requirements_checked = False
    if fault == 'explain': current.action = 'explain'
    before = deepcopy(current.plan)
    with pytest.raises(ValueError):
        current.execute_tool('revision', 'edit_plan_text', value)
    assert current.plan == before


def test_append_uses_existing_line_boundary_and_rejects_multiline_literal():
    user = '追加一行“提醒：带伞。”。'
    current = runtime(user)
    current.plan.text = SOURCE + '\n'
    _, reply = current.execute_tool('revision', 'edit_plan_text', args(user))
    assert reply.plan_text == SOURCE + '\n提醒：带伞。'
    user = '追加文字：提醒：带伞。\n另起一行。'
    current = runtime(user)
    before = deepcopy(current.plan)
    with pytest.raises(ValueError):
        current.execute_tool('revision', 'edit_plan_text', args(user, new='提醒：带伞。\n另起一行。'))
    assert current.plan == before


def test_literal_edit_ends_loop_without_another_drafting_model(monkeypatch):
    user = '在末尾追加一行“提醒：带伞。”，其他保持。'
    local = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    tid = 'trip-hangzhou'
    ready_for_planning(local, tid)
    local._apply_text_plan(tid, SOURCE)
    calls = scripted(monkeypatch, [requirements_unchanged(), completion(calls=[('edit_plan_text', args(user))])])
    local.request_revision(tid, feedback=user)
    assert local.plans[tid].text == SOURCE + '\n提醒：带伞。'
    assert len(calls) == 2 and local.plans[tid].display_status == 'pending'
    assert all(kwargs['agent_role'] == 'revision' for _, kwargs in calls)
