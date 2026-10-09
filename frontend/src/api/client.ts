const JSON_HEADERS = { 'Content-Type': 'application/json' }

export function notifyError(message: string) {
  const text = message === 'bilibili_replace_outdated_first'
    ? '上次追加被旧规则拦截，请点击“重试B站追加”继续'
    : message
  if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('app-error', { detail: text }))
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, options)
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`
    try {
      const body = await response.json()
      detail = typeof body.detail === 'string' ? body.detail : body.detail?.message || detail
    } catch { /* response is not JSON */ }
    notifyError(detail)
    throw new Error(detail)
  }
  return (response.status === 204 ? undefined : await response.json()) as T
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) => request<T>(path, {
    method: 'POST',
    headers: body === undefined ? undefined : JSON_HEADERS,
    body: body === undefined ? undefined : JSON.stringify(body),
  }),
  patch: <T>(path: string, body: unknown) => request<T>(path, { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) => request<T>(path, { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify(body) }),
  delete: (path: string) => request<void>(path, { method: 'DELETE' }),
  upload: <T>(path: string, form: FormData) => request<T>(path, { method: 'POST', body: form }),
}
