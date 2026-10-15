const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '../..');
(async () => {
  const artifacts = process.env.TRAVEL_CHAT_ARTIFACTS || fs.mkdtempSync(path.join(os.tmpdir(), 'travel-chat-ui-'));
  fs.mkdirSync(artifacts, {recursive:true});
  const reserve = net.createServer();
  await new Promise(resolve => reserve.listen(0, '127.0.0.1', resolve));
  const port = reserve.address().port;
  await new Promise(resolve => reserve.close(resolve));
  const origin = `http://127.0.0.1:${port}`;
  const server = spawn(process.env.TRAVEL_TEST_PYTHON || 'python', ['tests/browser/chat_ui_fixture.py', String(port)], {
    cwd:root, env:{...process.env, TRAVEL_PERSISTENCE:'0', PYTHONDONTWRITEBYTECODE:'1'}, stdio:['ignore','pipe','pipe']});
  let logs = '', browser;
  server.stdout.on('data', chunk => logs += chunk);
  server.stderr.on('data', chunk => logs += chunk);
  try {
    let ready = false;
    for (let i=0;i<100;i++) {
      if (server.exitCode !== null) throw Error(logs);
      try {if ((await fetch(origin+'/health')).ok) {ready=true;break;}} catch {}
      await new Promise(resolve => setTimeout(resolve,100));
    }
    assert.ok(ready,logs);
    browser = await chromium.launch({channel:'chrome',headless:true});
    const context = await browser.newContext({viewport:{width:390,height:844}});
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.setDefaultTimeout(15000);
    await page.goto(origin);
    await page.locator('[name=email]').fill('chat-preview@example.com');
    await page.locator('[name=password]').fill('chat-preview-2026');
    await Promise.all([page.waitForURL('**/trips'),page.locator('.login-card [type=submit]').click()]);
    const workspace = await page.locator('.trip-card-link').first().getAttribute('href');
    const tid = workspace.split('/').pop();
    await page.goto(origin+workspace+'?tab=chat');
    const composer = page.locator('.composer textarea');
    const list = page.locator('[data-chat-list]');
    assert.equal(await page.locator('.chat-agent-card, .message-meta, .message.user .avatar').count(),0);
    assert.equal(await page.getByRole('link',{name:'旅行需求',exact:true}).isVisible(),false);
    await page.getByLabel('更多聊天操作',{exact:true}).click();
    assert.equal(await page.getByRole('link',{name:'旅行需求',exact:true}).isVisible(),true);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.chat-menu').evaluate(el=>el.open),false);
    await page.locator('.chat-heading').click();
    assert.equal(await page.locator('.chat-reply-content strong').count()>0,true);
    assert.equal(await page.locator('.chat-full-reply').count(),1);
    assert.equal(await page.locator('.chat-full-reply').evaluate(el=>el.open),false);
    const planPreview = page.locator('.chat-full-reply > summary');
    assert.match(await planPreview.innerText(),/首日湖滨、次日休息后返程/);
    assert.match(await planPreview.innerText(),/预算取舍/);
    assert.match(await planPreview.innerText(),/酒店位置和接待条件/);
    assert.match(await planPreview.innerText(),/展开完整行程/);
    assert.doesNotMatch(await planPreview.innerText(),/安排 12/);
    assert.equal(await page.evaluate(()=>window.chatInjected),undefined);
    assert.equal(await page.locator('.chat-reply a[href^="javascript:"]').count(),0);
    for (const width of [320,390,430,1000]) {
      await page.setViewportSize({width,height:844});
      await page.waitForTimeout(100);
      const geometry = await page.evaluate(()=>({width:document.querySelector('.phone-shell').getBoundingClientRect().width,
        overflow:document.documentElement.scrollWidth>innerWidth, list:document.querySelector('[data-chat-list]').getBoundingClientRect().bottom,
        dock:document.querySelector('.chat-dock').getBoundingClientRect().top, bottom:document.querySelector('.bottom-nav').getBoundingClientRect().bottom}));
      assert.ok(Math.abs(geometry.width-Math.min(width,430))<1);
      assert.equal(geometry.overflow,false);
      assert.ok(geometry.list<=geometry.dock+1 && geometry.bottom<=844+1);
      await page.screenshot({path:path.join(artifacts,`chat-${width}.png`)});
    }
    await page.setViewportSize({width:390,height:844});
    await composer.fill('预算不变。\n下午多休息。\n原文其他部分保留。');
    assert.ok((await composer.boundingBox()).height>60);
    await page.reload();
    assert.equal(await composer.inputValue(),'预算不变。\n下午多休息。\n原文其他部分保留。');
    await composer.fill('');
    assert.equal(await page.getByRole('button',{name:'发送消息',exact:true}).isDisabled(),true);
    await composer.fill('第一行');
    await composer.press('Shift+Enter');
    await composer.type('第二行');
    assert.equal(await composer.inputValue(),'第一行\n第二行');
    // Desktop Enter submits; the model gate keeps a real task in progress.
    await context.request.post(origin+'/_test/block');
    await composer.press('Enter');
    await page.waitForFunction(()=>document.querySelector('[data-agent-thinking-message]'));
    await page.waitForFunction(()=>document.querySelector('.composer').dataset.submitting!=='true');
    assert.equal(await composer.isEnabled(),true);
    await composer.fill('正在等待时写下一条草稿');
    assert.equal(await composer.inputValue(),'正在等待时写下一条草稿');
    // A second chat is allowed and supersedes the previous task.
    await page.getByRole('button',{name:'发送消息',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.composer').dataset.submitting!=='true');
    const tasks = (await (await context.request.get(origin+`/api/trips/${tid}/tasks`)).json()).tasks;
    assert.equal(tasks[0].status,'superseded');
    await composer.fill('这份草稿不要因为回复到达而丢失');
    await context.request.post(origin+'/_test/release');
    await page.locator('.chat-reply-content').filter({hasText:'收到你的补充'}).last().waitFor();
    assert.equal(await composer.inputValue(),'这份草稿不要因为回复到达而丢失');
    // Open long text and read history. Polls keep expansion, scroll anchor and selection.
    await composer.fill('');
    await page.locator('.chat-full-reply > summary').click();
    await list.evaluate(el=>{el.scrollTop=80;});
    const anchorBefore = await page.evaluate(()=>captureChatView(document.querySelector('[data-chat-list]')));
    await context.request.post(origin+'/_test/new-reply');
    await page.locator('[data-chat-new]').waitFor({state:'visible'});
    const anchorAfter = await page.evaluate(()=>captureChatView(document.querySelector('[data-chat-list]')));
    assert.equal(anchorBefore.anchorId,anchorAfter.anchorId);
    assert.ok(Math.abs(anchorBefore.anchorOffset-anchorAfter.anchorOffset)<2);
    assert.equal(await page.locator('.chat-full-reply').evaluate(el=>el.open),true);
    await page.screenshot({path:path.join(artifacts,'chat-expanded-390.png')});
    await page.locator('[data-chat-new]').click();
    assert.ok(await list.evaluate(el=>el.scrollHeight-el.scrollTop-el.clientHeight<3));
    // Lost acknowledgement recovers one execution, without erasing a typed draft.
    await page.route(`**/api/trips/${tid}/messages`, async route=>{await route.fetch();await route.abort();},{times:1});
    await composer.fill('断网恢复测试消息');
    await page.getByRole('button',{name:'发送消息',exact:true}).click();
    await composer.fill('断网期间的新草稿');
    await page.waitForFunction(()=>document.querySelector('.composer').dataset.submitting!=='true');
    await page.waitForFunction(()=>!sessionStorage.getItem(`travelassistant:task-submit:${document.querySelector('[data-travel-tasks-url]').dataset.travelTasksUrl}`));
    await page.waitForFunction(()=>!document.querySelector('[data-optimistic]'));
    assert.equal(await composer.inputValue(),'断网期间的新草稿');
    const recovered = (await (await context.request.get(origin+`/api/trips/${tid}/tasks`)).json()).tasks;
    assert.equal(recovered.filter(t=>t.payload.body==='断网恢复测试消息').length,1);
    assert.equal(await page.locator('.chat-user-text').filter({hasText:'断网恢复测试消息'}).count(),1);
    // An explicit rejection offers retry; retry must preserve a separate draft.
    await page.route(`**/api/trips/${tid}/messages`, route=>route.fulfill({status:409,contentType:'application/json',body:'{"error":"暂时无法发送"}'}),{times:1});
    await composer.fill('明确失败后重试的消息');
    await page.getByRole('button',{name:'发送消息',exact:true}).click();
    await page.locator('[data-retry-message]').waitFor();
    await composer.fill('重试期间保留的独立草稿');
    await page.locator('[data-retry-message]').click();
    await page.waitForFunction(()=>document.querySelector('.composer').dataset.submitting!=='true');
    assert.equal(await composer.inputValue(),'重试期间保留的独立草稿');
    assert.equal(await page.locator('.failed-message').count(),0);
    // Composition events do not submit incomplete Chinese input.
    const countBefore = (await (await context.request.get(origin+`/api/trips/${tid}/tasks`)).json()).tasks.length;
    await composer.evaluate(el=>el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',isComposing:true,bubbles:true,cancelable:true})));
    assert.equal((await (await context.request.get(origin+`/api/trips/${tid}/tasks`)).json()).tasks.length,countBefore);
    const mobileContext = await browser.newContext({viewport:{width:390,height:700},isMobile:true,hasTouch:true,storageState:await context.storageState()});
    const mobile = await mobileContext.newPage();
    await mobile.goto(origin+workspace+'?tab=chat');
    await mobile.locator('.composer textarea').fill('手机输入');
    await mobile.locator('.composer textarea').press('Enter');
    assert.equal(await mobile.locator('.composer textarea').inputValue(),'手机输入\n');
    await mobile.setViewportSize({width:390,height:560});
    await mobile.waitForTimeout(100);
    assert.ok((await mobile.locator('.composer textarea').boundingBox()).y<560);
    await mobileContext.close();
    assert.equal((await (await context.request.get(origin+'/_test/state')).json()).initial_unchanged,true);
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(artifacts,'report.json'),JSON.stringify({passed:true,widths:[320,390,430,1000],errors,scope:'isolated_memory_store_synthetic_replies'},null,2));
    console.log('PASS: chat layout, Markdown safety, full replies, drafts, input methods, supersession, recovery and reading position');
    console.log('Artifacts: '+artifacts);
  } catch (error) {
    if (browser) {
      const page = browser.contexts()[0]?.pages()[0];
      if (page) {
        await page.screenshot({path:path.join(artifacts,'failure.png')});
        fs.writeFileSync(path.join(artifacts,'failure.html'),await page.content());
        const debug = await page.evaluate(async()=>{
          const endpoint=document.querySelector('[data-travel-tasks-url]')?.dataset.travelTasksUrl;
          return {tasks:endpoint?await (await fetch(endpoint)).json():null,storage:{...sessionStorage},locals:[...document.querySelectorAll('[data-optimistic]')].map(el=>({...el.dataset}))};
        });
        fs.writeFileSync(path.join(artifacts,'failure-state.json'),JSON.stringify(debug,null,2));
      }
    }
    throw error;
  } finally {
    if (browser) await browser.close();
    server.kill();
    fs.writeFileSync(path.join(artifacts,'fixture.log'),logs);
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
