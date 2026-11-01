<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError } from '@/api/client'
import { tripsApi } from '@/api/trips'
import type { CityOutingValues, LodgingCandidate, Trip } from '@/types'

const route = useRoute()
const router = useRouter()
const id = computed(() => typeof route.params.id === 'string' ? route.params.id : undefined)
const editing = computed(() => Boolean(id.value))
const trip = ref<Trip | null>(null)
const values = reactive<CityOutingValues>({
  lodging_address: '', transport_mode: '', start_date: '', day_count: '',
  outing_start_time: '', outing_end_time: '', travelers: '', budget_amount: '',
  budget_scope: 'per_person', pace: '', must_visit: '', avoid: '', dietary: '',
  note: '', city_hint: '', revision: '',
})
const candidates = ref<LodgingCandidate[]>([])
const choiceToken = ref('')
const selectedPoi = ref('')
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const saved = ref(false)
const conflict = ref(false)
const latestRevision = ref<string | null>(null)
const refreshing = ref(false)

async function load() {
  if (!id.value) return
  loading.value = true
  error.value = ''
  try {
    const result = await tripsApi.requirements(id.value)
    trip.value = result.trip
    Object.assign(values, result.values)
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '游玩设置加载失败，请重试。' }
  finally { loading.value = false }
}
onMounted(load)

function resetChoices() {
  candidates.value = []
  choiceToken.value = ''
  selectedPoi.value = ''
}
async function submit() {
  if (busy.value || conflict.value) return
  busy.value = true
  error.value = ''
  saved.value = false
  try {
    // Only the server-issued choice token and candidate identity confirm a place.
    // The server retains the draft; client coordinates are never accepted or sent.
    const payload = candidates.value.length
      ? { choice_token: choiceToken.value, poi_id: selectedPoi.value }
      : Object.fromEntries(Object.entries(values).map(([key, value]) => [key, String(value ?? '')]))
    const result = await tripsApi.save(id.value, payload)
    if (result.status === 'choices_required') {
      candidates.value = result.candidates
      choiceToken.value = result.choice_token
      selectedPoi.value = ''
      Object.assign(values, result.values)
    } else if (editing.value) {
      trip.value = result.trip
      values.revision = String(result.trip.requirements.revision)
      resetChoices()
      saved.value = true
      window.scrollTo({ top: 0 })
    } else {
      await router.replace(`/workspace/${encodeURIComponent(result.trip.id)}?tab=chat`)
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '保存失败，请重试。'
    if (cause instanceof ApiError && cause.status === 409) {
      conflict.value = true
      latestRevision.value = null
      // Keep every local field; an old place token must not bypass the revision check.
      resetChoices()
    }
  } finally { busy.value = false }
}
async function checkLatest() {
  if (!id.value || refreshing.value) return
  refreshing.value = true
  try {
    const result = await tripsApi.requirements(id.value)
    trip.value = result.trip
    latestRevision.value = String(result.trip.requirements.revision)
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '读取最新版本失败，请重试。' }
  finally { refreshing.value = false }
}
function keepDraft() {
  if (latestRevision.value === null) return
  values.revision = latestRevision.value
  conflict.value = false
  error.value = ''
  latestRevision.value = null
}
async function useLatest() {
  if (!window.confirm('放弃当前未保存的修改，读取最新游玩设置？')) return
  resetChoices()
  conflict.value = false
  latestRevision.value = null
  await load()
}
</script>

<template>
  <section class="page-stack city-outing-screen">
    <header class="page-header">
      <RouterLink class="text-link" :to="editing ? `/workspace/${id}?tab=chat` : '/trips'">{{ editing ? '返回聊天' : '返回' }}</RouterLink>
      <div><p class="muted">从住处出发，发现这座城</p><h1>{{ editing ? '游玩设置' : '今天，想去哪儿逛逛？' }}</h1></div>
    </header>
    <p v-if="loading" class="muted" role="status">正在加载游玩设置…</p>
    <div v-if="error" class="form-error" role="alert">
      <p>{{ error }}</p>
      <template v-if="conflict">
        <p>设置已在其他页面更新。你的修改仍保留在下方，不会被自动覆盖。</p>
        <div class="settings-conflict-actions">
          <button v-if="latestRevision === null" type="button" :disabled="refreshing" @click="checkLatest">{{ refreshing ? '正在读取…' : '检查最新版本，保留草稿' }}</button>
          <button v-else type="button" @click="keepDraft">已读取最新版本，继续编辑我的草稿</button>
          <button type="button" @click="useLatest">放弃草稿，使用最新设置</button>
        </div>
      </template>
      <button v-else-if="editing && !trip" class="text-link" type="button" @click="load">重新加载</button>
    </div>
    <p v-if="saved && !error" class="trip-notice" role="status">游玩设置已保存。下次生成或调整会使用新设置；已有行程保留。</p>
    <template v-if="!loading && (!editing || trip)">
      <template v-if="candidates.length">
        <section class="form-helper-card"><strong>确认你的住宿位置</strong><p>请按城市和地址选择具体位置，路线会从这里出发并返回。</p></section>
        <form class="form-stack" :aria-busy="busy" @submit.prevent="submit">
          <fieldset class="lodging-choices"><legend>找到这些候选</legend>
            <label v-for="place in candidates" :key="place.poi_id" class="lodging-choice"><input v-model="selectedPoi" type="radio" name="poi_id" :value="place.poi_id" required /><span><strong>{{ place.name }}</strong><small>{{ place.city }} {{ place.district }} · {{ place.address }}</small></span></label>
          </fieldset>
          <button class="primary-action button-reset" type="submit" :disabled="busy || !selectedPoi">{{ busy ? '正在确认…' : editing ? '确认位置并保存' : '从这里开始' }}</button>
          <button class="text-link" type="button" :disabled="busy" @click="resetChoices">重新填写地址</button>
        </form>
      </template>
      <template v-else>
        <section class="form-helper-card"><strong>选好住处、出行方式和日期，就能开始</strong><p>请填写开始日期和游玩天数，结束日期自动计算。每天从住处出发并返回。</p></section>
        <form class="form-stack trip-create-form" :aria-busy="busy" @submit.prevent="submit">
          <fieldset><legend>你的住宿</legend>
            <label><span>住宿地址或酒店名称</span><input v-model="values.lodging_address" name="lodging_address" maxlength="300" placeholder="例如：杭州西湖湖滨银泰附近的具体酒店" autocomplete="street-address" required /></label>
            <small v-if="trip?.lodging.name">已确认：{{ trip.lodging.name }} · {{ trip.lodging.city }} {{ trip.lodging.address }}</small>
            <label><span>出行方式</span><select v-model="values.transport_mode" name="transport_mode" required><option value="" disabled>请选择</option><option value="driving">自驾</option><option value="transit">公交地铁</option></select></label>
          </fieldset>
          <fieldset><legend>什么时候逛</legend><div class="create-field-row">
            <label><span>开始日期（必填）</span><input v-model="values.start_date" type="date" name="start_date" required /></label>
            <label><span>游玩天数（必填）</span><input v-model="values.day_count" type="number" name="day_count" min="1" max="365" step="1" required /></label>
          </div></fieldset>
          <details class="city-options" :open="editing || Boolean(error)">
            <summary>更多设置 <span>每日时间、人数、预算和偏好，可选</span></summary>
            <fieldset><legend>每日时间（可选）</legend><div class="create-field-row">
              <label><span>每天出门时间</span><input v-model="values.outing_start_time" type="time" name="outing_start_time" /></label>
              <label><span>每天返回时间</span><input v-model="values.outing_end_time" type="time" name="outing_end_time" /></label>
            </div><small>每天出门和返回时间可留空，规划时会给建议时段。</small></fieldset>
            <fieldset><legend>怎么逛更舒服</legend>
              <label><span>同行人数（含自己）</span><input v-model="values.travelers" type="number" name="travelers" min="1" max="100" step="1" placeholder="可留空" /></label>
              <div class="create-field-row">
                <label><span>游玩预算（元）</span><input v-model="values.budget_amount" type="number" name="budget_amount" min="0.01" max="100000000" step="0.01" placeholder="可留空" /></label>
                <label><span>预算口径</span><select v-model="values.budget_scope" name="budget_scope"><option value="per_person">每人预算</option><option value="total">同行人总预算</option></select></label>
              </div>
              <small>费用范围：{{ values.budget_coverage || '餐饮、市内交通和门票；不含住宿及跨城交通' }}。聊天中可以调整。</small>
              <label><span>节奏偏好</span><input v-model="values.pace" name="pace" maxlength="300" placeholder="例如：轻松、少步行、留午休" /></label>
              <label><span>必去地点</span><textarea v-model="values.must_visit" name="must_visit" rows="2" placeholder="每行一个，可留空" /></label>
              <label><span>避开的安排</span><textarea v-model="values.avoid" name="avoid" rows="2" placeholder="例如：不爬山、不购物" /></label>
              <label><span>饮食禁忌与过敏</span><textarea v-model="values.dietary" name="dietary" rows="2" placeholder="例如：不吃辣、花生过敏" /></label>
              <label><span>补充说明</span><textarea v-model="values.note" name="note" rows="2" maxlength="2000" /></label>
              <label><span>城市提示</span><input v-model="values.city_hint" name="city_hint" maxlength="100" placeholder="地址有重名时可补充城市" /></label>
            </fieldset>
          </details>
          <button class="primary-action button-reset" type="submit" :disabled="busy || conflict">{{ busy ? '正在保存…' : editing ? '保存游玩设置' : '创建并开始对话' }}</button>
          <p v-if="editing" class="requirements-footer">保存后可以在聊天里说“生成路线”，或继续补充偏好。</p>
        </form>
      </template>
    </template>
  </section>
</template>
