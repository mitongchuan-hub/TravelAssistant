// Run: python tests/browser/render_itinerary.py <fixture-dir>
//      node tests/browser/itinerary_browser.cjs <fixture-dir>
// Requests stay inside synthetic fixtures; no live model or business data is used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require('playwright');
const fixtureDir = path.resolve(process.argv[2]);
const root = path.resolve(__dirname, '../..');
(async () => {
  const browser = await chromium.launch({channel:'chrome', headless:true});
  const page = await browser.newPage({viewport:{width:390,height:844}, reducedMotion:'reduce'});
  page.setDefaultTimeout(8000);
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('requestfailed', request => console.log('Request failed:', request.url(), request.failure()));
  let mode = 'failure';
  let posts = [];
  let holdPost = null;
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    if(url.pathname.startsWith('/static/')) {
      const file = path.join(root, 'app', url.pathname);
      res.writeHead(200, {'content-type': file.endsWith('.css')?'text/css':'text/javascript'});
      res.end(fs.readFileSync(file));
      return;
    }
    if(req.method === 'POST') {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      posts.push(Buffer.concat(chunks).toString());
      if(mode==='hold') { holdPost=res;return; }
      if(mode==='network') { req.socket.destroy(); return; }
      res.writeHead(303, {location:mode==='success'?'/workspace/fixture-trip?tab=itinerary':'/workspace/fixture-trip?tab=chat'});
      res.end();
      return;
    }
    res.setHeader('content-type', 'text/html; charset=utf-8');
    if(url.searchParams.get('tab')==='chat') {
      res.end('<section class="chat-list"><article class="message agent"><div class="message-body"><p>模型响应超时，原行程保持不变。</p></div></article></section>');
      return;
    }
    const fixture = url.searchParams.get('fixture') || 'itinerary';
    res.end(fs.readFileSync(path.join(fixtureDir,fixture+'.html')));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const visit = async fixture => {
    await page.goto(origin+'/workspace/fixture-trip?tab=itinerary&fixture='+fixture,{waitUntil:'load'});
  };
  const sheet = page.locator('[data-revision-sheet]');
  const field = sheet.locator('textarea');
  const triggers = page.locator('[data-revision-trigger]');
  try {
    for(const width of [320,390,430,1000]) {
      await page.setViewportSize({width,height:844});
      await visit('itinerary');
      assert.equal(await page.locator('[data-day-link]').count(),7);
      assert.equal(await page.locator('.timeline-details[open]').count(),0);
      assert.equal(await triggers.count(),35);
      assert.equal(await page.locator('[data-revision-form]').count(),1);
      await page.locator('[data-day-link]').last().click();
      await page.waitForFunction(()=>document.querySelector('[data-day-link]:last-child').getAttribute('aria-current')==='location');
      const geometry = await page.evaluate(()=>{
        const nav=document.querySelector('[data-day-nav]').getBoundingClientRect();
        const day=document.querySelector('#itinerary-day-7').getBoundingClientRect();
        const rail=document.querySelector('.timeline-time').getBoundingClientRect();
        const card=document.querySelector('.timeline-card').getBoundingClientRect();
        const c=getComputedStyle(document.querySelector('.timeline-time'));
        return {navBottom:nav.bottom,dayTop:day.top,railHeight:rail.height,cardHeight:card.height,bg:c.backgroundColor,overflow:document.documentElement.scrollWidth>innerWidth};
      });
      assert.ok(geometry.dayTop>=geometry.navBottom,JSON.stringify(geometry));
      assert.ok(geometry.railHeight<geometry.cardHeight);
      assert.equal(geometry.bg,'rgba(0, 0, 0, 0)');
      assert.equal(geometry.overflow,false);
      await page.locator('[data-day-link]').first().click();
      if(width===390) await page.screenshot({path:path.join(fixtureDir,'timeline-mobile.png')});
      const y=await page.evaluate(()=>scrollY);
      await triggers.first().click();
      assert.equal(await sheet.evaluate(el=>el.open),true);
      assert.equal(await sheet.locator('[data-revision-title]').innerText(),await triggers.first().getAttribute('data-item-title'));
      await field.fill('下午再去，想减少步行。');
      for(let i=0;i<7;i++) {
        await page.keyboard.press('Tab');
        assert.equal(await sheet.evaluate(el=>el.contains(document.activeElement)),true);
      }
      await page.keyboard.press('Escape');
      assert.equal(await sheet.evaluate(el=>el.open),false);
      assert.equal(await triggers.first().evaluate(el=>el===document.activeElement),true);
      assert.ok(Math.abs(await page.evaluate(()=>scrollY)-y)<5);
      await triggers.nth(1).click();
      assert.equal(await field.inputValue(),'');
      await field.fill('交通改坐公交');
      await sheet.locator('[data-revision-close]').first().click();
      await triggers.first().click();
      assert.equal(await field.inputValue(),'下午再去，想减少步行。');
      if(width===390) await page.screenshot({path:path.join(fixtureDir,'revision-mobile.png')});
      await page.keyboard.press('Escape');
      // Scrolling, rather than clicking the nav, also updates the selected date.
      await page.locator('#itinerary-day-4').evaluate(el=>scrollTo({top:scrollY+el.getBoundingClientRect().top-document.querySelector('[data-day-nav]').getBoundingClientRect().height-12,behavior:'instant'}));
      await page.waitForFunction(()=>document.querySelectorAll('[data-day-link]')[3].hasAttribute('aria-current'));
      await page.evaluate(()=>sessionStorage.clear());
    }
    console.log('PASS: date jumps, scroll selection, layout, focus, per-item drafts at 4 widths');

    await page.setViewportSize({width:390,height:500});
    await visit('single');
    assert.equal(await page.locator('[data-day-link]').innerText(),'第 1 天\n日期待定');
    assert.equal(await page.locator('.timeline-details').count(),1);
    assert.equal(await page.getByRole('link', {name: '核实具体地点', includeHidden: true}).count(),0);
    await triggers.first().click();
    assert.ok((await sheet.boundingBox()).height<=484);
    await sheet.locator('[type=submit]').click();
    assert.equal(posts.length,0);
    await field.fill('   ');
    await sheet.locator('[type=submit]').click();
    assert.equal(posts.length,0);
    await field.fill('换成室内活动');
    await page.mouse.click(5,5);
    assert.equal(await sheet.evaluate(el=>el.open),false);
    await triggers.first().click();
    assert.equal(await field.inputValue(),'换成室内活动');
    console.log('PASS: undated item with place verification, small viewport, empty input, backdrop close');

    mode='failure';
    await sheet.locator('[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('[data-revision-feedback]').textContent.includes('修改意见已保留'));
    assert.ok(posts.at(-1).includes('bare-item'));
    assert.ok(posts.at(-1).includes('换成室内活动'));
    assert.equal(await field.inputValue(),'换成室内活动');
    assert.equal(await sheet.evaluate(el=>el.open),true);
    assert.ok((await sheet.locator('[data-revision-feedback]').innerText()).includes('模型响应超时'));
    mode='network';
    await sheet.locator('[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('[data-revision-feedback]').textContent.includes('未能确认调整结果'));
    assert.equal(await field.inputValue(),'换成室内活动');
    await page.reload();
    await triggers.first().click();
    assert.equal(await field.inputValue(),'换成室内活动');
    console.log('PASS: server/network failures and refresh preserve feedback');

    await sheet.locator('[data-revision-close]').first().click();
    await visit('itinerary');
    await page.locator('[data-day-link]').nth(2).click();
    await triggers.nth(12).click();
    await field.fill('只调整这一项');
    mode='hold';
    const beforePosts=posts.length;
    await sheet.locator('[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('[data-revision-form]').getAttribute('aria-busy')==='true');
    await page.waitForTimeout(80);
    assert.equal(posts.length,beforePosts+1);
    assert.ok(posts.at(-1).includes('item-2-2'));
    await sheet.locator('form').evaluate(el=>el.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
    assert.equal(posts.length,beforePosts+1);
    await sheet.locator('[data-revision-close]').first().click();
    assert.equal(await page.locator('[data-revision-notice]').isVisible(),true);
    await triggers.nth(12).click();
    assert.equal(await sheet.locator('[type=submit]').isDisabled(),true);
    assert.equal(await field.inputValue(),'只调整这一项');
    assert.ok(holdPost);
    const navigation=page.waitForURL('**/workspace/fixture-trip?tab=itinerary#itinerary-item-item-2-2');
    holdPost.writeHead(303,{location:'/workspace/fixture-trip?tab=itinerary'});
    holdPost.end();
    await navigation;
    await page.waitForLoadState('load');
    const saved=await page.locator('#itinerary-item-item-2-2').boundingBox();
    const nav=await page.locator('[data-day-nav]').boundingBox();
    assert.ok(saved.y>=nav.y+nav.height-1);
    assert.ok(saved.y<400);
    console.log('PASS: single submission, correct item ID, close/reopen in flight, success returns to item');
    assert.equal(await page.evaluate(()=>JSON.parse(sessionStorage.getItem('travelassistant:revision-drafts:/workspace/fixture-trip'))['item-2-2']),undefined);
    await page.emulateMedia({reducedMotion:'no-preference'});
    await page.locator('[data-day-link]').last().click();
    await page.waitForFunction(()=>{
      const day=document.querySelector('#itinerary-day-7').getBoundingClientRect();
      const nav=document.querySelector('[data-day-nav]').getBoundingClientRect();
      return day.top>=nav.bottom && day.top<nav.bottom+24 && document.querySelector('[data-day-link]:last-child').hasAttribute('aria-current');
    });
    console.log('PASS: smooth scrolling settles on the selected day');

    await visit('empty');
    assert.equal(await page.locator('[data-day-nav]').count(),0);
    assert.equal(await sheet.count(),0);
    assert.equal(await page.getByText('还没有行程',{exact:true}).count(),1);
    assert.deepEqual(errors,[]);
    console.log('PASS: empty state, no browser runtime errors');
  } catch (error) {
    console.error('Browser errors:', errors);
    console.error('Navigation:', await page.locator('[data-day-link]').evaluateAll(links => links.map(link => ({href:link.hash,current:link.getAttribute('aria-current')}))));
    await page.screenshot({path:path.join(fixtureDir,'failure.png')});
    throw error;
  } finally {
    await browser.close();
    server.closeAllConnections();
    await new Promise(resolve=>server.close(resolve));
  }
})().catch(e=>{console.error(e);process.exitCode=1});
