import { expect, test, type Page } from '@playwright/test'

async function createTrip(page: Page) {
  await page.goto('/?mode=register')
  await page.getByLabel('邮箱', { exact: true }).fill(`composer-${Date.now()}@example.com`)
  await page.getByLabel('密码', { exact: true }).fill('Offline-browser-123!')
  await page.getByRole('button', { name: '创建账号', exact: true }).click()
  await expect(page).toHaveURL(/\/trips$/)
  await page.getByRole('link', { name: '新建旅行', exact: true }).first().click()
  await page.getByLabel('住宿地址或酒店名称', { exact: true }).fill('杭州湖滨酒店')
  await page.getByRole('combobox', { name: '出行方式', exact: true }).selectOption('transit')
  await page.getByLabel('开始日期（必填）', { exact: true }).fill('2030-05-01')
  await page.getByLabel('游玩天数（必填）', { exact: true }).fill('1')
  await page.getByRole('button', { name: '创建并开始对话', exact: true }).click()
  await page.getByRole('radio', { name: /湖滨酒店（西湖店）/ }).check()
  await page.getByRole('button', { name: '从这里开始', exact: true }).click()
  await expect(page).toHaveURL(/\/workspace\/[^?]+\?tab=chat$/)
  return new URL(page.url()).pathname.split('/').at(-1)!
}

async function height(page: Page) {
  return page.getByRole('textbox', { name: '聊天消息' }).evaluate(el => el.getBoundingClientRect().height)
}

test('real Chrome: composer stays compact across delayed CSS, tabs, focus and multiline drafts', async ({ page, context }) => {
  await context.route('**/*', async route => {
    if (!['127.0.0.1', 'localhost'].includes(new URL(route.request().url()).hostname)) return route.abort()
    // Reproduce measuring before a dynamically reinserted chat stylesheet is ready.
    if (/\/chat-[^/]+\.css$/.test(route.request().url())) await new Promise(resolve => setTimeout(resolve, 300))
    return route.continue()
  })
  const id = await createTrip(page)
  await page.goto(`/workspace/${encodeURIComponent(id)}?tab=board`)
  for (let i = 0; i < 3; i++) {
    await page.getByRole('link', { name: '聊天', exact: true }).click()
    const input = page.getByRole('textbox', { name: '聊天消息' })
    await expect(input).toBeVisible()
    await expect.poll(() => height(page)).toBe(38)
    await input.focus()
    await expect.poll(() => height(page)).toBe(38)
    await input.blur()
    await expect.poll(() => height(page)).toBe(38)
    await page.getByRole('link', { name: '想法', exact: true }).click()
  }
  await page.getByRole('link', { name: '聊天', exact: true }).click()
  const input = page.getByRole('textbox', { name: '聊天消息' })
  const multiline = '第一行\n第二行\n第三行'
  await input.fill(multiline)
  await expect.poll(() => height(page)).toBe(86)
  await page.getByRole('link', { name: '想法', exact: true }).click()
  await page.getByRole('link', { name: '聊天', exact: true }).click()
  await expect(input).toHaveValue(multiline)
  await expect.poll(() => height(page)).toBe(86)
  await input.fill('很长的多行草稿\n'.repeat(20))
  await expect.poll(() => height(page)).toBe(126)
  await expect(input).toHaveCSS('overflow-y', 'auto')
  await input.fill('这是一段用于检查窄屏换行与宽度重测的旅行草稿，想去西湖散步，也想沿着湖边看风景。')
  const wideHeight = await height(page)
  await page.setViewportSize({ width: 320, height: 932 })
  await expect.poll(() => height(page)).toBeGreaterThan(wideHeight)
  const narrowHeight = await height(page)
  await page.setViewportSize({ width: 430, height: 932 })
  await expect.poll(() => height(page)).toBeLessThan(narrowHeight)
  await input.fill('')
  await expect.poll(() => height(page)).toBe(38)
  await page.screenshot({ path: 'test-results/composer-empty-430.png', fullPage: true })
})

test('real Chrome: async snapshot restores multiline draft and history navigation resizes it', async ({ page, context }) => {
  await context.route('**/*', route => ['127.0.0.1', 'localhost'].includes(new URL(route.request().url()).hostname) ? route.continue() : route.abort())
  const id = await createTrip(page)
  const path = `/workspace/${encodeURIComponent(id)}`
  await page.goto(`${path}?tab=chat`)
  const input = page.getByRole('textbox', { name: '聊天消息' })
  await input.fill('恢复第一行\n恢复第二行\n恢复第三行')
  await page.route(`**/api/trips/${encodeURIComponent(id)}/workspace`, async route => {
    await new Promise(resolve => setTimeout(resolve, 300))
    await route.continue()
  })
  await page.reload()
  await expect(input).toHaveValue('恢复第一行\n恢复第二行\n恢复第三行')
  await expect.poll(() => height(page)).toBe(86)
  // Tab controls intentionally replace history. Seed a board history entry to
  // exercise the router's popstate path rather than the tab click handler.
  await page.evaluate(() => { history.pushState({ ...history.state }, '', '?tab=board'); dispatchEvent(new PopStateEvent('popstate', { state: history.state })) })
  await expect(input).toBeHidden()
  await page.setViewportSize({ width: 320, height: 932 })
  await page.goBack()
  await expect(input).toBeVisible()
  await expect.poll(() => height(page)).toBe(86)
  await page.goForward()
  await expect(input).toBeHidden()
  await page.goBack()
  await expect(input).toHaveValue('恢复第一行\n恢复第二行\n恢复第三行')
  await expect.poll(() => height(page)).toBe(86)
  await input.fill('')
  await expect.poll(() => height(page)).toBe(38)
})
