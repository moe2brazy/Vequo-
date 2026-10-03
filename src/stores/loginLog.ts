import { ref } from 'vue'

/**
 * 登录记录（前端 localStorage 持久化）
 * 条目形状：{ user: string, role?: string, ts: number }
 */
const KEY = 'ops.auth.loginLog'
const MAX = 10

function load(): any[] {
  // 2026-10-03 修复（P2）：原实现只 try 了 JSON.parse 的**语法**错，兜不住**形状**不对
  // 的数据（如值为 "null"、被写成 {}、或后续版本换成对象结构）—— parse 正常返回，
  // 原样赋给 ref。此后 App.vue 的 recordLogin() 执行 loginLog.value.unshift(...) 抛
  // TypeError，且该异常发生在 handleLoginSuccess 内、由 OpsLogin 的 @success 同步调用 →
  // 抛出后跳转工作区的语句永不执行 → **密码正确、点了登录、什么都没发生，也没红字提示**。
  // 同一个存储层里 chat.ts 本来就有形状校验，这里属于两种写法不一致。
  // 另：清过一次浏览器存储后同样触发，极难定位（解析成功、无错误日志）。
  try {
    const d = JSON.parse(localStorage.getItem(KEY) || '[]')
    return Array.isArray(d) ? d : []
  } catch { return [] }
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
