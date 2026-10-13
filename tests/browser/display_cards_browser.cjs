// Visual/readability acceptance for realistic one, three and seven-day cards.
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
  const server = spawn(process.env.TRAVEL_TEST_PYTHON || 'python', ['tests/browser/display_cards_fixture.py', String(port)], {
    cwd: root, env: {...process.env, TRAVEL_PERSISTENCE:'0', PYTHONDONTWRITEBYTECODE:'1'}, stdio:['ignore','pipe','pipe'],
  });
  let output = '', startupError, browser;
  server.on('error', error => {startupError=error;});
  server.stdout.on('data', data=>{output+=data;});
  server.stderr.on('data', data=>{output+=data;});
  const artifacts = process.env.TRAVEL_CARD_ARTIFACTS || fs.mkdtempSync(path.join(os.tmpdir(), 'travel-cards-ui-'));
  fs.mkdirSync(artifacts, {recursive:true});
  const phase = process.env.TRAVEL_CARD_PHASE || 'after';
  try {
    let ready=false;
    for (let i=0;i<100;i++) {
      if (startupError) throw startupError;
      if (server.exitCode!==null) throw Error(output);
      try {if ((await fetch(origin+'/health', {signal:AbortSignal.timeout(500)})).ok) {ready=true;break;}} catch {}
      await new Promise(resolve=>setTimeout(resolve,100));
    }
    assert.ok(ready,output);
    browser=await chromium.launch({channel:'chrome',headless:true});
    const context=await browser.newContext({viewport:{width:390,height:844}});
    const page=await context.newPage();
    const errors=[];
    page.on('pageerror', error=>errors.push(error.message));
    await page.goto(origin);
    await page.locator('[name=email]').fill('cards@example.com');
    await page.locator('[name=password]').fill('cards-browser-2026');
    await Promise.all([page.waitForURL('**/trips'),page.locator('.login-card [type=submit]').click()]);
    const {trips}=await (await context.request.get(origin+'/_test/cards')).json();
    const expectedRows=JSON.parse(fs.readFileSync(path.join(root,'docs/evaluations/display-combined-weather-20261012/results.json'),'utf8'));
    const compact=value=>value.replace(/\s+/g,'').replace(/[。；;.!！？?]+$/,'');
    const measurements=[];
    for (const [cid,count] of [['LAT-012',1],['LAT-014',3],['LAT-015',7]]) {
      await page.goto(`${origin}/workspace/${trips[cid]}?tab=itinerary`);
      assert.equal(await page.locator('.display-day').count(),count);
      assert.equal(await page.evaluate(()=>window.weatherInjected),undefined);
      const source=await page.locator('[data-itinerary-text]').textContent();
      const cardText=await page.locator('[data-display-panel]').textContent();
      for (const width of [320,390,430,768,1280]) {
        await page.setViewportSize({width,height:844});
        await page.evaluate(()=>window.scrollTo({top:0,behavior:'instant'}));
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${cid} overflow at ${width}`);
        if (phase==='after') {
          const geometry=await page.evaluate(()=>({shell:document.querySelector('.phone-shell').getBoundingClientRect().width,nav:document.querySelector('.bottom-nav').getBoundingClientRect().width}));
          assert.ok(Math.abs(geometry.shell-Math.min(width,430))<1,`unexpected shell width: ${JSON.stringify(geometry)}`);
          assert.ok(Math.abs(geometry.nav-geometry.shell)<1,`navigation width differs: ${JSON.stringify(geometry)}`);
          if(count>1) {
            const rows=await page.locator('.display-preflight').evaluate(el=>[...el.querySelectorAll('[data-display-weather], [data-display-prep]')].map(row=>{
              const r=row.getBoundingClientRect();return {left:r.left,top:r.top,bottom:r.bottom,width:r.width};
            }));
            assert.equal(rows.length,3);
            for(let i=1;i<rows.length;i++) {
              assert.ok(Math.abs(rows[i].left-rows[0].left)<1 && Math.abs(rows[i].width-rows[0].width)<1,`preflight rows should fill the same width at ${width}`);
              assert.ok(rows[i].top>=rows[i-1].bottom+10,`preflight rows overlap at ${width}`);
            }
          }
        }
        assert.equal(await page.locator('[data-itinerary-text]').textContent(),source);
        assert.equal(await page.locator('[data-display-panel]').textContent(),cardText);
        measurements.push({case:cid,width,days:count,pageHeight:await page.evaluate(()=>document.documentElement.scrollHeight)});
        if ([390,1280].includes(width)) {
          await page.screenshot({path:path.join(artifacts,`${phase}-${cid}-${width}.png`),fullPage:true});
          await page.screenshot({path:path.join(artifacts,`${phase}-${cid}-${width}-top.png`)});
          if(phase==='after') await page.locator('.display-preflight').screenshot({path:path.join(artifacts,`${phase}-${cid}-${width}-preflight.png`),style:'.bottom-nav { visibility: hidden !important; }'});
          const firstDay = page.locator('.display-day').first();
          await firstDay.evaluate(el => {
            const nav = document.querySelector('[data-display-day-nav]');
            window.scrollTo({top: scrollY + el.getBoundingClientRect().top - (nav?.getBoundingClientRect().height || 0) - 16, behavior:'instant'});
          });
          await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
          await page.screenshot({path:path.join(artifacts,`${phase}-${cid}-${width}-day.png`)});
        }
      }
      if (phase==='after') {
        const expected=expectedRows.find(row=>row.case_id===cid && row.attempt===2).result.display;
        const actual=compact(cardText);
        const retained=[expected.overview,expected.budget_note,...expected.pending_items,...expected.risks,
          ...Object.values(expected.preparation).flatMap(items=>items.flatMap(item=>[item.title,item.body])),
          ...expected.weather.flatMap(item=>[item.date_text,item.summary,item.temperature_text,item.precipitation_text,item.queried_at_text,...item.notes]),
          ...expected.days.flatMap(day=>[day.theme,...day.items.flatMap(item=>[item.time_text,item.title,item.location,item.duration_text,item.description,item.cost_text,...item.notes])])];
        for(const value of retained.filter(Boolean)) assert.ok(actual.includes(compact(value)),`${cid} omitted source field: ${value}`);
        const weather=page.locator('[data-display-weather]');
        assert.equal(await weather.count(),1);
        assert.ok((await weather.innerText()).includes(cid==='LAT-015'?'约25%':cid==='LAT-014'?'≤41%':'最高约12%'));
        assert.equal(await page.locator('.itinerary-summary-stats').getByText('行程状态',{exact:true}).count(),0);
        assert.equal(await page.locator('.itinerary-summary-card').count(),1);
        assert.equal(await page.locator('.display-overview').count(),0);
        assert.equal(await page.locator('.travel-page-top p').count(),0);
        // Budget details remain keyboard accessible, without truncating the model text.
        const budget=page.locator('[data-display-budget]');
        await budget.locator(':scope > summary').focus();
        await page.keyboard.press('Enter');
        assert.equal(await budget.evaluate(el=>el.open),true);
        assert.ok((await budget.innerText()).includes('非实际报价'));
        const lodging=page.locator('[data-display-prep="accommodation"]');
        if(count>1) {
          assert.equal(await lodging.isVisible(),true);
          assert.equal(await lodging.locator('summary').count(),0);
          assert.equal(await lodging.locator('.display-prep-entry p').first().isVisible(),true);
          assert.ok((await lodging.innerText()).includes('候选'));
        }
        if (count>1) {
          await page.locator('[data-display-day-link]').last().click();
          const last=page.locator('.display-day').last();
          await page.waitForFunction(() => {
            const days = document.querySelectorAll('.display-day');
            const nav = document.querySelector('[data-display-day-nav]');
            const top = days[days.length - 1].getBoundingClientRect().top;
            const links = document.querySelectorAll('[data-display-day-link]');
            return top >= nav.getBoundingClientRect().bottom && top < 200 && links[links.length - 1].getAttribute('aria-current') === 'location';
          });
          assert.ok((await last.boundingBox()).y>=0,'last day hidden behind navigation');
          assert.equal(await page.locator('[data-display-day-link]').last().getAttribute('aria-current'),'location');
        }
        await page.locator('[data-display-edit]').first().click();
        assert.ok((await page.locator('#text-revision-feedback').inputValue()).startsWith('调整'));
        await page.locator('.display-original summary').click();
        assert.equal(await page.locator('[data-itinerary-text]').isVisible(),true);
      }
    }
    if(phase==='after') {
      const storedBefore=await (await context.request.get(origin+'/_test/cards-source')).json();
      await page.goto(`${origin}/workspace/${trips.duplicates}?tab=itinerary`);
      assert.equal(await page.locator('.itinerary-summary-card').count(),1);
      assert.equal(await page.evaluate(()=>window.weatherInjected),undefined);
      assert.ok((await page.locator('[data-display-weather]').textContent()).includes('<script>window.weatherInjected = true</script>'));
      assert.equal(await page.locator('.display-description, .display-location').count(),0);
      const notes=page.locator('.display-item-notes');
      assert.equal(await notes.getByText('开放待核实',{exact:true}).count(),1);
      assert.equal(await notes.getByText('建议最晚15:00前出发',{exact:true}).count(),1);
      assert.equal(await notes.getByText('最晚15:00前出发',{exact:true}).count(),1);
      assert.equal(await page.locator('.display-section--pending li').count(),1);
      assert.equal(await page.locator('.display-section--risks li').count(),1);
      assert.ok((await page.locator('.display-section--risks').innerText()).includes('微辣也不要'));
      assert.deepEqual(await (await context.request.get(origin+'/_test/cards-source')).json(),storedBefore);
      console.log('PASS: identical repetitions render once; different qualifiers and persisted cards remain intact');
    }
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(artifacts,`${phase}-measurements.json`),JSON.stringify(measurements,null,2));
    console.log(`PASS: ${phase} dense cards at five widths; all source text retained; no overflow or script injection`);
    console.log('Artifacts: '+artifacts);
  } finally {
    if(browser) await browser.close();
    if(server.exitCode===null) {const closed=new Promise(resolve=>server.once('close',resolve));server.kill();await closed;}
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
