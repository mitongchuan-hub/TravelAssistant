<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, type CSSProperties } from 'vue'
import SafeMarkdown from '@/components/SafeMarkdown'
import { useFocusDialog } from '@/composables/useFocusDialog'
const props = defineProps<{ text: string; revision: number; destination: string; city: boolean; busy: boolean; feedback: string; generationWarning: string; saveRequirements: (text: string, expectedRevision: number) => Promise<boolean> }>()
const emit = defineEmits<{ generate: [] }>()
const editing = ref(false), draft = ref(''), expectedRevision = ref(0), original = ref(''), saving = ref(false)
const body = ref(''), adding = ref(false), addNotice = ref('')
const dialogOpen = ref(false), dialog = ref<HTMLElement | null>(null), selected = ref<string | null>(null)
const canvas = ref<HTMLElement | null>(null), dimensions = ref({ width: 330, height: 500 })
useFocusDialog(dialogOpen, dialog, close)
function close() { if (saving.value || editing.value) return; dialogOpen.value = false; selected.value = null }
function show(text: string | null) { selected.value = text; dialogOpen.value = true }
let observer: ResizeObserver | undefined
onMounted(() => {
  observer = new ResizeObserver(entries => { const rect = entries[0]?.contentRect; if (rect && rect.width && rect.height) dimensions.value = { width: rect.width, height: rect.height } })
  if (canvas.value) observer.observe(canvas.value)
})
onBeforeUnmount(() => observer?.disconnect())
// Paragraphs and their indices are presentation only; the sole saved value is the complete text.
const paragraphs = computed(() => props.text.split(/\r?\n\s*\r?\n/).map(text => text.trim()).filter(Boolean))
const layouts: Record<number, number[][]> = {
  1: [[.50,.48]], 2: [[.25,.25],[.66,.66]], 3: [[.23,.18],[.66,.42],[.35,.72]], 4: [[.22,.16],[.66,.28],[.30,.56],[.70,.76]],
  5: [[.22,.16],[.64,.24],[.30,.46],[.68,.58],[.48,.82]], 6: [[.22,.14],[.64,.23],[.28,.40],[.68,.53],[.32,.72],[.70,.84]],
  7: [[.22,.13],[.64,.22],[.28,.38],[.68,.50],[.30,.66],[.70,.78],[.42,.90]], 8: [[.22,.12],[.66,.21],[.26,.34],[.70,.45],[.30,.58],[.72,.69],[.28,.82],[.70,.91]],
}
const stageHeight = computed(() => dimensions.value.height * Math.max(1, Math.ceil(paragraphs.value.length / 8)))
const notes = computed(() => paragraphs.value.map((text, index) => {
  const { width: w, height: h } = dimensions.value
  const group = Math.floor(index / 8), count = Math.min(8, paragraphs.value.length - group * 8)
  const point = layouts[count]![index % 8]!, width = Math.max(94, Math.min(136, w * (count > 6 ? .32 : .38)))
  const chars = Array.from(text), preview = chars.slice(0, 36).join('') + (chars.length > 36 ? '…' : '')
  const height = 42 + Math.min(4, Math.ceil(Math.min(chars.length, 36) / Math.max(5, Math.floor(width / 15)))) * 18
  const left = Math.max(18, Math.min(point[0]! * w - width / 2, w - width - 18))
  const top = group * h + Math.max(52, Math.min(point[1]! * h - height / 2, h - height - 18))
  return { text, preview, left, top, width, height, style: { '--idea-width': `${width}px`, '--idea-left': `${left}px`, '--idea-top': `${top}px`, '--rotate': `${[-5, 4, -3, 5, -4, 3, -2, 4][index % 8]}deg` } as CSSProperties }
}))
const path = computed(() => notes.value.map((p, i, all) => {
  const x = p.left + p.width / 2, y = p.top + p.height / 2
  if (!i) return `M ${x} ${y}`
  const prev = all[i - 1]!, px = prev.left + prev.width / 2, py = prev.top + prev.height / 2, mid = (px + x) / 2
  return `C ${mid} ${py}, ${mid} ${y}, ${x} ${y}`
}).join(' '))
const count = computed(() => Array.from(draft.value).length)
const savedCount = computed(() => Array.from(props.text).length)
const originalCount = computed(() => Array.from(original.value).length)
const overLimit = computed(() => count.value > 20000 && !(originalCount.value > 20000 && count.value <= originalCount.value))
const changedElsewhere = computed(() => editing.value && props.revision !== expectedRevision.value)
function edit() { draft.value = props.text; original.value = props.text; expectedRevision.value = props.revision; editing.value = true }
async function save() {
  if (props.busy || saving.value || overLimit.value) return
  saving.value = true
  try { if (await props.saveRequirements(draft.value, expectedRevision.value)) { editing.value = false; selected.value = null } }
  finally { saving.value = false }
}
async function add() {
  const addition = body.value.trim()
  if (!addition || props.busy || adding.value || editing.value) return
  const text = props.text + (props.text ? '\n\n' : '') + addition, revision = props.revision
  if (Array.from(text).length > 20000) { addNotice.value = '合并后正文超过 20,000 字，输入已保留；请查看全文并整理正文。'; return }
  adding.value = true; addNotice.value = ''
  try {
    if (await props.saveRequirements(text, revision)) body.value = ''
    else addNotice.value = '未保存，输入已保留。请核对最新正文后再添加，不会覆盖其他更新。'
  } catch { addNotice.value = '未保存，输入已保留。请检查连接后再添加。' }
  finally { adding.value = false }
}
</script>
<template>
  <section class="idea-wall-screen">
    <div :inert="dialogOpen || undefined">
      <header class="idea-wall-top"><RouterLink to="/trips"><span>旅行想法</span>{{ destination.replace('周末旅行', '想法墙') }}</RouterLink><form @submit.prevent="emit('generate')"><button class="generate-plan" type="submit" :disabled="busy || editing || adding">{{ city ? '生成路线' : '生成计划' }}</button></form></header>
    </div>
    <section ref="canvas" class="idea-wall-canvas" :inert="dialogOpen || undefined" :data-card-count="notes.length" aria-label="旅行想法白板">
      <h2 class="sr-only">旅行想法白板</h2>
      <button class="wall-body-tool" type="button" @click="show(null)">查看全文<span v-if="paragraphs.length > 8"> · {{ paragraphs.length }} 段，可滚动</span></button>
      <div class="idea-wall-stage" :style="{ height: `${stageHeight}px` }">
        <svg class="travel-route" :viewBox="`0 0 ${dimensions.width} ${stageHeight}`" aria-hidden="true"><path :d="path" /></svg>
        <div v-if="!paragraphs.length" class="idea-wall-empty"><strong>这里先留给你的旅行想法</strong><p>写下想去的地方、预算、节奏或不想接受的安排，也可以在聊天中告诉助手。</p><span>从下面开始记录第一条想法</span></div>
        <button v-for="(note, index) in notes" :key="index" class="floating-idea" type="button" :style="note.style" @click="show(note.text)"><strong>{{ note.preview }}</strong><span>查看全文</span></button>
      </div>
    </section>
    <div :inert="dialogOpen || undefined">
      <p v-if="savedCount > 20000" class="chat-send-feedback" role="status">正文超过 20,000 字，完整内容已保留；请先整理正文再生成行程。</p>
      <p v-if="generationWarning" class="chat-send-feedback" role="status">{{ generationWarning }}</p>
      <p v-if="feedback" class="chat-send-feedback" role="status">{{ feedback }}</p>
      <p v-if="addNotice" class="chat-send-feedback" role="status">{{ addNotice }}</p>
      <slot name="recovery" />
    </div>
    <form class="wall-quick-add" :inert="dialogOpen || undefined" @submit.prevent="add"><input v-model="body" name="body" aria-label="旅行想法" placeholder="写一个旅行想法" autocomplete="off" :disabled="adding" required /><button type="submit" :disabled="busy || adding || !body.trim()">{{ adding ? '保存中…' : '添加' }}</button></form>
    <div :inert="dialogOpen || undefined"><slot /></div>
    <section v-if="dialogOpen" class="idea-detail-sheet open" aria-label="想法详情">
      <button class="sheet-scrim" type="button" aria-label="关闭想法详情" tabindex="-1" :disabled="editing || saving" @click="close" />
      <article ref="dialog" class="idea-detail-panel" role="dialog" aria-modal="true" aria-labelledby="idea-detail-title" tabindex="-1">
        <div class="sheet-handle" aria-hidden="true" />
        <div class="idea-detail-heading"><h2 id="idea-detail-title">{{ editing ? '编辑全部正文' : selected === null ? '旅行想法全文' : '旅行想法' }}</h2><button type="button" :disabled="editing || saving" @click="close">关闭</button></div>
        <form v-if="editing" class="requirements-editor" @submit.prevent="save">
          <label for="requirements-text">需求正文</label>
          <textarea id="requirements-text" v-model="draft" name="requirements_text" rows="12" :disabled="saving" aria-describedby="requirements-count requirements-edit-notice" />
          <p id="requirements-count" class="muted">{{ count.toLocaleString() }} / 20,000 字（Unicode 码点）</p>
          <p id="requirements-edit-notice" role="status">{{ overLimit ? '正文超过 20,000 字，请缩短后保存。' : count > 20000 ? '迁移的超长正文可原样保留或缩短，不能继续增长；生成前请整理至 20,000 字以内。' : '保存只更新需求正文，已有行程保留，不会自动重新生成。' }}</p>
          <p v-if="changedElsewhere" role="alert">需求已在其他页面或聊天中更新。你的草稿仍保留；保存将检查开始编辑时的版本，不会覆盖新版本。请先复制草稿，再取消并重新编辑以核对最新正文。</p>
          <p v-if="feedback" role="status">{{ feedback }}</p>
          <div class="requirements-actions"><button type="submit" :disabled="busy || saving || overLimit">{{ saving ? '正在保存…' : '保存' }}</button><button type="button" :disabled="saving" @click="editing = false; selected = null">取消</button></div>
        </form>
        <template v-else>
          <SafeMarkdown v-if="selected !== null || text" :text="selected ?? text" />
          <p v-else>这里先留给你的旅行想法</p>
          <div class="requirements-actions"><button v-if="selected !== null" type="button" @click="selected = null">查看全部正文</button><button type="button" :disabled="busy || adding" @click="edit">编辑全部正文</button></div>
        </template>
      </article>
    </section>
  </section>
</template>
<style scoped>
/* Restore the original floating wall and sheet with the incumbent fog-blue palette. */
.idea-wall-canvas { position: relative; flex: 1 0 auto; height: max(460px, calc(100dvh - 240px)); overflow: auto; border: 1px solid var(--line); border-radius: 16px; background: var(--surface); }
.idea-wall-stage { position: relative; min-height: 100%; }
.travel-route { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
.travel-route path { fill: none; stroke: var(--ocean); opacity: .3; stroke-width: 3; stroke-dasharray: 9 12; stroke-linecap: round; stroke-linejoin: round; }
.floating-idea { position: absolute; left: 0; top: 0; display: inline-flex; width: var(--idea-width); min-height: 46px; flex-direction: column; justify-content: center; gap: 6px; padding: 9px 12px; text-align: left; border: 1px solid var(--line); border-radius: 11px; background: var(--mist); color: var(--ink); box-shadow: 0 5px 10px rgb(17 45 78 / 5%); transform: translate(var(--idea-left), var(--idea-top)) rotate(var(--rotate)); transition: transform 160ms ease, box-shadow 160ms ease; overflow-wrap: anywhere; }
.floating-idea:nth-of-type(3n + 1) { background: var(--surface-accent); }
.floating-idea:nth-of-type(3n + 2) { background: var(--fog); }
.floating-idea:active { box-shadow: var(--shadow-soft); transform: translate(var(--idea-left), calc(var(--idea-top) + 2px)) rotate(var(--rotate)); }
.floating-idea strong { font-size: 14px; line-height: 1.3; white-space: pre-wrap; }
.floating-idea span { font-size: 10px; font-weight: 850; }
.wall-body-tool { position: sticky; top: 8px; left: 12px; z-index: 2; float: right; margin: 8px 10px -44px 0; min-height: 36px; padding: 5px 9px; border: 1px solid var(--line); border-radius: 10px; color: var(--ink); background: var(--surface); font-size: 12px; }
.idea-wall-empty { position: absolute; top: 50%; left: 50%; width: min(280px, calc(100% - 48px)); transform: translate(-50%, -54%); text-align: center; color: var(--muted); pointer-events: none; }
.idea-wall-empty strong { display: block; color: var(--ink); font-size: 18px; line-height: 1.3; }
.idea-wall-empty p { margin: 10px 0 12px; font-size: 13px; line-height: 1.6; }
.idea-wall-empty span { display: inline-block; padding: 6px 10px; border: 1px dashed var(--line); border-radius: 999px; background: var(--surface); font-size: 11px; font-weight: 800; }
.wall-quick-add { display: grid; grid-template-columns: minmax(0, 1fr) 64px; gap: 8px; flex: 0 0 auto; padding: 7px; border: 1px solid var(--line); border-radius: 18px; background: var(--surface); box-shadow: var(--shadow-soft); }
.wall-quick-add input { min-width: 0; min-height: 44px; border: 1px solid var(--line); border-radius: 12px; background: var(--surface); }
.wall-quick-add button { min-height: 44px; border: 1px solid transparent; border-radius: 10px; background: var(--ocean); color: var(--on-action); font-size: 13px; font-weight: 950; }
.idea-detail-sheet { position: fixed; inset: 0; z-index: 40; display: flex; align-items: flex-end; justify-content: center; }
.idea-detail-panel { position: relative; width: min(100%, 430px); max-height: 90dvh; overflow-y: auto; padding: 12px 18px calc(22px + env(safe-area-inset-bottom)); border: 1px solid var(--line); border-bottom: 0; border-radius: 24px 24px 0 0; background: var(--surface); box-shadow: 0 -8px 30px rgb(17 45 78 / 10%); overflow-wrap: anywhere; }
.idea-detail-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
.idea-detail-heading h2 { margin: 0; font-size: 22px; }
.idea-detail-heading button { min-height: 44px; padding: 0 12px; border: 0; border-radius: 10px; background: var(--ocean); color: var(--on-action); }
.requirements-editor { display: grid; gap: 12px; }
.requirements-editor textarea { width: 100%; min-height: 260px; box-sizing: border-box; resize: vertical; font: inherit; line-height: 1.65; }
.requirements-editor p { margin: 0; font-size: 13px; line-height: 1.7; }
.requirements-actions { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 16px; }
.requirements-actions button { min-height: 44px; padding: 8px 14px; border: 1px solid var(--line); border-radius: 12px; color: var(--ink); background: var(--surface); }
@media (prefers-reduced-motion: reduce) { .floating-idea { transition: none; } }
</style>
