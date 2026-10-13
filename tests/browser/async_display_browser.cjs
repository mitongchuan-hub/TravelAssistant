// Prose is visible while cards are blocked; late cards preserve reading and drafts.
const assert = require('node:assert/strict');
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
    cwd: root, env: {...process.env, TRAVEL_PERSISTENCE:'0', PYTHONDONTWRITEBYTECODE:'1'}, stdio:['ignore','pipe','pipe'],
  });
  let output = '', startupError, browser;
  server.on('error', error => {startupError = error;});
  server.stdout.on('data', data => {output += data;});
  server.stderr.on('data', data => {output += data;});
  try {
    let ready = false;
    for (let i=0; i<100; i++) {
      if (startupError) throw startupError;
      if (server.exitCode !== null) throw Error(output);
      try {if ((await fetch(origin+'/health')).ok) {ready=true;break;}} catch {}
      await new Promise(resolve => setTimeout(resolve,100));
    }
    assert.ok(ready,output);
    browser = await chromium.launch({channel:'chrome',headless:true});
    const context = await browser.newContext({viewport:{width:390,height:844}});
    const page = await context.newPage();
    page.setDefaultTimeout(12000);
    const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(origin);
    await page.locator('[name=email]').fill('text-browser@example.com');
    await page.locator('[name=password]').fill('text-browser-2026');
    await Promise.all([page.waitForURL('**/trips'),page.locator('.login-card [type=submit]').click()]);
    const workspace=await page.locator('.trip-card-link').first().getAttribute('href');
    const tid=workspace.split('/').pop();
    await context.request.post(origin+'/_test/block-display');
    await page.goto(origin+workspace+'?tab=itinerary');
    const began=Date.now();
    await page.getByRole('button',{name:'让 Agent 生成第一版'}).click();
    await page.locator('[data-itinerary-text]').waitFor({state:'visible'});
    const textVisibleMs=Date.now()-began;
    assert.equal(await page.locator('[data-display-itinerary]').count(),0);
    assert.ok((await page.locator('[data-display-notice]').innerText()).includes('正在整理卡片'));
    assert.equal((await (await context.request.get(origin+'/_test/display-started')).json()).started,true);
    const tasks=(await (await context.request.get(origin+`/api/trips/${tid}/tasks`)).json()).tasks;
    assert.equal(tasks.at(-1).status,'succeeded');
    const source=await page.locator('[data-itinerary-text]').textContent();
    const draft=page.locator('#text-revision-feedback');
    await draft.fill('未提交的修改草稿，卡片到达不能清空。');
    await draft.focus();
    await draft.evaluate(el=>{el.scrollIntoView({block:'center',behavior:'instant'});el.setSelectionRange(4,9);});
    const before=await draft.boundingBox();
    await context.request.post(origin+'/_test/release-display');
    await page.locator('[data-display-itinerary]').waitFor({state:'attached'});
    const cardsVisibleMs=Date.now()-began;
    assert.equal(await draft.inputValue(),'未提交的修改草稿，卡片到达不能清空。');
    assert.deepEqual(await draft.evaluate(el=>[el.selectionStart,el.selectionEnd]),[4,9]);
    assert.equal(await page.locator('.display-original').evaluate(el=>el.open),true);
    assert.equal(await page.locator('[data-itinerary-text]').textContent(),source);
    const after=await draft.boundingBox();
    assert.ok(Math.abs(before.y-after.y)<3,`reading anchor moved: ${before.y} -> ${after.y}`);
    console.log('PASS: prose is visible before blocked cards; primary task succeeds; draft, selection and viewport survive');
    await page.locator('[data-display-edit]').first().click();
    assert.ok((await draft.inputValue()).includes('西湖散步'));
    console.log('PASS: asynchronously inserted card controls work');

    await context.request.post(origin+'/_test/fail-display');
    await draft.fill('下午减少步行，其他安排不变');
    await page.getByRole('button',{name:'更新行程',exact:true}).click();
    await page.locator('[data-display-retry]').waitFor();
    const latest=(await (await context.request.get(origin+`/api/trips/${tid}/tasks`)).json()).tasks.at(-1);
    assert.equal(latest.status,'succeeded');
    const stateBefore=(await (await context.request.get(origin+`/api/trips/${tid}/plan-state`)).json());
    const revised=await page.locator('[data-itinerary-text]').textContent();
    await draft.fill('重试卡片时保留这段草稿');
    await page.locator('[data-display-retry]').click();
    await page.locator('[data-display-itinerary]').waitFor();
    const stateAfter=(await (await context.request.get(origin+`/api/trips/${tid}/plan-state`)).json());
    assert.equal(stateBefore.plan_id,stateAfter.plan_id);
    assert.equal(await page.locator('[data-itinerary-text]').textContent(),revised);
    assert.equal(await draft.inputValue(),'重试卡片时保留这段草稿');
    assert.equal((await (await context.request.get(origin+`/api/trips/${tid}/tasks`)).json()).tasks.length,2);
    console.log('PASS: conversion failure preserves prose and primary success; card-only retry does not replan');
    for (const width of [320,390,430,1000]) {
      await page.setViewportSize({width,height:844});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    }
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({mode:'controlled',text_visible_ms:textVisibleMs,cards_visible_ms:cardsVisibleMs,
      page_errors:errors,note:'Controlled browser verification, not real model latency.'}));
  } finally {
    if (browser) await browser.close();
    if (server.exitCode===null) {
      const closed=new Promise(resolve=>server.once('close',resolve));
      server.kill();await closed;
    }
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
