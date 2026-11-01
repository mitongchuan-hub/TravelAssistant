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
const { snapshot, messages, loading, error, feedback, busy, submitting, latestTask, pending } = workspace
const board = computed(() => route.query.tab === 'board')
const guideOpen = ref(route.query.tab === 'itinerary' || route.query.guide === '1')
const list = ref<HTMLElement | null>(null), input = ref<HTMLTextAreaElement | null>(null)
const draft = ref(''), draftFeedback = ref(''), composing = ref(false), newReplies = ref(false)
const draftKey = `travelassistant:chat-draft:${encodeURIComponent(userId)}:${encodeURIComponent(tripId)}`
try { draft.value = sessionStorage.getItem(draftKey) || '' } catch { /* Private-mode storage is optional. */ }
if (typeof route.query.draft === 'string' && !draft.value) draft.value = route.query.draft
function saveDraft() { try { sessionStorage.setItem(draftKey, draft.value) } catch { /* Keep draft in memory. */ } }
let frame = 0
function resizeComposer() { if (!input.value) return; const el = input.value; el.style.height = '0px'; el.style.height = `${Math.max(38, Math.min(el.scrollHeight, 126))}px`; el.style.overflowY = el.scrollHeight > 126 ? 'auto' : 'hidden' }
watch(draft, () => { saveDraft(); void nextTick(resizeComposer) })
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
  if (composing.value || submitting.value || pending.value || !draft.value.trim()) return
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
function openGuide() { guideOpen.value = true }
async function tab(value: 'board' | 'chat') { guideOpen.value = false; await router.replace({ query: { tab: value } }); await nextTick(); resizeComposer(); viewportChanged() }
async function generate() { await tab('chat'); await workspace.send('请根据已保存的要求和想法生成同城行程。') }
function viewportChanged() {
  cancelAnimationFrame(frame)
  frame = requestAnimationFrame(() => {
    const shell = document.querySelector<HTMLElement>('.phone-shell--chat'), viewport = window.visualViewport
    if (shell && viewport) { const bottom = nearBottom(); shell.style.setProperty('--chat-viewport-height', `${viewport.height}px`); shell.style.setProperty('--chat-viewport-top', `${viewport.offsetTop}px`); shell.classList.toggle('chat-keyboard-open', document.activeElement === input.value && innerHeight - viewport.height > 120); if (bottom) toLatest() }
  })
}
watch(board, value => { document.body.classList.toggle('chat-page', !value); void nextTick(viewportChanged) }, { immediate: true })
watch(() => route.query.tab, value => { if (value === 'itinerary') guideOpen.value = true })
onMounted(() => { document.body.classList.toggle('chat-page', !board.value); window.addEventListener('resize', viewportChanged); window.visualViewport?.addEventListener('resize', viewportChanged); window.visualViewport?.addEventListener('scroll', viewportChanged); resizeComposer(); viewportChanged() })
onBeforeUnmount(() => { document.body.classList.remove('chat-page'); window.removeEventListener('resize', viewportChanged); window.visualViewport?.removeEventListener('resize', viewportChanged); window.visualViewport?.removeEventListener('scroll', viewportChanged); cancelAnimationFrame(frame); const shell = document.querySelector<HTMLElement>('.phone-shell'); shell?.style.removeProperty('--chat-viewport-height'); shell?.style.removeProperty('--chat-viewport-top'); shell?.classList.remove('chat-keyboard-open') })
</script>
<template>
  <section :class="['workspace', board ? 'workspace--board' : 'workspace--chat']">
    <div v-if="!snapshot" class="workspace-initial" role="status"><p>{{ loading ? '正在读取旅行记录…' : error || '旅行暂时无法打开。' }}</p><button v-if="!loading" type="button" @click="workspace.refresh">重新读取</button><RouterLink to="/trips">返回我的旅行</RouterLink></div>
    <template v-else>
      <IdeaBoard v-if="board" :cards="snapshot.wall_cards" :all-cards="snapshot.idea_cards" :trip-id="tripId" :destination="snapshot.trip.destination" :city="snapshot.trip.mode === 'city'" :busy="busy" :feedback="feedback || error" :add-idea="workspace.addIdea" :delete-idea="workspace.deleteIdea" @generate="generate">
        <template #default="slotProps"><nav class="bottom-nav bottom-nav--v2" aria-label="底部导航" :inert="slotProps.inert || undefined"><RouterLink class="active" :to="{ query: { tab: 'board' } }" @click.prevent="tab('board')">想法</RouterLink><RouterLink :to="{ query: { tab: 'chat' } }" @click.prevent="tab('chat')">聊天</RouterLink></nav></template>
      </IdeaBoard>
      <section v-show="!board" class="travel-tab-screen chat-screen" :inert="guideOpen || undefined">
        <header class="chat-top"><RouterLink class="chat-back" to="/trips" aria-label="返回我的旅行">‹</RouterLink><div class="chat-heading"><h1>{{ snapshot.trip.destination.replace('周末旅行', '') }}</h1><p>{{ snapshot.trip.date_range }}</p></div><button class="guide-open" type="button" aria-controls="guide-preview" :aria-expanded="guideOpen" @click="openGuide">攻略</button></header>
        <section ref="list" class="chat-list" aria-label="与旅行助手的对话" @scroll.passive="nearBottom() && (newReplies = false)">
          <p v-if="error" class="chat-send-feedback" role="status">{{ error }} <button class="chat-message-action" type="button" @click="workspace.refresh">重新连接</button></p>
          <ChatMessage v-for="(message, index) in messages" :key="message.id" :message="message" :previous="messages[index - 1]" :busy="busy" @draft="recoverDraft" @retry="workspace.retryPending" @guide="openGuide"/>
          <div v-if="!messages.length" class="chat-empty"><span aria-hidden="true">✦</span><h2>一起聊聊这趟旅行</h2><p>想去哪里、怎么玩，或者先说一个小想法。</p></div>
          <p v-if="latestTask?.kind !== 'chat' && latestTask && typeof latestTask.status === 'string' && ['queued', 'running', 'failed'].includes(latestTask.status)" class="chat-send-feedback" role="status">{{ latestTask.status === 'failed' ? latestTask.message || '本次处理未完成，已有内容已保留。' : '正在安排你的路线，已有内容仍可查看。' }}<button v-if="latestTask.status === 'failed'" class="chat-message-action" type="button" :disabled="busy" @click="workspace.retryTask(latestTask)">重试</button></p>
        </section>
        <section class="chat-dock" aria-label="聊天输入区"><button v-if="newReplies" class="chat-new-replies" type="button" @click="toLatest">有新回复 ↓</button><form class="composer" :aria-busy="busy" @submit.prevent="send"><textarea ref="input" v-model="draft" name="body" rows="1" aria-label="聊天消息" :placeholder="snapshot.trip.mode === 'city' ? '想去哪儿逛逛？也可以直接说生成路线…' : '写下你的旅行想法…'" autocomplete="off" maxlength="10000" required @compositionstart="composing = true" @compositionend="composing = false" @keydown="keydown" @focus="viewportChanged" @blur="viewportChanged"/><button type="submit" aria-label="发送消息" title="发送消息" :disabled="submitting || !!pending || composing || !draft.trim()"><span aria-hidden="true">↑</span></button></form><p v-if="feedback || draftFeedback" class="chat-send-feedback" role="status">{{ draftFeedback || feedback }}<button v-if="pending?.failed && pending.kind === 'plan'" class="chat-message-action" type="button" @click="workspace.retryPending">重试请求</button><button v-if="pending?.failed" class="chat-message-action" type="button" @click="dismissPending">{{ pending.kind === 'chat' ? '移回草稿' : '取消未提交请求' }}</button></p></section>
        <nav class="bottom-nav bottom-nav--v2" aria-label="底部导航"><RouterLink :to="{ query: { tab: 'board' } }" @click.prevent="tab('board')">想法</RouterLink><RouterLink class="active" :to="{ query: { tab: 'chat' } }" @click.prevent="tab('chat')">聊天</RouterLink></nav>
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
