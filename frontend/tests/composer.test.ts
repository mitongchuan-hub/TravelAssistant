import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { reactive } from 'vue'
import WorkspacePage from '@/pages/WorkspacePage.vue'
import { apiRequest } from '@/api/client'
import { deferred, snapshot } from './fixtures'

const routing = vi.hoisted(() => ({ route: { params: { id: 'trip-test' }, query: {} as Record<string, string> } }))
vi.mock('@/api/client', () => ({ apiRequest: vi.fn(), ApiError: class extends Error {} }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ user: { id: 'user-test' } }) }))
vi.mock('vue-router', () => ({ useRoute: () => routing.route, useRouter: () => ({ replace: vi.fn() }) }))

const options = { global: { stubs: { RouterLink: true, IdeaBoard: true, GuidePreview: true } } }
let reads = 0
beforeEach(() => {
  routing.route = reactive({ params: { id: 'trip-test' }, query: {} })
  sessionStorage.clear()
  vi.mocked(apiRequest).mockReset()
  reads = 0
  // happy-dom has no layout: emulate visible/hidden geometry and a misleading
  // empty-placeholder measurement. Real dimensions are covered in Chrome.
  vi.spyOn(HTMLTextAreaElement.prototype, 'getClientRects').mockImplementation(() =>
    (routing.route.query.tab === 'board' ? [] : [{}]) as unknown as DOMRectList)
  vi.spyOn(HTMLTextAreaElement.prototype, 'scrollHeight', 'get').mockImplementation(function (this: HTMLTextAreaElement) {
    reads++
    return this.value ? this.value.split('\n').length * 24 + 14 : 102
  })
})
afterEach(() => { vi.restoreAllMocks(); sessionStorage.clear() })

describe('workspace version mismatch recovery', () => {
  it.each(['chat', 'board'])('leaves initial loading and recovers on reread in the %s tab', async tab => {
    routing.route.query.tab = tab
    const value = snapshot()
    value.trip.requirements.transport_mode = 'transit'
    value.trip.lodging = { poi_id: 'fake-lodging' }
    const { text: _text, ...legacyRequirements } = value.trip.requirements
    vi.mocked(apiRequest).mockResolvedValueOnce({ ...value, trip: { ...value.trip, requirements: legacyRequirements } })
    const wrapper = mount(WorkspacePage, { global: { stubs: { RouterLink: true, GuidePreview: true } } })
    try {
      await flushPromises()
      expect(wrapper.text()).not.toContain('正在读取旅行记录')
      expect(wrapper.text()).toContain('前后端版本不一致，请重启后端后重新读取')
      expect(wrapper.find('.requirements-heading').exists()).toBe(false)
      value.trip.requirements.text = '重新读取后的安全正文'
      vi.mocked(apiRequest).mockResolvedValueOnce(value)
      await wrapper.get('.workspace-initial button').trigger('click'); await flushPromises()
      expect(wrapper.find('.workspace-initial').exists()).toBe(false)
      expect(wrapper.text()).toContain('重新读取后的安全正文')
      expect(wrapper.text()).not.toContain('前后端版本不一致')
    } finally { wrapper.unmount() }
  })
})

describe('temporarily hidden undo', () => {
  it('does not offer a new undo even when the server allows it', async () => {
    const value = snapshot({ messages: [{ id: 'reply-a', sender_type: 'assistant', body: '已保存修改。', created_at: '12:00', status: 'sent' }] })
    value.undo = { available_count: 2, latest_entry_id: 'entry-a', message_id: 'reply-a', version: 4, can_undo: true, reason: '' }
    vi.mocked(apiRequest).mockResolvedValue(value)
    const wrapper = mount(WorkspacePage, options)
    await flushPromises()
    expect(wrapper.text()).not.toContain('撤销上次修改')
    expect(wrapper.text()).not.toContain('确认撤销结果')
    expect(wrapper.find('button[aria-label="发送消息"]').exists()).toBe(true)
    expect(vi.mocked(apiRequest).mock.calls.some(call => call[0].endsWith('/undo'))).toBe(false)
    wrapper.unmount()
  })

  it('keeps recovery for an already submitted undo without starting a new one', async () => {
    sessionStorage.setItem('travelassistant:pending:user-test:trip-test:undo', JSON.stringify({ entry_id: 'old-entry', expected_version: 3, request_id: 'stable' }))
    vi.mocked(apiRequest).mockResolvedValue(snapshot())
    const wrapper = mount(WorkspacePage, options)
    await flushPromises()
    expect(wrapper.text()).not.toContain('撤销上次修改')
    expect(wrapper.findAll('button').some(button => button.text() === '确认撤销结果')).toBe(true)
    expect(vi.mocked(apiRequest).mock.calls.some(call => call[0].endsWith('/undo'))).toBe(false)
    wrapper.unmount()
  })
})

describe('follow-up composer controls', () => {
  it('shows send beside the square stop only with a draft and keeps waiting controls near input', async () => {
    const value = snapshot(); value.queue.current_task = { id: 'running', status: 'running', message: '' }
    value.queue.waiting_tasks = [{ id: 'b', status: 'waiting', body: '之后去博物馆', message: '' }]
    vi.mocked(apiRequest).mockResolvedValue(value)
    const wrapper = mount(WorkspacePage, options); await flushPromises()
    expect(wrapper.find('button[aria-label="终止处理"] svg rect').exists()).toBe(true)
    expect(wrapper.find('button[aria-label="发送消息"]').exists()).toBe(false)
    await wrapper.get('textarea').setValue('再看看餐厅')
    expect(wrapper.get('button[aria-label="发送消息"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.find('button[aria-label="终止处理"]').exists()).toBe(true)
    expect(wrapper.get('.chat-dock').text()).toContain('之后去博物馆')
    expect(wrapper.find('button[aria-label="取消等待消息：之后去博物馆"]').exists()).toBe(true)
    wrapper.unmount()
  })
  it('does not send through composition or Enter and keeps the draft during an uncertain ACK', async () => {
    vi.mocked(apiRequest).mockResolvedValueOnce(snapshot())
    const wrapper = mount(WorkspacePage, options); await flushPromises()
    await wrapper.get('textarea').setValue('输入法草稿')
    await wrapper.get('textarea').trigger('compositionstart')
    await wrapper.get('form.composer').trigger('submit')
    await wrapper.get('textarea').trigger('keydown', { key: 'Enter', isComposing: true, keyCode: 229 })
    expect(vi.mocked(apiRequest).mock.calls).toHaveLength(1)
    expect(wrapper.get<HTMLTextAreaElement>('textarea').element.value).toBe('输入法草稿')
    wrapper.unmount()
  })
  it('shows pause reason and explicit current-content resume without disabling new messages', async () => {
    const value = snapshot(); Object.assign(value.queue, { paused: true, pause_reason: 'failed' })
    vi.mocked(apiRequest).mockResolvedValue(value)
    const wrapper = mount(WorkspacePage, options); await flushPromises()
    expect(wrapper.get('.chat-dock').text()).toContain('失败')
    expect(wrapper.get('.chat-dock').text()).toContain('基于当前保存内容继续')
    await wrapper.get('textarea').setValue('继续补充')
    expect(wrapper.get('button[aria-label="发送消息"]').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })
})

describe('composer sizing lifecycle', () => {
  it('sets empty drafts to 38px without measuring placeholder height after async snapshot', async () => {
    const response = deferred<ReturnType<typeof snapshot>>()
    vi.mocked(apiRequest).mockReturnValueOnce(response.promise)
    const wrapper = mount(WorkspacePage, options)
    expect(wrapper.find('textarea').exists()).toBe(false)
    response.resolve(snapshot()); await flushPromises()
    const input = wrapper.get<HTMLTextAreaElement>('textarea')
    expect(input.element.style.height).toBe('38px')
    expect(input.element.style.overflowY).toBe('hidden')
    expect(reads).toBe(0)
    await input.trigger('focus'); await input.trigger('blur')
    expect(input.element.style.height).toBe('38px')
    wrapper.unmount()
  })

  it('sizes restored drafts after async mount, caps multiline text and resets after clearing', async () => {
    sessionStorage.setItem('travelassistant:chat-draft:user-test:trip-test', '第一行\n第二行\n第三行')
    const response = deferred<ReturnType<typeof snapshot>>()
    vi.mocked(apiRequest).mockReturnValueOnce(response.promise)
    const wrapper = mount(WorkspacePage, options)
    response.resolve(snapshot()); await flushPromises()
    const input = wrapper.get<HTMLTextAreaElement>('textarea')
    expect(input.element.value).toBe('第一行\n第二行\n第三行')
    expect(input.element.style.height).toBe('86px')
    await input.setValue('长草稿\n'.repeat(20))
    expect(input.element.style.height).toBe('126px')
    expect(input.element.style.overflowY).toBe('auto')
    await input.setValue('')
    expect(input.element.style.height).toBe('38px')
    expect(input.element.style.overflowY).toBe('hidden')
    wrapper.unmount()
  })

  it('skips hidden layout and resizes on reactive route changes without the tab click handler', async () => {
    routing.route.query.tab = 'board'
    sessionStorage.setItem('travelassistant:chat-draft:user-test:trip-test', '第一行\n第二行\n第三行')
    vi.mocked(apiRequest).mockResolvedValueOnce(snapshot())
    const wrapper = mount(WorkspacePage, options)
    await flushPromises()
    expect(reads).toBe(0)
    routing.route.query.tab = 'chat'; await flushPromises()
    const input = wrapper.get<HTMLTextAreaElement>('textarea')
    expect(input.element.style.height).toBe('86px')
    routing.route.query.tab = 'board'; await flushPromises()
    const before = reads
    await input.setValue('新第一行\n新第二行')
    expect(reads).toBe(before)
    routing.route.query.tab = 'chat'; await flushPromises()
    expect(input.element.style.height).toBe('62px')
    wrapper.unmount()
  })
})
