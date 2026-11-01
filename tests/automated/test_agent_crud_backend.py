"""Offline tests for the direct-agent transactional Store boundary."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.services.agent_crud import prepare_changes, exact_edit
from app.storage.store import DemoStore
from app.workflow.turn_commit import TurnPersistenceError
from app.workflow.execution import ExecutionResult


def test_delete_trip_cleans_model_runs_and_rejects_late_model_log(tmp_path):
    import sqlite3
    from app.storage.persistence import insert_agent_run
    database = tmp_path / 'cleanup.sqlite3'
    saved = DemoStore(db_path=str(database), persistence_enabled=True, display_worker_enabled=False)
    saved._persist()  # Materialize demo seed snapshot before writing bound traces.
    trip = 'trip-hangzhou'
    row = {'request_id': 'normal-run', 'trip_id': trip, 'user_input': '测试',
           'ok': True, 'created_at': '2030-01-01T00:00:00'}
    insert_agent_run(database, row)
    insert_agent_run(database, row | {'request_id': 'unbound-run', 'trip_id': None})
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT COUNT(*) FROM agent_runs WHERE trip_id=?', (trip,)).fetchone()[0] == 1
    assert saved.delete_trip(trip)
    insert_agent_run(database, row | {'request_id': 'late-run'})
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT request_id FROM agent_runs').fetchall() == [('unbound-run',)]
        assert connection.execute('SELECT COUNT(*) FROM trips WHERE id=?', (trip,)).fetchone()[0] == 0
    restored = DemoStore(db_path=str(database), persistence_enabled=True, display_worker_enabled=False)
    assert trip not in restored.trips
    assert trip not in restored.messages and trip not in restored.plans and trip not in restored.idea_cards


def test_agent_run_existence_check_holds_immediate_write_transaction(monkeypatch, tmp_path):
    import sqlite3
    from app.storage import persistence
    database = tmp_path / 'serialized-run.sqlite3'
    saved = DemoStore(db_path=str(database), persistence_enabled=True, display_worker_enabled=False)
    saved._persist()
    real_connect = sqlite3.connect
    checked = []

    class CheckedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.lstrip().startswith('SELECT 1 FROM trips WHERE id'):
                assert self.in_transaction, 'trip check must not run in autocommit'
                # A deferred read transaction still races an independent writer.
                # BEGIN IMMEDIATE must reserve the writer before this SELECT.
                with real_connect(database, timeout=0) as competing:
                    with pytest.raises(sqlite3.OperationalError, match='locked'):
                        competing.execute('BEGIN IMMEDIATE')
                checked.append(parameters)
            return super().execute(sql, parameters)

    monkeypatch.setattr(persistence.sqlite3, 'connect',
        lambda *args, **kwargs: real_connect(*args, factory=CheckedConnection, **kwargs))
    row = {'request_id': 'serialized-run', 'trip_id': 'trip-hangzhou', 'user_input': '测试',
           'ok': True, 'created_at': '2030-01-01T00:00:00'}
    persistence.insert_agent_run(database, row)
    assert checked == [('trip-hangzhou',)]
    with real_connect(database) as connection:
        assert connection.execute('SELECT request_id FROM agent_runs').fetchall() == [('serialized-run',)]
    assert saved.delete_trip('trip-hangzhou')
    persistence.insert_agent_run(database, row | {'request_id': 'late-run'})
    assert checked == [('trip-hangzhou',), ('trip-hangzhou',)]
    with real_connect(database) as connection:
        assert connection.execute('SELECT COUNT(*) FROM agent_runs').fetchone()[0] == 0


@pytest.fixture
def store():
    return DemoStore(persistence_enabled=False, display_worker_enabled=False)


def execution(store):
    key = 'trip-hangzhou'
    return SimpleNamespace(trip=deepcopy(store.trips[key]), plan=deepcopy(store.plans[key]),
                           idea_cards=deepcopy(store.idea_cards[key]), display_target=None)


def proposal(store, body, args, *, task=True, kind='revision'):
    key = 'trip-hangzhou'
    if task:
        # Exercise the same sealed transaction through chat or a legacy task.
        payload = {'feedback': body} if kind == 'revision' else {'body': body}
        row, _ = store.submit_task(key, kind, payload, 'request-' + str(len(store.tasks)))
        store.tasks[row['id']]['status'] = 'running'
        context = store.capture_request_context(key, row['id'])
    else:
        context = store.capture_request_context(key, body=body,
                                                entry='main' if kind == 'chat' else 'revision')
    payload = prepare_changes({'user_quote': body, **args}, context, execution(store), [])
    return context, ExecutionResult(changes=payload)


@pytest.mark.parametrize('task', [False, True])
def test_normal_chat_commits_authorized_changes_atomically(store, task):
    context, candidate = proposal(store, '请记下我想去西湖，必去地点添加西湖', {
        'requirements_patch': {'must_visit': {'add': ['西湖']}},
        'ideas': [{'action': 'create', 'kind': '地点', 'title': '西湖', 'body': '我想去西湖'}],
    }, task=task, kind='chat')
    assert context.entry == 'main'
    assert candidate.changes is not None
    before = len(store.idea_cards[context.trip_id])
    receipt = store.commit_turn(context, candidate)
    assert receipt.committed
    assert store.trips[context.trip_id].requirements.must_visit == ['西湖']
    assert len(store.idea_cards[context.trip_id]) == before + 1


@pytest.mark.parametrize('body', ['你好', '必去地点添加西湖，记下晚上夜骑，并重新规划行程'])
def test_normal_chat_text_commit_preserves_business_state_and_never_triggers_display(store, monkeypatch, body):
    key = 'trip-hangzhou'
    row, _ = store.submit_task(key, 'chat', {'body': body}, 'readonly-chat')
    store.tasks[row['id']]['status'] = 'running'
    context = store.capture_request_context(key, row['id'])
    assert context.entry == 'main'
    before = deepcopy((store.trips[key].requirements, store.plans[key],
                       store.idea_cards[key], store.display_jobs))
    message_ids = [message.id for message in store.messages[key]]
    # Fail even if a staged Store attempts display work, with display enabled.
    monkeypatch.setattr(DemoStore, 'display_enabled', lambda self: True)

    def unexpected_display(*args, **kwargs):
        pytest.fail('ordinary chat must not queue, retry, or wake display generation')

    for method in ('_queue_display', 'retry_display', '_notify_display_worker'):
        monkeypatch.setattr(DemoStore, method, unexpected_display)
    answer = '我可以解释当前行程。'
    candidate = ExecutionResult(answer=answer)
    assert candidate.changes is None
    receipt = store.commit_turn(context, candidate)
    assert receipt.committed
    assert receipt.accepted_artifact_ids == ()
    assert receipt.task_status == 'succeeded'
    assert (store.trips[key].requirements, store.plans[key],
            store.idea_cards[key], store.display_jobs) == before
    assert [message.id for message in store.messages[key]] == message_ids
    reply = next(message for message in store.messages[key] if message.id == receipt.message_id)
    assert reply.id == row['thinking_id']
    assert reply.body == answer
    assert reply.status == 'sent'
    assert store.tasks[row['id']]['status'] == 'succeeded'
    assert store.tasks[row['id']]['committed_effect_kinds'] == []
    assert store.commit_turn(context, candidate) == receipt


def test_sparse_requirement_and_idea_are_one_commit(store):
    context, candidate = proposal(store, '请记下我想去西湖，必去地点添加西湖', {
        'requirements_patch': {'must_visit': {'add': ['西湖']}},
        'ideas': [{'action': 'create', 'kind': '地点', 'title': '西湖', 'body': '我想去西湖'}],
    })
    before = len(store.idea_cards[context.trip_id])
    receipt = store.commit_turn(context, candidate)
    assert receipt.committed
    assert store.trips[context.trip_id].requirements.must_visit == ['西湖']
    assert len(store.idea_cards[context.trip_id]) == before + 1
    assert store.tasks[context.execution_task_id]['committed_effect_kinds'] == ['requirements', 'ideas']
    assert store.commit_turn(context, candidate) == receipt


def test_same_request_rejects_different_digest(store):
    context, candidate = proposal(store, '请记下我想去西湖', {
        'ideas': [{'action': 'create', 'kind': '地点', 'title': '西湖', 'body': '我想去西湖'}]})
    store.commit_turn(context, candidate)
    changed = replace(candidate, answer='另一个结果')
    with pytest.raises(ValueError, match='摘要'):
        store.commit_turn(context, changed)


def test_concurrent_idea_update_rejects_whole_unit(store):
    context, candidate = proposal(store, '更新龙井茶园想法，并将必去地点添加西湖', {
        'requirements_patch': {'must_visit': {'add': ['西湖']}},
        'ideas': [{'action': 'update', 'idea_id': 'idea-place-2', 'changes': {'title': '慢游龙井'}}],
    })
    cards = store.idea_cards[context.trip_id]
    cards[1] = replace(cards[1], body='用户已在页面修改')
    before = deepcopy(store.trips[context.trip_id])
    with pytest.raises(ValueError, match='想法'):
        store.commit_turn(context, candidate)
    assert store.trips[context.trip_id] == before
    assert cards[1].title == '龙井茶园'


def test_staging_failure_does_not_publish_business_subset(store, monkeypatch):
    context, candidate = proposal(store, '更新龙井茶园想法，并将必去地点添加西湖', {
        'requirements_patch': {'must_visit': {'add': ['西湖']}},
        'ideas': [{'action': 'update', 'idea_id': 'idea-place-2', 'changes': {'title': '慢游龙井'}}],
    })
    before = deepcopy((store.trips, store.plans, store.idea_cards, store.messages, store.tasks))
    monkeypatch.setattr('app.workflow.turn_commit.save_snapshot', lambda *a, **kw: (_ for _ in ()).throw(OSError('disk full')))
    store.persistence_enabled = True
    with pytest.raises(TurnPersistenceError):
        store.commit_turn(context, candidate)
    assert (store.trips, store.plans, store.idea_cards, store.messages, store.tasks) == before


@pytest.mark.parametrize('body', ['不要清空行程', '如果清空行程会怎样', '上次说清空行程', '清空聊天', '清空行程并删除旅行', '是的'])
def test_clear_needs_full_current_positive_command(store, body):
    context = store.capture_request_context('trip-hangzhou', body=body)
    with pytest.raises(ValueError):
        prepare_changes({'user_quote': body, 'itinerary': {'action': 'clear'}}, context, execution(store))


def test_clear_removes_only_plan_and_display_jobs(store):
    key = 'trip-hangzhou'
    before_trip = deepcopy(store.trips[key])
    cards = deepcopy(store.idea_cards[key])
    old_messages = [row.id for row in store.messages[key]]
    context, candidate = proposal(store, '请清空当前行程', {'itinerary': {'action': 'clear'}})
    store.display_jobs['job-test'] = {'trip_id': key, 'status': 'queued'}
    receipt = store.commit_turn(context, candidate)
    assert receipt.committed
    assert not store.plans[key].has_content
    assert store.trips[key].requirements == before_trip.requirements
    assert store.idea_cards[key] == cards
    assert all(item in [row.id for row in store.messages[key]] for item in old_messages)
    assert not any(row['trip_id'] == key for row in store.display_jobs.values())


def test_exact_edit_requires_full_quote_and_unique_user_replacement(store):
    store.plans['trip-hangzhou'].text = '09:00 西湖\n10:00 龙井'
    body = '把“09:00 西湖”改为“09:30 西湖”'
    context, candidate = proposal(store, body, {'itinerary': {
        'action': 'edit_text', 'operation': 'replace', 'old_text': '09:00 西湖', 'new_text': '09:30 西湖'}})
    old_basis = deepcopy(store.plans[context.trip_id].requirements_snapshot)
    store.commit_turn(context, candidate)
    assert store.plans[context.trip_id].text == '09:30 西湖\n10:00 龙井'
    assert store.plans[context.trip_id].requirements_snapshot == old_basis
    with pytest.raises(ValueError):
        exact_edit('西湖', {'action': 'edit_text', 'operation': 'replace', 'old_text': '西湖', 'new_text': '灵隐'}, '把西湖改为龙井')


def test_edit_quote_glyph_normalization_and_ambiguous_reference():
    result = exact_edit('提示：“早点出发”', {'action': 'edit_text', 'operation': 'replace',
        'old_text': '“早点出发”', 'new_text': '“晚点出发”'}, '请把“早点出发”改为"晚点出发"')
    assert result == '提示："晚点出发"'
    with pytest.raises(ValueError):
        exact_edit('西湖，西湖', {'action': 'edit_text', 'operation': 'replace',
                   'old_text': '西湖', 'new_text': '龙井'}, '请改为龙井')


def test_failed_workspace_never_retains_crud_subset(store):
    context = store.capture_request_context('trip-hangzhou', body='请记下西湖')
    payload = prepare_changes({'user_quote': context.body, 'ideas': [
        {'action': 'create', 'kind': '地点', 'title': '西湖', 'body': '西湖'}]}, context, execution(store))
    assert payload['args']['user_quote'] == context.body
    assert ExecutionResult.failure('budget exhausted').changes is None


@pytest.mark.parametrize('body', ['删掉午餐', '不要删除龙井茶园', '如果删除龙井茶园', '他说“删除龙井茶园”'])
def test_unrelated_negative_hypothetical_or_quoted_delete_is_not_authority(store, body):
    context = store.capture_request_context('trip-hangzhou', body=body)
    with pytest.raises(ValueError):
        prepare_changes({'user_quote': body, 'ideas': [
            {'action': 'delete', 'idea_id': 'idea-place-2'}]}, context, execution(store))


@pytest.mark.parametrize('body', ['不要确认当前行程', '如果确认行程', '他说“确认当前行程”'])
def test_confirmation_requires_current_positive_instruction(store, body):
    context = store.capture_request_context('trip-hangzhou', body=body)
    with pytest.raises(ValueError):
        prepare_changes({'user_quote': body, 'itinerary': {'action': 'confirm'}}, context, execution(store))


def test_append_nullable_old_text_and_context():
    assert exact_edit('西湖', {'action': 'edit_text', 'operation': 'append',
                            'old_text': None, 'context': None, 'new_text': '早点回酒店'},
                      '请追加一行：早点回酒店') == '西湖\n早点回酒店'


def test_invalid_idea_kind_rejected_at_backend(store):
    context = store.capture_request_context('trip-hangzhou', body='记录西湖')
    with pytest.raises(ValueError, match='分类'):
        prepare_changes({'user_quote': context.body, 'ideas': [
            {'action': 'create', 'kind': '任意类型', 'title': '西湖', 'body': '西湖'}]}, context, execution(store))


def ready_for_plan(store):
    trip = store.trips['trip-hangzhou']
    trip.requirements.city = '杭州'
    trip.requirements.lodging_address = '湖滨路1号'
    trip.requirements.start_date = trip.requirements.end_date = '2030-05-01'
    trip.requirements.day_count = 1
    trip.lodging = {'name': '湖滨酒店'}


def test_combined_replace_covers_retained_ids_and_new_idea_body(store):
    ready_for_plan(store)
    ids = [card.id for card in store.idea_cards['trip-hangzhou'] if card.id != 'idea-place-2']
    text = '新行程：上午西湖，晚上夜骑。'
    context, candidate = proposal(store, '请删除龙井茶园想法，记下晚上夜骑，并重新规划行程', {
        'ideas': [{'action': 'delete', 'idea_id': 'idea-place-2'},
                  {'action': 'create', 'kind': '地点', 'title': '夜骑', 'body': '晚上夜骑'}],
        'itinerary': {'action': 'replace', 'text': text, 'idea_outcomes': [
            {'idea_ref': key, 'status': 'scheduled', 'reason': '上午西湖'} for key in ids]},
        'next_question': '还需要调整什么？',
    })
    receipt = store.commit_turn(context, candidate)
    assert receipt.committed
    assert store.plans[context.trip_id].text == text
    assert not any(card.id == 'idea-place-2' for card in store.idea_cards[context.trip_id])
    assert any(card.body == '晚上夜骑' for card in store.idea_cards[context.trip_id])
    assert store.tasks[context.execution_task_id]['needs_input']


def test_full_plan_coverage_cannot_omit_existing_idea(store):
    ready_for_plan(store)
    context = store.capture_request_context('trip-hangzhou', body='请生成行程')
    with pytest.raises(ValueError, match='覆盖'):
        prepare_changes({'user_quote': context.body, 'itinerary': {
            'action': 'replace', 'text': '上午西湖', 'idea_outcomes': []}}, context, execution(store))


def test_plan_builder_failure_cannot_save_requirement_subset(store, monkeypatch):
    ready_for_plan(store)
    context, candidate = proposal(store, '添加必去西湖，并生成行程', {
        'requirements_patch': {'must_visit': {'add': ['西湖']}},
        'itinerary': {'action': 'replace', 'text': '上午西湖', 'idea_outcomes': [
            {'idea_ref': card.id, 'status': 'scheduled', 'reason': '上午西湖'}
            for card in store.idea_cards['trip-hangzhou']]}})
    before = deepcopy((store.trips, store.plans, store.idea_cards, store.messages, store.tasks))
    monkeypatch.setattr(DemoStore, '_apply_text_plan', lambda *a, **kw: (_ for _ in ()).throw(ValueError('invalid plan')))
    with pytest.raises(ValueError, match='invalid plan'):
        store.commit_turn(context, candidate)
    assert (store.trips, store.plans, store.idea_cards, store.messages, store.tasks) == before


def test_standalone_confirmation_compatibility(store):
    receipt = store.confirm_current_turn('trip-hangzhou')
    assert receipt.committed
    assert store.plans['trip-hangzhou'].status == '已确认'


def test_deleted_trip_result_returns_failure_without_resurrection(store):
    context, result = proposal(store, '记录西湖', {'ideas': [
        {'action': 'create', 'kind': '地点', 'title': '西湖', 'body': '西湖'}]}, task=False)
    assert store.delete_trip(context.trip_id)
    receipt = store.commit_turn(context, result)
    assert not receipt.committed and '删除' in receipt.reason
    assert context.trip_id not in store.trips and context.trip_id not in store.idea_cards


def test_direct_result_detaches_changes_and_rechecks_failure_before_save(store):
    context, result = proposal(store, '记录西湖', {'ideas': [
        {'action': 'create', 'kind': '地点', 'title': '西湖', 'body': '西湖'}]})
    source = deepcopy(result.changes)
    detached = ExecutionResult(changes=source)
    source['args']['ideas'][0]['body'] = '后来修改的内容'
    assert detached.changes == result.changes
    result.failed = True
    before = deepcopy((store.trips, store.idea_cards, store.plans, store.messages))
    with pytest.raises(ValueError, match='失败结果'):
        store.commit_turn(context, result)
    assert (store.trips, store.idea_cards, store.plans, store.messages) == before


def test_failed_result_cannot_include_changes(store):
    _, result = proposal(store, '记录西湖', {'ideas': [
        {'action': 'create', 'kind': '地点', 'title': '西湖', 'body': '西湖'}]})
    with pytest.raises(ValueError, match='失败结果'):
        ExecutionResult(changes=result.changes, failed=True)


@pytest.mark.parametrize('field,value', [
    ('owner_id', 'another-owner'), ('trip_id', 'another-trip'),
    ('request_id', 'another-request'), ('message_id', 'another-message'),
    ('execution_task_id', 'another-task'), ('requirements_revision', 999),
    ('workflow_revision', 999), ('sequence', 999), ('epoch', 999),
])
def test_direct_changes_cannot_be_committed_under_another_request(store, field, value):
    context, result = proposal(store, '记录西湖', {'ideas': [
        {'action': 'create', 'kind': '地点', 'title': '西湖', 'body': '西湖'}]})
    before = deepcopy((store.trips, store.idea_cards, store.plans, store.messages))
    foreign = replace(context, **{field: value})
    try:
        receipt = store.commit_turn(foreign, result)
        assert receipt is None or not receipt.committed
    except ValueError:
        pass
    assert (store.trips, store.idea_cards, store.plans, store.messages) == before


def test_historic_workflow_and_receipt_are_readable_but_cannot_be_authored(store):
    from app.workflow.execution import serialize
    from app.workflow.task_context import normalize_workflow
    key = 'trip-hangzhou'
    source = store.messages[key][0].id
    history = normalize_workflow({'schema_version': 2, 'revision': 4, 'sequence': 0,
        'epoch': 0, 'tasks': [{'id': 'historic-task', 'goal': '旧会话目标',
            'status': 'completed', 'source_message_ids': [source]}]})
    store.trips[key].workflow_state = deepcopy(history)
    context = store.capture_request_context(key, body='你好')
    candidate = ExecutionResult(answer='你好')
    receipt = store.commit_turn(context, candidate)
    saved = store.trips[key].workflow_state
    assert saved['tasks'] == history['tasks']
    assert receipt.conversation_task_states['historic-task'] == 'completed'
    assert serialize(receipt)['conversation_task_states'] == {'historic-task': 'completed'}


def test_direct_change_mutation_and_current_plan_conflict_are_rejected(store):
    key = 'trip-hangzhou'
    context, candidate = proposal(store, '确认当前行程', {'itinerary': {'action': 'confirm'}})
    tampered = deepcopy(candidate)
    tampered.changes['args']['itinerary']['action'] = 'clear'
    with pytest.raises(ValueError, match='摘要'):
        store.commit_turn(context, tampered)
    store.plans[key].text = '此时已由另一个请求保存的新行程'
    before = deepcopy((store.trips[key], store.plans[key], store.messages[key]))
    receipt = store.commit_turn(context, candidate)
    assert not receipt.committed
    assert (store.trips[key], store.plans[key], store.messages[key]) == before


def test_historic_receipt_replay_keeps_rejected_artifact_details(store):
    from app.workflow.execution import CommitReceipt, serialize
    context, candidate = proposal(store, '记录西湖', {'ideas': [
        {'action': 'create', 'kind': '地点', 'title': '西湖', 'body': '西湖'}]})
    receipt = store.commit_turn(context, candidate)
    historical = replace(receipt, rejected_artifacts=({'artifact_id': 'legacy-artifact', 'reason': '旧候选未采用'},))
    # Saved receipts may predate the removal of executable partial-effect commits.
    store.tasks[context.execution_task_id]['receipt'] = serialize(historical)
    replayed = store.commit_turn(context, candidate)
    assert isinstance(replayed, CommitReceipt)
    assert replayed.rejected_artifacts == historical.rejected_artifacts
