import { ref } from 'vue'

/**
 * 登录记录（前端 localStorage 持久化）
 * 条目形状：{ user: string, role?: string, ts: number }
 */
const KEY = 'ops.auth.loginLog'
const MAX = 10

function load(): any[] {
  try { return JSON.parse(localStorage.getItem(KEY) || '[]') } catch { return [] }
}
function save(arr: any[]) {
  try { localStorage.setItem(KEY, JSON.stringify(arr)) } catch { /* ignore */ }
}

export const loginLog = ref<any[]>(load())

export function recordLogin(entry: { user: string; role?: string }) {
  if (!entry?.user) return
  loginLog.value.unshift({ ...entry, ts: Date.now() })
  if (loginLog.value.length > MAX) loginLog.value.length = MAX
  save(loginLog.value)
}

export function clearLoginLog() {
  loginLog.value = []
  save(loginLog.value)
}

/** 友好时间：刚刚 / N 分钟前 / 今天 HH:mm / 昨天 HH:mm / M-D HH:mm */
export function formatRelative(ts: number): string {
  const now = Date.now()
  const diff = now - ts
  if (diff < 60_000) return '刚刚'
  if (diff < 60 * 60_000) return `${Math.floor(diff / 60_000)} 分钟前`
  const d = new Date(ts)
  const nowD = new Date()
  const sameDay = d.toDateString() === nowD.toDateString()
  if (sameDay) return `今天 ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
  const yest = new Date(now - 86_400_000)
  if (d.toDateString() === yest.toDateString()) return `昨天 ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
  return `${d.getMonth() + 1}-${d.getDate()} ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
}
