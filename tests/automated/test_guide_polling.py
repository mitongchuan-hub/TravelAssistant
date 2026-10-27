"""Exercise the actual preview controller when a fragment is newer than its state poll."""
from pathlib import Path
import shutil
import subprocess

import pytest


def run_controller_checks(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required to run the preview controller')
    controller = Path(__file__).resolve().parents[2] / 'app/static/guide.js'
    result = subprocess.run(
        [node, '-e', script, str(controller)], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_newer_idle_fragment_keeps_retry_enabled_after_busy_state_poll():
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
    run_controller_checks(script)


def test_preview_controls_and_current_download_work_at_every_screen_width():
    run_controller_checks(r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let scheduled, stateCalls = 0;
function control() {
  const node = {
    attrs: {}, handlers: {}, hidden: false,
    addEventListener: (type, callback) => {node.handlers[type] = callback;},
    setAttribute: (name, value) => {node.attrs[name] = String(value);},
    removeAttribute: name => {delete node.attrs[name];},
    hasAttribute: name => name in node.attrs,
    getClientRects: () => [{}],
    focus: () => {document.activeElement = node;},
  };
  return node;
}
function fragment(version, downloadUrl) {
  return {
    dataset: {version, planId: 'plan', sourceHash: 'source',
      requirementsRevision: '0', businessBusy: 'false', downloadUrl},
    querySelector: () => null, querySelectorAll: () => [],
    contains: () => false, replaceWith: () => {},
  };
}
const panel = fragment('initial', '/guide?version=old&download=1');
let next = fragment('new', '/guide?version=new&download=1');
const opener = control(), closer = control(), download = control();
download.hidden = true;
const chat = {inert: false};
const controller = {...control(), dataset: {stateUrl: '/state', partialUrl: '/partial'},
  querySelector: () => panel};
const preview = {...control(), dataset: {initialOpen: 'false'},
  querySelector: selector => ({'[data-display-controller]': controller,
    '[data-guide-close]': closer, '[data-guide-download]': download})[selector],
  querySelectorAll: () => [closer, download]};
const workspace = {dataset: {}, querySelector: selector =>
  selector === '.chat-screen' ? chat : opener};
const document = {
  hidden: false, activeElement: null,
  querySelector: selector => selector === '[data-guide-preview]' ? preview : workspace,
  addEventListener: () => {},
  createElement: () => ({content: {querySelector: () => next}}),
};
const context = {
  document, matchMedia: () => ({matches: false, addEventListener: () => {}}),
  location: {origin: 'https://localhost'}, URL, AbortSignal,
  setTimeout: callback => {scheduled = callback; return 1;}, clearTimeout: () => {},
  fetch: async url => url === '/state'
    ? {ok: true, json: async () => ({version: ++stateCalls === 1 ? 'new' : 'empty',
      plan_id: 'plan', source_hash: 'source'})}
    : {ok: true, text: async () => '<fragment>'},
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
assert.equal(opener.attrs['aria-expanded'], 'false');
assert.equal(download.hidden, false);
assert.equal(download.attrs.href, '/guide?version=old&download=1');
opener.handlers.click();
assert.equal(chat.inert, true);
assert.equal(preview.attrs['aria-modal'], 'true');
assert.equal(document.activeElement, closer);
preview.handlers.keydown({key: 'Escape'});
assert.equal(chat.inert, false);
assert.equal(document.activeElement, opener);
setImmediate(async () => {
  try {
    assert.equal(download.attrs.href, '/guide?version=new&download=1');
    next = fragment('empty', '');
    await scheduled();
    assert.equal(download.hidden, true);
    assert.equal('href' in download.attrs, false);
  } catch (error) {
    console.error(error);
    process.exitCode = 1;
  }
});
""")
