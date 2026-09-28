<template>
  <!-- 命令面板（P0-2 对标 ThoughtSpot command palette）：Ctrl+K 全局搜索 + 跳转 + 直接提问 -->
  <Teleport to="body">
    <div v-if="visible" class="fixed inset-0 z-[999] bg-black/25 flex items-start justify-center pt-[14vh]" @click.self="close">
      <div class="w-[560px] max-w-[92vw] bg-white rounded-2xl shadow-2xl border border-gray-200 overflow-hidden">
        <!-- 输入框 -->
        <div class="flex items-center gap-2 px-4 py-3 border-b border-gray-100">
          <span class="text-gray-300 text-sm shrink-0"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <circle cx="11" cy="11" r="8" /> <path d="m21 21-4.3-4.3" /> </svg></span></span>
          <input
            ref="inputEl"
            v-model="kw"
            type="text"
            :placeholder="searchPlaceholder"
            class="flex-1 text-sm outline-none placeholder:text-gray-300"
            @keydown.down.prevent="move(1)"
            @keydown.up.prevent="move(-1)"
            @keydown.enter.prevent="pick(active)"
            @keydown.esc="close"
          />
          <kbd class="text-[10px] text-gray-400 border border-gray-200 rounded px-1.5 py-0.5 bg-gray-50 shrink-0">ESC</kbd>
        </div>

        <!-- 结果列表 -->
        <div class="max-h-[46vh] overflow-y-auto py-1.5">
          <div v-if="!kw.trim()" class="px-4 py-3 text-[11px] text-gray-400">
            <p class="mb-1.5">页面导航</p>
            <div class="grid grid-cols-2 gap-1">
              <button
                v-for="(p, i) in pages"
                :key="p.key"
                class="flex items-center gap-2 px-2.5 py-2 rounded-lg text-left text-xs text-gray-600 hover:bg-gray-100"
                :class="{ 'bg-violet-50 text-violet-700': i === active }"
                @mouseenter="active = i"
                @click="pick(i)"
              ><span class="w-4 text-center">{{ p.icon }}</span>{{ p.label }}</button>
            </div>
            <p class="mt-2 mb-1 text-gray-400">历史问题</p>
            <button
              v-for="(h, i) in historyQ"
              :key="'h' + i"
              class="block w-full px-2.5 py-1.5 rounded-lg text-left text-xs text-gray-500 hover:bg-gray-100 truncate"
              :class="{ 'bg-violet-50 text-violet-700': pages.length + i === active }"
              @mouseenter="active = pages.length + i"
              @click="pick(pages.length + i)"
            ><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="m3 21 1.9-5.7a8.5 8.5 0 1 1 3.8 3.8z" /> </svg></span> {{ h }}</button>
            <p v-if="!historyQ.length" class="text-[11px] text-gray-300">暂无历史提问</p>
          </div>

          <template v-else>
            <!-- 直接提问 -->
            <button
              class="flex items-center gap-2 w-full px-4 py-2.5 text-left text-sm text-gray-700 hover:bg-violet-50"
              :class="{ 'bg-violet-50': active === 0 }"
              @mouseenter="active = 0"
              @click="pick(0)"
            >
              <span class="text-violet-500 text-xs shrink-0">提问</span>
              <span class="truncate">{{ kw }}</span>
            </button>

            <p v-if="tables.length" class="px-4 pt-2 pb-1 text-[11px] text-gray-400">数据表</p>
            <button
              v-for="(t, i) in tables"
              :key="'t' + i"
              class="flex items-center gap-2 w-full px-4 py-2 text-left hover:bg-gray-50"
              :class="{ 'bg-violet-50': active === 1 + i }"
              @mouseenter="active = 1 + i"
              @click="pick(1 + i)"
            >
              <span class="text-[10px] px-1.5 py-0.5 rounded bg-blue-50 text-blue-600 shrink-0">表</span>
              <span class="text-xs text-gray-700 truncate">{{ t.label }}</span>
              <span class="text-[10px] text-gray-300 truncate ml-auto max-w-[40%]">{{ t.table_name }}</span>
            </button>

            <p v-if="metrics.length" class="px-4 pt-2 pb-1 text-[11px] text-gray-400">指标口径</p>
            <button
              v-for="(m, i) in metrics"
              :key="'m' + i"
              class="flex items-center gap-2 w-full px-4 py-2 text-left hover:bg-gray-50"
              :class="{ 'bg-violet-50': active === 1 + tables.length + i }"
              @mouseenter="active = 1 + tables.length + i"
              @click="pick(1 + tables.length + i)"
            >
              <span class="text-[10px] px-1.5 py-0.5 rounded bg-amber-50 text-amber-600 shrink-0">指标</span>
              <span class="text-xs text-gray-700 truncate">{{ m.name }}</span>
            </button>

            <p v-if="!tables.length && !metrics.length" class="px-4 py-3 text-[11px] text-gray-300">按回车直接提问</p>
          </template>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import { isAdmin } from '../auth'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'navigate', page: string): void; (e: 'ask', q: string): void }>()

const kw = ref('')
const active = ref(0)
const inputEl = ref<HTMLInputElement | null>(null)
const tables = ref<any[]>([])
const metrics = ref<any[]>([])
const historyQ = ref<string[]>([])
const searchTimer = ref<any>(null)

const PAGE_ITEMS = [
  { key: 'overview', label: '总览', icon: '▦' },
  { key: 'ask', label: '智能问析', icon: '◌' },
  { key: 'data', label: '数据资源', icon: '▤' },
  { key: 'knowledge', label: '业务知识', icon: '⌘' },
  { key: 'metrics', label: '指标口径', icon: '∑' },
  { key: 'permission', label: '权限管理', icon: '☰' },
  { key: 'account', label: '账户设置', icon: '◉' },
  { key: 'settings', label: '系统设置', icon: '⚙' },
]
// 管理专区页面：面板导航与「命中表」都在这里挡掉。业务人员不来这一套 ——
// 尤其「数据资源」页是库表结构/字段字典，摆给业务人员看纯属干扰。
const ADMIN_ONLY_PAGE_KEYS = ['data', 'metrics', 'permission', 'settings']
function allowedPages() {
  return isAdmin() ? PAGE_ITEMS : PAGE_ITEMS.filter((p) => !ADMIN_ONLY_PAGE_KEYS.includes(p.key))
}
// 用 ref 而非 computed：isAdmin() 读 localStorage（非响应式），computed 会把登录前算出的
// 结果一直缓存；面板每次打开时刷新一次，登录/登出后重新打开就是对的。
const pages = ref(allowedPages())
// 占位文案跟着能搜到的东西走：业务人员这边没有表和指标口径可搜，就别提着
const searchPlaceholder = computed(() => isAdmin()
  ? '搜索表 / 指标 / 页面，或直接输入问题…'
  : '搜索页面，或直接输入问题…')

// 结果总长（键盘导航范围）
const totalLen = computed(() => {
  if (!kw.value.trim()) return pages.value.length + historyQ.value.length
  return 1 + tables.value.length + metrics.value.length
})

const loadHistory = () => {
  try {
    const data = localStorage.getItem('ask_chat_history')
    if (!data) { historyQ.value = []; return }
    const chats = JSON.parse(data)
    const qs: string[] = []
    for (const c of Array.isArray(chats) ? chats : []) {
      const msgs = c?.messages || []
      for (const m of msgs) {
        if (m?.role === 'user' && m?.content && !qs.includes(m.content)) {
          qs.push(String(m.content).slice(0, 40))
          if (qs.length >= 6) break
        }
      }
      if (qs.length >= 6) break
    }
    historyQ.value = qs
  } catch {
    historyQ.value = []
  }
}

// 搜索请求序号：防竞态（P2 修复）——慢的旧请求晚于新请求返回时，不覆盖新关键字结果
let searchSeq = 0

const doSearch = async () => {
  const q = kw.value.trim()
  if (!q) { tables.value = []; metrics.value = []; return }
  // 库表与指标口径都是管理专区的内容：非管理员不拉取（搜到了也只能跳进没权限的页），
  // 面板里只留「直接提问」和页面导航 —— 业务人员要数据，问就是了。
  if (!isAdmin()) { tables.value = []; metrics.value = []; return }
  const mySeq = ++searchSeq
  try {
    const [tRes, mRes] = await Promise.all([
      fetch(`/api/tables/search?q=${encodeURIComponent(q)}`).then(r => r.json()).catch(() => ({ tables: [] })),
      fetch(`/api/metrics/search?q=${encodeURIComponent(q)}&limit=5`).then(r => r.json()).catch(() => ({ metrics: [] })),
    ])
    if (mySeq !== searchSeq) return   // 已被更新的搜索取代，丢弃本次结果
    tables.value = (tRes.tables || []).slice(0, 6)
    metrics.value = (mRes.metrics || []).slice(0, 4)
  } catch {
    if (mySeq !== searchSeq) return
    tables.value = []
    metrics.value = []
  }
}

watch(kw, () => {
  active.value = 0
  if (searchTimer.value) clearTimeout(searchTimer.value)
  if (!kw.value.trim()) { tables.value = []; metrics.value = []; return }
  searchTimer.value = setTimeout(doSearch, 180)  // 输入防抖，避免每击键一次请求
})

watch(() => props.visible, (v) => {
  if (v) {
    pages.value = allowedPages()   // 每次打开重算：期间可能登录/登出过，身份变了菜单范围跟着变
    kw.value = ''
    active.value = 0
    tables.value = []
    metrics.value = []
    loadHistory()
    nextTick(() => inputEl.value?.focus())
  }
})

const move = (d: number) => {
  const n = totalLen.value
  if (n <= 0) return
  active.value = (active.value + d + n) % n
}

const pick = (idx: number) => {
  const q = kw.value.trim()
  if (!q) {
    // 无输入 → 页面导航 + 历史问题
    if (idx < pages.value.length) {
      emit('navigate', pages.value[idx].key)
    } else if (historyQ.value[idx - pages.value.length]) {
      emit('ask', historyQ.value[idx - pages.value.length])
    }
    emit('close')
    return
  }
  if (idx === 0) {
    // 直接提问
    emit('ask', q)
    emit('close')
    return
  }
  const tIdx = idx - 1
  if (tIdx < tables.value.length) {
    // 命中表 → 跳数据资源页（可后续扩展为直接打开表）
    emit('navigate', 'data')
    emit('close')
    return
  }
  const mIdx = tIdx - tables.value.length
  if (mIdx < metrics.value.length) {
    emit('navigate', 'metrics')
    emit('close')
  }
}

const close = () => emit('close')

onMounted(() => {
  loadHistory()
})
</script>
