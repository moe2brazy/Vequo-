<script setup lang="ts">
/**
 * NL2Python 深度计算面板（P0-1）
 *
 * 对标 Microsoft Fabric Code Interpreter / Vanna Python 沙箱 / ThoughtSpot 可检查 Python：
 * 竞品的共同点不是"能跑代码"，而是**代码先给用户看、确认后才执行**。
 * 所以这里强制三段式：生成代码 → 用户审阅确认 → 沙箱执行。
 *
 * 安全说明（后端 python_sandbox 已做，前端只做呈现）：
 *  - 独立子进程 + 硬超时，死循环不会拖垮服务
 *  - 导入白名单 / 内建裁剪 / 文件与网络 IO 封禁
 *  - 数据只读注入（df），无数据库连接、无写回通道
 */
import { ref } from 'vue'

const props = defineProps<{
  query: string
  columns: string[]
  rows: any[]
}>()

type Stage = 'idle' | 'planning' | 'review' | 'running' | 'done' | 'error'
const stage = ref<Stage>('idle')
const code = ref('')
const explanation = ref('')
const errorMsg = ref('')
const stdout = ref('')
const result = ref<any>(null)
const elapsed = ref(0)
const rowCount = ref(0)

function authHeaders() {
  return { 'Content-Type': 'application/json' }
}

// 回传上限：大结果集没必要整包送去做统计，既省带宽也避免请求体过大被网关拒
const MAX_SEND_ROWS = 2000
const sendRows = () => (props.rows || []).slice(0, MAX_SEND_ROWS)

async function generateCode() {
  stage.value = 'planning'
  errorMsg.value = ''
  try {
    const res = await fetch('/api/agent/python/plan', {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ query: props.query, columns: props.columns, rows: sendRows() }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data?.detail || `生成失败(${res.status})`)
    if (!data.success) throw new Error(data.error || '代码生成失败')
    code.value = data.code || ''
    explanation.value = data.explanation || ''
    rowCount.value = data.row_count || 0
    stage.value = 'review'
  } catch (e: any) {
    errorMsg.value = e?.message || '网络错误'
    stage.value = 'error'
  }
}

async function runCode() {
  stage.value = 'running'
  errorMsg.value = ''
  try {
    const res = await fetch('/api/agent/python/run', {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ code: code.value, query: props.query, columns: props.columns, rows: sendRows() }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data?.detail || `执行失败(${res.status})`)
    stdout.value = data.stdout || ''
    result.value = data.result ?? null
    elapsed.value = data.elapsed_ms || 0
    if (!data.success) {
      errorMsg.value = data.error || '执行失败'
      stage.value = 'error'
      return
    }
    stage.value = 'done'
  } catch (e: any) {
    errorMsg.value = e?.message || '网络错误'
    stage.value = 'error'
  }
}

function reset() {
  stage.value = 'idle'
  code.value = ''
  explanation.value = ''
  errorMsg.value = ''
  stdout.value = ''
  result.value = null
}

/** 把沙箱结果规整成可渲染的表格结构（dataframe / dict / list 三种常见形态） */
function asTable(v: any): { columns: string[]; rows: any[] } | null {
  if (!v) return null
  if (v.__kind__ === 'dataframe' && Array.isArray(v.rows)) {
    return { columns: v.columns || [], rows: v.rows }
  }
  if (v.__kind__ === 'series' && v.data && typeof v.data === 'object') {
    return { columns: ['名称', v.name || '值'], rows: Object.entries(v.data).map(([k, val]) => ({ '名称': k, [v.name || '值']: val })) }
  }
  if (Array.isArray(v)) {
    if (!v.length) return null
    if (v.every((x) => x && typeof x === 'object' && !Array.isArray(x))) {
      const cols = Array.from(new Set(v.flatMap((x) => Object.keys(x)))).slice(0, 20)
      return { columns: cols, rows: v.slice(0, 200) }
    }
    return { columns: ['值'], rows: v.slice(0, 200).map((x) => ({ 值: x })) }
  }
  if (typeof v === 'object') {
    return { columns: ['项', '值'], rows: Object.entries(v).slice(0, 200).map(([k, val]) => ({ 项: k, 值: val })) }
  }
  return null
}

const resultText = () => {
  const r = result.value
  if (r === null || r === undefined) return ''
  if (typeof r === 'object') return ''
  return String(r)
}
</script>

<template>
  <div class="mt-2 border border-slate-200 rounded-xl overflow-hidden bg-white">
    <!-- 入口：未开始时只显示一个按钮，避免默认占用版面 -->
    <div v-if="stage === 'idle'" class="px-3 py-2">
      <button
        @click="generateCode"
        class="px-2.5 py-1 text-[11.5px] rounded-full border border-slate-300 text-slate-600 hover:bg-slate-50 hover:border-slate-400 transition"
        title="SQL 算不出的统计（相关性、聚类、分位数、假设检验…）交给库外 Python 计算"
      >
        用 Python 深度计算
      </button>
    </div>

    <!-- 生成中 -->
    <div v-else-if="stage === 'planning'" class="px-3 py-2.5 text-[11.5px] text-slate-500 flex items-center gap-2">
      <span class="inline-block w-3 h-3 border-2 border-slate-300 border-t-indigo-500 rounded-full animate-spin"></span>
      正在生成计算代码…
    </div>

    <!-- 代码审阅 + 二次确认（执行前的必经闸门） -->
    <div v-else-if="stage === 'review'">
      <div class="px-3 py-2 bg-slate-50 border-b border-slate-200 flex items-center justify-between gap-2">
        <div class="text-[11.5px] text-slate-600 min-w-0">
          <span class="font-medium text-slate-700">Python 计算方案</span>
          <span v-if="explanation" class="text-slate-500"> · {{ explanation }}</span>
        </div>
        <span class="text-[10.5px] text-slate-400 whitespace-nowrap">{{ rowCount }} 行数据</span>
      </div>
      <pre class="px-3 py-2.5 text-[11px] leading-relaxed bg-slate-900 text-slate-100 overflow-auto max-h-72 whitespace-pre"><code>{{ code }}</code></pre>
      <div class="px-3 py-2 flex items-center gap-2 border-t border-slate-200">
        <button
          @click="runCode"
          class="px-3 py-1 text-[11.5px] bg-indigo-600 text-white rounded-full hover:bg-indigo-700 transition"
        >确认执行（沙箱 · 只读）</button>
        <button
          @click="reset"
          class="px-2.5 py-1 text-[11.5px] text-slate-500 hover:bg-slate-100 rounded-full transition"
        >取消</button>
        <span class="text-[10.5px] text-slate-400">沙箱内禁用文件 / 网络 / 数据库访问</span>
      </div>
    </div>

    <!-- 执行中 -->
    <div v-else-if="stage === 'running'" class="px-3 py-2.5 text-[11.5px] text-slate-500 flex items-center gap-2">
      <span class="inline-block w-3 h-3 border-2 border-slate-300 border-t-indigo-500 rounded-full animate-spin"></span>
      沙箱执行中…
    </div>

    <!-- 结果 / 报错 -->
    <div v-else>
      <div class="px-3 py-2 bg-slate-50 border-b border-slate-200 flex items-center justify-between gap-2">
        <span class="text-[11.5px] font-medium" :class="stage === 'error' ? 'text-rose-600' : 'text-slate-700'">
          {{ stage === 'error' ? '执行未通过' : '计算结果' }}
        </span>
        <div class="flex items-center gap-2">
          <span v-if="elapsed" class="text-[10.5px] text-slate-400">{{ elapsed }} ms</span>
          <button @click="reset" class="text-[11px] text-slate-500 hover:bg-slate-200 rounded-full px-2 py-0.5 transition">收起</button>
        </div>
      </div>

      <div v-if="errorMsg" class="px-3 py-2 text-[11.5px] text-rose-600 whitespace-pre-wrap">{{ errorMsg }}</div>

      <div v-if="stdout" class="px-3 py-2 text-[11.5px] text-slate-700 whitespace-pre-wrap leading-relaxed">{{ stdout }}</div>

      <!-- 结构化结果：优先表格，标量直接文本 -->
      <div v-if="asTable(result)" class="px-3 pb-2 overflow-auto max-h-72">
        <table class="w-full text-left text-[11px]">
          <thead>
            <tr class="text-slate-400 border-b border-slate-100">
              <th v-for="c in asTable(result)!.columns" :key="c" class="py-1 pr-3 font-normal whitespace-nowrap">{{ c }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(r, i) in asTable(result)!.rows" :key="i" class="border-b border-slate-50 last:border-0">
              <td v-for="c in asTable(result)!.columns" :key="c" class="py-1 pr-3 text-slate-600 whitespace-nowrap">{{ r?.[c] }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-else-if="resultText()" class="px-3 pb-2 text-[11.5px] text-slate-700">{{ resultText() }}</div>

      <!-- 失败时保留代码，方便改后重试 -->
      <div v-if="stage === 'error' && code" class="px-3 pb-2">
        <button @click="stage = 'review'" class="text-[11px] text-indigo-600 hover:underline">返回修改代码</button>
      </div>
    </div>
  </div>
</template>
