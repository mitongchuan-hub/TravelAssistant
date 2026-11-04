import { expect, test } from '@playwright/test'

test('blocked fake Agent accepts follow-ups, cancels precisely, pauses and explicitly resumes', async ({ page, context }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await context.route('**/*', route => ['127.0.0.1', 'localhost'].includes(new URL(route.request().url()).hostname) ? route.continue() : route.abort())
  await page.goto('/?mode=register')
  await page.getByLabel('邮箱', { exact: true }).fill(`followup-${Date.now()}@example.com`)
  await page.getByLabel('密码', { exact: true }).fill('Offline-browser-123!')
  await page.getByRole('button', { name: '创建账号', exact: true }).click()
  await page.getByRole('link', { name: '新建旅行', exact: true }).first().click()
  await page.getByLabel('住宿地址或酒店名称', { exact: true }).fill('杭州湖滨酒店')
  await page.getByRole('combobox', { name: '出行方式', exact: true }).selectOption('transit')
  await page.getByLabel('开始日期（必填）', { exact: true }).fill('2030-05-01')
  await page.getByLabel('游玩天数（必填）', { exact: true }).fill('1')
  await page.getByRole('button', { name: '创建并开始对话', exact: true }).click()
  await page.getByRole('radio', { name: /湖滨酒店（西湖店）/ }).check()
  await page.getByRole('button', { name: '从这里开始', exact: true }).click()
  await expect(page).toHaveURL(/\/workspace\/[^?]+\?tab=chat$/)
  const tripId = new URL(page.url()).pathname.split('/').at(-1)!
  const root = `/api/trips/${encodeURIComponent(tripId)}`
  const queue = async () => (await (await page.request.get(`${root}/workspace`)).json()).queue
  const control = async (operation: 'block' | 'release') => {
    const response = await page.request.post(`/__test__/followup/${operation}`, { data: { trip_id: tripId } })
    expect(response.ok()).toBe(true)
  }
  const input = page.getByRole('textbox', { name: '聊天消息' })
  const send = async (body: string) => {
    await input.fill(body)
    await page.getByRole('button', { name: '发送消息', exact: true }).click()
  }
  const dock = page.getByRole('region', { name: '聊天输入区' })
  await control('block')
  try {
    await send('followup 阻塞 A')
    await expect.poll(async () => (await queue()).current_task?.body).toBe('followup 阻塞 A')
    const currentId = (await queue()).current_task.id
    await send('补充 B：晚上夜骑')
    await expect(dock).toContainText('补充 B：晚上夜骑')
    await send('补充 C：费用以实际为准')
    await expect.poll(async () => (await queue()).waiting_tasks.map((task: { body: string }) => task.body)).toEqual(['补充 B：晚上夜骑', '补充 C：费用以实际为准'])
    expect((await queue()).current_task.id).toBe(currentId)
    await input.fill('保留的草稿')
    await expect(page.getByRole('button', { name: '发送消息', exact: true })).toBeEnabled()
    await expect(page.getByRole('button', { name: '终止处理', exact: true })).toBeVisible()
    expect(await page.getByRole('button', { name: '终止处理', exact: true }).locator('svg rect').count()).toBe(1)
    for (const width of [320, 430]) {
      await page.setViewportSize({ width, height: 932 })
      expect(await page.evaluate(() => Math.max(document.body.scrollWidth, document.documentElement.scrollWidth) - innerWidth)).toBeLessThanOrEqual(1)
    }
    await dock.getByRole('button', { name: '取消等待消息：补充 B：晚上夜骑', exact: true }).click()
    await expect.poll(async () => (await queue()).waiting_tasks.map((task: { body: string }) => task.body)).toEqual(['补充 C：费用以实际为准'])
    expect((await queue()).current_task.id).toBe(currentId)
    await page.getByRole('button', { name: '终止处理', exact: true }).click()
    await expect.poll(async () => (await queue()).paused).toBe(true)
    await control('release')
    await expect.poll(async () => (await queue()).draining).toBe(false)
    await send('补充 D：不去商场')
    await expect.poll(async () => (await queue()).waiting_tasks.map((task: { body: string }) => task.body)).toEqual(['补充 C：费用以实际为准', '补充 D：不去商场'])
    expect((await queue()).paused).toBe(true)
    await dock.getByRole('button', { name: '基于当前保存内容继续', exact: true }).click()
    await expect.poll(async () => { const value = await queue(); return !value.paused && !value.current_task && value.waiting_tasks.length === 0 && !value.draining }).toBe(true)
    const { tasks } = await (await page.request.get(`${root}/tasks`)).json()
    expect(tasks.find((task: { id: string }) => task.id === currentId).status).toBe('cancelled')
    expect(tasks.filter((task: { body: string; status: string }) => ['补充 C：费用以实际为准', '补充 D：不去商场'].includes(task.body)).map((task: { status: string }) => task.status)).toEqual(['succeeded', 'succeeded'])
    expect(errors).toEqual([])
  } finally { await control('release') }
})
