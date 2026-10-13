"""Actual SDK phases expose safe diagnostics for initialization/discovery/tools."""
import json
import pytest
from app.tools import amap_mcp
from test_amap_mcp import mcp_server


@pytest.mark.parametrize('method,expected_stage', [('initialize', 'initialize'), ('list_tools', 'discovery'), ('call_tool', 'tool')])
def test_failures_identify_phase_without_exposing_provider_message(monkeypatch, mcp_server, method, expected_stage):
    async def fail(*args, **kwargs):
        raise RuntimeError('https://mcp.amap.com/mcp?key=private-test-key')
    monkeypatch.setattr(amap_mcp.ClientSession, method, fail)
    phases = []
    with pytest.raises(amap_mcp.AmapMCPError) as error:
        with amap_mcp.observe_phases(phases.append), amap_mcp.session() as tools:
            tools.call('maps_text_search', {'city': '甲城', 'keywords': '站'})
    assert error.value.stage == expected_stage
    assert any(row['stage'] == expected_stage and row['status'] == 'failed' for row in phases)
    assert 'private-test-key' not in str(error.value) + json.dumps(phases)
    assert error.value.tool == ('maps_text_search' if method == 'call_tool' else None)


def test_success_phases_and_bad_diagnostics_callback_do_not_change_query(mcp_server):
    phases = []
    with amap_mcp.observe_phases(phases.append), amap_mcp.session() as tools:
        tools.call('maps_text_search', {'city': '甲城', 'keywords': '站'})
    assert [row['stage'] for row in phases] == ['initialize', 'discovery', 'tool']
    assert all(row['seconds'] >= 0 and row['status'] == 'completed' for row in phases)
    def broken(row):
        raise ValueError('observer unavailable')
    with amap_mcp.observe_phases(broken), amap_mcp.session() as tools:
        assert tools.call('maps_text_search', {'city': '甲城', 'keywords': '站'})['pois']


def test_initialization_timeout_has_correct_phase(monkeypatch, mcp_server):
    import anyio
    monkeypatch.setenv('AMAP_MCP_TIMEOUT_SECONDS', '0.03')
    async def delayed(*args, **kwargs):
        await anyio.sleep(1)
    monkeypatch.setattr(amap_mcp.ClientSession, 'initialize', delayed)
    phases = []
    with pytest.raises(amap_mcp.AmapMCPError) as error:
        with amap_mcp.observe_phases(phases.append), amap_mcp.session():
            pytest.fail('timed out initialization must not yield a client')
    assert error.value.stage == 'initialize'
    assert phases[0]['status'] == 'failed'
