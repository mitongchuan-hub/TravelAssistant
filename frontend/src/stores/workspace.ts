import { computed, onScopeDispose, ref } from 'vue'
import { apiRequest, ApiError } from '@/api/client'
import type { ChatMessage, Task, QueueState, WorkspaceSnapshot } from '@/types'

export type { ProgressStep, Progress, ChatMessage, LegacyRoute, LegacyItem, Plan, DisplayState, Task, WorkspaceSnapshot } from '@/types'
interface Pending { kind: 'chat'; request_id: string; payload: Record<string, unknown>; body?: string; created_at: string; failed?: boolean; cancel_requested?: boolean }
export const requestId = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`
const activeTask = (task: Task) => typeof task.status === 'string' && ['queued', 'running'].includes(task.status)
class WorkspaceContractError extends Error {
  constructor() { super('前后端版本不一致，请重启后端后重新读取。') }
}
export function useWorkspace(tripId: string, userId: string, onUnauthorized?: () => void) {
  const snapshot = ref<WorkspaceSnapshot | null>(null)
  const loading = ref(true), error = ref(''), feedback = ref(''), submitting = ref(false)
  const pending = ref<Pending | null>(null)
  const authRequired = ref(false), cancelling = ref(false)
  let postFlight: Promise<void> | null = null
  const contractReady = ref(false)
  const resuming = ref(false), cancellingWaiting = ref<string | null>(null)
  const currentStop = computed(() => snapshot.value?.queue.current_task ?? null)
  const waiting = computed(() => snapshot.value?.queue.waiting_tasks ?? [])
  const canCancel = computed(() => cancelling.value || !!currentStop.value || !!pending.value && !pending.value.failed)
  const canSend = computed(() => contractReady.value && !!snapshot.value && !authRequired.value && !undoing.value && !undoUncertain.value && !submitting.value && !pending.value && !cancelling.value && !resuming.value)
  let paused = false
  const root = `/api/trips/${encodeURIComponent(tripId)}`
  const storageKey = `travelassistant:pending:${encodeURIComponent(userId)}:${encodeURIComponent(tripId)}`
  const undoing = ref(false)
  const undoKey = storageKey + ':undo'
  let pendingUndo: { entry_id: string; expected_version: number; request_id: string } | null = null
  try { const saved = JSON.parse(sessionStorage.getItem(undoKey) || 'null'); if (saved && typeof saved.entry_id === 'string' && typeof saved.request_id === 'string' && Number.isInteger(saved.expected_version)) pendingUndo = saved } catch { /* Optional storage. */ }
  if (pendingUndo) feedback.value = '上次撤销结果待确认，请确认撤销结果；将复用同一请求标识。'
  const undoUncertain = ref(!!pendingUndo)
  function rememberUndo() { undoUncertain.value = !!pendingUndo; try { if (pendingUndo) sessionStorage.setItem(undoKey, JSON.stringify(pendingUndo)); else sessionStorage.removeItem(undoKey) } catch { /* In-memory recovery. */ } }
  let stopped = false, timer: ReturnType<typeof setTimeout> | undefined, refreshing: Promise<void> | null = null, epoch = 0
  const controllers = new Set<AbortController>()
  try { const saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null'); if (saved && saved.kind === 'chat' && !('retry_of' in saved) && typeof saved.request_id === 'string' && saved.payload && typeof saved.payload === 'object') pending.value = saved; else if (saved) sessionStorage.removeItem(storageKey) } catch { /* Storage is optional. */ }
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
  const busy = computed(() => undoing.value || !!pendingUndo || submitting.value || !!pending.value && !pending.value.failed || !!snapshot.value?.display_state.busy || !!currentStop.value || !!snapshot.value?.queue.draining || !!waiting.value.length && !snapshot.value?.queue.paused)
  const latestTask = computed(() => currentStop.value ?? snapshot.value?.tasks.at(-1))
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
  function messageFor(e: unknown) { return e instanceof ApiError || e instanceof WorkspaceContractError ? e.message : '连接暂时中断，已有内容和输入已保留。请稍后重试。' }
  function validQueue(value: unknown): value is QueueState {
    const q = value as QueueState | null | undefined
    const task = (t: Task) => !!t && typeof t === 'object' && typeof t.id === 'string' && !!t.id && typeof t.status === 'string'
    return !!q && typeof q === 'object'
      && (q.current_task === null || task(q.current_task) && activeTask(q.current_task))
      && Array.isArray(q.waiting_tasks)
      && q.waiting_tasks.every(t => task(t) && t.status === 'waiting' && typeof t.body === 'string')
      && new Set(q.waiting_tasks.map(t => t.id)).size === q.waiting_tasks.length
      && !q.waiting_tasks.some(t => t.id === q.current_task?.id)
      && typeof q.paused === 'boolean' && typeof q.pause_reason === 'string' && typeof q.draining === 'boolean'
      && [q.revision, q.chat_generation].every(n => Number.isSafeInteger(n) && n >= 0)
  }
  function olderQueue(q: QueueState) {
    const current = snapshot.value?.queue
    return !!current && (q.chat_generation < current.chat_generation || q.chat_generation === current.chat_generation && q.revision < current.revision)
  }
  function acceptWorkspace(data: unknown): WorkspaceSnapshot {
    // Check the new requirements contract before publishing to any render consumer.
    // Never turn legacy cards or malformed text into an editable empty body.
    const requirements = (data as WorkspaceSnapshot | null | undefined)?.trip?.requirements
    if (typeof requirements?.text !== 'string' || !Number.isSafeInteger(requirements.revision) || requirements.revision < 0 || !validQueue((data as WorkspaceSnapshot | null)?.queue)) {
      const mismatch = new WorkspaceContractError()
      contractReady.value = false; paused = true; error.value = mismatch.message
      throw mismatch
    }
    if (olderQueue((data as WorkspaceSnapshot).queue)) return snapshot.value!
    snapshot.value = data as WorkspaceSnapshot
    contractReady.value = true
    error.value = ''
    return snapshot.value
  }
  async function findPendingTask(item: Pending): Promise<Task | undefined> {
    const task = snapshot.value?.tasks.find(t => t.request_id === item.request_id)
    if (task) return task
    // Workspace only contains recent tasks; absence there is not proof that a
    // request was never accepted. Query durable history before any replay.
    const result = await request<{ tasks: Task[] }>(`/tasks?request_id=${encodeURIComponent(item.request_id)}`, { cache: 'no-store' })
    return result.tasks.find(t => t.request_id === item.request_id)
  }
  async function reconcile(currentEpoch: number) {
    const item = pending.value; if (!item) return
    const task = await findPendingTask(item)
    if (stopped || epoch !== currentEpoch || pending.value?.request_id !== item.request_id) return
    // A retained task is the durable acknowledgement, even if another tab has
    // already cleared its messages. Never replay an acknowledged chat request.
    if (task && !item.cancel_requested) {
      if (snapshot.value && !snapshot.value.tasks.some(t => t.id === task.id)) snapshot.value.tasks.push(task)
      remember(null); feedback.value = ''
    } else if (!task && !item.cancel_requested && !item.failed && (item.payload.chat_generation === undefined || item.payload.chat_generation !== snapshot.value?.queue.chat_generation)) {
      // A generation-less legacy pending is not new authorization in today's chat.
      // Look for its durable ACK first, but never stamp it with the current generation.
      remember({ ...item, failed: true }); feedback.value = '这条旧消息尚未确认送达，聊天记录已变化或缺少代数。请移回草稿后重新发送。'
    }
  }
  async function postPending() {
    if (postFlight) return postFlight
    postFlight = performPostPending()
    try { await postFlight } finally { postFlight = null }
  }
  async function performPostPending() {
    const item = pending.value; if (!item || item.failed || item.cancel_requested || submitting.value || stopped || paused || cancelling.value) return
    submitting.value = true; epoch++
    try {
      const body = { ...item.payload, request_id: item.request_id }
      const result = await request<{ task: Task; messages?: ChatMessage[] }>('/messages', { method: 'POST', body: JSON.stringify(body) })
      if (stopped) return
      if (!result.task?.id || result.task.request_id !== item.request_id) throw new Error('Unknown acknowledgement')
      if (snapshot.value && item.payload.chat_generation === snapshot.value.queue.chat_generation) {
        snapshot.value.tasks = [...snapshot.value.tasks.filter(t => t.id !== result.task.id), result.task]
        for (const message of result.messages ?? []) { const i = snapshot.value.messages.findIndex(m => m.id === message.id); if (i < 0) snapshot.value.messages.push(message); else snapshot.value.messages[i] = message }
      }
      // The slot tracks HTTP delivery, not execution or message projection.
      if (!pending.value?.cancel_requested) {
        remember(null)
        feedback.value = ''
      }
    } catch (e) {
      if (stopped) return
      if (authRequired.value) { feedback.value = messageFor(e); return }
      if (e instanceof ApiError && e.status >= 400 && e.status < 500 && ![408, 429].includes(e.status)) { remember({ ...item, failed: true }); feedback.value = messageFor(e) }
      else feedback.value = '连接中断，正在使用同一请求标识确认是否送达，请勿重复发送。'
    } finally { submitting.value = false }
  }
  async function cancel() {
    if (cancelling.value || stopped || authRequired.value) return false
    const stoppingCurrent = currentStop.value
    const stoppingCurrentId = stoppingCurrent?.id
    if (pending.value && !stoppingCurrentId) remember({ ...pending.value, cancel_requested: true })
    cancelling.value = true; epoch++; clearTimeout(timer)
    feedback.value = '正在终止，请稍候…'
    try {
      // Do not abort an uncertain submission: a browser abort is not a server cancellation.
      if (postFlight) await postFlight
      if (stopped || authRequired.value) return false
      const currentEpoch = epoch
      let task = stoppingCurrentId ? stoppingCurrent : pending.value ? snapshot.value?.tasks.find(t => t.request_id === pending.value!.request_id) : currentStop.value
      if (!task) {
        const data = await request<unknown>('/workspace', { cache: 'no-store' })
        if (stopped || epoch !== currentEpoch) return false
        const accepted = acceptWorkspace(data)
        task = stoppingCurrentId ? accepted.queue.current_task?.id === stoppingCurrentId ? accepted.queue.current_task : undefined : pending.value ? await findPendingTask(pending.value) : accepted.queue.current_task ?? undefined
      }
      if (stopped || epoch !== currentEpoch) return false
      if (!task?.id) throw new Error('尚无法确认请求是否送达，请重新连接后再次终止。')
      const result = await request<{ task: Task }>(`/tasks/${encodeURIComponent(task.id)}/cancel`, { method: 'POST', body: '{}' })
      if (stopped || epoch !== currentEpoch) return false
      if (result.task?.id !== task.id || activeTask(result.task) || result.task.status === 'waiting') throw new Error('服务未确认终止，请重试。')
      epoch++
      if (snapshot.value) {
        snapshot.value.tasks = [...snapshot.value.tasks.filter(t => t.id !== result.task.id), result.task]
        if (snapshot.value.queue.current_task?.id === result.task.id) snapshot.value.queue.current_task = null
      }
      if (!stoppingCurrentId || pending.value?.request_id === result.task.request_id) remember(null)
      feedback.value = ['superseded', 'cancelled'].includes(result.task.status ?? '') ? '已终止，消息、需求和行程已保留。' : '本次处理已结束，已有内容已保留。'
      return true
    } catch (e) {
      if (!stopped) feedback.value = `终止失败，未确认后台任务已停止。${e instanceof Error && !(e instanceof ApiError) ? e.message : messageFor(e)}`
      return false
    } finally { cancelling.value = false; schedule() }
  }
  async function refresh() {
    if (stopped || document.hidden || authRequired.value || cancelling.value) return
    if (refreshing) return refreshing
    paused = false
    refreshing = (async () => {
      const currentEpoch = epoch
      try {
        const data = await request<unknown>('/workspace', { cache: 'no-store' })
        if (stopped || epoch !== currentEpoch) return
        acceptWorkspace(data); await reconcile(currentEpoch)
        if (stopped || epoch !== currentEpoch) return
        if (pending.value && !pending.value.failed && !submitting.value) await postPending()
      } catch (e) { if (!stopped && epoch === currentEpoch) { error.value = messageFor(e); if (e instanceof ApiError && [401, 403, 404].includes(e.status)) paused = true } }
      finally { loading.value = false }
    })()
    try { await refreshing } finally { refreshing = null; schedule() }
  }
  async function undoLast() {
    if (undoing.value || submitting.value || pending.value || stopped || paused) return false
    if (!pendingUndo) {
      const value = snapshot.value?.undo
      if (!value?.can_undo) return false
      pendingUndo = { entry_id: value.latest_entry_id, expected_version: value.version, request_id: requestId() }
      rememberUndo()
    }
    undoing.value = true; epoch++
    try {
      const result = await request<{ receipt: { undone_entry_id: string } }>('/undo', { method: 'POST', body: JSON.stringify(pendingUndo) })
      if (result.receipt?.undone_entry_id !== pendingUndo.entry_id) throw new Error('Unknown undo acknowledgement')
      pendingUndo = null; rememberUndo()
      feedback.value = '已撤销本次修改，聊天记录已保留。'
      return true
    } catch (e) {
      if (e instanceof ApiError && [400, 403, 404, 409, 422].includes(e.status)) { pendingUndo = null; rememberUndo(); feedback.value = messageFor(e) }
      else feedback.value = '尚未确认撤销结果，请重试撤销；将使用同一请求标识，不会多退一步。'
      return false
    } finally { undoing.value = false; await refresh() }
  }
  async function submit(kind: 'chat', payload: Record<string, unknown>, body?: string) {
    if (!canSend.value || stopped || paused) return false
    remember({ kind, request_id: requestId(), payload: { ...payload, chat_generation: snapshot.value!.queue.chat_generation }, body, created_at: new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date()) })
    await postPending(); await refresh(); return !pending.value?.failed
  }
  function binding() { const state = snapshot.value!.display_state; return { expected_plan_id: state.plan_id, source_hash: state.source_hash, expected_requirements_revision: state.requirements_revision } }
  async function action(path: string, body?: unknown, method = 'POST') {
    if (undoing.value || pendingUndo || cancelling.value || submitting.value || stopped || paused) return false
    submitting.value = true; epoch++; feedback.value = ''
    try { await request(path, { method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) }); return true }
    catch (e) { if (!stopped) feedback.value = messageFor(e); return false }
    finally { submitting.value = false; await refresh() }
  }
  async function cancelWaiting(id: string) {
    if (!contractReady.value || stopped || authRequired.value || cancellingWaiting.value || !waiting.value.some(t => t.id === id)) return false
    cancellingWaiting.value = id; epoch++
    try {
      const result = await request<{ task: Task }>(`/tasks/${encodeURIComponent(id)}/cancel`, { method: 'POST', body: '{}' })
      if (stopped) return false
      if (result.task?.id !== id || ['waiting', 'queued', 'running'].includes(result.task.status ?? '')) throw new Error('Unknown cancellation acknowledgement')
      feedback.value = '已取消这条等待消息。'; return true
    } catch (e) { if (!stopped) feedback.value = `取消等待消息失败。${messageFor(e)}`; return false }
    finally { cancellingWaiting.value = null; await refresh() }
  }
  async function resumeQueue() {
    const q = snapshot.value?.queue
    if (!q?.paused || !contractReady.value || stopped || authRequired.value || resuming.value || pending.value || submitting.value || cancelling.value) return false
    const captured = { expected_revision: q.revision, chat_generation: q.chat_generation }
    resuming.value = true; epoch++
    try {
      const result = await request<unknown>('/queue/resume', { method: 'POST', body: JSON.stringify(captured) })
      if (stopped) return false
      if (!validQueue(result)) { contractReady.value = false; throw new WorkspaceContractError() }
      if (snapshot.value && !olderQueue(result)) snapshot.value.queue = result
      feedback.value = '已继续，将基于当前保存内容依次处理。'; return true
    } catch (e) { if (!stopped) feedback.value = `${messageFor(e)} 请核对最新状态后再点击继续。`; return false }
    finally { resuming.value = false; await refresh() }
  }
  function visibility() { clearTimeout(timer); if (!document.hidden) void refresh() }
  document.addEventListener('visibilitychange', visibility)
  void refresh()
  onScopeDispose(() => { stopped = true; epoch++; clearTimeout(timer); controllers.forEach(c => c.abort()); document.removeEventListener('visibilitychange', visibility) })
  function dismissPending() { const body = pending.value?.body; if (pending.value?.failed) { remember(null); feedback.value = '' }; return body }
  return { canSend, currentStop, waiting, cancelWaiting, cancellingWaiting, resumeQueue, resuming, undoLast, undoing, undoUncertain, snapshot, messages, pending, loading, error, feedback, busy, submitting, latestTask, authRequired, cancelling, canCancel, cancel, refresh, dismissPending,
    send: (body: string) => submit('chat', { body }, body),
    confirm: () => action('/plan/confirm', binding()), regenerate: () => action('/display', { plan_id: snapshot.value!.display_state.plan_id, source_hash: snapshot.value!.display_state.source_hash }),
    saveRequirements: (text: string, expected_revision: number) => action('/requirements-text', { text, expected_revision }, 'PUT'), clearMessages: () => action('/messages', undefined, 'DELETE') }
}
export type WorkspaceStore = ReturnType<typeof useWorkspace>
