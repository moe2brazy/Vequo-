import { ref, watch } from 'vue'

/**
 * 业务知识收藏（前端 localStorage 持久化）
 * 后端 /api/knowledge/favorite 不可用时的兜底存储。条目形状：
 *   { kind: 'object'|'metric'|'term', key: string, label: string, scene?: string, ts: number }
 */
const KEY = 'ops.knowledge.favorites'

function load(): any[] {
  // 2026-10-03 修复（与 loginLog.ts 同型）：补 Array.isArray 形状校验。
  // 非数组的合法 JSON（"null" / {}）会被原样赋给 ref，随后 list.value.some/unshift
  // 抛错 → KnowledgePage 渲染期直接白屏。
  try {
    const d = JSON.parse(localStorage.getItem(KEY) || '[]')
    return Array.isArray(d) ? d : []
  } catch { return [] }
}
function save(arr: any[]) {
  try { localStorage.setItem(KEY, JSON.stringify(arr)) } catch { /* ignore */ }
}

const list = ref<any[]>(load())
watch(list, (v) => save(v), { deep: true })

export const knowledgeFavorites = list

export function isFavorite(kind: string, key: string): boolean {
  return list.value.some((f) => f.kind === kind && f.key === key)
}

export function toggleFavorite(item: { kind: string; key: string; label: string; scene?: string }) {
  if (!item || !item.kind || !item.key || String(item.key) === 'undefined') return
  const idx = list.value.findIndex((f) => f.kind === item.kind && f.key === item.key)
  if (idx >= 0) {
    list.value.splice(idx, 1)
  } else {
    list.value.unshift({ ...item, ts: Date.now() })
    // 上限 200 条，避免无限增长
    if (list.value.length > 200) list.value.length = 200
  }
}

export function removeFavorite(kind: string, key: string) {
  const idx = list.value.findIndex((f) => f.kind === kind && f.key === key)
  if (idx >= 0) list.value.splice(idx, 1)
}

export function clearFavorites() {
  list.value = []
}
