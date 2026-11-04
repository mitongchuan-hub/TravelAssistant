import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import SafeMarkdown, { replyPreview, safeHref } from '@/components/SafeMarkdown'
import ChatMessage from '@/components/ChatMessage.vue'
import ChatProgress from '@/components/ChatProgress.vue'
import GuidePreview from '@/components/GuidePreview.vue'
import type { ChatMessage as Message } from '@/stores/workspace'
import { snapshot, deferred } from './fixtures'
import WorkspacePage from '@/pages/WorkspacePage.vue'
import { apiRequest } from '@/api/client'
vi.mock('@/api/client', () => ({ apiRequest: vi.fn(), ApiError: class extends Error {} }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ user: { id: 'user-test' } }) }))
vi.mock('vue-router', () => ({ useRoute: () => ({ params: { id: 'trip-test' }, query: {} }), useRouter: () => ({ replace: vi.fn() }) }));

// Component tests never fetch iframe documents; the real browser suite covers them.
(window as unknown as { happyDOM: { settings: { disableIframePageLoading: boolean } } }).happyDOM.settings.disableIframePageLoading = true
afterEach(() => { document.body.innerHTML = ''; vi.restoreAllMocks() })

describe('workspace generation prerequisites', () => {
  it('keeps legacy incomplete trips usable with explicit generation guidance and no fixed-info editor', async () => {
    sessionStorage.clear()
    const initial = snapshot()
    initial.trip.requirements.start_date = ''
    vi.mocked(apiRequest).mockReset().mockResolvedValueOnce(initial)
    const wrapper = mount(WorkspacePage, { global: { stubs: { RouterLink: true, GuidePreview: true } } })
    await flushPromises()
    expect(wrapper.text()).toContain('不能生成或重排行程，请新建旅行填写完整资料')
    expect(wrapper.find('button[aria-label="发送消息"]').exists()).toBe(true)
    expect(wrapper.find('[name="lodging_address"]').exists()).toBe(false)
    expect(wrapper.findAll('button').some(button => button.text() === '攻略')).toBe(true)
    expect(wrapper.findAll('button').some(button => /撤销上次修改/.test(button.text()))).toBe(false)
    wrapper.unmount()
  })
})

describe('workspace 终止 composer', () => {
  it('keeps historical failed tasks readable without a retry control', async () => {
    sessionStorage.clear()
    vi.stubGlobal('matchMedia', () => ({ matches: false }))
    const api = vi.mocked(apiRequest); api.mockReset()
    api.mockResolvedValueOnce(snapshot({ tasks: [{ id: 'old-revision', kind: 'revision', request_id: 'old', retry_of: 'older', status: 'failed', message: '本次处理未完成，请重新发送消息。已有内容已保留。' }] }))
    const wrapper = mount(WorkspacePage, { global: { stubs: { RouterLink: true, IdeaBoard: true, GuidePreview: true } } })
    await flushPromises()
    expect(wrapper.text()).toContain('本次处理未完成，请重新发送消息。已有内容已保留。')
    expect(wrapper.findAll('button').some(button => /重试|重发/.test(button.text()))).toBe(false)
    expect(wrapper.find('button[aria-label="发送消息"]').exists()).toBe(true)
    expect(api.mock.calls.every(call => call[1]?.method !== 'POST')).toBe(true)
    wrapper.unmount(); vi.unstubAllGlobals()
  })
  it('uses the existing button to 终止 and preserves the current draft when sending becomes available', async () => {
    sessionStorage.clear()
    vi.stubGlobal('matchMedia', () => ({ matches: false }))
    const task = { id: 'stop', kind: 'chat', request_id: 'stop', status: 'running', message: '' }
    const api = vi.mocked(apiRequest); api.mockReset()
    api.mockResolvedValueOnce(snapshot({ tasks: [task] }))
    const wrapper = mount(WorkspacePage, { global: { stubs: { RouterLink: true, IdeaBoard: true, GuidePreview: true } } })
    await flushPromises()
    await wrapper.get('textarea').setValue('未发送草稿')
    const stop = wrapper.get('button[aria-label="终止处理"]')
    expect(stop.attributes('type')).toBe('button')
    expect(stop.attributes('title')).toBe('终止处理')
    expect(stop.text()).toBe('')
    expect(stop.find('svg[aria-hidden="true"] rect').exists()).toBe(true)
    const result = deferred<unknown>(); api.mockReturnValueOnce(result.promise)
    await stop.trigger('click')
    expect(wrapper.get('button[aria-label="正在终止"]').attributes('disabled')).toBeDefined()
    result.resolve({ task: { ...task, status: 'superseded' } }); await flushPromises()
    expect(wrapper.get<HTMLTextAreaElement>('textarea').element.value).toBe('未发送草稿')
    expect(wrapper.get('button[aria-label="发送消息"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.text()).toContain('已终止')
    wrapper.unmount(); vi.unstubAllGlobals()
  })
})

describe('safe Markdown DOM (replaces server-rendered Markdown assertions)', () => {
  it('escapes raw HTML, disables remote images and executable links, preserves safe Markdown', () => {
    const wrapper = mount(SafeMarkdown, { props: { text: '# 行程\n\n<script>window.pwned=true</script>\n\n<img src=x onerror=alert(1)>\n\n![tracking](https://evil.invalid/pixel)\n\n[bad](javascript:alert(1)) [data](data:text/html,test) [safe](https://example.com/guide)\n\n**重点** `code`\n\n| 时间 | 地点 |\n| --- | --- |\n| 9:00 | 西湖 |' } })
    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.text()).toContain('<script>window.pwned=true</script>')
    // Disabled image syntax may leave an ordinary safe hyperlink, never an image request.
    expect(wrapper.findAll('a').every(link => /^https?:\/\//.test(link.attributes('href') || ''))).toBe(true)
    expect(wrapper.get('a[href="https://example.com/guide"]').attributes()).toMatchObject({ href: 'https://example.com/guide', target: '_blank', rel: 'noopener noreferrer', referrerpolicy: 'no-referrer' })
    expect(wrapper.get('strong').text()).toBe('重点')
    expect(wrapper.get('code').text()).toBe('code')
    expect(wrapper.get('table').text()).toContain('西湖')
    for (const href of ['javascript:alert(1)', 'data:text/html,x', '//evil.invalid', '/relative', 'mailto:a@b.com']) expect(safeHref(href)).toBeUndefined()
  })
  it('keeps short full replies intact and recognizes a bounded itinerary overview', () => {
    expect(replyPreview('坐公交到西湖，费用以实际为准。').long).toBe(false)
    expect(replyPreview('概览：慢游西湖。\n\n---\n\n09:00 公交 20 分钟\n\n费用以实际为准。', 'itinerary-draft')).toEqual({ long: true, preview: '概览：慢游西湖。\n', overview: true })
  })
})

describe('message and progress disclosure', () => {
  it('offers draft recovery only for failed replies and never emits a task retry', async () => {
    const previous: Message = { id: 'user', sender_type: 'user', body: '修改西湖时间', created_at: '09:00', status: 'sent' }
    const wrapper = mount(ChatMessage, { props: { previous, message: { id: 'failed', sender_type: 'agent', body: '本次处理未完成，请重新发送消息。已有内容已保留。', status: 'failed', created_at: '09:01' }, busy: false } })
    expect(wrapper.get('button').text()).toBe('移回草稿')
    await wrapper.get('button').trigger('click')
    expect(wrapper.emitted('draft')).toEqual([[previous.body]])
    expect(wrapper.emitted('retry')).toBeUndefined()
  })
  it('recovers rejected sends to draft instead of replaying them', async () => {
    const wrapper = mount(ChatMessage, { props: { message: { id: 'rejected', sender_type: 'user', body: '草稿', status: 'send_failed', created_at: '09:01' }, busy: false } })
    expect(wrapper.get('button').text()).toBe('移回草稿')
    await wrapper.get('button').trigger('click')
    expect(wrapper.emitted('recover')).toEqual([[]])
    expect(wrapper.emitted('retry')).toBeUndefined()
  })
  it('hides legacy business buttons from saved messages', () => {
    const wrapper = mount(ChatMessage, { props: { message: { id: 'old', sender_type: 'agent', status: 'sent', created_at: '09:00', body: '历史消息', agent_card: { kind: 'requirement-summary', actions: [{ label: '生成路线', target: 'generate' }, { label: '想法', target: 'board' }] } }, busy: false } })
    expect(wrapper.findAll('button')).toHaveLength(0)
  })
  it('shows short transport information and cost disclaimer once without duplicate card bullets', () => {
    const message: Message = { id: 'm1', sender_type: 'assistant', status: 'done', created_at: '09:00', body: '坐公交约 20 分钟到西湖。费用以现场为准。', agent_card: { kind: 'reply', bullets: ['费用以现场为准。', '费用以现场为准！', '建议带水', '建议带水'] } }
    const wrapper = mount(ChatMessage, { props: { message, busy: false } })
    expect(wrapper.find('.chat-full-reply').exists()).toBe(false)
    expect(wrapper.get('.chat-reply-content').text()).toBe(message.body)
    expect(wrapper.text().match(/费用以现场为准/g)).toHaveLength(1)
    expect(wrapper.findAll('.chat-supplemental li')).toHaveLength(1)
  })
  it('collapses long replies by default while keeping the complete text available', async () => {
    const body = '概览：西湖慢游。\n\n---\n\n' + '完整公交换乘安排与门票费用提醒。\n\n'.repeat(60)
    const wrapper = mount(ChatMessage, { props: { message: { id: 'm2', sender_type: 'assistant', status: 'done', created_at: '09:00', body, agent_card: { kind: 'itinerary-draft', actions: [{ label: '旧行程', target: 'itinerary' }, { label: '重复', target: 'itinerary' }] } }, busy: false }, attachTo: document.body })
    const details = wrapper.get<HTMLDetailsElement>('details.chat-full-reply')
    expect(details.element.open).toBe(false)
    expect(wrapper.get('.chat-reply-content').text()).toContain('完整公交换乘安排与门票费用提醒。')
    details.element.open = true
    await wrapper.get('.chat-collapse').trigger('click')
    expect(details.element.open).toBe(false)
    expect(document.activeElement).toBe(details.get('summary').element)
    expect(wrapper.findAll('.chat-message-action')).toHaveLength(1)
    await wrapper.get('.chat-message-action').trigger('click')
    expect(wrapper.emitted('guide')).toHaveLength(1)
    wrapper.unmount()
  })
  it.each([
    ['cancelled', '已终止'],
    ['superseded', '已由新消息替代'],
    [undefined, '处理已停止'],
  ] as const)('shows factual stopped progress for reason %s', (reason, heading) => {
    const wrapper = mount(ChatProgress, { props: { progress: { state: 'superseded', reason, duration_ms: 1000, steps: [{ label: '查询地图', status: 'stopped' }] } } })
    expect(wrapper.get('.chat-progress-heading').text()).toBe(heading)
    expect(wrapper.get('summary').attributes('aria-label')).toBe(`${heading}，展开或收起执行过程`)
    expect(wrapper.get<HTMLDetailsElement>('details').element.open).toBe(false)
    expect(wrapper.text()).toContain('已停止')
    if (reason !== 'superseded') expect(wrapper.text()).not.toContain('新消息替代')
    wrapper.unmount()
  })
  it('opens active progress, collapses completion, and allows reopening full steps', async () => {
    const wrapper = mount(ChatProgress, { props: { progress: { state: 'running', started_at: Date.now(), steps: [{ label: '查询地图', detail: '公交路线', status: 'running' }] } } })
    expect(wrapper.get<HTMLDetailsElement>('details').element.open).toBe(true)
    expect(wrapper.text()).toContain('正在查询地图')
    await wrapper.setProps({ progress: { state: 'succeeded', duration_ms: 65000, steps: [{ label: '查询地图', detail: '公交路线', status: 'succeeded', duration_ms: 2000 }] } })
    expect(wrapper.get<HTMLDetailsElement>('details').element.open).toBe(false)
    expect(wrapper.text()).toContain('1分05秒')
    expect(wrapper.text()).toContain('公交路线')
    const details = wrapper.get<HTMLDetailsElement>('details')
    details.element.open = true
    await details.trigger('toggle')
    await nextTick()
    expect(details.element.open).toBe(true)
    wrapper.unmount()
  })
})

describe('guide preview contract', () => {
  it('keeps confirmation and webpage retry in chat while retaining the saved text', () => {
    const initial = snapshot()
    initial.display_state.guide_available = false
    initial.display_state.display_status = 'fallback'
    const wrapper = mount(GuidePreview, { props: { open: true, snapshot: initial, busy: false, feedback: '' }, global: { stubs: { RouterLink: true } } })
    expect(wrapper.findAll('button').map(button => button.text())).toEqual(['返回聊天'])
    expect(wrapper.text()).toContain('请在聊天中告诉助手')
    for (const line of initial.plan.text.split('\n').filter(Boolean)) expect(wrapper.get('[data-itinerary-text]').text()).toContain(line.replace(/^#+\s*/, ''))
    wrapper.unmount()
  })
  it('sandboxes the stable versioned iframe and keeps its DOM context through busy/idle polling', async () => {
    const initial = snapshot()
    const wrapper = mount(GuidePreview, { props: { open: false, snapshot: initial, busy: false, feedback: '' }, global: { stubs: { RouterLink: true } } })
    const frame = wrapper.get('iframe').element
    const url = wrapper.get('iframe').attributes('src')
    expect(url).toContain('/api/trips/trip-test/guide?plan_id=plan-test&source_hash=hash-a&version=guide-1')
    expect(wrapper.get('iframe').attributes('sandbox')).toBe('allow-popups allow-popups-to-escape-sandbox')
    expect(wrapper.get('iframe').attributes('referrerpolicy')).toBe('no-referrer')
    expect(wrapper.get('.guide-download').attributes('href')).toBe(`${url}&download=true`)
    await wrapper.setProps({ snapshot: { ...initial, display_state: { ...initial.display_state, display_status: 'pending', busy: true } }, busy: true })
    expect(wrapper.get('iframe').element).toBe(frame)
    expect(wrapper.get('.guide-download').attributes('href')).toBe(`${url}&download=true`)
    await wrapper.setProps({ snapshot: structuredClone(initial), busy: false })
    expect(wrapper.get('iframe').element).toBe(frame)
    await wrapper.setProps({ snapshot: { ...initial, display_state: { ...initial.display_state, guide_version: 'guide-2' } } })
    expect(wrapper.get('iframe').element).not.toBe(frame)
    expect(wrapper.get('.guide-download').attributes('href')).toContain('version=guide-2&download=true')
    wrapper.unmount()
  })
  it('focuses the open dialog, emits Escape close, restores prior focus, and renders legacy route text', async () => {
    const trigger = document.createElement('button'); trigger.textContent = '打开'; document.body.append(trigger); trigger.focus()
    const initial = snapshot()
    initial.display_state.guide_available = false
    const wrapper = mount(GuidePreview, { props: { open: false, snapshot: initial, busy: false, feedback: '' }, attachTo: document.body, global: { stubs: { RouterLink: true } } })
    await wrapper.setProps({ open: true }); await flushPromises()
    expect(wrapper.get('aside').attributes('aria-modal')).toBe('true')
    expect(wrapper.get('aside').element.contains(document.activeElement)).toBe(true)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    expect(wrapper.emitted('close')).toHaveLength(1)
    await wrapper.setProps({ open: false }); await flushPromises()
    expect(document.activeElement).toBe(trigger)
    await wrapper.setProps({ snapshot: snapshot({ plan: { ...initial.plan, text: '', days: [{ label: '第一天', items: [{ title: '西湖', incoming_route: { origin_name: '酒店', destination_name: '西湖', duration_minutes: 20, reason: '步行' } }] }] } }) })
    expect(wrapper.get('[data-itinerary-text]').text()).toContain('酒店 → 西湖 · 查询估算 20 分钟 · 步行')
    wrapper.unmount()
  })
})
