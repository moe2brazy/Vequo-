// 安全 localStorage 写入 + 配额（QuotaExceeded）自动治理
// 背景（2026-09-03）：AskPage 历史/消息中心等会把 localStorage 写满，
// 登录写入 sqlbot_auth_user 无保护时直接抛 "Failed to execute 'setItem' … quota" → 登录失败。
// 本模块：任何 setItem 失败超配额时先自动清理（瘦身问析历史），再重试一次。

export const ASK_HISTORY_PREFIX = 'ask_chat_history:'

export function isQuotaErr(e: any): boolean {
  return !!e && (e?.name === 'QuotaExceededError' || e?.code === 22 || /quota|exceeded/i.test(String(e?.message || '')))
}

/** 瘦身一段问析历史 JSON（纯函数）：最近 N 会话 × 每会话最近 M 条，丢弃 thinking 大字段、截断 result */
function slimChatHistory(raw: string, keepChats = 6, keepMsgs = 25, resultCap = 40000): string | null {
  try {
    const arr = JSON.parse(raw)
    if (!Array.isArray(arr)) return null
    const out = arr.slice(0, keepChats).map((c: any) => {
      // 2026-10-01 修复：问析历史数组是「最新在前」（unshift 写入），
      // slice(-keepChats) 保留的其实是最旧的几个会话，改为 slice(0, keepChats)
      const msgs = (c?.messages || []).slice(-keepMsgs).map((m: any) => {
        const n: any = { ...m }
        delete n.thinking // 推理过程可再生成，体积最大
        if (typeof n.result === 'string' && n.result.length > resultCap) n.result = n.result.slice(0, resultCap)
        return n
      })
      return { ...c, messages: msgs }
    })
    return JSON.stringify(out)
  } catch {
    return null
  }
}

/** 配额满自动治理：瘦身问析历史；仍不够时删体积最大的问析历史 key */
export function pruneLocalStorage(): void {
  try {
    const keys: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)
      if (k && k.startsWith(ASK_HISTORY_PREFIX)) keys.push(k)
    }
    // 第一轮：逐 uid 瘦身（整体重写为更小体积）
    for (const k of keys) {
      const raw = localStorage.getItem(k)
      if (!raw) continue
      const slim = slimChatHistory(raw)
      if (slim && slim.length < raw.length) {
        try { localStorage.setItem(k, slim) } catch { /* 忽略单项失败 */ }
      }
    }
    // 第二轮：仍占最大者整删（尽量少删，先删体积最大的一个；太小的 key 删了也无济于事）
    // 2026-10-01 加固：删除前先把瘦身副本归档到 _last_pruned（尽力而为），
    // 降低用户整段对话被静默清空的损失；体积 <64KB 的 key 跳过不删
    let biggest = ''
    let biggestLen = 0
    for (const k of keys) {
      const len = (localStorage.getItem(k) || '').length
      if (len > biggestLen) { biggestLen = len; biggest = k }
    }
    if (biggest && biggestLen > 65536) {
      try {
        const raw = localStorage.getItem(biggest)
        if (raw) {
          const archived = slimChatHistory(raw, 2, 10, 8000)
          if (archived) localStorage.setItem(ASK_HISTORY_PREFIX + '_last_pruned', archived)
        }
      } catch { /* 归档失败不阻塞清理 */ }
      try { localStorage.removeItem(biggest) } catch { /* ignore */ }
    }
  } catch { /* 治理本身失败不抛，让调用方感知 */ }
}

/**
 * 安全写入：超配额时先自动治理再重试一次。
 * @returns 是否写入成功
 */
export function safeSetItem(key: string, value: string): boolean {
  try {
    localStorage.setItem(key, value)
    return true
  } catch (e: any) {
    if (!isQuotaErr(e)) return false
    console.warn('[storage] 本地存储配额满，自动清理后重试…')
    pruneLocalStorage()
    try {
      localStorage.setItem(key, value)
      return true
    } catch (e2: any) {
      console.error('[storage] 配额清理后仍写入失败:', e2?.name || e2)
      return false
    }
  }
}
