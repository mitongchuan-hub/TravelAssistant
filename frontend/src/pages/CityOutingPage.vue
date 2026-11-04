<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { tripsApi } from '@/api/trips'
import type { CityOutingValues, LodgingCandidate } from '@/types'

const router = useRouter()
const values = reactive<CityOutingValues>({
  lodging_address: '', transport_mode: '', start_date: '', day_count: '', revision: '',
})
const candidates = ref<LodgingCandidate[]>([])
const choiceToken = ref('')
const selectedPoi = ref('')
const busy = ref(false)
const error = ref('')
function resetChoices() {
  candidates.value = []
  choiceToken.value = ''
  selectedPoi.value = ''
}
async function submit() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    // Only the server-issued choice token and candidate identity confirm a place.
    // The server retains the draft; client coordinates are never accepted or sent.
    const payload = candidates.value.length
      ? { choice_token: choiceToken.value, poi_id: selectedPoi.value }
      : {
          lodging_address: String(values.lodging_address), transport_mode: String(values.transport_mode),
          start_date: String(values.start_date), day_count: String(values.day_count), revision: String(values.revision),
        }
    const result = await tripsApi.save(payload)
    if (result.status === 'choices_required') {
      candidates.value = result.candidates
      choiceToken.value = result.choice_token
      selectedPoi.value = ''
      Object.assign(values, result.values)
    } else {
      await router.replace(`/workspace/${encodeURIComponent(result.trip.id)}?tab=chat`)
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '保存失败，请重试。'
  } finally { busy.value = false }
}
</script>

<template>
  <section class="page-stack city-outing-screen">
    <header class="page-header">
      <RouterLink class="text-link" to="/trips">返回</RouterLink>
      <div><p class="muted">从住处出发，发现这座城</p><h1>今天，想去哪儿逛逛？</h1></div>
    </header>
    <div v-if="error" class="form-error" role="alert"><p>{{ error }}</p></div>
      <template v-if="candidates.length">
        <section class="form-helper-card"><strong>确认你的住宿位置</strong><p>请按城市和地址选择具体位置，路线会从这里出发并返回。</p></section>
        <form class="form-stack" :aria-busy="busy" @submit.prevent="submit">
          <fieldset class="lodging-choices"><legend>找到这些候选</legend>
            <label v-for="place in candidates" :key="place.poi_id" class="lodging-choice"><input v-model="selectedPoi" type="radio" name="poi_id" :value="place.poi_id" required /><span><strong>{{ place.name }}</strong><small>{{ place.city }} {{ place.district }} · {{ place.address }}</small></span></label>
          </fieldset>
          <button class="primary-action button-reset" type="submit" :disabled="busy || !selectedPoi">{{ busy ? '正在确认…' : '从这里开始' }}</button>
          <button class="lodging-reset" type="button" :disabled="busy" @click="resetChoices">重新填写地址</button>
        </form>
      </template>
      <template v-else>
        <section class="form-helper-card"><strong>选好住处、出行方式和日期，就能开始</strong><p>请填写开始日期和游玩天数，结束日期自动计算。每天从住处出发并返回。</p></section>
        <form class="form-stack trip-create-form" :aria-busy="busy" @submit.prevent="submit">
          <fieldset><legend>你的住宿</legend>
            <label><span>住宿地址或酒店名称</span><input v-model="values.lodging_address" name="lodging_address" maxlength="300" placeholder="请包含城市和具体分店，例如：杭州湖滨酒店（西湖店）" autocomplete="street-address" required /></label>
            <label><span>出行方式</span><select v-model="values.transport_mode" name="transport_mode" required><option value="" disabled>请选择</option><option value="driving">自驾</option><option value="transit">公交地铁</option></select></label>
          </fieldset>
          <fieldset><legend>什么时候逛</legend><div class="create-field-row">
            <label><span>开始日期（必填）</span><input v-model="values.start_date" type="date" name="start_date" required /></label>
            <label><span>游玩天数（必填）</span><input v-model="values.day_count" type="number" name="day_count" min="1" max="365" step="1" required /></label>
          </div></fieldset>
          <button class="primary-action button-reset" type="submit" :disabled="busy">{{ busy ? '正在保存…' : '创建并开始对话' }}</button>
        </form>
      </template>
  </section>
</template>
