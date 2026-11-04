"""Application skill loading is local methodology, never business evidence."""
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from tests.automated.test_readonly_chat_runtime import Script, call, runtime


def test_registered_reader_default_main_and_progressive_reference():
    runner = runtime(Script('完成'))
    assert 'read_skill' in runner.registry
    reader = runner.registry['read_skill']
    main = reader.execute({'name': 'travel-planning'})
    assert main['name'] == 'travel-planning' and main['resource'] == 'main'
    assert main['hash'] == hashlib.sha256(main['content'].encode()).hexdigest()
    assert main['version'] == '1'
    assert main['content'].startswith('---\nname: travel-planning\n')
    reference = reader.execute({'name': 'travel-planning', 'resource': 'output-and-revision'})
    assert reference['content'] != main['content']
    assert 'path' not in main
    assert [name for name, spec in runner.registry.items() if spec.terminal] == ['save_trip_changes']


@pytest.mark.parametrize('args', [
    {'name': '../travel-planning'}, {'name': 'travel-planning', 'resource': '../SKILL.md'},
    {'name': 'travel-planning', 'resource': 'C:\\secret'},
    {'name': 'travel-planning', 'resource': '/etc/passwd'},
    {'name': 'travel-planning', 'resource': 'https://example.com'},
    {'name': 'travel-planning', 'path': 'SKILL.md'}, {},
])
def test_reader_rejects_paths_unknown_resources_and_extra_parameters(args):
    runner = runtime(Script('完成'))
    assert 'read_skill' in runner.registry
    with pytest.raises(ValidationError):
        runner.registry['read_skill'].execute(args)


def test_skill_and_reference_reach_model_without_external_calls_or_evidence():
    script = Script([call('read_skill', {'name': 'travel-planning'}, 'skill')],
                    [call('read_skill', {'name': 'travel-planning', 'resource': 'output-and-revision'}, 'ref')],
                    '需要先补齐创建资料。')
    runner = runtime(script, body='生成同城行程')
    result = runner.run()
    assert not result.failed and result.answer == '需要先补齐创建资料。'
    assert runner.budget.tool_calls == 2 and runner.budget.external_calls == 0
    assert not runner.evidence and not result.changes
    messages = script.transcripts[-1]
    loaded = [json.loads(row['content']) for row in messages if row['role'] == 'tool']
    assert [r['resource'] for r in loaded] == ['main', 'output-and-revision']
    assert loaded[0]['content'] not in messages[0]['content']
    assert 'travel-planning' in messages[0]['content']
    assert '只记录不生成' in messages[0]['content']


def test_unknown_reader_arguments_fail_without_changing_trip():
    script = Script([call('read_skill', {'name': 'not-installed'})], '无法读取，不生成。')
    runner = runtime(script)
    result = runner.run()
    assert not result.failed and not result.changes and not runner.evidence
    assert runner.budget.external_calls == 0
    assert 'invalid_arguments' in script.transcripts[-1][-1]['content']


def test_resource_io_fails_closed_without_server_paths(monkeypatch):
    from app.agents.skills import read_skill_resource
    def missing(*args, **kwargs):
        raise OSError('C:/private/server/path')
    monkeypatch.setattr(Path, 'read_bytes', missing)
    with pytest.raises(ValueError) as exc:
        read_skill_resource('travel-planning')
    assert 'private' not in str(exc.value)


@pytest.mark.parametrize('escaped', ['travel-planning', 'output-and-revision.md'])
def test_skill_directory_and_reference_cannot_escape_root(monkeypatch, tmp_path, escaped):
    from app.agents import skills
    original = Path.resolve
    def resolve(path, *args, **kwargs):
        if path.name == escaped:
            return tmp_path / 'outside'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', resolve)
    with pytest.raises(ValueError, match='不可用'):
        skills.read_skill_resource('travel-planning', 'output-and-revision')


def test_loaded_method_stays_out_of_terminal_change_evidence():
    body = '只记录不吃辣。'
    script = Script([call('read_skill', {'name': 'travel-planning'})],
                    [call('save_trip_changes', {'user_quote': body, 'requirements_text': '不吃辣。'}, 'save')])
    runner = runtime(script, body=body)
    result = runner.run()
    assert not result.failed and result.changes
    assert result.changes['args'] == {'user_quote': body, 'requirements_text': '不吃辣。'}
    assert not runner.evidence and runner.budget.external_calls == 0
    assert 'travel-planning' not in json.dumps(result.changes, ensure_ascii=False)
    assert len(script.transcripts) == 2


def test_resolved_resource_cannot_escape_root(monkeypatch, tmp_path):
    from app.agents import skills
    original = Path.resolve
    def resolve(path, *args, **kwargs):
        if path.name == 'SKILL.md':
            return tmp_path / 'outside.md'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', resolve)
    with pytest.raises(ValueError, match='不可用'):
        skills.read_skill_resource('travel-planning')


def test_catalog_metadata_comes_from_main_document():
    from app.agents.skills import skill_catalog, read_skill_resource
    catalog = skill_catalog()
    content = read_skill_resource('travel-planning')['content']
    assert f"description: {catalog[0]['description']}" in content
    assert catalog[0]['resources'] == ['main', 'evidence-and-routing', 'output-and-revision']


def test_skill_schema_and_catalog_share_resource_allowlist():
    from app.agents.skills import skill_catalog
    reader = runtime(Script()).registry['read_skill']
    catalog = skill_catalog()[0]
    assert reader.parameters['properties']['name']['enum'] == [catalog['name']]
    assert reader.parameters['properties']['resource']['enum'] == catalog['resources']


def test_trigger_definitions_are_not_run_and_do_not_enter_model_input():
    from app.agents import skills
    path = Path(skills.__file__).parent / 'travel-planning/evals/evals.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    assert data['status'] == 'not_run' and len(data['evals']) == 3
    assert any(c['should_trigger'] for c in data['trigger_evals'])
    assert any(not c['should_trigger'] for c in data['trigger_evals'])
    script = Script('你好')
    assert not runtime(script).run().failed
    assert data['evals'][0]['prompt'] not in json.dumps(script.transcripts, ensure_ascii=False)


def test_oversized_resource_is_rejected_not_truncated(monkeypatch):
    from app.agents.skills import read_skill_resource
    monkeypatch.setattr(Path, 'read_bytes', lambda *a: b'x' * 25000)
    with pytest.raises(ValueError):
        read_skill_resource('travel-planning')
