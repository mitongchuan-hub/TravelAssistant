const { chromium } = require('../../../frontend/node_modules/@playwright/test');
const fs = require('node:fs/promises');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const crypto = require('node:crypto');
async function fingerprints(dir) {
  const files = ['index.html', 'render.cjs', 'DESIGN.md', ...(await fs.readdir(path.join(dir, 'images'))).map(n => `images/${n}`)];
  return Object.fromEntries(await Promise.all(files.map(async name => [name, crypto.createHash('sha256').update(await fs.readFile(path.join(dir, name))).digest('hex')])));
}
(async () => {
  const previous = path.resolve(__dirname, '../mobile-styles');
  const before = await fingerprints(previous);
  await fs.mkdir(path.join(__dirname, 'images'), { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: 'chrome', args: ['--allow-file-access-from-files'] });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const url = pathToFileURL(path.join(__dirname, 'index.html')).href;
  const report = [];
  for (let n = 1; n <= 10; n++) {
    await page.goto(`${url}?style=${n}`);
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: path.join(__dirname, 'images', `B${String(n).padStart(2, '0')}.png`) });
    report.push(await page.evaluate(() => ({ title: document.title, horizontalOverflow: document.documentElement.scrollWidth > innerWidth, contentOverflow: document.querySelector('.content').scrollHeight > document.querySelector('.content').clientHeight, clippedText: [...document.querySelectorAll('p,h1,h2,.route,.context,.outcome')].filter(e => e.scrollWidth > e.clientWidth + 1).map(e => e.textContent) })));
  }
  await page.setViewportSize({ width: 1800, height: 1900 });
  await page.goto(url);
  await page.waitForFunction(() => [...document.querySelectorAll('iframe')].every(f => f.contentDocument?.querySelector('.phone')));
  await page.screenshot({ path: path.join(__dirname, 'images', 'overview.png'), fullPage: true });
  await page.setViewportSize({ width: 375, height: 812 });
  for (let n = 1; n <= 10; n++) {
    await page.goto(`${url}?style=${n}`);
    report[n - 1].smallPhone = await page.evaluate(() => ({ horizontalOverflow: document.documentElement.scrollWidth > innerWidth, contentOverflow: document.querySelector('.content').scrollHeight > document.querySelector('.content').clientHeight }));
  }
  await page.setViewportSize({ width: 1020, height: 1300 });
  await page.goto(pathToFileURL(path.join(__dirname, 'compare.html')).href);
  await page.waitForFunction(() => [...document.querySelectorAll('iframe')].every(f => f.contentDocument?.querySelector('.phone')));
  await page.screenshot({ path: path.join(__dirname, 'images', 'comparison.png'), fullPage: true });
  await browser.close();
  const originalUnchanged = JSON.stringify(before) === JSON.stringify(await fingerprints(previous));
  console.log(JSON.stringify({ report, errors, originalUnchanged }, null, 2));
  if (!originalUnchanged || errors.length || report.some(r => r.horizontalOverflow || r.clippedText.length || r.smallPhone.horizontalOverflow)) process.exitCode = 1;
})().catch(error => { console.error(error); process.exitCode = 1; });
