<script setup lang="ts">
/**
 * 一键生成看板（P0-3）
 *
 * 对标 ThoughtSpot SpotterViz / 帆软 FineBI 智能仪表板：Agent 把一个分析主题
 * 拆成多个互补视角，每个视角一张卡片（图 + 一句洞察），按栅格排布成整页看板。
 *
 * 复用现有能力，不重复造轮子：
 *  - 卡片图型由后端「数据形态驱动选图」决定（与问答主流程同一套规则）
 *  - 图表渲染直接用双引擎（ECharts + AntV G2Plot），与 AskPage 完全一致
 *  - 取数走确定性编译优先，卡片上标注 sql_source（compiled / llm）便于审计
 */
import { ref, nextTick, onBeforeUnmount } from 'vue'
import { renderChart, disposeChart, resizeChart } from '../charts'

const props = defineProps<{ query: string }>()

const loading = ref(false)
const started = ref(false)
const errorMsg = ref('')
const title = ref('')
const cards = ref<any[]>([])
const elapsed = ref(0)
const panelRef = ref<HTMLElement | null>(null)
let observer: ResizeObserver | null = null
// 卸载守卫：生成看板是「await 取数（最长几十秒）→ 写 title/cards/elapsed → 再渲染」，
// 期间用户完全可能切页或关掉这个面板。卸载后回调继续写已销毁实例的 ref 虽不会报错，
// 但属于对失效组件的操作，统一用一个标志挡掉。
let disposed = false

async function renderCards() {
  await nextTick()
  const root = panelRef.value
  if (!root) return
  const nodes = Array.from(root.querySelectorAll<HTMLElement>('.echart'))
  await Promise.all(nodes.map(async (el) => {
    if (el.dataset.rendered) return
    el.dataset.rendered = '1'
    try {
      const type = el.dataset.type || 'bar'
      const cols = JSON.parse(el.dataset.cols || '[]')
      const rows = JSON.parse(el.dataset.rows || '[]')
      if (!cols.length || !rows.length) throw new Error('no data')
      disposeChart(el)
      const engine = await renderChart(el, type, cols, rows)
      if (!engine) throw new Error('no engine')
      el.dataset.engine = engine
    } catch {
      el.innerHTML = '<div class="text-[11px] text-slate-400 p-3">该卡片数据不足以成图</div>'
      el.dataset.engine = 'none'
    }
  }))
  if (!observer) {
    observer = new ResizeObserver(() => {
      panelRef.value?.querySelectorAll<HTMLElement>('.echart').forEach((n) => resizeChart(n))
    })
  }
  nodes.forEach((el) => {
    if (el.dataset.engine !== 'none') observer!.observe(el)
  })
}

async function generate() {
  started.value = true
  loading.value = true
  errorMsg.value = ''
  cards.value = []
  try {
    const res = await fetch('/api/dashboard/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: props.query, max_cards: 6 }),
    })
    const data = await res.json().catch(() => ({}))
    if (disposed) return
    if (!res.ok) throw new Error(data?.detail || `生成失败(${res.status})`)
    if (!data.success) throw new Error(data.error || '看板生成失败')
    title.value = data.title || props.query
    cards.value = data.cards || []
    elapsed.value = data.elapsed_ms || 0
  } catch (e: any) {
    if (disposed) return
    errorMsg.value = e?.message || '网络错误'
  } finally {
    if (disposed) return
    // 修复：必须在 loading 复位之后再渲染。
    // 模板是 v-if="!started" → v-else-if="loading" → v-else-if="errorMsg" → v-else(卡片网格)，
    // loading 为 true 时 .echart 容器尚未挂载，此时 querySelectorAll 返回空数组 → 图表永不渲染。
    loading.value = false
    await renderCards()
  }
}

onBeforeUnmount(() => {
  disposed = true
  panelRef.value?.querySelectorAll<HTMLElement>('.echart').forEach((el) => disposeChart(el))
  try { observer?.disconnect() } catch { /* 忽略 */ }
  observer = null
})

const colsJson = (c: any) => JSON.stringify(c.columns || [])
const rowsJson = (c: any) => JSON.stringify(c.rows || [])
</script>

<template>
  <div ref="panelRef" class="mt-2">
    <!-- 入口 -->
    <div v-if="!started">
      <button
        @click="generate"
        class="px-2.5 py-1 text-[11.5px] rounded-full border border-slate-300 text-slate-600 hover:bg-slate-50 hover:border-slate-400 transition"
        title="Agent 自动规划多个分析视角，生成整页看板"
      >
        <span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <line x1="18" x2="18" y1="20" y2="10" /> <line x1="12" x2="12" y1="20" y2="4" /> <line x1="6" x2="6" y1="20" y2="14" /> </svg></span> 一键生成看板
      </button>
    </div>

    <!-- 生成中 -->
    <div v-else-if="loading" class="px-3 py-3 border border-slate-200 rounded-xl text-[11.5px] text-slate-500 flex items-center gap-2">
      <span class="inline-block w-3 h-3 border-2 border-slate-300 border-t-indigo-500 rounded-full animate-spin"></span>
      正在规划分析视角并取数…
    </div>

    <!-- 报错 -->
    <div v-else-if="errorMsg" class="px-3 py-2 border border-rose-200 bg-rose-50 rounded-xl text-[11.5px] text-rose-600">
      {{ errorMsg }}
      <button @click="started = false" class="ml-2 text-indigo-600 hover:underline">收起</button>
    </div>

    <!-- 看板 -->
    <div v-else class="border border-slate-200 rounded-xl overflow-hidden bg-white">
      <div class="px-3 py-2 bg-slate-50 border-b border-slate-200 flex items-center justify-between gap-2">
        <span class="text-[12px] font-medium text-slate-700 truncate">{{ title }}</span>
        <span class="text-[10.5px] text-slate-400 whitespace-nowrap">{{ cards.length }} 张卡片 · {{ elapsed }} ms</span>
      </div>

      <div class="p-2.5 grid grid-cols-2 gap-2.5">
        <div
          v-for="(c, i) in cards"
          :key="c.id || (c.title || 'card') + '-' + i"
          :class="c.span === 2 ? 'col-span-2' : 'col-span-1'"
          class="border border-slate-100 rounded-lg p-2.5 min-w-0"
        >
          <div class="flex items-baseline justify-between gap-2 mb-1.5">
            <span class="text-[11.5px] font-medium text-slate-700 truncate">{{ c.title }}</span>
            <span
              v-if="c.sql_source === 'compiled'"
              class="text-[10px] text-blue-600 bg-blue-50 px-1.5 py-0.5 rounded whitespace-nowrap"
              title="走确定性指标编译，未依赖 LLM 生成 SQL"
            >口径编译</span>
            <span
              v-else-if="c.sql_source === 'llm'"
              class="text-[10px] text-amber-600 bg-amber-50 px-1.5 py-0.5 rounded whitespace-nowrap"
              title="未命中注册口径，由 LLM 生成（待收敛进指标注册表）"
            >LLM 兜底</span>
          </div>

          <div
            v-if="!c.error"
            class="echart"
            style="height: 220px"
            :data-type="c.chart_type"
            :data-cols="colsJson(c)"
            :data-rows="rowsJson(c)"
          ></div>
          <div v-else class="h-[220px] flex items-center justify-center text-[11px] text-slate-400 px-3 text-center">
            {{ c.error }}
          </div>

          <div v-if="c.insight" class="mt-1.5 text-[11px] text-slate-600 leading-relaxed">
            {{ c.insight }}
          </div>
          <div v-if="!c.error" class="mt-1 text-[10px] text-slate-400">{{ c.row_count }} 行</div>
        </div>
      </div>
    </div>
  </div>
</template>
