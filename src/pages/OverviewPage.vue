<template>
  <div class="space-y-7">
    <!-- ====== 问候头部 + 问数入口（hero 卡：品牌微光 + 柔和投影） ====== -->
    <div class="ov-rise ov-hero flex items-end justify-between gap-4">
      <div>
        <div class="text-[26px] font-semibold tracking-tight text-gray-900">
          {{ greeting }}，我是 <span class="bg-gradient-to-r from-primary to-primary-dark bg-clip-text text-transparent">Vequo</span>
        </div>
        <p class="text-[13px] text-gray-400 mt-1">你的智能数据分析助手，随时为你服务</p>
      </div>
      <button
        @click="emit('navigate', 'ask')"
        class="ov-ask-btn inline-flex items-center gap-1.5 px-5 py-2.5 text-[13px] font-medium text-white rounded-full transition"
      >
        <AppIcon name="brain" :size="15" /> 去问析
      </button>
    </div>

    <!-- ====== 数据洞察图表（自动分析当前库生成，附白话解读） ====== -->
    <section class="ov-rise" style="animation-delay: 30ms">
      <div class="flex items-center justify-between mb-4 px-0.5">
        <div class="flex items-center gap-3.5">
          <span class="stat-chip" style="background: rgba(32,40,33,0.06); color: #1d2129">
            <AppIcon name="bar-chart-2" :size="20" :stroke-width="1.9" />
          </span>
          <div>
            <h2 class="text-[15px] font-semibold text-gray-900">数据洞察图表</h2>
            <p class="text-xs text-gray-400 mt-0.5">自动分析当前数据库数据生成，每张图都带白话解读</p>
          </div>
        </div>
        <button
          @click="loadCharts"
          :disabled="chartsLoading"
          class="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-[#1d2129] border border-[#c9cdd4] bg-white hover:bg-[#e8f3ff] hover:text-[#2E7CF0] rounded-full transition disabled:opacity-50"
        >
          <AppIcon name="refresh-cw" :size="13" :class="chartsLoading ? 'animate-spin' : ''" /> 刷新
        </button>
      </div>

      <div v-if="chartsLoading" class="grid gap-4 md:grid-cols-2">
        <div v-for="i in 2" :key="i" class="rounded-2xl border border-black/[0.05] bg-white p-5">
          <div class="h-4 w-32 rounded bg-gray-100 animate-pulse mb-3"></div>
          <div class="h-44 rounded-xl bg-gray-50 animate-pulse"></div>
        </div>
      </div>
      <div v-else-if="!charts.length" class="rounded-2xl border border-black/[0.05] bg-white p-8 text-center text-sm text-gray-400">
        暂无洞察图表（当前数据库数据不足以形成有意义的分布 / 趋势）
      </div>
      <div v-else class="grid gap-4 md:grid-cols-2">
        <DataChartCard v-for="c in charts" :key="c.id" :chart="c" />
      </div>
    </section>

    <!-- ====== 当前业务关注点 ====== -->
    <section class="ov-rise rounded-2xl border border-black/[0.05] bg-white p-5 shadow-[0_1px_2px_rgba(0,0,0,0.04)]" style="animation-delay: 120ms">
      <div class="flex items-start justify-between gap-4">
        <div class="flex items-center gap-3.5">
          <span class="stat-chip" style="background: rgba(255,159,10,0.12); color: #D97E06">
            <AppIcon name="activity" :size="20" :stroke-width="1.9" />
          </span>
          <div>
            <h2 class="text-[15px] font-semibold text-gray-900">当前业务关注点</h2>
            <p class="text-xs text-gray-400 mt-0.5">Vequo 根据近期数据主动发现的几个值得关注的问题</p>
          </div>
        </div>
        <span class="hidden rounded-full bg-amber-50 border border-amber-100 px-3 py-1 text-[11px] font-medium text-amber-600 sm:inline-flex">自动发现</span>
      </div>

      <!-- 骨架屏：扫描中 -->
      <div v-if="attentionLoading" class="mt-4 grid gap-3 md:grid-cols-2">
        <div v-for="i in 4" :key="i" class="rounded-xl border border-gray-100 p-4 flex items-start gap-3">
          <div class="h-9 w-9 rounded-lg bg-gray-100 animate-pulse shrink-0"></div>
          <div class="flex-1 space-y-2 pt-0.5">
            <div class="h-3 w-24 rounded bg-gray-50 animate-pulse"></div>
            <div class="h-4 w-3/4 rounded bg-gray-100 animate-pulse"></div>
            <div class="h-3 w-full rounded bg-gray-50 animate-pulse"></div>
            <div class="h-3 w-5/6 rounded bg-gray-50 animate-pulse"></div>
          </div>
        </div>
      </div>

      <div v-else-if="attentionPoints.length" class="mt-4 grid gap-3 md:grid-cols-2">
        <div
          v-for="(point, idx) in attentionPoints"
          :key="point.id"
          class="attention-card group"
          :class="toneOf(point.tone).card"
          :style="{ animationDelay: `${idx * 70}ms` }"
        >
          <span class="attention-chip" :class="toneOf(point.tone).chip">
            <AppIcon :name="toneOf(point.tone).icon" :size="17" :stroke-width="2" />
          </span>
          <div class="min-w-0 flex-1">
            <div class="text-[11px] font-medium text-gray-400">{{ point.category }} · 自动发现</div>
            <div class="mt-0.5 text-[14.5px] font-semibold text-gray-900">{{ point.title }}</div>
            <div class="mt-1 text-[13px] leading-relaxed text-gray-500">{{ point.detail }}</div>
            <div class="mt-3 flex items-start gap-1.5 border-t border-gray-50 pt-2 text-[11px] text-gray-400">
              <AppIcon name="ruler" :size="12" class="mt-px shrink-0 text-gray-300" />
              <span>判断规则：{{ point.rule }}</span>
            </div>
          </div>
        </div>
      </div>
      <div v-else class="py-10 flex flex-col items-center gap-2 text-sm text-gray-400">
        <AppIcon name="check-circle" :size="28" class="text-blue-400" :stroke-width="1.5" />
        当前没有发现需要优先关注的问题
      </div>
    </section>

    <!-- ====== 指标异动提醒（对标 Tableau Pulse：后台定时监测，主动发现） ====== -->
    <section v-if="monitorAlerts.length" class="ov-rise rounded-2xl border border-red-100 bg-white p-5 shadow-sm" style="animation-delay: 180ms">
      <div class="flex items-start justify-between gap-4">
        <div class="flex items-center gap-3.5">
          <span class="stat-chip" style="background: rgba(255,59,48,0.10); color: #E53935">
            <AppIcon name="alert-octagon" :size="20" :stroke-width="1.9" />
          </span>
          <div>
            <h2 class="text-[15px] font-semibold text-gray-900">指标异动提醒</h2>
            <p class="text-xs text-gray-400 mt-0.5">系统定时监测关键指标，变化超阈值时主动提醒——不再等您来问。</p>
          </div>
        </div>
      </div>
      <div class="mt-4 grid gap-3 md:grid-cols-2">
        <div v-for="(alert, i) in monitorAlerts" :key="alert.id || i" class="rounded-xl border border-red-100 bg-white p-4">
          <div class="flex items-start gap-3">
            <span class="text-xl">{{ alert.direction === 'down' ? '↓' : '↑' }}</span>
            <div class="min-w-0 flex-1">
              <div class="text-xs font-medium text-gray-400">{{ alert.detected_at }} · 自动监测</div>
              <div class="mt-1 font-semibold text-gray-800">{{ alert.rule_name }}</div>
              <div class="mt-1 text-sm font-medium" :class="alert.direction === 'down' ? 'text-blue-600' : 'text-red-600'">
                {{ alert.change_pct > 0 ? '+' : '' }}{{ alert.change_pct }}%（{{ alert.prev_value }} → {{ alert.value }}）
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- ====== AI 数据总览报告 ====== -->
    <section class="ov-rise rounded-2xl border border-black/[0.05] bg-white p-5 shadow-[0_1px_2px_rgba(0,0,0,0.04)]" style="animation-delay: 60ms">
      <div class="flex items-center justify-between gap-3">
        <div class="flex items-center gap-3.5">
          <span class="stat-chip" style="background: rgba(77,158,255,0.12); color: #2E7CF0">
            <AppIcon name="file-text" :size="20" :stroke-width="1.9" />
          </span>
          <div>
            <div class="text-[15px] font-semibold text-gray-900">AI 数据总览报告</div>
            <div class="text-xs text-gray-400 mt-0.5">生成单文件 HTML 报告，自动在浏览器新标签页打开</div>
          </div>
        </div>
        <div class="flex gap-2 shrink-0 items-center">
          <button
            v-if="canDo('export')"
            @click="generateReport"
            :disabled="reportLoading"
            class="inline-flex items-center gap-1.5 px-4 py-2 text-[13px] font-medium text-white bg-primary hover:bg-primary-dark rounded-full transition shadow-sm disabled:opacity-50"
          >
            <AppIcon v-if="!reportLoading" name="sparkles" :size="15" />
            <AppIcon v-else name="refresh-cw" :size="15" class="animate-spin" />
            {{ reportLoading ? '生成中…' : '生成报告' }}
          </button>
          <span v-else class="inline-flex items-center gap-1.5 text-xs text-gray-400" title="当前角色未开通「导出」操作权限。管理员可在「权限管理 → 角色 → 选择角色 → 权限 → 操作权限」中勾选「导出数据」并保存">
            <AppIcon name="lock" :size="13" /> 导出需管理员开通权限
          </span>
        </div>
      </div>
      <!-- 已生成报告 → 下载 Word / PDF 与资产沉淀 -->
      <div v-if="reportOpened" class="mt-3.5 flex flex-wrap items-center gap-2">
        <button
          @click="downloadReport('docx')"
          :disabled="!!downloadLoading"
          class="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-[#4D9EFF] hover:bg-[#2E7CF0] rounded-full transition disabled:opacity-50"
        >
          <AppIcon name="download" :size="13" />
          {{ downloadLoading === 'docx' ? '下载中…' : '下载 Word' }}
        </button>
        <button
          @click="downloadReport('pdf')"
          :disabled="!!downloadLoading"
          class="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-[#4D9EFF] hover:bg-[#2E7CF0] rounded-full transition disabled:opacity-50"
        >
          <AppIcon name="download" :size="13" />
          {{ downloadLoading === 'pdf' ? '下载中…' : '下载 PDF' }}
        </button>
        <button
          @click="saveAsAsset"
          :disabled="assetSaving"
          title="把当前报告保存为可复用资产"
          class="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-[#1d2129] border border-[#c9cdd4] bg-white hover:bg-[#e8f3ff] hover:text-[#2E7CF0] rounded-full transition disabled:opacity-50"
        >
          <AppIcon name="package" :size="13" />
          {{ assetSaving ? '保存中…' : '保存为资产' }}
        </button>
        <button
          @click="openAssets"
          class="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-[#1d2129] border border-[#c9cdd4] bg-white hover:bg-[#e8f3ff] hover:text-[#2E7CF0] rounded-full transition"
        >
          <AppIcon name="folder" :size="13" /> 历史报告
        </button>
      </div>
      <div v-if="reportOpened" class="mt-3.5 flex items-center gap-2 rounded-xl border border-blue-100 bg-blue-50/70 px-4 py-2.5 text-xs text-blue-700">
        <AppIcon name="check-circle" :size="15" class="text-blue-500" />
        报告已在浏览器新标签页打开（单文件 HTML，可保存 / 分享 / 打印）
      </div>
      <div v-if="reportError" class="mt-3 text-xs text-red-500">{{ reportError }}</div>

      <!-- 历史报告资产弹窗 -->
      <div v-if="assetsOpen" class="fixed inset-0 bg-black/30 flex items-center justify-center z-50" @click.self="assetsOpen = false">
        <div class="bg-white rounded-2xl shadow-xl w-[640px] max-w-[92vw] max-h-[80vh] flex flex-col overflow-hidden">
          <div class="flex items-center justify-between px-4 py-3 border-b border-gray-100">
            <span class="text-sm font-semibold text-gray-700">历史报告资产</span>
            <button @click="assetsOpen = false" class="text-gray-400 hover:text-gray-600 text-lg leading-none">×</button>
          </div>
          <div class="flex-1 overflow-y-auto p-3 space-y-2">
            <div v-if="!assets.length" class="text-center text-xs text-gray-400 py-8">
              暂无历史报告。先生成报告并点击「保存为资产」即可沉淀复用。
            </div>
            <div v-for="a in assets" :key="a.id" class="border border-gray-100 rounded-xl p-3">
              <div class="flex items-start justify-between gap-2">
                <div class="min-w-0">
                  <div class="text-xs font-semibold text-gray-700 truncate">{{ a.title }}</div>
                  <div class="text-[11px] text-gray-400 mt-0.5">
                    {{ a.created_at }} · {{ a.section_count }} 章节 · {{ a.created_by }}
                  </div>
                </div>
                <div class="flex gap-1.5 shrink-0">
                  <button @click="viewAsset(a)" class="px-2 py-1 text-[11px] text-[#1d2129] border border-[#c9cdd4] bg-white hover:bg-[#e8f3ff] hover:text-[#2E7CF0] rounded">查看</button>
                  <button @click="regenerateAsset(a)" :disabled="a._busy" class="px-2 py-1 text-[11px] text-white bg-[#4D9EFF] hover:bg-[#2E7CF0] rounded disabled:opacity-50">
                    {{ a._busy ? '生成中…' : '再生成' }}
                  </button>
                  <button @click="deleteAsset(a)" class="px-2 py-1 text-[11px] bg-rose-50 hover:bg-rose-100 rounded text-rose-600">删除</button>
                </div>
              </div>
              <div v-if="a._detail" class="mt-2 border-t border-gray-100 pt-2 text-[11px] text-gray-600 whitespace-pre-wrap max-h-56 overflow-y-auto">{{ a._detail }}</div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- ====== 主动洞察 / 数据盲点（P1-4 对标 SpotIQ + P1-2 对标 FineBI） ====== -->
    <section class="ov-rise" style="animation-delay: 240ms">
      <InsightsPanel @ask="(q) => emit('navigate-ask', q)" />
    </section>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onActivated } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import InsightsPanel from '../components/InsightsPanel.vue'
import DataChartCard from '../components/DataChartCard.vue'
import { canDo } from '../auth'

// ========== 北京时间问候语 ==========
function getBeijingGreeting(): string {
  const now = new Date()
  const utcHours = now.getUTCHours()
  const bjHours = (utcHours + 8) % 24

  if (bjHours >= 5 && bjHours < 12) return 'Good morning'
  if (bjHours >= 12 && bjHours < 13) return 'Good noon'
  if (bjHours >= 13 && bjHours < 18) return 'Good afternoon'
  return 'Good evening'
}

const emit = defineEmits<{
  (e: 'navigate', page: string): void
  (e: 'navigate-ask', question: string): void
}>()

// 统一请求：检查 res.ok，非 2xx 抛错（否则 401/403/500 的 JSON 体被当成功，data.xxx 为 undefined 页面静默显示空数据）
async function fetchJson(url: string, init?: RequestInit): Promise<any> {
  const res = await fetch(url, init)
  let data: any = {}
  try { data = await res.json() } catch { /* 非 JSON 响应体（如网关 HTML） */ }
  if (!res.ok) {
    const detail = data?.detail || data?.message || `HTTP ${res.status}`
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return data
}

const greeting = ref(getBeijingGreeting())

// ── AI 数据报告（单文件 HTML：自动在浏览器新标签页打开）──
const reportLoading = ref(false)
const reportError = ref('')
const reportOpened = ref(false)
// ── AI 数据报告下载（Word / PDF）──
const downloadLoading = ref('')   // '' | 'docx' | 'pdf'
// ── 报告资产沉淀（保存为资产 / 历史报告列表 / 再生成 / 删除）──
const assetSaving = ref(false)
const assetsOpen = ref(false)
const assets = ref<any[]>([])

const saveAsAsset = async () => {
  assetSaving.value = true
  reportError.value = ''
  try {
    // 2026-10-01 修复：改用 fetchJson（含 res.ok 校验），
    // 403/500 错误体不再被当成功解析、真实失败原因（权限不足等）不再被吞
    const d = await fetchJson('/api/overview/report-export', { method: 'POST' })
    if (!d.success) throw new Error(d.error || d.detail || '报告生成失败')
    const saved = await fetchJson('/api/reports', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: '数据总览报告', markdown: d.markdown, tables: Object.keys(d.tables || {}) }),
    })
    if (!saved.success) throw new Error(saved.detail || '保存失败')
    reportError.value = ''
    alert('报告已保存为资产，可在「历史报告」中查看/再生成')
  } catch (e: any) {
    reportError.value = e?.message || '保存失败'
  } finally {
    assetSaving.value = false
  }
}

const openAssets = async () => {
  assetsOpen.value = true
  await loadAssets()
}

// 报告资产三接口原先都是 `fetch(...).then(r => r.json())`：不判 res.ok，
// 403/500 的错误体（{detail: ...}）被当成功 → 展示"（无内容）"，
// 用户误以为报告为空而不是权限不足/服务异常。统一改用 fetchJson（已含 ok 校验）。
const loadAssets = async () => {
  try {
    const d = await fetchJson('/api/reports')
    assets.value = (d.reports || []).map((a: any) => ({ ...a, _busy: false, _detail: '' }))
  } catch {
    assets.value = []
  }
}

const viewAsset = async (a: any) => {
  try {
    const d = await fetchJson(`/api/reports/${a.id}`)
    a._detail = d.report?.markdown || '（无内容）'
  } catch (e: any) {
    a._detail = '加载失败：' + (e?.message || '')
  }
}

const regenerateAsset = async (a: any) => {
  a._busy = true
  a._detail = ''
  try {
    const d = await fetchJson(`/api/reports/${a.id}/regenerate`, { method: 'POST' })
    if (!d.success) throw new Error(d.detail || '再生成失败')
    a._detail = d.report?.markdown || '（无内容）'
  } catch (e: any) {
    a._detail = '再生成失败：' + (e?.message || '')
  } finally {
    a._busy = false
  }
}

const deleteAsset = async (a: any) => {
  if (!confirm('确认删除该报告资产？')) return
  try {
    // 修复（P1）：fetch 只在网络层失败时 reject，403/404/500 都会正常 resolve，
    // 原实现因此会把本地列表乐观清空（用户以为删掉了，刷新后资产又出现）。
    // 同文件的 saveAsAsset / generateReport 都做了 res.ok 校验，只有这里漏了。
    const resp = await fetch(`/api/reports/${a.id}`, { method: 'DELETE' })
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}))
      throw new Error((err as any)?.detail || `删除失败(${resp.status})`)
    }
    assets.value = assets.value.filter((x) => x.id !== a.id)
  } catch (e: any) {
    alert(e?.message || '删除失败')
  }
}

const generateReport = async () => {
  reportLoading.value = true
  reportError.value = ''
  reportOpened.value = false
  // 同步打开新窗口（必须在用户点击手势内，先于任何 await，否则浏览器拦截弹窗）
  const win = window.open('', '_blank')
  if (win) {
    win.document.write(
      '<html><head><meta charset="UTF-8"><title>生成报告</title></head>' +
      '<body style="font-family:sans-serif;display:flex;align-items:center;justify-content:center;' +
      'height:100vh;color:#6b7280;font-size:15px">正在生成报告，请稍候（约 1 分钟）…</body></html>'
    )
  }
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), 100000)
  try {
    const resp = await fetch('/api/overview/report-html', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      signal: ctrl.signal,
    })
    if (!resp.ok) {
      const text = await resp.text().catch(() => '')
      throw new Error(text.slice(0, 120) || `生成失败(${resp.status})`)
    }
    const html = await resp.text()
    if (win && !win.closed) {
      win.document.open()
      win.document.write(html)
      win.document.close()
    } else {
      const blob = new Blob([html], { type: 'text/html;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = '数据总览报告.html'
      document.body.appendChild(a)
      a.click()
      a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 30000)
    }
    reportOpened.value = true
  } catch (e: any) {
    reportError.value = (e?.name === 'AbortError' ? '生成超时（超过 100 秒），请稍后重试' : e?.message) || '生成失败'
    if (win && !win.closed) {
      try { win.close() } catch { /* ignore */ }
    }
  } finally {
    clearTimeout(timer)
    reportLoading.value = false
  }
}

const downloadReport = async (format: 'docx' | 'pdf') => {
  downloadLoading.value = format
  reportError.value = ''
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), 100000)
  try {
    const resp = await fetch(`/api/overview/report-download?format=${format}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      signal: ctrl.signal,
    })
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}))
      throw new Error(err?.detail || `导出失败(${resp.status})`)
    }
    const blob = await resp.blob()
    const url = URL.createObjectURL(blob)
    const cd = resp.headers.get('content-disposition') || ''
    const m = cd.match(/filename\*=UTF-8''([^;]+)/i)
    const fname = m ? decodeURIComponent(m[1]) : (format === 'docx' ? '数据总览报告.docx' : '数据总览报告.pdf')
    const a = document.createElement('a')
    a.href = url
    a.download = fname
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 30000)
  } catch (e: any) {
    reportError.value = (e?.name === 'AbortError' ? '导出超时（超过 100 秒），请稍后重试' : e?.message) || '导出失败'
  } finally {
    clearTimeout(timer)
    downloadLoading.value = ''
  }
}

// ── 当前业务关注点 ──
const attentionLoading = ref(false)
const attentionPoints = ref<any[]>([])

// ========== 业务关注点：按严重级别映射图标与配色 ==========
const TONE_MAP: Record<string, { icon: string; chip: string; card: string }> = {
  red: {
    icon: 'alert-octagon',
    chip: 'bg-red-100 text-red-500',
    card: 'bg-red-50/70 border-red-100 hover:border-red-200',
  },
  orange: {
    icon: 'alert-triangle',
    chip: 'bg-orange-100 text-orange-500',
    card: 'bg-orange-50/70 border-orange-100 hover:border-orange-200',
  },
  amber: {
    icon: 'alert-triangle',
    chip: 'bg-amber-100 text-amber-600',
    card: 'bg-amber-50/70 border-amber-100 hover:border-amber-200',
  },
  cyan: {
    icon: 'info',
    chip: 'bg-cyan-100 text-cyan-600',
    card: 'bg-cyan-50/70 border-cyan-100 hover:border-cyan-200',
  },
}
const toneOf = (tone: string) =>
  TONE_MAP[tone] || { icon: 'info', chip: 'bg-gray-100 text-gray-600', card: 'bg-gray-50/70 border-gray-200 hover:border-gray-300' }

// 指标异动提醒（对标 Tableau Pulse：后台定时监测，变化超阈值主动发现）
const monitorAlerts = ref<any[]>([])
const loadMonitorAlerts = async () => {
  try {
    const data = await fetchJson('/api/monitor/alerts')
    monitorAlerts.value = data.alerts || []
  } catch {
    monitorAlerts.value = []
  }
}

const loadAttentionPoints = async () => {
  attentionLoading.value = true
  try {
    const data = await fetchJson('/api/tables/attention-points')
    attentionPoints.value = data.points || []
  } catch (error) {
    console.error('加载业务关注点失败:', error)
  } finally {
    attentionLoading.value = false
  }
}

// ── 数据洞察图表（自动分析当前库，附白话解读） ──
const charts = ref<any[]>([])
const chartsLoading = ref(false)
const loadCharts = async () => {
  chartsLoading.value = true
  try {
    const data = await fetchJson('/api/overview/charts')
    charts.value = data.charts || []
  } catch (error) {
    console.error('加载数据洞察图表失败:', error)
    charts.value = []
  } finally {
    chartsLoading.value = false
  }
}

onMounted(() => {
  loadAttentionPoints()
  loadMonitorAlerts()
  loadCharts()
})

// KeepAlive 缓存页切回时刷新（onMounted 只执行一次）：切到其他页再回来，
// 总览图表/关注点/异动应展示最新数据，而不是缓存的旧快照。
// 修复：KeepAlive 组件首次进入时 onMounted 与 onActivated 都会触发（Vue 语义：mounted 先于 activated），
// 原实现导致首屏固定发 6 个请求（3 个接口各请求两遍）。用首次标记跳过，避免重复打重接口。
let activatedOnce = false
onActivated(() => {
  if (!activatedOnce) { activatedOnce = true; return }
  loadAttentionPoints()
  loadMonitorAlerts()
  loadCharts()
})
</script>

<style scoped>
/* ===== 入场动画：分区依次上浮淡入（苹果式编排感） ===== */
@keyframes ov-rise {
  from {
    opacity: 0;
    transform: translateY(14px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}
.ov-rise {
  animation: ov-rise 0.55s cubic-bezier(0.22, 0.61, 0.36, 1) both;
}

/* ===== 图标徽标：圆角方块 + 柔和底色（Apple Settings 风格） ===== */
.stat-chip {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  border-radius: 12px;
  flex-shrink: 0;
}

/* ===== 关注点卡片 ===== */
.attention-card {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  border-radius: 14px;
  border: 1px solid #e6e8e1;
  background: rgba(255, 255, 255, 0.9);
  padding: 16px;
  animation: ov-rise 0.5s cubic-bezier(0.22, 0.61, 0.36, 1) both;
  transition: box-shadow 0.25s ease, border-color 0.25s ease, transform 0.25s ease;
}
.attention-card:hover {
  transform: translateY(-1px);
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.05);
}
.attention-chip {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border-radius: 10px;
  flex-shrink: 0;
}

/* ===== 问候 hero 卡（2026-09-14 UI 精修 v2）：白卡 + 品牌微光 + 柔和投影 ===== */
.ov-hero {
  position: relative;
  overflow: hidden;
  padding: 22px 26px;
  border: 1px solid #e5e6eb;
  border-radius: 18px;
  background:
    radial-gradient(420px 180px at 92% -40%, rgba(77, 158, 255, .14), transparent 65%),
    radial-gradient(300px 160px at 55% 130%, rgba(77, 158, 255, .07), transparent 60%),
    #ffffff;
  box-shadow: 0 1px 2px rgba(16, 24, 40, .04), 0 10px 30px -12px rgba(16, 24, 40, .08);
}
.ov-ask-btn {
  background: linear-gradient(135deg, #5FAEFF 0%, #2E7CF0 100%);
  box-shadow: 0 4px 14px rgba(46, 124, 240, .3);
}
.ov-ask-btn:hover {
  background: linear-gradient(135deg, #4D9EFF 0%, #1F66D6 100%);
  box-shadow: 0 8px 22px -4px rgba(46, 124, 240, .5);
  transform: translateY(-1px);
}
.ov-ask-btn:active {
  transform: translateY(0) scale(.97);
}
</style>
