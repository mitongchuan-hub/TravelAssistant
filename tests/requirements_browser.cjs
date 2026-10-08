// End-to-end requirements editing against an isolated, memory-only app.
// TRAVEL_TEST_PYTHON=<project Python> node tests/requirements_browser.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const { spawn } = require('node:child_process');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
(async () => {
  const reservation = net.createServer();
  await new Promise(resolve => reservation.listen(0, '127.0.0.1', resolve));
  const port = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));
  const origin = `http://127.0.0.1:${port}`;
  const server = spawn(process.env.TRAVEL_TEST_PYTHON || 'python', [
    '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port),
  ], {cwd: root, env: {...process.env, TRAVEL_PERSISTENCE: '0', TRAVEL_AGENT_DISABLE_LLM: '1', OPENAI_API_KEY: ''}, stdio: ['ignore', 'pipe', 'pipe']});
  let output = '';
  let startupError = null;
  server.on('error', error => { startupError = error; });
  server.stdout.on('data', data => { output += data; });
  server.stderr.on('data', data => { output += data; });
  let browser;
  try {
    let ready = false;
    for (let attempt = 0; attempt < 100; attempt++) {
      if (startupError) throw startupError;
      if (server.exitCode !== null) throw new Error(output);
      try {
        const res = await fetch(origin + '/health', {signal: AbortSignal.timeout(500)});
        if (res.ok) { ready = true; break; }
      } catch { /* Wait briefly for the isolated app to start. */ }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.ok(ready, output);
    browser = await chromium.launch({channel: 'chrome', headless: true});
    const context = await browser.newContext({viewport: {width: 390, height: 844}});
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.setDefaultTimeout(8000);
    await page.goto(origin + '/?mode=register');
    await page.locator('[name=email]').fill('requirements-browser@example.com');
    await page.locator('[name=password]').fill('requirements-browser-2026');
    await Promise.all([page.waitForURL('**/trips'), page.locator('.login-card [type=submit]').click()]);
    await page.goto(origin + '/trips/new');
    await page.locator('[name=destination]').fill('杭州旅行');
    await page.locator('[name=budget]').fill('人均 1500');
    await page.locator('[name=style]').fill('少步行');
    await Promise.all([page.waitForURL('**/workspace/**?tab=chat'), page.locator('.form-stack [type=submit]').click()]);
    const workspace = new URL(page.url()).pathname;
    await page.goto(origin + workspace + '?tab=board');
    for (let index = 0; index < 10; index++) {
      await page.locator('.wall-quick-add input').fill(`完整想法${index + 1}`);
      await Promise.all([page.waitForNavigation(), page.locator('.wall-quick-add [type=submit]').click()]);
    }
    assert.equal(await page.locator('.floating-idea').count(), 8);
    assert.equal(await page.locator('.requirements-preview, .all-ideas').count(), 0);
    assert.equal(await page.locator('link[href*="requirements.css"]').count(), 0);
    const wall = await page.locator('.idea-wall-canvas').boundingBox();
    assert.ok(wall.height >= 480, 'original wall must fill the screen');
    await page.locator('.floating-idea').first().click();
    assert.equal(await page.locator('[data-idea-detail-title]').innerText(), '完整想法3');
    await page.keyboard.press('Escape');
    await page.goto(origin + workspace + '?tab=chat');
    await page.getByRole('link', {name: '旅行需求', exact: true}).click();
    assert.equal(await page.locator('.idea-list-item').count(), 10);
    await page.locator('.all-ideas > summary').click();
    const firstIdea = page.locator('.idea-list-item').first();
    await firstIdea.click();
    assert.equal(await page.locator('[data-idea-detail-title]').innerText(), '完整想法1');
    assert.ok(await page.locator('[data-idea-delete-id]').inputValue());
    await page.keyboard.press('Escape');
    assert.equal(await firstIdea.evaluate(el => document.activeElement === el), true);
    console.log('PASS: original board restored; all ideas remain accessible from requirements');

    assert.equal(await page.locator('[name=budget_amount]').inputValue(), '1500');
    assert.equal(await page.locator('[name=budget_scope]').inputValue(), 'per_person');
    for (const width of [320, 390, 430, 1000]) {
      await page.setViewportSize({width, height: 844});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `overflow at ${width}`);
    }
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('[name=start_date]').fill('2026-11-01');
    await page.locator('[name=end_date]').fill('2026-11-03');
    await page.locator('[name=arrival_time]').fill('21:00');
    await page.locator('[name=departure_time]').fill('15:00');
    await page.locator('[name=travelers]').fill('2');
    assert.equal(await page.locator('[name=day_count]').inputValue(), '3');
    assert.equal(await page.locator('[name=day_count]').evaluate(el => el.readOnly), true);
    await page.locator('[name=budget_amount]').fill('2000');
    await page.locator('[name=budget_scope]').selectOption('total');
    await page.locator('[name=must_visit]').fill('西湖\n灵隐寺');
    await page.locator('[name=avoid]').fill('不爬山');
    await page.locator('[name=dietary]').fill('不吃海鲜\n花生过敏');
    await page.locator('[name=pace]').fill('少步行，不早起');
    await Promise.all([page.waitForNavigation(), page.locator('.requirements-form [type=submit]').click()]);
    assert.ok((await page.locator('.requirements-notice').innerText()).includes('旅行需求已保存'));
    await page.reload();
    assert.equal(await page.locator('[name=dietary]').inputValue(), '不吃海鲜\n花生过敏');
    assert.equal(await page.locator('[name=arrival_time]').inputValue(), '21:00');
    assert.equal(await page.locator('[name=departure_time]').inputValue(), '15:00');
    assert.equal(await page.locator('[name=travelers]').inputValue(), '2');
    fs.mkdirSync(path.join(root, '.cache', 'requirements-ui-check'), {recursive: true});
    await page.screenshot({path: path.join(root, '.cache', 'requirements-ui-check', 'editor-mobile.png'), fullPage: true});
    console.log('PASS: responsive editor, date calculation, budget scope, save and reload');

    const stale = await context.newPage();
    await stale.goto(origin + workspace + '/requirements');
    await page.locator('[name=budget_amount]').fill('2500');
    await Promise.all([page.waitForNavigation(), page.locator('.requirements-form [type=submit]').click()]);
    await stale.locator('[name=note]').fill('旧页面尚未保存的说明');
    const staleResponse = stale.waitForResponse(res => res.request().method() === 'POST');
    await stale.locator('.requirements-form [type=submit]').click();
    assert.equal((await staleResponse).status(), 400);
    assert.ok((await stale.locator('[role=alert]').innerText()).includes('已经更新'));
    assert.equal(await stale.locator('[name=note]').inputValue(), '旧页面尚未保存的说明');
    await page.reload();
    assert.equal(await page.locator('[name=budget_amount]').inputValue(), '2500');
    await stale.close();
    console.log('PASS: stale form preserves unsaved input without overwriting new values');

    await page.goto(origin + workspace + '?tab=chat');
    page.once('dialog', dialog => dialog.accept());
    await Promise.all([page.waitForNavigation(), page.getByRole('button', {name: '清空聊天'}).click()]);
    assert.equal(await page.locator('.message').count(), 0);
    await page.goto(origin + workspace + '/requirements');
    assert.equal(await page.locator('[name=dietary]').inputValue(), '不吃海鲜\n花生过敏');
    await page.locator('[name=dietary]').fill('');
    await Promise.all([page.waitForNavigation(), page.locator('.requirements-form [type=submit]').click()]);
    assert.equal(await page.locator('[name=dietary]').inputValue(), '');
    assert.equal(await page.locator('[name=must_visit]').inputValue(), '西湖\n灵隐寺');
    await page.goto(origin + workspace + '?tab=board');
    for (const width of [320, 390, 430, 1000]) {
      await page.setViewportSize({width, height: 844});
      await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `board overflow at ${width}`);
      const geometry = await page.evaluate(() => {
        const wall = document.querySelector('.idea-wall-canvas').getBoundingClientRect();
        const input = document.querySelector('.wall-quick-add').getBoundingClientRect();
        const nav = document.querySelector('.bottom-nav').getBoundingClientRect();
        return {height: wall.height, inputBottom: input.bottom, navTop: nav.top};
      });
      assert.ok(geometry.height >= 480);
      assert.ok(geometry.inputBottom <= geometry.navTop + 1, JSON.stringify({width, ...geometry}));
    }
    await page.setViewportSize({width: 390, height: 844});
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({path: path.join(root, '.cache', 'requirements-ui-check', 'board-mobile.png'), fullPage: true});
    const submitting = page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith('/tasks'));
    await page.getByRole('button', {name: '生成计划', exact: true}).click();
    const acceptedTask = await submitting;
    assert.equal(acceptedTask.status(), 202);
    const originalTask = (await acceptedTask.json()).task;
    await page.waitForFunction(() => !document.querySelector('[data-task-retry]').hidden);
    await page.reload();
    await page.waitForFunction(() => !document.querySelector('[data-task-retry]').hidden);
    assert.ok((await page.locator('[data-task-message]').innerText()).includes('模型服务'));
    const retrying = page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith('/retry'));
    await page.getByRole('button', {name: '重试', exact: true}).click();
    const retriedTask = (await (await retrying).json()).task;
    assert.notEqual(retriedTask.id, originalTask.id);
    await page.waitForFunction(() => !document.querySelector('[data-task-retry]').hidden);
    const serverTasks = await (await context.request.get(origin + '/api/trips/' + workspace.split('/').pop() + '/tasks')).json();
    assert.equal(serverTasks.tasks.length, 2);
    assert.equal(serverTasks.tasks[1].status, 'failed');
    assert.deepEqual(errors, []);
    console.log('PASS: real app accepts queued tasks, preserves failure after reload, and supports retry');
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) {
      const closed = new Promise(resolve => server.once('close', resolve));
      server.kill();
      await closed;
    }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
