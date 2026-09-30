const assert = require('node:assert/strict');
const path = require('node:path');
const { chromium } = require('playwright');

// Browser regressions use synthetic messages and the real frontend script.
// Run with Playwright available to Node: node tests/chat_browser.cjs
(async () => {
  const browser = await chromium.launch({ channel: 'chrome' });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  try {
    await page.route('http://chat.test/**', route => route.fulfill({
      contentType: 'text/html', body: '<main></main>',
    }));
    await page.goto('http://chat.test/');
    await page.setContent(`
      <section class="chat-list" data-chat-list data-messages-url="/partial"></section>
      <form class="composer" data-api-url="/send"><input name="body"><button type="submit">Send</button></form>
    `);
    await page.addScriptTag({ path: path.resolve(__dirname, '../app/static/app.js') });
    const result = await page.evaluate(async () => {
      const list = document.querySelector('[data-chat-list]');
      const input = document.querySelector('input');
      const form = document.querySelector('form');
      const message = (id, body, thinking = false) => `<article class="message agent" data-message-id="${id}" ${thinking ? 'data-agent-thinking-message="true" data-status="thinking"' : ''}><div class="message-body"><p>${body}</p><details><summary>Card</summary>Details</details></div></article>`;
      let remote = message('a1', 'AI response');
      window.fetch = async () => new Response(remote, { headers: { 'Content-Type': 'text/html' } });
      input.value = 'unsent draft';
      await refreshChatMessages(list);
      const whileTyping = list.textContent.includes('AI response') && input.value === 'unsent draft';
      list.querySelector('details').open = true;
      await refreshChatMessages(list);
      const detailsStayOpen = list.querySelector('details').open;
      const failed = appendUserMessage('failed text');
      markMessageFailed(failed, 'failed text');
      remote += message('a2', 'updated reply');
      await refreshChatMessages(list);
      const retrySurvivesPoll = list.contains(failed) && !!failed.querySelector('[data-retry-message]');

      // A failed second send must leave the first request's thinking indicator alone.
      list.innerHTML = message('old-thinking', 'Thinking', true);
      window.fetch = async () => new Response('failed', { status: 500 });
      input.value = 'second message';
      await submitComposerMessage(form);
      const previousThinkingSurvives = !!list.querySelector('[data-message-id="old-thinking"]');
      const failedOnlyOwnMessage = list.querySelectorAll('[data-agent-thinking-message]').length === 1
        && list.querySelectorAll('.failed-message').length === 1 && !input.disabled;

      // A GET already in flight cannot erase a newly submitted local bubble.
      list.innerHTML = message('a1', 'existing');
      let release;
      window.fetch = () => new Promise(resolve => { release = resolve; });
      const stalePoll = refreshChatMessages(list);
      form.dataset.submitting = 'true';
      const pending = appendUserMessage('just sent');
      release(new Response(message('a1', 'existing')));
      await stalePoll;
      const pendingSurvives = list.contains(pending);
      form.dataset.submitting = 'false';

      // A poll timeout must release the in-flight guard so the next poll can work.
      window.fetch = async () => { throw new DOMException('timeout', 'TimeoutError'); };
      await refreshChatMessages(list);
      window.fetch = async () => new Response(message('final', 'recovered'));
      await refreshChatMessages(list);
      const pollingRecovers = list.textContent.includes('recovered');
      return { whileTyping, detailsStayOpen, retrySurvivesPoll, previousThinkingSurvives,
        failedOnlyOwnMessage, pendingSurvives, pollingRecovers };
    });
    for (const [name, passed] of Object.entries(result)) {
      assert.equal(passed, true, name);
      console.log(`PASS ${name}`);
    }
    assert.deepEqual(errors, [], 'browser runtime errors');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
