import { defineStore } from 'pinia'
import { ref } from 'vue'
import { tripsApi } from '@/api/trips'
import type { Trip } from '@/types'

export const useTripsStore = defineStore('trips', () => {
  const trips = ref<Trip[]>([])
  const loading = ref(false)
  async function load(): Promise<void> {
    loading.value = true
    try { trips.value = (await tripsApi.list()).trips } finally { loading.value = false }
  }
  async function remove(id: string): Promise<void> {
    await tripsApi.remove(id)
    trips.value = trips.value.filter(trip => trip.id !== id)
  }
  function clear(): void { trips.value = [] }
  return { trips, loading, load, remove, clear }
})
