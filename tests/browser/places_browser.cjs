const assert = require('node:assert/strict');
const fs = require('node:fs');
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
  const server = spawn(process.env.TRAVEL_TEST_PYTHON || 'python', ['tests/browser/place_fixture.py', String(port)], {cwd: root, stdio: ['ignore', 'pipe', 'pipe']});
  let output = '', startupError;
  server.on('error', error => { startupError = error; });
  server.stdout.on('data', data => { output += data; });
  server.stderr.on('data', data => { output += data; });
  let browser;
  try {
    let ready = false;
    for (let i = 0; i < 100; i++) {
      if (startupError) throw startupError;
      if (server.exitCode !== null) throw new Error(output);
      try { if ((await fetch(origin + '/health', {signal: AbortSignal.timeout(500)})).ok) { ready = true; break; } } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.ok(ready, output);
    browser = await chromium.launch({channel: 'chrome', headless: true});
    const context = await browser.newContext({viewport: {width: 390, height: 844}, permissions: ['clipboard-read', 'clipboard-write']});
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(origin + '/');
    await page.locator('[name=email]').fill('browser-places@example.com');
    await page.locator('[name=password]').fill('places-browser-2026');
    await Promise.all([page.waitForURL('**/trips'), page.locator('.login-card [type=submit]').click()]);
    await page.goto(origin + '/workspace/trip-hangzhou?tab=itinerary');
    assert.equal(await page.locator('a[href*="/places/"]').count(), 0);
    const details = page.locator('.timeline-details').first();
    await details.locator('summary').click();
    assert.ok((await details.locator('.timeline-route').innerText()).includes('预计 35 分钟'));
    assert.ok((await details.locator('.timeline-route').innerText()).includes('杭州东站 → 灵隐寺'));
    assert.ok((await details.locator('.timeline-route').innerText()).includes('高德路线规划'));
    const info = page.locator('.timeline-place').first();
    assert.ok((await info.innerText()).includes('灵隐路1号'));
    assert.ok((await info.innerText()).includes('地点已匹配'));
    const url = new URL(await info.getByRole('link', {name: '打开地图'}).getAttribute('href'));
    assert.equal(url.searchParams.get('position'), '120.12,30.25');
    await info.getByRole('button', {name: '复制地址'}).click();
    assert.ok((await page.evaluate(() => navigator.clipboard.readText())).includes('灵隐路1号'));
    for (const width of [320, 390, 430, 1000]) {
      await page.setViewportSize({width, height: 844});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    }
    await page.setViewportSize({width: 390, height: 844});
    fs.mkdirSync(path.join(root, '.cache', 'routes-ui-check'), {recursive: true});
    await details.screenshot({path: path.join(root, '.cache', 'routes-ui-check', 'mobile-detail.png')});
    await page.reload();
    await page.locator('.timeline-details').first().locator('summary').click();
    assert.ok((await page.locator('.timeline-place').first().innerText()).includes('灵隐路1号'));
    await page.locator('.timeline-details').nth(1).locator('summary').click();
    assert.ok((await page.locator('.timeline-place').nth(1).innerText()).includes('区域级建议'));
    assert.ok((await page.locator('.timeline-details').nth(1).locator('.timeline-route').innerText()).includes('交通耗时待核实'));
    assert.equal(await page.locator('.timeline-place').nth(1).getByRole('link', {name: '打开地图'}).count(), 0);
    const removed = await context.request.get(origin + '/workspace/trip-hangzhou/places/any-item');
    assert.equal(removed.status(), 404);
    console.log('PASS: Agent place/route results, estimates and pending states, mobile layout, refresh, no manual lookup');
    assert.deepEqual(errors, []);
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) {
      const closed = new Promise(resolve => server.once('close', resolve));
      server.kill(); await closed;
    }
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
