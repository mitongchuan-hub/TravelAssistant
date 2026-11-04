<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useWorkspace } from '@/stores/workspace'
import ChatMessage from '@/components/ChatMessage.vue'
import IdeaBoard from '@/components/IdeaBoard.vue'
import GuidePreview from '@/components/GuidePreview.vue'
const route = useRoute(), router = useRouter(), auth = useAuthStore()
const tripId = String(route.params.id), userId = auth.user!.id
const workspace = useWorkspace(tripId, userId, () => { saveDraft(); void router.replace({ name: 'auth' }) })
const { snapshot, messages, loading, error, feedback, busy, submitting, latestTask, pending, canCancel, canSend, waiting, cancellingWaiting, resuming, cancelling, undoing, undoUncertain } = workspace
const pauseReason = computed(() => {
  const reason = snapshot.value?.queue.pause_reason ?? ''
  const reasons: Record<string, string> = { failed: '上一轮处理失败', cancelled: '上一轮已手动终止', restart: '服务重启，未继续执行', manual_write: '已手动修改保存内容', conflict: '保存内容发生变化' }
  return reasons[reason] || '处理已暂停，请核对当前保存内容'
})
const board = computed(() => route.query.tab === 'board')
const guideOpen = ref(route.query.tab === 'itinerary' || route.query.guide === '1')
const list = ref<HTMLElement | null>(null), input = ref<HTMLTextAreaElement | null>(null)
const draft = ref(''), draftFeedback = ref(''), composing = ref(false), newReplies = ref(false)
const draftKey = `travelassistant:chat-draft:${encodeURIComponent(userId)}:${encodeURIComponent(tripId)}`
try { draft.value = sessionStorage.getItem(draftKey) || '' } catch { /* Private-mode storage is optional. */ }
if (typeof route.query.draft === 'string' && !draft.value) draft.value = route.query.draft
function saveDraft() { try { sessionStorage.setItem(draftKey, draft.value) } catch { /* Keep draft in memory. */ } }
let frame = 0
function resizeComposer() {
  const el = input.value
  if (!el || board.value || !el.getClientRects().length) return
  // Empty drafts must not inherit placeholder/global textarea measurements.
  if (!draft.value) { el.style.height = '38px'; el.style.overflowY = 'hidden'; return }
  el.style.height = '0px'
  const height = el.scrollHeight
  el.style.height = `${Math.max(38, Math.min(height, 126))}px`
  el.style.overflowY = height > 126 ? 'auto' : 'hidden'
}
watch(draft, saveDraft)
watch([input, draft, board], resizeComposer, { flush: 'post' })
let composerWidth = 0
const composerObserver = new ResizeObserver(entries => {
  const width = entries[0]?.contentRect.width ?? 0
  if (width > 0 && width !== composerWidth) { composerWidth = width; resizeComposer() }
})
watch(input, el => { composerObserver.disconnect(); composerWidth = 0; if (el) composerObserver.observe(el) }, { flush: 'post' })
function toLatest() { if (list.value) list.value.scrollTop = list.value.scrollHeight; newReplies.value = false }
function nearBottom() { return !list.value || list.value.scrollHeight - list.value.scrollTop - list.value.clientHeight < 100 }
interface ScrollAnchor { bottom: boolean; scroll: number; id?: string; offset?: number }
let anchor: ScrollAnchor | null = null, initialScroll = true
function captureAnchor() {
  const el = list.value; if (!el) return
  const top = el.getBoundingClientRect().top
  const node = [...el.querySelectorAll<HTMLElement>('[data-message-id]')].find(n => n.getBoundingClientRect().bottom > top + 1)
  anchor = { bottom: nearBottom(), scroll: el.scrollTop, id: node?.dataset.messageId, offset: node ? node.getBoundingClientRect().top - top : undefined }
}
watch(() => JSON.stringify(messages.value), () => {
  captureAnchor()
  void nextTick(() => {
    const el = list.value; if (!el) return
    if (initialScroll || anchor?.bottom) { toLatest(); initialScroll = false }
    else if (anchor) { el.scrollTop = anchor.scroll; const node = [...el.querySelectorAll<HTMLElement>('[data-message-id]')].find(n => n.dataset.messageId === anchor?.id); if (node && anchor.offset !== undefined) el.scrollTop += node.getBoundingClientRect().top - el.getBoundingClientRect().top - anchor.offset; newReplies.value = true }
  })
}, { flush: 'pre' })
async function send() {
  if (!canSend.value || composing.value || !draft.value.trim()) return
  if (draft.value.trim().length > 10000) { draftFeedback.value = '消息不能超过 10000 字，请缩短后发送。'; return }
  const body = draft.value.trim(); draft.value = ''; draftFeedback.value = ''; toLatest()
  const accepted = await workspace.send(body)
  if (!accepted && !draft.value) draft.value = body
}
function keydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && !composing.value && event.keyCode !== 229 && matchMedia('(pointer: fine)').matches && !matchMedia('(pointer: coarse)').matches) { event.preventDefault(); void send() }
}
function recoverDraft(body: string) { if (draft.value.trim()) draftFeedback.value = '输入框已有草稿，请发送或清空后再重试。'; else draft.value = body; input.value?.focus({ preventScroll: true }) }
function dismissPending() { const body = workspace.dismissPending(); if (body) recoverDraft(body) }
async function terminate() { if (!draft.value && pending.value?.body) draft.value = pending.value.body; saveDraft(); draftFeedback.value = ''; await workspace.cancel(); input.value?.focus({ preventScroll: true }) }
function openGuide() { guideOpen.value = true }
async function tab(value: 'board' | 'chat') { guideOpen.value = false; await router.replace({ query: { tab: value } }) }
const generationWarning = computed(() => {
  const trip = snapshot.value?.trip
  if (!trip) return ''
  const r = trip.requirements
  if (!r.start_date || !r.end_date || !r.day_count || !r.lodging_address || !r.city || !['transit', 'driving'].includes(r.transport_mode) || !trip.lodging.poi_id) return '这条旧旅行缺少完整创建资料，仍可聊天、维护需求正文和查看文稿；不能生成或重排行程，请新建旅行填写完整资料。'
  if (Array.from(r.text).length > 20000) return '需求正文超过 20,000 字，请先整理正文再生成行程。'
  return ''
})
async function generate() { if (generationWarning.value) return; await tab('chat'); await workspace.send('请根据已保存的要求和想法生成同城行程。') }
function viewportChanged() {
  cancelAnimationFrame(frame)
  frame = requestAnimationFrame(() => {
    const shell = document.querySelector<HTMLElement>('.phone-shell--chat'), viewport = window.visualViewport
    if (shell && viewport) { const bottom = nearBottom(); shell.style.setProperty('--chat-viewport-height', `${viewport.height}px`); shell.style.setProperty('--chat-viewport-top', `${viewport.offsetTop}px`); shell.classList.toggle('chat-keyboard-open', document.activeElement === input.value && innerHeight - viewport.height > 120); if (bottom) toLatest() }
  })
}
watch(board, value => { document.body.classList.toggle('chat-page', !value); void nextTick(viewportChanged) }, { immediate: true })
watch(() => route.query.tab, value => { if (value === 'itinerary') guideOpen.value = true })
onMounted(() => { document.body.classList.toggle('chat-page', !board.value); window.addEventListener('resize', viewportChanged); window.visualViewport?.addEventListener('resize', viewportChanged); window.visualViewport?.addEventListener('scroll', viewportChanged); viewportChanged() })
onBeforeUnmount(() => { composerObserver.disconnect(); document.body.classList.remove('chat-page'); window.removeEventListener('resize', viewportChanged); window.visualViewport?.removeEventListener('resize', viewportChanged); window.visualViewport?.removeEventListener('scroll', viewportChanged); cancelAnimationFrame(frame); const shell = document.querySelector<HTMLElement>('.phone-shell'); shell?.style.removeProperty('--chat-viewport-height'); shell?.style.removeProperty('--chat-viewport-top'); shell?.classList.remove('chat-keyboard-open') })
</script>
<template>
  <section :class="['workspace', board ? 'workspace--board' : 'workspace--chat']">
    <div v-if="!snapshot" class="workspace-initial" role="status"><p>{{ loading ? '正在读取旅行记录…' : error || '旅行暂时无法打开。' }}</p><button v-if="!loading" type="button" @click="workspace.refresh">重新读取</button><RouterLink to="/trips">返回我的旅行</RouterLink></div>
    <template v-else>
      <IdeaBoard v-show="board" :text="snapshot.trip.requirements.text" :revision="snapshot.trip.requirements.revision" :destination="snapshot.trip.destination" :city="snapshot.trip.mode === 'city'" :busy="busy" :feedback="feedback || error" :generation-warning="generationWarning" :save-requirements="workspace.saveRequirements" @generate="generate">
        <template #default><nav class="bottom-nav bottom-nav--v2" aria-label="底部导航"><RouterLink class="active" :to="{ query: { tab: 'board' } }" @click.prevent="tab('board')"><span aria-hidden="true">✦</span>想法</RouterLink><RouterLink :to="{ query: { tab: 'chat' } }" @click.prevent="tab('chat')"><span aria-hidden="true">✉</span>聊天</RouterLink></nav></template>
      </IdeaBoard>
      <section v-show="!board" class="travel-tab-screen chat-screen" :inert="guideOpen || undefined">
        <header class="chat-top"><RouterLink class="chat-back" to="/trips" aria-label="返回我的旅行">‹</RouterLink><div class="chat-heading"><h1>{{ snapshot.trip.destination.replace('周末旅行', '') }}</h1><p>{{ snapshot.trip.date_range }}</p></div><button class="guide-open" type="button" aria-controls="guide-preview" :aria-expanded="guideOpen" @click="openGuide">攻略</button></header>
        <section ref="list" class="chat-list" aria-label="与旅行助手的对话" @scroll.passive="nearBottom() && (newReplies = false)">
          <p v-if="generationWarning" class="chat-send-feedback" role="status">{{ generationWarning }}</p>
          <p v-if="error" class="chat-send-feedback" role="status">{{ error }} <button class="chat-message-action" type="button" @click="workspace.refresh">重新连接</button></p>
          <template v-for="(message, index) in messages" :key="message.id"><ChatMessage :message="message" :previous="messages[index - 1]" :busy="busy" @draft="recoverDraft" @recover="dismissPending" @guide="openGuide"/></template>
          <div v-if="!messages.length" class="chat-empty"><span aria-hidden="true">✦</span><h2>一起聊聊这趟旅行</h2><p>想去哪里、怎么玩，或者先说一个小想法。</p></div>
          <p v-if="latestTask?.kind !== 'chat' && latestTask && typeof latestTask.status === 'string' && ['queued', 'running', 'failed'].includes(latestTask.status)" class="chat-send-feedback" role="status">{{ latestTask.status === 'failed' ? latestTask.message || '本次处理未完成，请重新发送消息。已有内容已保留。' : '正在安排你的路线，已有内容仍可查看。' }}</p>
        </section>
        <section class="chat-dock" aria-label="聊天输入区"><button v-if="newReplies" class="chat-new-replies" type="button" @click="toLatest">有新回复 ↓</button><section v-if="waiting.length || snapshot.queue.paused || snapshot.queue.draining" class="chat-queue" aria-label="等待消息队列"><p v-if="snapshot.queue.paused" role="status">队列已暂停：{{ pauseReason }}。新消息只会加入等待。<button class="chat-message-action" type="button" :disabled="resuming || submitting || !!pending || cancelling" @click="workspace.resumeQueue">{{ resuming ? '正在继续…' : '基于当前保存内容继续' }}</button></p><p v-else-if="snapshot.queue.draining" role="status">上一轮正在结束，等待消息会依次处理。</p><p v-else-if="waiting.length" role="status">{{ waiting.length }} 条消息等待上一轮完成</p><ol v-if="waiting.length"><li v-for="task in waiting" :key="task.id"><span :title="task.body">{{ task.body }}</span><button class="chat-message-action" type="button" :aria-label="`取消等待消息：${task.body}`" :disabled="!!cancellingWaiting" @click="workspace.cancelWaiting(task.id!)">{{ cancellingWaiting === task.id ? '取消中…' : '取消' }}</button></li></ol></section><form :class="['composer', { 'composer--followup': canCancel && !!draft.trim() }]" :aria-busy="submitting" @submit.prevent="send"><textarea ref="input" v-model="draft" name="body" rows="1" aria-label="聊天消息" :placeholder="snapshot.trip.mode === 'city' ? '想去哪儿逛逛？也可以直接说生成路线…' : '写下你的旅行想法…'" autocomplete="off" maxlength="10000" required @compositionstart="composing = true" @compositionend="composing = false" @keydown="keydown" @focus="viewportChanged" @blur="viewportChanged"/><button v-if="canCancel" type="button" :aria-label="cancelling ? '正在终止' : '终止处理'" :title="cancelling ? '正在终止' : '终止处理'" :disabled="cancelling" @click="terminate"><svg aria-hidden="true" width="14" height="14" viewBox="0 0 16 16" focusable="false"><rect x="1" y="1" width="14" height="14" rx="2" fill="currentColor" /></svg></button><button v-if="!canCancel || draft.trim()" type="submit" aria-label="发送消息" title="发送消息" :disabled="!canSend || composing || !draft.trim()"><svg aria-hidden="true" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" focusable="false"><path d="M12 19V5m-6 6 6-6 6 6" /></svg></button></form><p v-if="feedback || draftFeedback" class="chat-send-feedback" role="status">{{ draftFeedback || feedback }}<button v-if="undoUncertain" class="chat-message-action" type="button" :disabled="undoing" @click="workspace.undoLast">确认撤销结果</button><button v-if="pending?.failed" class="chat-message-action" type="button" @click="dismissPending">{{ pending.kind === 'chat' ? '移回草稿' : '取消未提交请求' }}</button></p></section>
        <nav class="bottom-nav bottom-nav--v2" aria-label="底部导航"><RouterLink :to="{ query: { tab: 'board' } }" @click.prevent="tab('board')"><span aria-hidden="true">✦</span>想法</RouterLink><RouterLink class="active" :to="{ query: { tab: 'chat' } }" @click.prevent="tab('chat')"><span aria-hidden="true">✉</span>聊天</RouterLink></nav>
      </section>
      <GuidePreview :open="guideOpen" :snapshot="snapshot" :busy="busy" :feedback="feedback" @close="guideOpen = false"/>
    </template>
  </section>
</template>
<style scoped>
.workspace-initial { display: grid; gap: 20px; padding: 32px 20px; color: var(--ink); }
.workspace-initial button, .workspace-initial a { min-height: 44px; }
.chat-menu-panel button:disabled { opacity: .5; cursor: not-allowed; }
</style>
