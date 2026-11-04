export interface User { id: string; name: string; initials: string }

export interface TripRequirements {
  start_date: string
  end_date: string
  city: string
  day_count: number | null
  transport_mode: string
  lodging_address: string
  revision: number
  text: string
}

// The server whitelists only fields present in the saved place; {} is valid.
export interface Place {
  poi_id?: string
  name?: string
  address?: string
  longitude?: number
  latitude?: number
  city?: string
  district?: string
  source?: string
  fetched_at?: string
  selected_at?: string
  place_type?: string
  opening_hours?: string
}

export interface LodgingCandidate extends Place {
  poi_id: string
  name: string
  address: string
  city: string
  district: string
}

export interface Trip {
  id: string
  destination: string
  date_range: string
  owner: User
  status: string
  last_activity: string
  requirements: TripRequirements
  mode: string
  lodging: Place
}

// Progress is a whitelist projection, so neither it nor a step requires keys.
export interface ProgressStep {
  id?: number
  kind?: string
  label?: string
  detail?: string
  status?: string
  started_at?: number
  duration_ms?: number
}

export interface Progress {
  state?: string
  reason?: 'cancelled' | 'superseded'
  revision?: number
  started_at?: number
  updated_at?: number
  duration_ms?: number
  omitted?: number
  steps?: ProgressStep[]
}

export interface AgentAction { label: string; target: string }
export interface AgentCard {
  kind: string
  title?: string
  summary?: string
  bullets?: string[]
  actions?: AgentAction[]
}

export interface ChatMessage {
  id: string
  // Server messages have a sender; optimistic local messages do not yet.
  sender?: User
  sender_type: string
  body: string
  created_at: string
  status: string
  execution_progress?: Progress
  agent_card?: AgentCard | null
}


export interface LegacyRoute {
  origin_poi_id?: string
  origin_name?: string
  destination_poi_id?: string
  destination_name?: string
  mode?: string
  status?: string
  duration_minutes?: number | null
  distance_meters?: number | null
  source?: string
  fetched_at?: string
  reason?: string
}

export interface LegacyMeal {
  meal_type?: string
  recommendation?: string
  cuisine?: string
  budget?: string
  reservation?: string
}

export interface LegacyItem {
  id?: string
  time?: string
  title: string
  location?: string
  duration?: string
  reason?: string
  notes?: string
  satisfies?: string[]
  category?: string
  meal?: LegacyMeal | null
  place?: Place | null
  place_scope?: string
  travel_mode?: string
  incoming_route?: LegacyRoute | null
  departure_route?: LegacyRoute | null
}

export interface LegacyDay { id?: string; date?: string; label?: string; theme?: string; items: LegacyItem[] }
export interface PreparationItem { title: string; body: string; status: string }
export interface Plan {
  id: string
  title: string
  status: string
  text: string
  days: LegacyDay[]
  overview: string
  constraints_met: string[]
  pending_items: string[]
  risks: string[]
  preparation: { clothing: PreparationItem[]; accommodation: PreparationItem[] }
  requirements_revision: number | null
  display_status: string
  display_source_hash: string
  has_content: boolean
}

export type BasisStatus = 'empty' | 'fresh' | 'stale' | 'unknown'
export interface DisplayState {
  plan_id: string
  source_hash: string
  display_status: string
  version: string
  enabled: boolean
  has_text: boolean
  requirements_revision: number
  basis_status: BasisStatus
  basis_message: string
  guide_available: boolean
  guide_version: string
  busy: boolean
}

export interface Task {
  body?: string
  id?: string
  trip_id?: string
  kind?: string
  request_id?: string
  retry_of?: string
  status?: string
  created_at?: string
  updated_at?: string
  message_id?: string
  thinking_id?: string
  plan_id?: string
  requirements_revision?: number
  message: string
}

export interface UndoState { available_count: number; latest_entry_id: string; message_id: string; version: number; can_undo: boolean; reason: string }
export interface QueueState {
  current_task: Task | null
  waiting_tasks: Task[]
  paused: boolean
  pause_reason: string
  revision: number
  chat_generation: number
  draining: boolean
}
export interface WorkspaceSnapshot {
  queue: QueueState
  undo?: UndoState
  trip: Trip
  plan: Plan
  messages: ChatMessage[]
  tasks: Task[]
  metrics: Record<string, unknown>
  display_state: DisplayState
}

export interface Session { user: User | null; csrf_token: string }
export interface CityOutingValues {
  lodging_address: string
  transport_mode: string
  start_date: string
  day_count: string
  revision: string
}
export interface LodgingChoice { choice_token: string; poi_id: string }
export interface RequirementsResponse { trip: Trip; values: CityOutingValues }
export type SaveTripResponse =
  | { status: 'saved'; trip: Trip }
  | { status: 'choices_required'; choice_token: string; candidates: LodgingCandidate[]; values: CityOutingValues }
