<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch, type CSSProperties } from 'vue'
import type { Idea } from '@/stores/workspace'
import { useFocusDialog } from '@/composables/useFocusDialog'
const props = defineProps<{ cards: Idea[]; allCards: Idea[]; tripId: string; destination: string; city: boolean; busy: boolean; feedback: string; addIdea: (body: string) => Promise<boolean>; deleteIdea: (id: string) => Promise<boolean> }>()
const emit = defineEmits<{ generate: [] }>()
const body = ref(''), selected = ref<Idea | null>(null), dialogOpen = ref(false), dialog = ref<HTMLElement | null>(null), canvas = ref<HTMLElement | null>(null)
const dimensions = ref({ width: 330, height: 500 })
useFocusDialog(dialogOpen, dialog, close)
function close() { dialogOpen.value = false; selected.value = null }
let observer: ResizeObserver | undefined
onMounted(() => { observer = new ResizeObserver(entries => { const rect = entries[0]?.contentRect; if (rect) dimensions.value = { width: rect.width, height: rect.height } }); if (canvas.value) observer.observe(canvas.value) })
onBeforeUnmount(() => observer?.disconnect())
watch(() => props.allCards, cards => { if (selected.value) { selected.value = cards.find(c => c.id === selected.value?.id) ?? null; if (!selected.value) close() } })
const layouts: Record<number, number[][]> = {
  1: [[.50,.48]], 2: [[.25,.25],[.66,.66]], 3: [[.23,.18],[.66,.42],[.35,.72]], 4: [[.22,.16],[.66,.28],[.30,.56],[.70,.76]],
  5: [[.22,.16],[.64,.24],[.30,.46],[.68,.58],[.48,.82]], 6: [[.22,.14],[.64,.23],[.28,.40],[.68,.53],[.32,.72],[.70,.84]],
  7: [[.22,.13],[.64,.22],[.28,.38],[.68,.50],[.30,.66],[.70,.78],[.42,.90]], 8: [[.22,.12],[.66,.21],[.26,.34],[.70,.45],[.30,.58],[.72,.69],[.28,.82],[.70,.91]],
}
const visible = computed(() => props.cards.slice(-8))
const positions = computed(() => {
  const { width: w, height: h } = dimensions.value, count = visible.value.length
  const width = Math.max(94, Math.min(136, w * (count > 6 ? .32 : .38)))
  return visible.value.map((card, index) => {
    const point = layouts[count]?.[index] ?? [.5,.5]
    const height = 39 + Math.max(1, Math.ceil(card.title.length / Math.max(5, Math.floor(width / 18)))) * 18
    const left = Math.max(18, Math.min(point[0]! * w - width / 2, w - width - 18)), top = Math.max(18, Math.min(point[1]! * h - height / 2, h - height - 18))
    return { left, top, width, height, style: { '--idea-width': `${width}px`, '--idea-left': `${left}px`, '--idea-top': `${top}px`, '--idea-title-size': count > 6 ? '13px' : '15px', '--rotate': /^-?\d+(\.\d+)?deg$/.test(card.rotation) ? card.rotation : '0deg' } as CSSProperties }
  })
})
const path = computed(() => positions.value.map((p, i, all) => { const x = p.left + p.width / 2, y = p.top + p.height / 2; if (!i) return `M ${x} ${y}`; const prev = all[i-1]!, px = prev.left + prev.width / 2, py = prev.top + prev.height / 2, mid = (px + x) / 2; return `C ${mid} ${py}, ${mid} ${y}, ${x} ${y}` }).join(' '))
async function add() { const text = body.value.trim(); if (!text || props.busy) return; if (await props.addIdea(text)) body.value = '' }
async function remove() { const idea = selected.value; if (idea && window.confirm('确定要删除这条旅行想法吗？删除后无法恢复。') && await props.deleteIdea(idea.id)) { close(); await nextTick() } }
</script>
<template>
  <section class="idea-wall-screen">
    <div :inert="dialogOpen || undefined">
      <header class="idea-wall-top"><RouterLink to="/trips"><span>旅行想法</span>{{ destination.replace('周末旅行', '想法墙') }}</RouterLink><form @submit.prevent="emit('generate')"><button class="generate-plan" type="submit" :disabled="busy">{{ city ? '生成路线' : '生成计划' }}</button></form></header>
    </div>
    <section ref="canvas" class="idea-wall-canvas" :inert="dialogOpen || undefined" :data-card-count="visible.length" aria-label="旅行想法白板">
      <h2 class="sr-only">旅行想法白板</h2><svg class="travel-route" :viewBox="`0 0 ${dimensions.width} ${dimensions.height}`" aria-hidden="true"><path :d="path"/></svg>
      <div v-if="!allCards.length" class="idea-wall-empty"><strong>这里先留给你的旅行想法</strong><p>写下想去的地方、预算、节奏或不想接受的安排，Agent 会帮你整理。</p><span>从下面开始记录第一条想法</span></div>
      <button v-for="(card, index) in visible" :key="card.id" class="floating-idea" type="button" :style="positions[index]?.style" @click="selected = card; dialogOpen = true"><strong>{{ card.title }}</strong></button>
    </section>
    <p v-if="feedback" class="chat-send-feedback" role="status">{{ feedback }}</p>
    <div :inert="dialogOpen || undefined"><slot name="recovery" /></div>
    <form class="wall-quick-add" :inert="dialogOpen || undefined" @submit.prevent="add"><input v-model="body" name="body" aria-label="旅行想法" placeholder="写一个旅行想法" autocomplete="off" maxlength="12000" required/><button type="submit" :disabled="busy || !body.trim()">添加</button></form>
    <slot :inert="dialogOpen" />
    <section v-if="dialogOpen && selected" class="idea-detail-sheet open" aria-label="想法详情"><button class="sheet-scrim" type="button" aria-label="关闭想法详情" tabindex="-1" @click="close"/><article ref="dialog" class="idea-detail-panel" role="dialog" aria-modal="true" aria-labelledby="idea-detail-title" tabindex="-1"><div class="sheet-handle" aria-hidden="true"/><div class="idea-detail-heading"><span>旅行想法</span><button type="button" @click="close">关闭</button></div><h2 id="idea-detail-title">{{ selected.title }}</h2><p>{{ selected.body }}</p><div class="idea-detail-statusbar"><strong>{{ selected.status }}</strong><span>{{ selected.author }}</span></div><dl><div><dt>来源</dt><dd>{{ selected.author }}</dd></div><div><dt>采纳状态</dt><dd>{{ selected.status }}</dd></div></dl><form class="idea-delete-form" @submit.prevent="remove"><button type="submit" :disabled="busy">删除这条想法</button></form></article></section>
  </section>
</template>
