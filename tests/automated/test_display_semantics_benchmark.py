import json
from types import SimpleNamespace
import pytest
from app import agent_client
from tests.performance.run_display_semantics import run
from tests.performance.review_display_semantics import build_report


def test_semantic_benchmark_stops_after_two_transport_failures_and_preserves_them(monkeypatch, tmp_path):
    monkeypatch.setenv('TRAVEL_PERSISTENCE', '0')
    calls = []
    def timeout(source, dates, **kwargs):
        calls.append(source)
        return {'display': None, 'display_status': 'fallback', 'error_type': 'APITimeoutError'}
    monkeypatch.setattr(agent_client, 'generate_plan_display', timeout)
    args = SimpleNamespace(output=tmp_path/'run', repeats=3, cases=None, before=None, max_consecutive_failures=2)
    assert run(args) == 2
    assert len(calls) == 2
    status = json.loads((args.output/'status.json').read_text())
    assert status['status'] == 'blocked' and status['planned_samples'] == 12
    rows = json.loads((args.output/'results.json').read_text(encoding='utf-8'))
    assert len(rows) == 2 and [r['attempt'] for r in rows] == [1, 2]
    checks = json.loads((args.output/'comparison/checks.json').read_text(encoding='utf-8'))
    assert all(r['quality_grade'] == 'not_evaluated' and not r['available'] for r in checks)
    with pytest.raises(ValueError, match='empty'):
        run(args)
    assert len(calls) == 2  # Existing evidence must never be overwritten or rerun.


def test_semantic_comparison_rejects_changed_source_instead_of_claiming_improvement(tmp_path):
    changed = tmp_path/'changed.json'
    changed.write_text(json.dumps([{'case_id':'LAT-012', 'source_text':'changed prose'}]), encoding='utf-8')
    with pytest.raises(ValueError, match='Source changed'):
        build_report(None, changed, tmp_path/'report')
    assert not (tmp_path/'report/checks.json').exists()
