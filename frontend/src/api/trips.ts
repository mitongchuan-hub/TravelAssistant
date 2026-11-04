import { apiRequest } from './client'
import type { CityOutingValues, LodgingChoice, RequirementsResponse, SaveTripResponse, Trip } from '@/types'

export const tripsApi = {
  list: () => apiRequest<{ trips: Trip[] }>('/api/trips'),
  remove: (id: string) => apiRequest<{ deleted: boolean }>(`/api/trips/${encodeURIComponent(id)}`, { method: 'DELETE', body: '{}' }),
  requirements: (id: string) => apiRequest<RequirementsResponse>(`/api/trips/${encodeURIComponent(id)}/requirements`),
  save: (values: CityOutingValues | LodgingChoice) => apiRequest<SaveTripResponse>(
    '/api/trips',
    { method: 'POST', body: JSON.stringify(values) },
  ),
}
