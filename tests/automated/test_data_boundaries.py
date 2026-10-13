"""Restoration preserves content; only explicitly old displays are migrated."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from app.models import IdeaCard
from app.persistence import serialize_state, save_snapshot, load_snapshot, restore_state
from app.plan_display import validate_display, migrate_stored_display, display_format
from app.store import DemoStore, USERS
from test_agent_roles import display_data, scripted, completion
from app import agent_client


@pytest.mark.parametrize('schema_version', [1, 4, 5])
def test_reload_preserves_cards_even_with_old_placeholder_text_or_same_titles(tmp_path, schema_version):
    local = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    tid = 'trip-hangzhou'
    cards = [
        IdeaCard('keep-1', '待归类', '从一句话开始',
                 'Agent 会在对话中把地点、预算、节奏和禁忌整理成可执行的旅行约束。', 'Agent', '待整理'),
        IdeaCard('keep-2', '禁忌', '同名', 'Agent 会结合当前对话判断它属于地点、预算、节奏还是禁忌。', '你', '已整理'),
        IdeaCard('keep-3', '禁忌', '同名', '同行人对花生过敏，大家不要点花生。', '你', '待整理'),
    ]
    local.idea_cards[tid] = cards
    snapshot = serialize_state(local, USERS)
    snapshot['schema_version'] = schema_version
    database = tmp_path / 'isolated.sqlite3'
    save_snapshot(database, snapshot)
    expected = [asdict(card) for card in cards]
    for _ in range(2):
        restored = DemoStore(db_path=str(database), persistence_enabled=True, display_worker_enabled=False)
        assert [asdict(card) for card in restored.idea_cards[tid]] == expected
        assert load_snapshot(database)['idea_cards'][tid] == expected


def test_legacy_restoration_keeps_message_plan_and_card_text_verbatim():
    local = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    snapshot = serialize_state(local, USERS)
    snapshot['schema_version'] = 1
    tid = 'trip-hangzhou'
    sentence = '同行人对花生过敏。大家的旅行群里讨论成员偏好和群聊。'
    snapshot['messages'][tid][0]['body'] = sentence
    snapshot['plans'][tid]['text'] = sentence
    item = snapshot['plans'][tid]['days'][0]['items'][0]
    item.update(reason=sentence, notes=sentence, satisfies=[sentence])
    restore_state(local, snapshot, deepcopy(USERS))
    assert local.messages[tid][0].body == sentence
    assert local.plans[tid].text == sentence
    restored = local.plans[tid].days[0].items[0]
    assert restored.reason == restored.notes == sentence and restored.satisfies == [sentence]


@pytest.mark.parametrize('version', ['1.0', '1.1', None, 'future'])
def test_new_model_response_never_gets_legacy_migration(version):
    data = display_data()
    if version is None:
        del data['schema_version']
    else:
        data['schema_version'] = version
    del data['weather']
    before = deepcopy(data)
    with pytest.raises(ValidationError):
        validate_display(data)
    assert data == before


def test_only_explicit_old_stored_display_is_upgraded_without_mutating_source():
    old = display_data()
    old['schema_version'] = '1.0'
    del old['weather']
    before = deepcopy(old)
    upgraded = migrate_stored_display(old)
    assert validate_display(upgraded)['weather'] == []
    assert old == before and migrate_stored_display(upgraded) == upgraded
    current = display_data()
    del current['weather']
    assert migrate_stored_display(current) == current
    with pytest.raises(ValidationError):
        validate_display(migrate_stored_display(current))


def test_persisted_old_display_uses_read_migration():
    local = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    snapshot = serialize_state(local, USERS)
    old = display_data()
    old['schema_version'] = '1.0'
    del old['weather']
    snapshot['plans']['trip-hangzhou']['display'] = old
    restore_state(local, snapshot, deepcopy(USERS))
    assert local.plans['trip-hangzhou'].display == migrate_stored_display(old)
    assert snapshot['plans']['trip-hangzhou']['display'] == old


def test_schema_has_unique_keys_and_requires_every_top_level_field():
    def unique(items):
        names = [key for key, _ in items]
        assert len(names) == len(set(names))
        return dict(items)
    schema = json.loads(Path('app/schemas/itinerary-display-v1.json').read_text(encoding='utf-8'), object_pairs_hook=unique)
    assert set(schema['required']) == set(schema['properties'])
    assert 'weather' in display_format()['json_schema']['schema']['required']
    assert validate_display(display_data())['weather'] == []


def test_missing_weather_gets_bounded_correction_instead_of_silent_success(monkeypatch):
    invalid = display_data()
    del invalid['weather']
    calls = scripted(monkeypatch, [completion(invalid), completion(display_data())])
    result = agent_client.generate_plan_display('原文没有预报，不能新增天气。', ['2026-11-14', '2026-11-15'], 'isolated')
    assert result['display_status'] == 'ready' and len(calls) == 2
    assert 'weather' in json.loads(calls[1][0][1]['content'])['validation_error']
