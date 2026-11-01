import { computed, onScopeDispose, ref } from 'vue'
import { apiRequest, ApiError } from '@/api/client'
import type { ChatMessage, Task, WorkspaceSnapshot } from '@/types'

export type { ProgressStep, Progress, ChatMessage, Idea, LegacyRoute, LegacyItem, Plan, DisplayState, Task, WorkspaceSnapshot } from '@/types'
interface Pending { kind: 'chat' | 'plan'; request_id: string; payload: Record<string, unknown>; body?: string; created_at: string; failed?: boolean; retry_of?: string }
export const requestId = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`
const activeTask = (task: Task) => typeof task.status === 'string' && ['queued', 'running'].includes(task.status)
export function useWorkspace(tripId: string, userId: string, onUnauthorized?: () => void) {
  const snapshot = ref<WorkspaceSnapshot | null>(null)
  const loading = ref(true), error = ref(''), feedback = ref(''), submitting = ref(false)
  const pending = ref<Pending | null>(null)
  const authRequired = ref(false)
  let paused = false
  const root = `/api/trips/${encodeURIComponent(tripId)}`
  const storageKey = `travelassistant:pending:${encodeURIComponent(userId)}:${encodeURIComponent(tripId)}`
  let stopped = false, timer: ReturnType<typeof setTimeout> | undefined, refreshing: Promise<void> | null = null, epoch = 0
  const controllers = new Set<AbortController>()
  try { const saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null'); if (saved && ['chat', 'plan'].includes(saved.kind) && typeof saved.request_id === 'string' && typeof saved.payload === 'object') pending.value = saved } catch { /* Storage is optional. */ }
  function remember(value: Pending | null) { pending.value = value; try { if (value) sessionStorage.setItem(storageKey, JSON.stringify(value)); else sessionStorage.removeItem(storageKey) } catch { /* In-memory recovery still works. */ } }
  async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
    const controller = new AbortController(); controllers.add(controller)
    const timeout = setTimeout(() => controller.abort(), 15000)
    try { return await apiRequest<T>(root + path, { ...options, signal: controller.signal }) }
    catch (e) {
      if (!stopped && e instanceof ApiError && e.status === 401) {
        paused = true; authRequired.value = true; epoch++; clearTimeout(timer)
        onUnauthorized?.()
      }
      throw e
    } finally { clearTimeout(timeout); controllers.delete(controller) }
  }
  const busy = computed(() => submitting.value || !!pending.value && !pending.value.failed || !!snapshot.value?.display_state.busy || !!snapshot.value?.tasks.some(activeTask))
  const latestTask = computed(() => snapshot.value?.tasks.find(activeTask) ?? snapshot.value?.tasks.at(-1))
  const messages = computed(() => {
    const result = [...(snapshot.value?.messages ?? [])]
    const item = pending.value
    if (item?.kind === 'chat') {
      const task = snapshot.value?.tasks.find(t => t.request_id === item.request_id)
      if (!task?.message_id || !result.some(m => m.id === task.message_id)) result.push({ id: `pending-${item.request_id}`, sender_type: 'user', body: item.body || '', created_at: item.created_at, status: item.failed ? 'send_failed' : 'sending' })
    }
    return result
  })
  function schedule() { clearTimeout(timer); if (!stopped && !paused && !document.hidden) timer = setTimeout(() => void refresh(), busy.value || snapshot.value?.display_state.display_status === 'pending' ? 1300 : 3500) }
  function messageFor(e: unknown) { return e instanceof ApiError ? e.message : '连接暂时中断，已有内容和输入已保留。请稍后重试。' }
  function reconcile() {
    const item = pending.value; if (!item) return
    const task = snapshot.value?.tasks.find(t => t.request_id === item.request_id)
    // A retained task is the durable acknowledgement, even if another tab has
    // already cleared its messages. Never replay an acknowledged chat request.
    if (task) { remember(null); feedback.value = '' }
  }
  async function postPending() {
    const item = pending.value; if (!item || item.failed || submitting.value || stopped || paused) return
    submitting.value = true; epoch++
    try {
      const path = item.retry_of ? `/tasks/${encodeURIComponent(item.retry_of)}/retry` : item.kind === 'chat' ? '/messages' : '/tasks'
      const body = item.retry_of ? { request_id: item.request_id } : item.kind === 'chat' ? { ...item.payload, request_id: item.request_id } : { kind: item.kind, payload: item.payload, request_id: item.request_id }
      const result = await request<{ task: Task; messages?: ChatMessage[] }>(path, { method: 'POST', body: JSON.stringify(body) })
      if (stopped) return
      if (!result.task?.id || result.task.request_id !== item.request_id) throw new Error('Unknown acknowledgement')
      if (snapshot.value) {
        snapshot.value.tasks = [...snapshot.value.tasks.filter(t => t.id !== result.task.id), result.task]
        for (const message of result.messages ?? []) { const i = snapshot.value.messages.findIndex(m => m.id === message.id); if (i < 0) snapshot.value.messages.push(message); else snapshot.value.messages[i] = message }
      }
      // Keep the optimistic text until its server message is present in a snapshot.
      if (item.kind !== 'chat' || result.messages?.some(m => m.id === result.task.message_id)) remember(null)
      feedback.value = ''
    } catch (e) {
      if (stopped) return
      if (authRequired.value) { feedback.value = messageFor(e); return }
      if (e instanceof ApiError && e.status >= 400 && e.status < 500 && ![408, 429].includes(e.status)) { remember({ ...item, failed: true }); feedback.value = messageFor(e) }
      else feedback.value = '连接中断，正在使用同一请求标识确认是否送达，请勿重复发送。'
    } finally { submitting.value = false }
  }
  async function refresh() {
    if (stopped || document.hidden || authRequired.value) return
    if (refreshing) return refreshing
    paused = false
    refreshing = (async () => {
      const currentEpoch = epoch
      try {
        const data = await request<WorkspaceSnapshot>('/workspace', { cache: 'no-store' })
        if (stopped || epoch !== currentEpoch) return
        snapshot.value = data; error.value = ''; reconcile()
        if (pending.value && !pending.value.failed && !submitting.value) await postPending()
      } catch (e) { if (!stopped) { error.value = messageFor(e); if (e instanceof ApiError && [401, 403, 404].includes(e.status)) paused = true } }
      finally { loading.value = false }
    })()
    try { await refreshing } finally { refreshing = null; schedule() }
  }
  async function submit(kind: 'chat' | 'plan', payload: Record<string, unknown>, body?: string) {
    if (submitting.value || (kind !== 'chat' && busy.value) || pending.value || stopped || paused) return false
    remember({ kind, request_id: requestId(), payload, body, created_at: new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date()) })
    await postPending(); await refresh(); return true
  }
  function binding() { const state = snapshot.value!.display_state; return { expected_plan_id: state.plan_id, source_hash: state.source_hash, expected_requirements_revision: state.requirements_revision } }
  async function action(path: string, body?: unknown, method = 'POST') {
    if (submitting.value || stopped || paused) return false
    submitting.value = true; epoch++; feedback.value = ''
    try { await request(path, { method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) }); return true }
    catch (e) { if (!stopped) feedback.value = messageFor(e); return false }
    finally { submitting.value = false; await refresh() }
  }
  async function retryPending() { if (!pending.value || submitting.value) return; remember({ ...pending.value, failed: false }); await postPending(); await refresh() }
  async function retryTask(task: Task) {
    if (busy.value || pending.value || stopped || paused) return
    remember({ kind: 'plan', retry_of: task.id, request_id: requestId(), payload: {}, created_at: '' })
    await postPending(); await refresh()
  }
  function visibility() { clearTimeout(timer); if (!document.hidden) void refresh() }
  document.addEventListener('visibilitychange', visibility)
  void refresh()
  onScopeDispose(() => { stopped = true; epoch++; clearTimeout(timer); controllers.forEach(c => c.abort()); document.removeEventListener('visibilitychange', visibility) })
  function dismissPending() { const body = pending.value?.body; if (pending.value?.failed) { remember(null); feedback.value = '' }; return body }
  return { snapshot, messages, pending, loading, error, feedback, busy, submitting, latestTask, authRequired, refresh, retryPending, retryTask, dismissPending,
    send: (body: string) => submit('chat', { body }, body), generate: () => submit('plan', binding()),
    confirm: () => action('/plan/confirm', binding()), regenerate: () => action('/display', { plan_id: snapshot.value!.display_state.plan_id, source_hash: snapshot.value!.display_state.source_hash }),
    addIdea: (body: string) => action('/ideas', { body }), deleteIdea: (id: string) => action(`/ideas/${encodeURIComponent(id)}`, undefined, 'DELETE'), clearMessages: () => action('/messages', undefined, 'DELETE') }
}
export type WorkspaceStore = ReturnType<typeof useWorkspace>
