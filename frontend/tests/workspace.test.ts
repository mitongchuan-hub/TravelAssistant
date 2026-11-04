import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, type EffectScope } from 'vue'
import { flushPromises } from '@vue/test-utils'
import { apiRequest, ApiError } from '@/api/client'
import { useWorkspace, type WorkspaceStore } from '@/stores/workspace'
import { deferred, snapshot } from './fixtures'

vi.mock('@/api/client', () => ({ apiRequest: vi.fn(), ApiError: class ApiError extends Error { constructor(public status: number, public code: string, message: string) { super(message) } } }))
const api = vi.mocked(apiRequest)
let scopes: EffectScope[] = []
function workspace(trip = 'trip-test', user = 'user-test'): WorkspaceStore {
  const scope = effectScope(); scopes.push(scope)
  return scope.run(() => useWorkspace(trip, user))!
}
beforeEach(() => { vi.useFakeTimers(); api.mockReset(); sessionStorage.clear(); Object.defineProperty(document, 'hidden', { configurable: true, value: false }) })
afterEach(() => { scopes.forEach(scope => scope.stop()); scopes = []; vi.useRealTimers() })

describe('workspace response contract', () => {
  it.each([
    ['missing requirements', undefined],
    ['null requirements', null],
    ['legacy cards without text', { revision: 1, cards: [{ text: '旧卡片' }] }],
    ['null text', { revision: 1, text: null }],
    ['numeric text', { revision: 1, text: 42 }],
    ['array text', { revision: 1, text: [] }],
    ['object text', { revision: 1, text: {} }],
    ['missing revision', { text: '' }],
    ['string revision', { revision: '1', text: '' }],
    ['null revision', { revision: null, text: '' }],
    ['negative revision', { revision: -1, text: '' }],
    ['fractional revision', { revision: 1.5, text: '' }],
    ['infinite revision', { revision: Infinity, text: '' }],
    ['NaN revision', { revision: NaN, text: '' }],
    ['unsafe revision', { revision: Number.MAX_SAFE_INTEGER + 1, text: '' }],
  ])('rejects %s without publishing an unsafe snapshot', async (_name, requirements) => {
    const value = snapshot()
    api.mockResolvedValueOnce({ ...value, trip: { ...value.trip, requirements } })
    const state = workspace(); await flushPromises()
    expect(state.snapshot.value).toBeNull()
    expect(state.loading.value).toBe(false)
    expect(state.error.value).toBe('前后端版本不一致，请重启后端后重新读取。')
    api.mockResolvedValueOnce(snapshot())
    await state.refresh()
    expect(state.snapshot.value?.trip.requirements.text).toBe('')
    expect(state.error.value).toBe('')
  })
  it.each([0, 1, Number.MAX_SAFE_INTEGER])('accepts a string body and safe non-negative revision %s', async revision => {
    const value = snapshot(); value.trip.requirements.revision = revision
    api.mockResolvedValueOnce(value)
    const state = workspace(); await flushPromises()
    expect(state.snapshot.value).toEqual(value)
    expect(state.error.value).toBe('')
  })
  it.each(['refresh', 'cancel'] as const)('retains a safe snapshot when %s receives an old DTO', async operation => {
    const safe = snapshot(); safe.trip.requirements.text = '已保存的正文'
    api.mockResolvedValueOnce(safe)
    const state = workspace(); await flushPromises()
    const previous = state.snapshot.value
    api.mockResolvedValueOnce({ ...safe, trip: { ...safe.trip, requirements: { revision: 2, cards: [] } } })
    await state[operation]()
    expect(state.snapshot.value).toBe(previous)
    expect(state.snapshot.value?.trip.requirements.text).toBe('已保存的正文')
    expect(state.error.value).toContain('前后端版本不一致')
    expect(api.mock.calls.every(call => call[1]?.method !== 'POST')).toBe(true)
  })
})

describe('workspace polling and optimistic recovery', () => {
  it('does not replay an old unsent plan request as chat authorization', async () => {
    sessionStorage.setItem('travelassistant:pending:user-test:trip-test', JSON.stringify({ kind: 'plan', request_id: 'old-plan', payload: {} }))
    api.mockResolvedValueOnce(snapshot())
    const state = workspace(); await flushPromises()
    expect(state.pending.value).toBeNull()
    expect(api.mock.calls.every(call => call[1]?.method !== 'POST')).toBe(true)
    expect('generate' in state).toBe(false)
  })
  it('discards obsolete retry pending without posting or inventing chat authorization', async () => {
    sessionStorage.setItem('travelassistant:pending:user-test:trip-test', JSON.stringify({ kind: 'chat', retry_of: 'old-revision', request_id: 'obsolete', payload: {} }))
    api.mockResolvedValueOnce(snapshot())
    const state = workspace(); await flushPromises()
    expect(state.pending.value).toBeNull()
    expect(sessionStorage.getItem('travelassistant:pending:user-test:trip-test')).toBeNull()
    expect(api.mock.calls.every(call => call[1]?.method !== 'POST')).toBe(true)
    expect('retryTask' in state).toBe(false)
  })
  it('resends a failed task instruction as a new message and request identity', async () => {
    api.mockResolvedValueOnce(snapshot({ tasks: [{ id: 'failed', kind: 'chat', status: 'failed', request_id: 'old-request', message: '' }] }))
    const state = workspace(); await flushPromises()
    api.mockImplementationOnce(async (path, options) => {
      expect(path).toBe('/api/trips/trip-test/messages')
      const body = JSON.parse(String(options?.body))
      expect(body.body).toBe('请修改行程')
      expect(body.request_id).not.toBe('old-request')
      return { task: { id: 'new-chat', kind: 'chat', status: 'queued', request_id: body.request_id, message_id: 'new-message' }, messages: [{ id: 'new-message', sender_type: 'user', body: body.body }] }
    })
    api.mockResolvedValueOnce(snapshot())
    await state.send('请修改行程')
    expect(state.pending.value).toBeNull()
  })
  it('polls idle state too, picks up another tab’s newer guide and stops all work on unmount', async () => {
    const first = snapshot()
    api.mockResolvedValueOnce(first)
    const state = workspace(); await flushPromises()
    expect(state.snapshot.value?.display_state.guide_version).toBe('guide-1')
    api.mockResolvedValueOnce({ ...first, display_state: { ...first.display_state, guide_version: 'guide-2' } })
    await vi.advanceTimersByTimeAsync(3500)
    expect(state.snapshot.value?.display_state.guide_version).toBe('guide-2')
    const count = api.mock.calls.length
    scopes[0]!.stop()
    await vi.advanceTimersByTimeAsync(10000)
    expect(api).toHaveBeenCalledTimes(count)
  })
  it('rejects late responses after unmount and isolates different user/trip scopes', async () => {
    const old = deferred<ReturnType<typeof snapshot>>()
    api.mockReturnValueOnce(old.promise)
    const first = workspace('old-trip', 'old-user')
    const signal = api.mock.calls[0]![1]!.signal as AbortSignal
    scopes[0]!.stop()
    expect(signal.aborted).toBe(true)
    const current = snapshot(); current.trip.id = 'new-trip'
    api.mockResolvedValueOnce(current)
    const second = workspace('new-trip', 'new-user'); await flushPromises()
    old.resolve(snapshot()); await flushPromises()
    expect(first.snapshot.value).toBeNull()
    expect(second.snapshot.value?.trip.id).toBe('new-trip')
    expect(api.mock.calls[1]![0]).toBe('/api/trips/new-trip/workspace')
  })
  it('saves the exact requirements body with the captured revision via the narrow PUT endpoint', async () => {
    api.mockResolvedValueOnce(snapshot())
    const state = workspace(); await flushPromises()
    api.mockResolvedValueOnce(snapshot()); api.mockResolvedValueOnce(snapshot())
    expect(await state.saveRequirements('', 7)).toBe(true)
    expect(api.mock.calls[1]?.[0]).toBe('/api/trips/trip-test/requirements-text')
    expect(api.mock.calls[1]?.[1]?.method).toBe('PUT')
    expect(JSON.parse(String(api.mock.calls[1]?.[1]?.body))).toEqual({ text: '', expected_revision: 7 })
  })
  it('does not overwrite a newer mutation with an in-flight stale poll', async () => {
    const original = snapshot(); api.mockResolvedValueOnce(original)
    const state = workspace(); await flushPromises()
    const stale = deferred<ReturnType<typeof snapshot>>()
    api.mockReturnValueOnce(stale.promise)
    const refresh = state.refresh()
    api.mockResolvedValueOnce({})
    const action = state.saveRequirements('夜骑', 1)
    await flushPromises()
    stale.resolve(snapshot({ plan: { ...original.plan, title: 'stale response' } }))
    await refresh; await action
    expect(state.snapshot.value?.plan.title).toBe('西湖慢游')
  })
  it('reconciles a lost POST response before retrying, using one request id with no duplicate optimistic message', async () => {
    const original = snapshot(); api.mockResolvedValueOnce(original)
    const state = workspace(); await flushPromises()
    let requestId = ''
    api.mockImplementationOnce(async (_path, options) => {
      requestId = JSON.parse(String(options?.body)).request_id
      throw new ApiError(0, 'network_error', 'lost acknowledgement')
    })
    api.mockImplementationOnce(async () => snapshot({ tasks: [{ id: 'task-1', kind: 'chat', request_id: requestId, status: 'running', message: '', message_id: 'server-message' }], messages: [{ id: 'server-message', sender_type: 'user', body: '晚上夜骑', created_at: '09:00', status: 'done' }] }))
    await state.send('晚上夜骑')
    expect(requestId).not.toBe('')
    expect(state.pending.value).toBeNull()
    expect(state.messages.value.filter(message => message.body === '晚上夜骑')).toHaveLength(1)
    expect(api.mock.calls.filter(call => call[1]?.method === 'POST')).toHaveLength(1)
  })
  it('looks up a lost acknowledgement outside the latest 20 tasks without replaying the POST', async () => {
    api.mockResolvedValueOnce(snapshot())
    const state = workspace(); await flushPromises()
    let id = ''
    api.mockImplementationOnce(async (_path, options) => {
      id = JSON.parse(String(options?.body)).request_id
      throw new ApiError(0, 'network_error', 'lost acknowledgement')
    })
    const recent = Array.from({ length: 20 }, (_, i) => ({ id: `recent-${i}`, kind: 'chat', request_id: `recent-request-${i}`, status: 'succeeded', message: '' }))
    api.mockResolvedValueOnce(snapshot({ tasks: recent, messages: [] }))
    api.mockImplementationOnce(async path => {
      expect(path).toBe(`/api/trips/trip-test/tasks?request_id=${encodeURIComponent(id)}`)
      return { tasks: [{ id: 'historical', kind: 'chat', request_id: id, status: 'succeeded', message: '', message_id: 'deleted' }] }
    })
    await state.send('已在历史中完成')
    expect(state.pending.value).toBeNull()
    expect(state.messages.value).toEqual([])
    expect(state.busy.value).toBe(false)
    expect(api.mock.calls.filter(call => call[1]?.method === 'POST')).toHaveLength(1)
  })
  it('persists uncertain sends only within the current user and trip, and retries with the same identifier', async () => {
    api.mockResolvedValueOnce(snapshot())
    const state = workspace(); await flushPromises()
    api.mockRejectedValueOnce(new ApiError(0, 'network_error', 'offline'))
    api.mockResolvedValueOnce(snapshot())
    api.mockResolvedValueOnce({ tasks: [] })
    api.mockRejectedValueOnce(new ApiError(0, 'network_error', 'offline'))
    await state.send('保留我的输入')
    const stored = sessionStorage.getItem('travelassistant:pending:user-test:trip-test')
    expect(stored).toContain('保留我的输入')
    const posts = api.mock.calls.filter(call => call[1]?.method === 'POST')
    expect(posts).toHaveLength(2)
    expect(JSON.parse(String(posts[0]![1]!.body)).request_id).toBe(JSON.parse(String(posts[1]![1]!.body)).request_id)
    api.mockResolvedValueOnce(snapshot())
    const other = workspace('trip-test', 'other-user'); await flushPromises()
    expect(other.pending.value).toBeNull()
  })
  it('clears a lost acknowledgement when the durable task remains but another tab cleared its messages', async () => {
    api.mockResolvedValueOnce(snapshot())
    const state = workspace(); await flushPromises()
    let sentId = ''
    api.mockImplementationOnce(async (_path, options) => {
      sentId = JSON.parse(String(options?.body)).request_id
      throw new ApiError(0, 'network_error', 'lost acknowledgement')
    })
    api.mockImplementationOnce(async () => snapshot({ messages: [], tasks: [{ id: 'durable-task', kind: 'chat', request_id: sentId, status: 'succeeded', message: '', message_id: 'deleted-message' }] }))
    await state.send('另一页面清空前的消息')
    expect(state.pending.value).toBeNull()
    expect(state.messages.value).toEqual([])
    expect(state.busy.value).toBe(false)
    api.mockResolvedValueOnce(snapshot())
    await state.refresh()
    expect(api.mock.calls.filter(call => call[1]?.method === 'POST')).toHaveLength(1)
    expect(sessionStorage.getItem('travelassistant:pending:user-test:trip-test')).toBeNull()
  })
  it('pauses after 401, preserves uncertain text, and permits a fresh authenticated scope to reconcile', async () => {
    const expired = vi.fn()
    api.mockResolvedValueOnce(snapshot())
    const scope = effectScope(); scopes.push(scope)
    const state = scope.run(() => useWorkspace('trip-test', 'user-test', expired))!
    await flushPromises()
    api.mockRejectedValueOnce(new ApiError(401, 'unauthorized', '请先登录'))
    await state.send('登录后继续确认这条消息')
    expect(expired).toHaveBeenCalledTimes(1)
    expect(state.authRequired.value).toBe(true)
    const saved = JSON.parse(sessionStorage.getItem('travelassistant:pending:user-test:trip-test')!)
    expect(saved.body).toBe('登录后继续确认这条消息')
    const count = api.mock.calls.length
    await vi.advanceTimersByTimeAsync(10000)
    expect(api).toHaveBeenCalledTimes(count)
    scope.stop()
    api.mockResolvedValueOnce(snapshot({ tasks: [{ id: 'authenticated-task', kind: 'chat', request_id: saved.request_id, status: 'succeeded', message: '', message_id: 'server-message' }], messages: [{ id: 'server-message', sender_type: 'user', body: saved.body, created_at: '09:00', status: 'sent' }] }))
    const restored = workspace(); await flushPromises()
    expect(restored.authRequired.value).toBe(false)
    expect(restored.pending.value).toBeNull()
    expect(restored.messages.value).toHaveLength(1)
    expect(api.mock.calls.filter(call => call[1]?.method === 'POST')).toHaveLength(1)
  })
  it.each([403, 404])('allows explicit reconnect after a %s snapshot failure rather than permanently stopping', async status => {
    api.mockRejectedValueOnce(new ApiError(status, 'unavailable', '暂不可用'))
    const state = workspace(); await flushPromises()
    await vi.advanceTimersByTimeAsync(10000)
    expect(api).toHaveBeenCalledTimes(1)
    api.mockResolvedValueOnce(snapshot())
    await state.refresh()
    expect(state.snapshot.value?.trip.id).toBe('trip-test')
    expect(state.error.value).toBe('')
    api.mockResolvedValueOnce(snapshot())
    await vi.advanceTimersByTimeAsync(3500)
    expect(api).toHaveBeenCalledTimes(3)
  })
  it.each(['queued', 'running'])('终止 %s task and rejects stale polling without losing data', async status => {
    const task = { id: 'task-stop', kind: 'chat', request_id: 'stop', status, message: '' }
    api.mockResolvedValueOnce(snapshot({ tasks: [task] }))
    const state = workspace(); await flushPromises()
    const stale = deferred<ReturnType<typeof snapshot>>()
    api.mockReturnValueOnce(stale.promise)
    const poll = state.refresh()
    api.mockResolvedValueOnce({ task: { ...task, status: 'superseded' } })
    expect(await state.cancel()).toBe(true)
    expect(state.busy.value).toBe(false)
    stale.resolve(snapshot({ tasks: [task] })); await poll
    expect(state.busy.value).toBe(false)
    expect(state.snapshot.value?.plan.title).toBe('西湖慢游')
    expect(api.mock.calls.some(([path]) => path.endsWith('/tasks/task-stop/cancel'))).toBe(true)
  })
  it('终止 failure remains explicit and does not pretend the task stopped', async () => {
    api.mockResolvedValueOnce(snapshot({ tasks: [{ id: 'task-stop', kind: 'chat', request_id: 'stop', status: 'running', message: '' }] }))
    const state = workspace(); await flushPromises()
    api.mockRejectedValueOnce(new ApiError(503, 'unavailable', '暂不可用'))
    expect(await state.cancel()).toBe(false)
    expect(state.busy.value).toBe(true)
    expect(state.feedback.value).toContain('终止失败')
  })
  it('终止 during submitting waits for acknowledgement and does not abort the POST', async () => {
    api.mockResolvedValueOnce(snapshot())
    const state = workspace(); await flushPromises()
    const ack = deferred<unknown>()
    let id = ''
    api.mockImplementationOnce((_path, options) => { id = JSON.parse(String(options?.body)).request_id; return ack.promise })
    const send = state.send('保留输入')
    const postSignal = api.mock.calls[1]![1]!.signal as AbortSignal
    const cancel = state.cancel()
    expect(postSignal.aborted).toBe(false)
    api.mockResolvedValueOnce({ task: { id: 'accepted', kind: 'chat', request_id: id, status: 'superseded', message: '' } })
    ack.resolve({ task: { id: 'accepted', kind: 'chat', request_id: id, status: 'queued', message: '' }, messages: [] })
    expect(await cancel).toBe(true)
    api.mockResolvedValueOnce(snapshot())
    await send
    expect(state.pending.value).toBeNull()
    expect(state.busy.value).toBe(false)
    expect(state.feedback.value).toContain('已终止')
  })
  it('终止 an uncertain submission never replays it and preserves a durable retryable stop intent', async () => {
    api.mockResolvedValueOnce(snapshot())
    const state = workspace(); await flushPromises()
    const ack = deferred<unknown>()
    api.mockReturnValueOnce(ack.promise)
    const send = state.send('不确定送达')
    const cancel = state.cancel()
    api.mockResolvedValueOnce(snapshot())
    api.mockResolvedValueOnce({ tasks: [] })
    ack.reject(new ApiError(0, 'network_error', 'offline'))
    expect(await cancel).toBe(false)
    await send
    expect(state.feedback.value).toContain('终止失败')
    expect(state.pending.value?.cancel_requested).toBe(true)
    api.mockResolvedValueOnce(snapshot())
    api.mockResolvedValueOnce({ tasks: [] })
    await state.refresh()
    expect(api.mock.calls.filter(call => call[1]?.method === 'POST')).toHaveLength(1)
    expect(sessionStorage.getItem('travelassistant:pending:user-test:trip-test')).toContain('cancel_requested')
  })
  it('recovers a persisted cancel intent from exact history and accepts terminal cancellation', async () => {
    const id = 'old/request ?'
    sessionStorage.setItem('travelassistant:pending:user-test:trip-test', JSON.stringify({ kind: 'chat', request_id: id, payload: { body: '停止' }, cancel_requested: true }))
    const task = { id: 'old-task', kind: 'chat', request_id: id, status: 'succeeded', message: '' }
    api.mockResolvedValueOnce(snapshot()).mockResolvedValueOnce({ tasks: [task] })
    const state = workspace(); await flushPromises()
    expect(state.pending.value?.cancel_requested).toBe(true)
    expect(api.mock.calls[1]![0]).toBe(`/api/trips/trip-test/tasks?request_id=${encodeURIComponent(id)}`)
    api.mockResolvedValueOnce(snapshot()).mockResolvedValueOnce({ tasks: [task] })
    api.mockResolvedValueOnce({ task })
    expect(await state.cancel()).toBe(true)
    expect(state.pending.value).toBeNull()
    expect(state.busy.value).toBe(false)
    expect(state.feedback.value).toContain('已结束')
    expect(state.snapshot.value?.tasks).toContainEqual(task)
    expect(api.mock.calls.filter(call => call[1]?.method === 'POST').map(call => call[0])).toEqual(['/api/trips/trip-test/tasks/old-task/cancel'])
  })
  it('cancels a running historical pending task rather than resending its message', async () => {
    sessionStorage.setItem('travelassistant:pending:user-test:trip-test', JSON.stringify({ kind: 'chat', request_id: 'pending-stop', payload: {}, cancel_requested: true }))
    api.mockResolvedValueOnce(snapshot()).mockResolvedValueOnce({ tasks: [] })
    const state = workspace(); await flushPromises()
    const task = { id: 'historical-running', kind: 'chat', request_id: 'pending-stop', status: 'running', message: '' }
    api.mockResolvedValueOnce(snapshot()).mockResolvedValueOnce({ tasks: [task] })
    api.mockResolvedValueOnce({ task: { ...task, status: 'superseded' } })
    expect(await state.cancel()).toBe(true)
    expect(state.feedback.value).toContain('已终止')
    expect(api.mock.calls.filter(call => call[1]?.method === 'POST').map(call => call[0])).toEqual(['/api/trips/trip-test/tasks/historical-running/cancel'])
  })
  it('does not replay when the exact-history lookup is unavailable', async () => {
    sessionStorage.setItem('travelassistant:pending:user-test:trip-test', JSON.stringify({ kind: 'chat', request_id: 'uncertain', payload: {} }))
    api.mockResolvedValueOnce(snapshot()).mockRejectedValueOnce(new ApiError(503, 'unavailable', '历史暂不可用'))
    const state = workspace(); await flushPromises()
    expect(state.pending.value?.request_id).toBe('uncertain')
    expect(state.error.value).toBe('历史暂不可用')
    expect(api.mock.calls.every(call => call[1]?.method !== 'POST')).toBe(true)
  })
  it.each([false, true])('pauses on exact-history 401 and preserves pending cancel=%s', async cancelRequested => {
    sessionStorage.setItem('travelassistant:pending:user-test:trip-test', JSON.stringify({ kind: 'chat', request_id: 'expired-query', payload: {}, cancel_requested: cancelRequested }))
    const expired = vi.fn()
    api.mockResolvedValueOnce(snapshot()).mockRejectedValueOnce(new ApiError(401, 'unauthorized', '请先登录'))
    const scope = effectScope(); scopes.push(scope)
    const state = scope.run(() => useWorkspace('trip-test', 'user-test', expired))!
    await flushPromises()
    expect(state.authRequired.value).toBe(true)
    expect(expired).toHaveBeenCalledTimes(1)
    expect(state.pending.value?.request_id).toBe('expired-query')
    expect(state.pending.value?.cancel_requested).toBe(cancelRequested)
    expect(sessionStorage.getItem('travelassistant:pending:user-test:trip-test')).toContain('expired-query')
    await vi.advanceTimersByTimeAsync(10000)
    expect(api).toHaveBeenCalledTimes(2)
    expect(await state.cancel()).toBe(false)
  })
  it.each([false, true])('rejects stale exact-history %s results across a newer mutation and polling round', async found => {
    sessionStorage.setItem('travelassistant:pending:user-test:trip-test', JSON.stringify({ kind: 'chat', request_id: 'old-query', payload: {} }))
    const lookup = deferred<{ tasks: { id: string; kind: string; request_id: string; status: string; message: string }[] }>()
    api.mockResolvedValueOnce(snapshot()).mockReturnValueOnce(lookup.promise)
    const state = workspace(); await flushPromises()
    api.mockResolvedValueOnce({})
    const mutation = state.saveRequirements('新一轮', 1)
    await flushPromises()
    lookup.resolve({ tasks: found ? [{ id: 'old-result', kind: 'chat', request_id: 'old-query', status: 'running', message: '' }] : [] })
    await mutation
    expect(state.pending.value?.request_id).toBe('old-query')
    expect(state.snapshot.value?.tasks).toEqual([])
    expect(api.mock.calls.filter(call => call[0].endsWith('/messages'))).toHaveLength(0)
    const current = snapshot(); current.plan.title = '新一轮快照'
    api.mockResolvedValueOnce(current).mockResolvedValueOnce({ tasks: [{ id: 'current-result', kind: 'chat', request_id: 'old-query', status: 'succeeded', message: '' }] })
    await state.refresh()
    expect(state.snapshot.value?.plan.title).toBe('新一轮快照')
    expect(state.pending.value).toBeNull()
    expect(state.snapshot.value?.tasks.map(task => task.id)).toEqual(['current-result'])
  })
  it('does not let a previous polling lookup undo a newer cancellation or replay its POST', async () => {
    sessionStorage.setItem('travelassistant:pending:user-test:trip-test', JSON.stringify({ kind: 'chat', request_id: 'cancel-race', payload: {} }))
    const oldLookup = deferred<{ tasks: [] }>()
    api.mockResolvedValueOnce(snapshot()).mockReturnValueOnce(oldLookup.promise)
    const state = workspace(); await flushPromises()
    const task = { id: 'cancel-race-task', kind: 'chat', request_id: 'cancel-race', status: 'running', message: '' }
    api.mockResolvedValueOnce(snapshot()).mockResolvedValueOnce({ tasks: [task] })
    api.mockResolvedValueOnce({ task: { ...task, status: 'superseded' } })
    expect(await state.cancel()).toBe(true)
    oldLookup.resolve({ tasks: [] }); await flushPromises()
    expect(state.pending.value).toBeNull()
    expect(state.snapshot.value?.tasks[0]?.status).toBe('superseded')
    expect(state.feedback.value).toContain('已终止')
    expect(api.mock.calls.filter(call => call[1]?.method === 'POST').map(call => call[0])).toEqual(['/api/trips/trip-test/tasks/cancel-race-task/cancel'])
  })
  it('preserves a new cancel intent and stops all POSTs when its exact lookup returns 401', async () => {
    sessionStorage.setItem('travelassistant:pending:user-test:trip-test', JSON.stringify({ kind: 'chat', request_id: 'cancel-expired', payload: {}, cancel_requested: true }))
    api.mockResolvedValueOnce(snapshot()).mockResolvedValueOnce({ tasks: [] })
    const state = workspace(); await flushPromises()
    api.mockResolvedValueOnce(snapshot()).mockRejectedValueOnce(new ApiError(401, 'unauthorized', '请先登录'))
    expect(await state.cancel()).toBe(false)
    expect(state.authRequired.value).toBe(true)
    expect(state.pending.value?.cancel_requested).toBe(true)
    const count = api.mock.calls.length
    await vi.advanceTimersByTimeAsync(10000)
    expect(api).toHaveBeenCalledTimes(count)
    expect(api.mock.calls.every(call => call[1]?.method !== 'POST')).toBe(true)
  })
  it('aborts exact-history lookup on unmount and ignores late acknowledgement', async () => {
    sessionStorage.setItem('travelassistant:pending:user-test:trip-test', JSON.stringify({ kind: 'chat', request_id: 'late-query', payload: {} }))
    const lookup = deferred<{ tasks: { id: string; kind: string; request_id: string; status: string; message: string }[] }>()
    api.mockResolvedValueOnce(snapshot()).mockReturnValueOnce(lookup.promise)
    const state = workspace(); await flushPromises()
    const signal = api.mock.calls[1]![1]!.signal as AbortSignal
    scopes[0]!.stop()
    expect(signal.aborted).toBe(true)
    lookup.resolve({ tasks: [{ id: 'late', kind: 'chat', request_id: 'late-query', status: 'succeeded', message: '' }] })
    await flushPromises()
    expect(state.pending.value?.request_id).toBe('late-query')
    expect(state.snapshot.value?.tasks).toEqual([])
    expect(api.mock.calls).toHaveLength(2)
  })
  it('pauses hidden-tab polling and resumes on visibility change', async () => {
    api.mockResolvedValue(snapshot())
    workspace(); await flushPromises()
    Object.defineProperty(document, 'hidden', { configurable: true, value: true })
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(10000)
    expect(api).toHaveBeenCalledTimes(1)
    Object.defineProperty(document, 'hidden', { configurable: true, value: false })
    document.dispatchEvent(new Event('visibilitychange')); await flushPromises()
    expect(api).toHaveBeenCalledTimes(2)
  })
})


describe('follow-up queue', () => {
  it.each([null, {}, { ...snapshot().queue, revision: -1 }, { ...snapshot().queue, chat_generation: '1' }, { ...snapshot().queue, waiting_tasks: [{ id: 'w', status: 'running', body: 'wrong' }] }, { ...snapshot().queue, draining: undefined }])('rejects malformed queue %j', async queue => {
    api.mockResolvedValueOnce({ ...snapshot(), queue })
    const state = workspace(); await flushPromises()
    expect(state.snapshot.value).toBeNull(); expect(state.canSend.value).toBe(false)
    expect(state.error.value).toContain('前后端版本不一致')
  })
  it('keeps delivery uncertain and blocks a second send until exact acknowledgement', async () => {
    api.mockResolvedValueOnce(snapshot())
    const state = workspace(); await flushPromises()
    api.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(snapshot()).mockResolvedValueOnce({ tasks: [] }).mockRejectedValueOnce(new Error('offline'))
    await state.send('B')
    expect(state.canSend.value).toBe(false)
    expect(await state.send('C')).toBe(false)
    const posts = api.mock.calls.filter(([, options]) => options?.method === 'POST')
    expect(posts).toHaveLength(2)
    expect(JSON.parse(String(posts[0]![1]?.body))).toEqual(JSON.parse(String(posts[1]![1]?.body)))
  })
  it('retains rejected capacity text for draft recovery without creating another request', async () => {
    api.mockResolvedValueOnce(snapshot())
    const state = workspace(); await flushPromises()
    api.mockRejectedValueOnce(new ApiError(409, 'queue_full', '最多等待50条消息')).mockResolvedValueOnce(snapshot()).mockResolvedValueOnce({ tasks: [] })
    expect(await state.send('保留这条补充')).toBe(false)
    expect(state.pending.value?.failed).toBe(true)
    expect(state.dismissPending()).toBe('保留这条补充')
    expect(api.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1)
  })
  it('stops current A rather than pending follow-up B', async () => {
    const current = { id: 'a', request_id: 'a-request', status: 'running', message: '' }
    const value = snapshot(); value.queue.current_task = current
    api.mockResolvedValueOnce(value)
    const state = workspace(); await flushPromises()
    const ack = deferred<unknown>(); let id = ''
    api.mockImplementationOnce((_path, options) => { id = JSON.parse(String(options?.body)).request_id; return ack.promise })
    const send = state.send('B'); const stop = state.cancel()
    api.mockResolvedValueOnce({ task: { ...current, status: 'cancelled' } })
    ack.resolve({ task: { id: 'b', request_id: id, status: 'waiting', message: '' } })
    expect(await stop).toBe(true)
    api.mockResolvedValueOnce(snapshot()); await send
    expect(api.mock.calls.filter(([path]) => path.endsWith('/cancel')).map(([path]) => path)).toEqual(['/api/trips/trip-test/tasks/a/cancel'])
  })
  it('requires an initial valid queue before sending', async () => {
    const first = deferred<unknown>(); api.mockReturnValueOnce(first.promise)
    const state = workspace()
    expect(await state.send('尚未读取')).toBe(false)
    first.resolve({ ...snapshot(), queue: undefined }); await flushPromises()
    expect(state.snapshot.value).toBeNull()
    expect(state.error.value).toContain('前后端版本不一致')
    expect(await state.send('不能发送')).toBe(false)
    expect(api.mock.calls).toHaveLength(1)
  })
  it('releases the delivery slot on ACK even without messages and sends follow-ups while running', async () => {
    const current = { id: 'a', status: 'running', message: '' }
    const value = snapshot(); value.queue.current_task = current; value.queue.chat_generation = 4
    api.mockResolvedValueOnce(value)
    const state = workspace(); await flushPromises()
    expect(state.canSend.value).toBe(true)
    api.mockImplementationOnce(async (_path, options) => {
      const payload = JSON.parse(String(options?.body)); expect(payload.chat_generation).toBe(4)
      return { task: { id: 'b', request_id: payload.request_id, status: 'waiting', message: '' } }
    }).mockResolvedValueOnce(value)
    await state.send('B')
    expect(state.pending.value).toBeNull()
    expect(state.canSend.value).toBe(true)
    expect(state.currentStop.value?.id).toBe('a')
  })
  it('retains all waiting tasks, cancels the specified item, and never stops waiting as current', async () => {
    const value = snapshot(); value.queue.paused = true
    value.queue.waiting_tasks = Array.from({ length: 25 }, (_, i) => ({ id: `w${i}`, status: 'waiting', body: `消息${i}`, message: '' }))
    api.mockResolvedValueOnce(value)
    const state = workspace(); await flushPromises()
    expect(state.waiting.value).toHaveLength(25)
    expect(state.busy.value).toBe(false)
    expect(state.currentStop.value).toBeNull()
    api.mockResolvedValueOnce({ task: { ...value.queue.waiting_tasks[24], status: 'cancelled' } })
    const updated = snapshot(); updated.queue = { ...value.queue, revision: 1, waiting_tasks: value.queue.waiting_tasks.slice(0, 24) }
    api.mockResolvedValueOnce(updated)
    expect(await state.cancelWaiting('w24')).toBe(true)
    expect(api.mock.calls.some(([path]) => path.endsWith('/tasks/w24/cancel'))).toBe(true)
  })
  it('uses explicit resume CAS once; conflicts refresh but never automatically retry', async () => {
    const value = snapshot(); Object.assign(value.queue, { paused: true, pause_reason: 'failed', revision: 7, chat_generation: 3 })
    api.mockResolvedValueOnce(value)
    const state = workspace(); await flushPromises()
    api.mockRejectedValueOnce(new ApiError(409, 'conflict', '队列已变化')).mockResolvedValueOnce(snapshot({ queue: { ...value.queue, revision: 8 } }))
    expect(await state.resumeQueue()).toBe(false)
    const calls = api.mock.calls.filter(([path]) => path.endsWith('/queue/resume'))
    expect(calls).toHaveLength(1)
    expect(JSON.parse(String(calls[0]![1]?.body))).toEqual({ expected_revision: 7, chat_generation: 3 })
  })
  it('never upgrades a legacy pending generation, and checks exact ACK history first', async () => {
    sessionStorage.setItem('travelassistant:pending:user-test:trip-test', JSON.stringify({ kind: 'chat', request_id: 'legacy', payload: { body: '旧消息' } }))
    const value = snapshot(); value.queue.chat_generation = 2
    api.mockResolvedValueOnce(value).mockResolvedValueOnce({ tasks: [] })
    const state = workspace(); await flushPromises()
    expect(api.mock.calls[1]?.[0]).toContain('/tasks?request_id=legacy')
    expect(api.mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
    expect(state.pending.value?.failed).toBe(true)
    expect(state.feedback.value).toContain('草稿')
  })
  it('rejects snapshots older than the published queue generation/revision', async () => {
    const value = snapshot(); value.queue.revision = 9
    api.mockResolvedValueOnce(value)
    const state = workspace(); await flushPromises()
    const stale = snapshot(); stale.queue.revision = 8; stale.queue.paused = true
    api.mockResolvedValueOnce(stale); await state.refresh()
    expect(state.snapshot.value?.queue.revision).toBe(9)
    expect(state.snapshot.value?.queue.paused).toBe(false)
  })
})

describe('Agent undo recovery', () => {
  it('reuses the request id after an uncertain response and never reports failure as success', async () => {
    const original = snapshot()
    original.undo = { available_count: 2, latest_entry_id: 'entry-a', message_id: 'reply-a', version: 4, can_undo: true, reason: '' }
    api.mockResolvedValueOnce(original)
    const state = workspace(); await flushPromises()
    api.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(original)
    expect(await state.undoLast()).toBe(false)
    expect(state.feedback.value).toContain('尚未确认')
    const first = JSON.parse(String(api.mock.calls.find(c => c[0].endsWith('/undo'))![1]?.body))
    expect(state.undoUncertain.value).toBe(true)
    api.mockResolvedValueOnce({ receipt: { undone_entry_id: 'entry-a' } }).mockResolvedValueOnce(original)
    expect(await state.undoLast()).toBe(true)
    const posts = api.mock.calls.filter(c => c[0].endsWith('/undo'))
    expect(JSON.parse(String(posts[1]![1]?.body))).toEqual(first)
    expect(state.undoUncertain.value).toBe(false)
  })
  it('recovers a pending request across remount and refreshes conflicts explicitly', async () => {
    sessionStorage.setItem('travelassistant:pending:user-test:trip-test:undo', JSON.stringify({ entry_id: 'old-entry', expected_version: 3, request_id: 'stable' }))
    api.mockResolvedValueOnce(snapshot())
    const state = workspace(); await flushPromises()
    expect(state.undoUncertain.value).toBe(true)
    api.mockRejectedValueOnce(new ApiError(409, 'conflict', '旅行已变化')).mockResolvedValueOnce(snapshot())
    expect(await state.undoLast()).toBe(false)
    expect(state.feedback.value).toBe('旅行已变化')
    expect(state.undoUncertain.value).toBe(false)
    expect(JSON.parse(String(api.mock.calls.find(c => c[0].endsWith('/undo'))![1]?.body)).request_id).toBe('stable')
  })
})
