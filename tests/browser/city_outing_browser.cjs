const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');

(async () => {
  const base = process.argv[2] || 'http://127.0.0.1:9012';
  const output = process.argv[3];
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const results = [];
  const errors = [];
  try {
    for (const width of [320, 390, 430, 1000]) {
      const context = await browser.newContext({ viewport: { width, height: 900 } });
      const page = await context.newPage();
      page.on('pageerror', e => errors.push(e.message));
      await page.goto(base);
      await page.locator('[name=email]').fill('city-preview@example.com');
      await page.locator('[name=password]').fill('city-preview-2026');
      await Promise.all([page.waitForURL('**/trips'), page.locator('.login-card button[type=submit]').click()]);
      await page.goto(base + '/trips/new');
      assert.deepEqual(await page.locator('form [required]').evaluateAll(nodes => nodes.map(n => n.name)), ['lodging_address']);
      assert.equal(await page.locator('details.city-options').getAttribute('open'), null);
      await page.screenshot({ path: path.join(output, `create-${width}.png`), fullPage: true });
      await page.locator('[name=lodging_address]').fill('测试住处');
      await Promise.all([page.waitForURL('**/workspace/*?tab=chat'), page.locator('.trip-create-form button[type=submit]').click()]);
      const tid = new URL(page.url()).pathname.split('/')[2];
      assert.match(await page.locator('[data-chat-list]').innerText(), /可以直接说/);
      await page.screenshot({ path: path.join(output, `chat-${width}.png`), fullPage: true });
      await page.goto(base + `/workspace/${tid}/requirements`);
      assert.equal(await page.locator('[name=arrival_location]').count(), 0);
      await page.locator('[name=dietary]').fill('不吃辣');
      await Promise.all([page.waitForURL('**/requirements?saved=1'), page.locator('.trip-create-form button[type=submit]').click()]);
      assert.match(await page.locator('[role=status]').innerText(), /已保存/);
      await page.screenshot({ path: path.join(output, `settings-${width}.png`), fullPage: true });
      await page.goto(base + `/workspace/${tid}?tab=board`);
      await Promise.all([page.waitForURL(url => url.pathname.startsWith('/workspace/') && url.searchParams.get('tab') === 'itinerary'), page.locator('.generate-plan').click()]);
      await page.waitForSelector('[data-display-itinerary]', { timeout: 15000 });
      assert.match(await page.locator('[data-itinerary-text]').textContent(), /第1天/);
      const layout = await page.evaluate(() => ({ width: innerWidth, content: document.documentElement.scrollWidth,
        shells: [...document.querySelectorAll('.phone-shell')].map(n => n.getBoundingClientRect().width) }));
      assert.ok(layout.content <= width + 1, `overflow at ${width}: ${JSON.stringify(layout)}`);
      assert.ok(layout.shells.every(w => w <= Math.min(width, 430) + 1));
      await page.screenshot({ path: path.join(output, `itinerary-${width}.png`), fullPage: true });
      const state = await (await page.request.get(base + '/_test/city-state')).json();
      assert.equal(state.trips[tid].mode, 'city');
      assert.equal(state.trips[tid].requirements.travelers, null);
      assert.equal(state.trips[tid].requirements.budget_amount, '');
      assert.deepEqual(state.trips[tid].requirements.dietary, ['不吃辣']);
      assert.equal(state.trips[tid].display_status, 'ready');
      results.push({ width, trip_id: tid, ...layout, text_and_cards: true });
      await context.close();
    }
    const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const page = await context.newPage();
    await page.goto(base);
    await page.locator('[name=email]').fill('city-preview@example.com');
    await page.locator('[name=password]').fill('city-preview-2026');
    await Promise.all([page.waitForURL('**/trips'), page.locator('.login-card button[type=submit]').click()]);
    await page.goto(base + '/trips/new');
    await page.locator('[name=lodging_address]').fill('重名酒店');
    await page.locator('.trip-create-form button[type=submit]').click();
    await page.waitForSelector('[name=choice_token]', { state: 'attached' });
    await page.screenshot({ path: path.join(output, 'address-choices-390.png'), fullPage: true });
    await page.locator('[name=poi_id][value=fixture-other]').check();
    await Promise.all([page.waitForURL('**/workspace/*?tab=chat'), page.locator('.form-stack button[type=submit]').click()]);
    assert.match(await page.locator('[data-chat-list]').innerText(), /西路2号/);
    assert.equal(errors.length, 0, errors.join('\n'));
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify({ results, ambiguous_choice: true, page_errors: errors }, null, 2));
    await context.close();
    console.log(JSON.stringify({ viewports: results.length, ambiguous_choice: true, page_errors: errors.length }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
