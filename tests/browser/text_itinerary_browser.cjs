// Real itinerary pages and task APIs, synthetic memory data and a prose model stub.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '../..');
(async () => {
  const reservation = net.createServer();
  await new Promise(resolve => reservation.listen(0, '127.0.0.1', resolve));
  const port = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));
  const origin = `http://127.0.0.1:${port}`;
  const server = spawn(process.env.TRAVEL_TEST_PYTHON || 'python', ['tests/browser/text_itinerary_fixture.py', String(port)], {
    cwd: root, env: {...process.env, TRAVEL_PERSISTENCE: '0', PYTHONDONTWRITEBYTECODE: '1'}, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '', startupError, browser;
  server.on('error', error => {startupError = error;});
  server.stdout.on('data', data => {output += data;});
  server.stderr.on('data', data => {output += data;});
  try {
    let ready = false;
    for (let attempt = 0; attempt < 100; attempt++) {
      if (startupError) throw startupError;
      if (server.exitCode !== null) throw new Error(output);
      try {
        if ((await fetch(origin + '/health', {signal: AbortSignal.timeout(500)})).ok) {ready = true; break;}
      } catch { /* Isolated startup. */ }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.ok(ready, output);
    browser = await chromium.launch({channel: 'chrome', headless: true});
    const context = await browser.newContext({viewport: {width: 390, height: 844}});
    const page = await context.newPage();
    page.setDefaultTimeout(12000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(origin);
    await page.locator('[name=email]').fill('text-browser@example.com');
    await page.locator('[name=password]').fill('text-browser-2026');
    await Promise.all([page.waitForURL('**/trips'), page.locator('.login-card [type=submit]').click()]);
    const workspace = await page.locator('.trip-card-link').first().getAttribute('href');
    await page.goto(origin + workspace + '?tab=itinerary');
    assert.equal(await page.getByText('还没有行程', {exact: true}).count(), 1);
    await page.getByRole('button', {name: '让 Agent 生成第一版'}).click();
    await page.waitForSelector('[data-display-itinerary]');
    await page.waitForSelector('[data-itinerary-text]', {state: 'attached'});
    assert.equal(await page.locator('.display-item').count(), 3);
    const original = await page.locator('[data-itinerary-text]').textContent();
    assert.ok(original.includes('下午：原地休息'));
    assert.ok(original.includes('<script>window.untrusted = true</script>'));
    assert.equal(await page.evaluate(() => window.untrusted), undefined);
    assert.equal(await page.locator('[data-day-nav], [data-revision-sheet]').count(), 0);
    assert.equal(await page.locator('[data-task-status]').count(), 0);
    assert.equal(await page.locator('[data-task-result]').count(), 0);
    await page.locator('[data-plan-evidence] summary').click();
    assert.equal(await page.getByText('受控天气资料', {exact: false}).isVisible(), true);
    assert.ok((await page.locator('[data-plan-evidence]').innerText()).includes('超出预报范围'));
    const evidenceText = await page.locator('[data-plan-evidence]').textContent();
    await page.locator('[data-plan-evidence] summary').click();
    for (const width of [320, 390, 430, 1000]) {
      await page.setViewportSize({width, height: 844});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `overflow at ${width}`);
      assert.equal(await page.locator('[data-itinerary-text]').textContent(), original);
    }
    await page.reload();
    assert.equal(await page.locator('[data-itinerary-text]').textContent(), original);
    console.log('PASS: generation returns to itinerary; full escaped prose survives refresh at four widths');

    await page.locator('[data-display-edit]').first().click();
    assert.ok((await page.locator('#text-revision-feedback').inputValue()).includes('西湖散步'));
    await page.locator('#text-revision-feedback').fill('下午减少步行，其他安排保持不变');
    const submitted = page.waitForResponse(res => res.request().method() === 'POST' && res.url().endsWith('/tasks'));
    await page.getByRole('button', {name: '更新行程', exact: true}).click();
    const submittedResponse = await submitted;
    const requestBody = submittedResponse.request().postDataJSON();
    assert.equal(requestBody.kind, 'revision');
    assert.equal(requestBody.payload.item_id, null);
    assert.equal(requestBody.payload.feedback, '下午减少步行，其他安排保持不变');
    await page.waitForFunction(() => document.querySelector('[data-itinerary-text]')?.textContent.includes('湖边休息，减少步行'));
    const revised = await page.locator('[data-itinerary-text]').textContent();
    assert.equal(revised, original.replace('原地休息', '湖边休息，减少步行'));
    await page.reload();
    assert.equal(await page.locator('[data-itinerary-text]').textContent(), revised);
    assert.equal(await page.locator('[data-plan-evidence]').textContent(), evidenceText);
    assert.equal(await page.locator('[data-task-status]').count(), 0);
    assert.equal(await page.locator('[data-revision-feedback]').isVisible(), false);
    console.log('PASS: text revision replaces the current itinerary and preserves the recorded source/unknown status');

    await Promise.all([page.waitForNavigation(), page.getByRole('button', {name: '确认当前行程'}).click()]);
    assert.equal(await page.locator('[data-itinerary-text]').textContent(), revised);
    assert.ok((await page.locator('.itinerary-summary-card').innerText()).includes('已确认'));
    assert.equal(await page.locator('.version-history').count(), 0);
    assert.equal(await page.locator('form[action$="/plan/restore"]').count(), 0);
    await page.goto(origin + workspace + '?tab=chat');
    assert.equal(await page.locator('[data-plan-evidence]').count(), 0);
    page.once('dialog', dialog => dialog.accept());
    await page.getByLabel('更多聊天操作', {exact: true}).click();
    await Promise.all([page.waitForNavigation(), page.getByRole('button', {name: '清空聊天'}).click()]);
    await page.goto(origin + workspace + '?tab=itinerary');
    assert.equal(await page.locator('[data-itinerary-text]').textContent(), revised);
    assert.deepEqual(errors, []);
    await page.goto(origin + workspace + '?tab=chat');
    const input = page.locator('.composer textarea[name=body]');
    await input.fill('旧生成');
    await page.locator('.composer button[type=submit]').click();
    await page.waitForFunction(async () => (await (await fetch('/_test/old-started')).json()).started);
    await page.waitForFunction(() => !document.querySelector('.composer textarea').disabled && document.querySelector('.composer').dataset.submitting !== 'true');
    await input.fill('最新消息');
    await page.locator('.composer button[type=submit]').click();
    await page.locator('.chat-reply-content > p').filter({hasText: /^最新回复已完成$/}).waitFor();
    const taskUrl = origin + '/api/trips/' + workspace.split('/').pop() + '/tasks';
    const currentTasks = (await (await context.request.get(taskUrl)).json()).tasks;
    assert.equal(currentTasks[currentTasks.length - 2].status, 'superseded');
    assert.equal(currentTasks[currentTasks.length - 1].status, 'succeeded');
    assert.equal(await page.locator('[data-agent-thinking-message]').count(), 0);
    await context.request.post(origin + '/_test/release-old');
    await page.reload();
    assert.equal(await page.getByText('被废弃旧回复', {exact: true}).count(), 0);
    await page.locator('.chat-reply-content > p').filter({hasText: /^最新回复已完成$/}).waitFor();
    assert.deepEqual(errors, []);
    console.log('PASS: second message starts before first completes; old response and thinking bubble are discarded');
    await page.goto(origin + workspace + '?tab=itinerary');
    const artifacts = fs.mkdtempSync(path.join(os.tmpdir(), 'travelassistant-text-ui-'));
    await page.setViewportSize({width: 390, height: 844});
    await page.screenshot({path: path.join(artifacts, 'text-mobile.png'), fullPage: true});
    console.log('PASS: confirmation and chat clearing preserve prose; no browser runtime errors');
    console.log('Artifacts: ' + artifacts);
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) {
      const closed = new Promise(resolve => server.once('close', resolve));
      server.kill();
      await closed;
    }
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
