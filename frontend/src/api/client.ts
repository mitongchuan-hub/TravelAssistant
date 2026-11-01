let csrfToken = ''
let sessionRequest: Promise<unknown> | null = null
let sessionGeneration = 0
let principal: string | null | undefined
let accountChanged = false
export function acknowledgeSession(): void { accountChanged = false }
let unauthorizedHandler: (() => void) | undefined
export function onUnauthorized(handler: () => void): void { unauthorizedHandler = handler }

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details: Record<string, unknown> = {}) {
    super(message)
    this.name = 'ApiError'
  }
}

export function setCsrfToken(token: string): void {
  sessionGeneration += 1
  csrfToken = token
}

async function ensureCsrf(): Promise<void> {
  if (csrfToken) return
  sessionRequest ??= apiRequest<{ csrf_token: string }>('/api/auth/session').finally(() => { sessionRequest = null })
  await sessionRequest
}

export async function apiRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  return request<T>(path, options, false)
}

async function request<T>(path: string, options: RequestInit, csrfRetried: boolean): Promise<T> {
  if (!path.startsWith('/api/')) throw new Error('API requests must use a same-origin /api/ path')
  const method = (options.method ?? 'GET').toUpperCase()
  const unsafe = !['GET', 'HEAD', 'OPTIONS'].includes(method)
  if (unsafe && accountChanged && !path.startsWith('/api/auth/')) {
    throw new ApiError(401, 'account_changed', '账号已在其他页面切换，请先确认当前账号；未提交的内容仍保留。')
  }
  if (unsafe) await ensureCsrf()
  const requestGeneration = sessionGeneration
  const sentToken = csrfToken
  const headers = new Headers(options.headers)
  headers.set('Accept', 'application/json')
  if (unsafe) {
    headers.set('Content-Type', 'application/json')
    headers.set('X-CSRF-Token', sentToken)
  }
  let response: Response
  try {
    response = await fetch(path, { ...options, method, headers, credentials: 'same-origin' })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError(0, 'network_error', '网络连接失败，请检查网络后重试。')
  }
  const contentType = response.headers.get('content-type') ?? ''
  const payload: unknown = contentType.includes('application/json') ? await response.json() : null
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith('/api/auth/')) {
      setCsrfToken('')
      unauthorizedHandler?.()
    }
    const envelope = payload as { error?: { code?: string; message?: string; [key: string]: unknown } } | null
    // A CSRF rejection is guaranteed not to have mutated state. Refresh once and
    // replay the exact body/request_id; never retry uncertain network/5xx outcomes.
    if (unsafe && !csrfRetried && requestGeneration === sessionGeneration && response.status === 403 && envelope?.error?.code === 'csrf_failed') {
      if (csrfToken === sentToken) csrfToken = ''
      await ensureCsrf()
      if (requestGeneration !== sessionGeneration) {
        throw new ApiError(403, 'session_changed', '登录状态已改变，请确认当前账号后重试。')
      }
      return request<T>(path, options, true)
    }
    throw new ApiError(response.status, envelope?.error?.code ?? 'request_failed', envelope?.error?.message ?? '请求失败，请稍后重试。', envelope?.error ?? {})
  }
  if (payload && typeof payload === 'object' && 'csrf_token' in payload && typeof payload.csrf_token === 'string') {
    if (path === '/api/auth/session') {
      // An earlier bootstrap must not replace a token rotated by login/logout.
      if (requestGeneration === sessionGeneration) {
        const session = payload as { csrf_token: string; user?: { id: string } | null }
        const nextPrincipal = session.user?.id ?? null
        if (principal !== undefined && principal !== nextPrincipal) {
          principal = nextPrincipal
          setCsrfToken(session.csrf_token)
          accountChanged = true
          unauthorizedHandler?.()
          throw new ApiError(401, 'account_changed', '账号已在其他页面切换，请先确认当前账号；未提交的内容仍保留。')
        }
        principal = nextPrincipal
        csrfToken = session.csrf_token
      }
    } else {
      const session = payload as { csrf_token: string; user?: { id: string } | null }
      principal = session.user?.id ?? null
      accountChanged = false
      setCsrfToken(payload.csrf_token)
    }
  }
  return payload as T
}
