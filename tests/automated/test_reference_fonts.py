"""Reference font compatibility never changes stored text or substantive wording."""
from copy import deepcopy
import pytest
from app.agent_runtime import _unique_reference
from test_literal_edit import runtime, args, SOURCE


def test_quote_fonts_match_complete_reference_but_word_changes_do_not():
    user = '只追加“提醒：带伞。”，其他不变。'
    current = runtime(user)
    normalized = user.replace('“', '"').replace('”', '"')
    current.check_quote({'user_quote': normalized}, whole=True)
    for changed in (normalized.replace('其他不变', '其他也改'), normalized.replace('带伞', '不带伞'), '提醒：带伞。'):
        with pytest.raises(ValueError):
            current.check_quote({'user_quote': changed}, whole=True)
    assert current.user_body == user


def test_literal_glyphs_are_restored_from_user_source_instead_of_rewritten():
    user = '追加一行：点餐时明确“不要辣、微辣也不要”。'
    current = runtime(user)
    value = args(user, new='点餐时明确"不要辣、微辣也不要"。')
    value['user_quote'] = user.replace('“', '"').replace('”', '"')
    value['old_text'] = ''
    _, reply = current.execute_tool('revision', 'edit_plan_text', value)
    assert reply.plan_text == SOURCE + '\n点餐时明确“不要辣、微辣也不要”。'
    assert current.user_body == user and current.plan.text == SOURCE


def test_source_reference_preserves_fonts_and_rejects_overlapping_or_ambiguous_matches():
    assert _unique_reference('说明：“保留原文”。', '说明："保留原文"。') == '说明：“保留原文”。'
    for source, fragment in [('aaa', 'aa'), ('“相同”与"相同"', '"相同"'), ('原句', '不存在')]:
        with pytest.raises(ValueError):
            _unique_reference(source, fragment)
