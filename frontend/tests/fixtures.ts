import type { WorkspaceSnapshot } from '@/types'

export function snapshot(overrides: Partial<WorkspaceSnapshot> = {}): WorkspaceSnapshot {
  return {
    trip: { id: 'trip-test', destination: '杭州', date_range: '2030-05-01', budget: '500', style: '轻松', note: '', owner: { id: 'user-test', name: '测试', initials: '测' }, status: '规划中', last_activity: '', mode: 'city', lodging: {}, requirements: { start_date: '2030-05-01', end_date: '2030-05-01', arrival_time: '', departure_time: '', city: '杭州', arrival_location: '', departure_location: '', day_count: 1, travelers: 2, budget_amount: '500', budget_scope: 'total', budget_coverage: '', transport_mode: 'walking', must_visit: [], avoid: [], dietary: [], pace: 'relaxed', lodging_address: '湖滨路1号', outing_start_time: '09:00', outing_end_time: '18:00', revision: 1 } },
    plan: { id: 'plan-test', title: '西湖慢游', status: '草稿', text: '# 西湖慢游\n\n09:00 湖滨散步\n\n费用以现场为准。', days: [], overview: '', constraints_met: [], pending_items: [], risks: [], preparation: { clothing: [], accommodation: [] }, requirements_revision: 1, display_status: 'ready', display_source_hash: 'hash-a', has_content: true },
    messages: [], tasks: [], idea_cards: [], wall_cards: [], metrics: {},
    display_state: { plan_id: 'plan-test', source_hash: 'hash-a', display_status: 'ready', version: '1', enabled: true, has_text: true, requirements_revision: 1, basis_status: 'fresh', basis_message: '方案与当前游玩设置一致，出发前仍需核实。', guide_available: true, guide_version: 'guide-1', busy: false },
    ...overrides,
  }
}
export function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
