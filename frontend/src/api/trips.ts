import { apiRequest } from './client'
import type { CityOutingValues, RequirementsResponse, SaveTripResponse, Trip } from '@/types'

export const tripsApi = {
  list: () => apiRequest<{ trips: Trip[] }>('/api/trips'),
  remove: (id: string) => apiRequest<{ deleted: boolean }>(`/api/trips/${encodeURIComponent(id)}`, { method: 'DELETE', body: '{}' }),
  requirements: (id: string) => apiRequest<RequirementsResponse>(`/api/trips/${encodeURIComponent(id)}/requirements`),
  save: (id: string | undefined, values: CityOutingValues) => apiRequest<SaveTripResponse>(
    id ? `/api/trips/${encodeURIComponent(id)}/requirements` : '/api/trips',
    { method: id ? 'PUT' : 'POST', body: JSON.stringify(values) },
  ),
}
