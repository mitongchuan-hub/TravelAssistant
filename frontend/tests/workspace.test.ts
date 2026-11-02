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
  it('does not overwrite a newer mutation with an in-flight stale poll', async () => {
    const original = snapshot(); api.mockResolvedValueOnce(original)
    const state = workspace(); await flushPromises()
    const stale = deferred<ReturnType<typeof snapshot>>()
    api.mockReturnValueOnce(stale.promise)
    const refresh = state.refresh()
    api.mockResolvedValueOnce({})
    const action = state.addIdea('夜骑')
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
  it('persists uncertain sends only within the current user and trip, and retries with the same identifier', async () => {
    api.mockResolvedValueOnce(snapshot())
    const state = workspace(); await flushPromises()
    api.mockRejectedValueOnce(new ApiError(0, 'network_error', 'offline'))
    api.mockResolvedValueOnce(snapshot())
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
    ack.reject(new ApiError(0, 'network_error', 'offline'))
    expect(await cancel).toBe(false)
    await send
    expect(state.feedback.value).toContain('终止失败')
    expect(state.pending.value?.cancel_requested).toBe(true)
    api.mockResolvedValueOnce(snapshot())
    await state.refresh()
    expect(api.mock.calls.filter(call => call[1]?.method === 'POST')).toHaveLength(1)
    expect(sessionStorage.getItem('travelassistant:pending:user-test:trip-test')).toContain('cancel_requested')
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
