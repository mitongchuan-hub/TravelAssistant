const { chromium } = require('../../../frontend/node_modules/@playwright/test');
const path = require('node:path');
const fs = require('node:fs/promises');
const { pathToFileURL } = require('node:url');
(async () => {
  const dir = __dirname;
  await fs.mkdir(path.join(dir, 'images'), { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: 'chrome', args: ['--allow-file-access-from-files'] });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  const url = pathToFileURL(path.join(dir, 'index.html')).href;
  const report = [];
  for (let i = 1; i <= 10; i++) {
    await page.goto(`${url}?style=${i}`);
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: path.join(dir, 'images', `${String(i).padStart(2, '0')}.png`) });
    report.push(await page.evaluate(() => ({ title: document.title, horizontalOverflow: document.documentElement.scrollWidth > innerWidth, contentOverflow: document.querySelector('.content').scrollHeight > document.querySelector('.content').clientHeight, clippedText: [...document.querySelectorAll('h1,h2,p,.route,.meta')].filter(e => e.scrollWidth > e.clientWidth + 1).map(e => e.textContent) })));
  }
  await page.setViewportSize({ width: 1800, height: 1900 });
  await page.goto(url);
  await page.waitForFunction(() => [...document.querySelectorAll('iframe')].every(f => f.contentDocument?.querySelector('.phone')));
  await page.screenshot({ path: path.join(dir, 'images', 'overview.png'), fullPage: true });
  await page.setViewportSize({ width: 375, height: 812 });
  for (let i = 1; i <= 10; i++) {
    await page.goto(`${url}?style=${i}`);
    report[i - 1].smallPhoneOverflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
  }
  await browser.close();
  console.log(JSON.stringify(report, null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; });
