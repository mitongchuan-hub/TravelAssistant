const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
(async () => {
  const tasks = [];
  let executions = 0, loseResponse = false;
  const html = `<!doctype html><meta charset="utf-8"><section data-travel-tasks-url="/api/trips/test/tasks">
    <aside data-task-status hidden><p data-task-message></p><button data-task-retry hidden>重试</button><a data-task-result hidden>查看结果</a></aside>
    <form action="/workspace/test/plan" method="post"><button type="submit">生成计划</button></form>
    <div id="plan-result">原行程</div>
    </section><script src="/static/app.js"></script><script src="/static/tasks.js"></script>`;
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname.startsWith('/static/')) {
      res.setHeader('content-type', 'text/javascript; charset=utf-8');
      res.end(fs.readFileSync(path.join(root, 'app', url.pathname))); return;
    }
    if (url.pathname.startsWith('/api/')) {
      res.setHeader('content-type', 'application/json');
      if (req.method === 'GET') { res.end(JSON.stringify({tasks})); return; }
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const data = JSON.parse(Buffer.concat(chunks).toString());
      let task;
      if (url.pathname.endsWith('/retry')) {
        const parent = tasks.find(t => url.pathname.includes('/' + t.id + '/'));
        task = tasks.find(t => t.retry_of === parent.id);
        if (!task) {
          executions++;
          task = {...parent, id: 'task-' + executions, status: 'queued', message: '等待处理', retry_of: parent.id, request_id: data.request_id};
          tasks.push(task);
        }
      } else {
        task = tasks.find(t => t.request_id === data.request_id);
        if (!task) {
          executions++;
          task = {id: 'task-' + executions, trip_id: 'test', kind: data.kind, payload: data.payload,
            request_id: data.request_id, status: 'queued', message: '等待处理', result_url: '/workspace/test?tab=itinerary'};
          tasks.push(task);
        }
      }
      if (loseResponse) { loseResponse = false; req.socket.destroy(); return; }
      res.statusCode = 202;
      res.end(JSON.stringify({task})); return;
    }
    res.setHeader('content-type', 'text/html; charset=utf-8');
    res.end(html);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = 'http://127.0.0.1:' + server.address().port;
  const browser = await chromium.launch({channel: 'chrome', headless: true});
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(origin + '/workspace/test?tab=board');
    loseResponse = true;
    await page.getByRole('button', {name: '生成计划'}).click();
    await page.waitForFunction(() => /未能确认|等待处理/.test(document.querySelector('[data-task-message]').textContent));
    assert.equal(executions, 1);
    await page.reload();
    await page.waitForFunction(() => document.querySelector('[data-task-message]').textContent === '等待处理');
    assert.equal(executions, 1);
    assert.equal(await page.getByRole('button', {name: '生成计划'}).isDisabled(), true);
    console.log('PASS: lost submission response recovers after reload without duplicate execution');
    tasks[0].status = 'running'; tasks[0].message = '正在处理';
    await page.waitForFunction(() => document.querySelector('[data-task-message]').textContent === '正在处理');
    await page.reload();
    await page.waitForFunction(() => document.querySelector('[data-task-message]').textContent === '正在处理');
    tasks[0].status = 'failed'; tasks[0].message = '时间冲突，原行程已保留';
    await page.waitForFunction(() => !document.querySelector('[data-task-retry]').hidden);
    assert.equal(await page.locator('#plan-result').innerText(), '原行程');
    loseResponse = true;
    await page.getByRole('button', {name: '重试', exact: true}).click();
    await page.waitForFunction(() => /刷新|等待处理/.test(document.querySelector('[data-task-message]').textContent));
    assert.equal(executions, 2);
    await page.reload();
    await page.waitForFunction(() => document.querySelector('[data-task-message]').textContent === '等待处理');
    assert.equal(executions, 2);
    console.log('PASS: running state survives reload; failed retry survives lost response');
    tasks[1].status = 'succeeded'; tasks[1].message = '已完成';
    await page.waitForURL('**/workspace/test?tab=itinerary&task=task-2');
    await page.waitForFunction(() => !document.querySelector('[data-task-result]').hidden);
    assert.equal(executions, 2);
    console.log('PASS: completion navigates to saved result and exposes result after refresh');
    assert.deepEqual(errors, []);
  } catch (error) {
    console.error('Task page HTML:', await page.content());
    console.error('Browser errors:', errors);
    throw error;
  } finally {
    await browser.close();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
