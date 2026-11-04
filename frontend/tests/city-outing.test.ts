import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import CityOutingPage from '@/pages/CityOutingPage.vue'
import { apiRequest } from '@/api/client'
import { snapshot, deferred } from './fixtures'

vi.mock('@/api/client', async importOriginal => ({
  ...await importOriginal<typeof import('@/api/client')>(),
  apiRequest: vi.fn(),
}))

const api = vi.mocked(apiRequest)
const values = {
  lodging_address: '杭州湖滨酒店（西湖店）', transport_mode: 'transit', start_date: '2030-05-01', day_count: '2', revision: '',
}
const choices = {
  status: 'choices_required', choice_token: 'server-choice-token', values,
  candidates: [
    { poi_id: 'hotel-west', name: '湖滨酒店（西湖店）', city: '杭州', district: '上城区', address: '湖滨路1号', longitude: 120.15, latitude: 30.25 },
    { poi_id: 'hotel-station', name: '湖滨酒店（城站店）', city: '杭州', district: '上城区', address: '城站路2号', longitude: 120.16, latitude: 30.25 },
  ],
}
const mounted: Array<{ unmount: () => void }> = []
afterEach(() => {
  mounted.splice(0).forEach(wrapper => wrapper.unmount())
  api.mockReset()
  vi.restoreAllMocks()
})

async function creationPage() {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/trips/new', component: CityOutingPage },
    { path: '/trips', component: { template: '<div />' } },
    { path: '/workspace/:id', component: { template: '<div />' } },
  ] })
  await router.push('/trips/new')
  await router.isReady()
  const wrapper = mount(CityOutingPage, { global: { plugins: [router] } })
  mounted.push(wrapper)
  await flushPromises()
  return { wrapper, router }
}
async function fillForm(wrapper: Awaited<ReturnType<typeof creationPage>>['wrapper']) {
  for (const [name, value] of Object.entries(values)) {
    if (name !== 'revision') await wrapper.get(`[name="${name}"]`).setValue(value)
  }
}

describe('CityOuting creation mode', () => {
  it('mounts the new form without any reads or writes and exposes native required/range contracts', async () => {
    const { wrapper } = await creationPage()
    expect(api).not.toHaveBeenCalled()
    expect(wrapper.get('h1').text()).toBe('今天，想去哪儿逛逛？')
    for (const name of ['lodging_address', 'transport_mode', 'start_date', 'day_count']) {
      expect(wrapper.get(`[name="${name}"]`).attributes('required')).toBeDefined()
    }
    expect(wrapper.findAll('input, select, textarea').map(field => field.attributes('name'))).toEqual([
      'lodging_address', 'transport_mode', 'start_date', 'day_count',
    ])
    expect(wrapper.find('details').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('更多设置')
    expect(wrapper.get('[name="day_count"]').attributes()).toMatchObject({ type: 'number', min: '1', max: '365', step: '1' })
    expect(wrapper.get('[name="lodging_address"]').attributes('maxlength')).toBe('300')
    expect(wrapper.get('[name="lodging_address"]').attributes('placeholder')).toContain('城市和具体分店')
  })

  it('posts the complete draft to the creation endpoint and replaces the route on success', async () => {
    const { wrapper, router } = await creationPage()
    await fillForm(wrapper)
    const response = deferred<unknown>()
    api.mockReturnValueOnce(response.promise)
    await wrapper.get('form').trigger('submit')
    expect(api).toHaveBeenCalledTimes(1)
    expect(api.mock.calls[0]?.[0]).toBe('/api/trips')
    expect(api.mock.calls[0]?.[1]?.method).toBe('POST')
    expect(JSON.parse(String(api.mock.calls[0]?.[1]?.body))).toEqual(values)
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeDefined()
    await wrapper.get('form').trigger('submit')
    expect(api).toHaveBeenCalledTimes(1)
    response.resolve({ status: 'saved', trip: snapshot().trip })
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/workspace/trip-test?tab=chat')
  })

  it('stays on candidates without another write, then confirms only the server token and POI identity', async () => {
    const { wrapper, router } = await creationPage()
    await fillForm(wrapper)
    api.mockResolvedValueOnce(choices)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(api).toHaveBeenCalledTimes(1)
    expect(router.currentRoute.value.fullPath).toBe('/trips/new')
    expect(wrapper.findAll('input[type="radio"]')).toHaveLength(2)
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[value="hotel-station"]').setValue(true)
    api.mockResolvedValueOnce({ status: 'saved', trip: snapshot().trip })
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(api).toHaveBeenCalledTimes(2)
    expect(api.mock.calls[1]?.[0]).toBe('/api/trips')
    expect(api.mock.calls[1]?.[1]?.method).toBe('POST')
    expect(JSON.parse(String(api.mock.calls[1]?.[1]?.body))).toEqual({ choice_token: 'server-choice-token', poi_id: 'hotel-station' })
    expect(router.currentRoute.value.fullPath).toBe('/workspace/trip-test?tab=chat')
  })

  it('returns from candidates to the complete draft without a request and discards the old choice token', async () => {
    const { wrapper, router } = await creationPage()
    await fillForm(wrapper)
    api.mockResolvedValueOnce(choices)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await wrapper.get('[value="hotel-west"]').setValue(true)
    await wrapper.get('button[type="button"]').trigger('click')
    expect(api).toHaveBeenCalledTimes(1)
    expect(router.currentRoute.value.fullPath).toBe('/trips/new')
    for (const [name, value] of Object.entries(values)) {
      if (name !== 'revision') expect(wrapper.get<HTMLInputElement>(`[name="${name}"]`).element.value).toBe(value)
    }
    api.mockResolvedValueOnce(choices)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(JSON.parse(String(api.mock.calls[1]?.[1]?.body))).toEqual(values)
    expect(wrapper.findAll<HTMLInputElement>('input[type="radio"]').every(radio => !radio.element.checked)).toBe(true)
  })

  it('keeps a failed initial save draft and restores the submit button for an explicit retry', async () => {
    const { wrapper, router } = await creationPage()
    await fillForm(wrapper)
    api.mockRejectedValueOnce(new Error('住宿查询暂时不可用'))
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('住宿查询暂时不可用')
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeUndefined()
    expect(router.currentRoute.value.fullPath).toBe('/trips/new')
    for (const [name, value] of Object.entries(values)) {
      if (name !== 'revision') expect(wrapper.get<HTMLInputElement>(`[name="${name}"]`).element.value).toBe(value)
    }
    api.mockResolvedValueOnce(choices)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(JSON.parse(String(api.mock.calls[1]?.[1]?.body))).toEqual(values)
  })

  it('retains the candidate and token on failed confirmation and allows retry without losing the draft', async () => {
    const { wrapper, router } = await creationPage()
    await fillForm(wrapper)
    api.mockResolvedValueOnce(choices)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await wrapper.get('[value="hotel-west"]').setValue(true)
    api.mockRejectedValueOnce(new Error('保存失败，请重试。'))
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('保存失败，请重试。')
    expect(wrapper.get<HTMLInputElement>('[value="hotel-west"]').element.checked).toBe(true)
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeUndefined()
    expect(router.currentRoute.value.fullPath).toBe('/trips/new')
    api.mockResolvedValueOnce({ status: 'saved', trip: snapshot().trip })
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(api.mock.calls[2]?.[1]).toEqual(api.mock.calls[1]?.[1])
    expect(router.currentRoute.value.fullPath).toBe('/workspace/trip-test?tab=chat')
  })
})

describe('production creation route regression', () => {
  it('lazy-loads CityOutingPage at the authenticated new-trip route and leaves requirements redirected', async () => {
    const { router } = await import('@/router')
    const route = router.getRoutes().find(route => route.name === 'trip-new')!
    expect(route.path).toBe('/trips/new')
    expect(route.meta).toMatchObject({ requiresAuth: true, title: '新建旅行' })
    const load = route.components!.default as () => Promise<{ default: unknown }>
    expect((await load()).default).toBe(CityOutingPage)
    expect(router.getRoutes().find(route => route.name === 'requirements')?.redirect).toBeTypeOf('function')
    expect(api).not.toHaveBeenCalled()
  })
})
