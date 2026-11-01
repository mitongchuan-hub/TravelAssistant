import { expect, test, type Page } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const pictures = fileURLToPath(new URL('../screenshots/', import.meta.url))
async function noOverflow(page: Page) {
  expect(await page.evaluate(() => ({ width: innerWidth, body: document.body.scrollWidth, html: document.documentElement.scrollWidth }))).toEqual(expect.objectContaining({ width: await page.evaluate(() => innerWidth) }))
  const excess = await page.evaluate(() => Math.max(document.body.scrollWidth, document.documentElement.scrollWidth) - innerWidth)
  expect(excess).toBeLessThanOrEqual(1)
}

test('real Chrome: chat-only lodging, ideas, itinerary, confirmation, guide and deep links', async ({ page, context }) => {
  mkdirSync(pictures, { recursive: true })
  const errors: string[] = [], external: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => {
    if (message.type() !== 'error') return
    // Chrome rejects Playwright's injected utility script in our intentionally
    // script-free sandbox. Keep the sandbox; ignore only this exact diagnostic.
    if (/^Blocked script execution in 'http:\/\/127\.0\.0\.1:8765\/api\/trips\/[^/]+\/guide\?.*because the document's frame is sandboxed and the 'allow-scripts' permission is not set\.$/.test(message.text())) return
    errors.push(message.text())
  })
  await context.route('**/*', route => {
    const url = new URL(route.request().url())
    if (!['127.0.0.1', 'localhost'].includes(url.hostname)) { external.push(url.href); return route.abort() }
    return route.continue()
  })
  const email = `browser-${Date.now()}@example.com`, password = 'Offline-browser-123!'
  await page.goto('/?mode=register')
  await noOverflow(page)
  await page.screenshot({ path: `${pictures}/auth-430.png`, fullPage: true })
  await page.getByLabel('邮箱', { exact: true }).fill(email)
  await page.getByLabel('密码', { exact: true }).fill(password)
  await page.getByRole('button', { name: '创建账号', exact: true }).click()
  await expect(page).toHaveURL(/\/trips$/)
  await page.getByRole('link', { name: '开始新的聊天', exact: true }).click()
  await expect(page).toHaveURL(/\/workspace\/[^?]+$/)
  const workspacePath = new URL(page.url()).pathname
  await expect(page.getByRole('heading', { name: '新的旅行', exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: '想法', exact: true })).toBeVisible()
  await page.getByRole('textbox', { name: '聊天消息' }).fill('我住杭州湖滨酒店（西湖店），2030-05-01开始，玩1天，坐公交地铁。')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(page.locator('[data-progress-state="succeeded"]')).toHaveCount(1)

  await page.getByRole('textbox', { name: '聊天消息' }).fill('记下晚上夜骑这个想法')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(page.locator('[data-progress-state="queued"], [data-progress-state="running"]').first()).toBeVisible()
  await expect(page.locator('[data-progress-state="succeeded"]').first()).toBeAttached()
  await expect(page.locator('[data-progress-state="succeeded"]').first()).not.toHaveAttribute('open', '')
  await expect(page.locator('[data-progress-state="succeeded"]')).toHaveCount(2)
  await page.getByRole('link', { name: '想法', exact: true }).click()
  await page.getByRole('button', { name: '晚上夜骑', exact: true }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.keyboard.press('Escape')
  await page.getByRole('link', { name: '聊天', exact: true }).click()
  await page.getByRole('textbox', { name: '聊天消息' }).fill('请生成行程')
  await expect(page.getByRole('button', { name: '发送消息' })).toBeEnabled()
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(page.locator('[data-progress-state="succeeded"]')).toHaveCount(3)
  await page.getByRole('textbox', { name: '聊天消息' }).fill('确认当前行程')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(page.locator('[data-progress-state="succeeded"]')).toHaveCount(4)
  await page.getByRole('button', { name: '攻略', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '旅行攻略', exact: true })
  await expect(dialog).toBeVisible()
  await expect(page.locator('.chat-screen')).toHaveAttribute('inert', '')
  const frame = page.locator('iframe.guide-frame')
  await expect(frame).toBeVisible()
  await expect(frame).toHaveAttribute('sandbox', 'allow-popups allow-popups-to-escape-sandbox')
  await expect(page.frameLocator('iframe.guide-frame').getByRole('heading', { name: '西湖慢游攻略' })).toBeVisible()
  await expect(page.getByRole('button', { name: '确认行程', exact: true })).toHaveCount(0)
  await expect(page.locator('.guide-plan-status')).toHaveText('已确认')
  const url = await frame.getAttribute('src')
  const handle = await frame.elementHandle()
  await page.waitForTimeout(3800)
  expect(await frame.getAttribute('src')).toBe(url)
  expect(await handle?.evaluate(node => node.isConnected)).toBe(true)
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('link', { name: '下载当前攻略 HTML' }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toMatch(/\.html$/)
  expect(await download.failure()).toBeNull()

  // One visual inspection round: phone sizes and wide viewport, no redesign loop.
  for (const width of [320, 430, 1280]) {
    await page.setViewportSize({ width, height: width === 1280 ? 900 : 932 })
    await noOverflow(page)
    await page.screenshot({ path: `${pictures}/guide-${width}.png`, fullPage: true })
    await page.keyboard.press('Escape')
    await expect(page.getByRole('button', { name: '攻略', exact: true })).toBeFocused()
    await noOverflow(page)
    await page.screenshot({ path: `${pictures}/chat-${width}.png`, fullPage: true })
    await page.getByRole('button', { name: '攻略', exact: true }).click()
  }
  await page.goto(`${workspacePath}?tab=itinerary`)
  await expect(page.getByRole('dialog', { name: '旅行攻略', exact: true })).toBeVisible()
  await expect(page.locator('iframe.guide-frame')).toHaveAttribute('src', url!)
  await page.keyboard.press('Escape')
  await page.getByRole('link', { name: '返回我的旅行', exact: true }).click()
  await noOverflow(page)
  await page.screenshot({ path: `${pictures}/trips-1280.png`, fullPage: true })
  await page.getByRole('button', { name: '退出', exact: true }).click()
  await expect(page).toHaveURL(/\/$/)
  await page.getByLabel('邮箱', { exact: true }).fill(email)
  await page.getByLabel('密码', { exact: true }).fill(password)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page).toHaveURL(/\/trips$/)
  await expect(page.getByRole('link', { name: /新的旅行/ })).toBeVisible()
  expect(external).toEqual([])
  expect(errors).toEqual([])
})
