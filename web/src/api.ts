import { mediaErrorMessage } from './mediaErrors'

export interface User {
  id: number
  username: string
  role: 'guest' | 'admin' | 'superadmin'
  pending_delete?: boolean
}

export interface RoomInfo {
  id: string
  name: string
  description: string
  position: number
  participants?: ParticipantInfo[]
  participant_count?: number
}

export interface ParticipantInfo {
  identity: string
  name: string
  tracks?: string[]
  is_speaking?: boolean
  audio_muted?: boolean
}

export interface InviteInfo {
  id: number
  invite_code?: string | null
  max_uses: number
  uses?: number
  expires_at: string
  revoked?: number
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options,
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
  })
  const body = response.status === 204 ? undefined : await response.json().catch(() => undefined)
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event('rikumo:auth-expired'))
    const fallback: Record<number, string> = {
      400: '提交的信息有误，请检查后重试。',
      401: '登录失败或登录状态已失效，请重新登录。',
      403: '没有权限进行此操作。',
      404: '请求的内容不存在或已删除。',
      405: '此操作暂不支持，请刷新页面后重试。',
      409: '信息发生冲突，请刷新后重试。',
      422: '填写的信息格式不正确，请检查后重试。',
      429: '操作过于频繁，请稍后重试。',
    }
    const detail = body?.detail
    const translated = typeof detail === 'string' ? mediaErrorMessage(new Error(detail)) : ''
    throw new Error(translated && translated !== '操作失败，请稍后重试。'
      ? translated
      : fallback[response.status] ?? (response.status >= 500
        ? '服务暂时不可用，请稍后重试。'
        : '请求失败，请稍后重试。'))
  }
  return body as T
}

export const post = (body?: unknown): RequestInit => ({
  method: 'POST',
  body: body === undefined ? undefined : JSON.stringify(body),
})
