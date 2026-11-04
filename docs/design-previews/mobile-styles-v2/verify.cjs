const { chromium } = require('../../../frontend/node_modules/@playwright/test');
const { pathToFileURL } = require('node:url');
const path = require('node:path');
(async () => {
 const browser = await chromium.launch({ headless: true, channel: 'chrome', args: ['--allow-file-access-from-files'] });
 const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
 const reports = [];
 for (let n = 1; n <= 10; n++) {
  await page.goto(pathToFileURL(path.join(__dirname, 'index.html')).href + '?style=' + n);
  reports.push(await page.evaluate(() => {
   const L = color => { const c=color.match(/[\d.]+/g).slice(0,3).map(x=>Number(x)/255).map(x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4); return .2126*c[0]+.7152*c[1]+.0722*c[2]; };
   const contrast = [...document.querySelectorAll('.title p,.context,.caution,.saved,.draft,.open-guide,.undo,.composer>span,.sample,.bottom button')].map(e => {
    let p=e,bg='rgba(0, 0, 0, 0)'; while(p){bg=getComputedStyle(p).backgroundColor;if(bg!=='rgba(0, 0, 0, 0)'&&bg!=='transparent')break;p=p.parentElement;}
    const f=L(getComputedStyle(e).color),b=L(bg);return { text:e.textContent.trim(), ratio:Number(((Math.max(f,b)+.05)/(Math.min(f,b)+.05)).toFixed(2)) };
   });
   const c=document.querySelector('.content');
   return { title:document.title, hiddenVerticalPixels:c.scrollHeight-c.clientHeight, lowContrast:contrast.filter(c=>c.ratio<4.5) };
  }));
 }
 console.log(JSON.stringify(reports,null,2)); await browser.close();
})().catch(e=>{console.error(e);process.exitCode=1});
