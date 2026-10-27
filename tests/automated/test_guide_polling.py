"""Exercise the actual preview controller when a fragment is newer than its state poll."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_newer_idle_fragment_keeps_retry_enabled_after_busy_state_poll():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required to run the preview controller')
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const button = {disabled: false};
let stateCalls = 0, scheduled;
const panel = {
  dataset: {version: 'boot'}, querySelector: () => null,
  contains: () => false, replaceWith: () => {},
};
// A request finishes after /state was read and before /partial is rendered.
const next = {
  dataset: {version: 'idle', planId: 'plan', sourceHash: 'source',
    businessBusy: 'false', requirementsRevision: '0'},
  querySelector: () => null, querySelectorAll: () => [button],
};
const controller = {
  dataset: {stateUrl: '/state', partialUrl: '/partial'},
  querySelector: () => panel, addEventListener: () => {},
};
const element = {
  setAttribute: () => {}, removeAttribute: () => {},
  addEventListener: () => {}, focus: () => {},
};
const preview = {
  ...element, dataset: {initialOpen: 'false'},
  querySelector: selector => selector === '[data-display-controller]' ? controller : element,
};
const workspace = {dataset: {}, querySelector: () => element};
const document = {
  hidden: false,
  querySelector: selector => selector === '[data-guide-preview]' ? preview : workspace,
  addEventListener: () => {},
  createElement: () => ({content: {querySelector: () => next}}),
};
const context = {
  document, matchMedia: () => ({matches: false, addEventListener: () => {}}),
  location: {origin: 'https://localhost'}, URL, AbortSignal,
  setTimeout: callback => {scheduled = callback; return 1;}, clearTimeout: () => {},
  fetch: async url => url === '/state'
    ? {ok: true, json: async () => {
      const busy = stateCalls++ === 0;
      return {version: busy ? 'busy' : 'idle', busy, plan_id: 'plan',
        source_hash: 'source', requirements_revision: 0};
    }}
    : {ok: true, text: async () => '<fragment>'},
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
setImmediate(async () => {
  try {
    assert.equal(button.disabled, false, 'the newer idle fragment must enable retry');
    await scheduled();
    assert.equal(stateCalls, 2);
    assert.equal(button.disabled, false, 'an unchanged version must keep retry enabled');
  } catch (error) {
    console.error(error);
    process.exitCode = 1;
  }
});
"""
    controller = Path(__file__).resolve().parents[2] / 'app/static/guide.js'
    result = subprocess.run(
        [node, '-e', script, str(controller)], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout
