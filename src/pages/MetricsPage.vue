<template>
  <div class="space-y-5">
    <!-- 头部：说明 + 搜索调试（page-hero：统一页面 Hero 横幅） -->
    <div class="page-hero flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="text-lg font-bold text-gray-800">指标口径注册表</h2>
        <p class="text-xs text-gray-500 mt-1">
          命中指标的固定口径会以「最高优先级」注入 SQL 生成 Prompt，并在结果复核时校验口径冲突。
          内置指标只读，可新增/编辑/删除自定义指标。
        </p>
      </div>
      <button
        @click="openCreate"
        class="px-3 py-1.5 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition"
      ><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M5 12h14" /> <path d="M12 5v14" /> </svg></span> 新增指标</button>
    </div>

    <!-- 搜索调试 -->
    <div class="flex gap-2">
      <input
        v-model="searchQ"
        placeholder="输入一句话试匹配指标（如：统计最近7天的良率）"
        class="flex-1 px-3 py-2 text-xs border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-200"
        @keyup.enter="doSearch"
      />
      <button @click="doSearch" class="px-3 py-2 text-xs bg-gray-100 hover:bg-gray-200 rounded-lg">试匹配</button>
    </div>
    <div v-if="searchResult" class="text-xs bg-blue-50 border border-blue-200 rounded-lg p-3 whitespace-pre-wrap text-gray-700">
      <div class="font-medium text-blue-700 mb-1">命中指标：{{ searchResult.metrics?.length || 0 }} 个</div>
      <div v-if="searchResult.metrics?.length">{{ searchResult.metrics.map((m: any) => m.name).join('、') }}</div>
      <div v-if="searchResult.hint" class="mt-1 text-gray-600">将注入的口径提示：<br>{{ searchResult.hint }}</div>
      <div v-else class="mt-1 text-gray-500">未命中任何指标口径。</div>
    </div>

    <!-- P0-4 指标候选：自动挖掘 + 人工审核（对标 SpotterModel / Fabric Copilot 建模） -->
    <div class="bg-white border border-gray-200 rounded-xl overflow-hidden">
      <div class="px-3 py-2.5 bg-gray-50 border-b border-gray-200 flex items-center justify-between gap-2">
        <div class="min-w-0">
          <span class="text-xs font-medium text-gray-700">待审核指标候选</span>
          <span class="text-[11px] text-gray-400 ml-1.5">
            从历史成功查询中自动提炼尚未治理的口径，<b class="text-gray-500">人工确认后才进注册表</b>
          </span>
        </div>
        <button
          @click="doMine"
          :disabled="mining"
          class="px-2.5 py-1 text-xs bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 whitespace-nowrap"
        >{{ mining ? '挖掘中…' : '挖掘候选' }}</button>
        <button
          @click="doScanSchema"
          :disabled="scanning"
          title="扫描库表结构自动生成候选指标（P0-1 语义层自动构建）"
          class="px-2.5 py-1 text-xs bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50 whitespace-nowrap"
        >{{ scanning ? '扫描中…' : '扫描库表' }}</button>
      </div>

      <div v-if="candErr" class="px-3 py-2 text-[11px] text-rose-600">{{ candErr }}</div>

      <div v-if="!candidates.length" class="px-3 py-5 text-[11px] text-gray-400 text-center">
        暂无候选。点击「挖掘候选」，系统会从历史成功查询里提炼尚未注册的口径。
      </div>

      <div v-else class="divide-y divide-gray-100">
        <div v-for="c in candidates" :key="c.id" class="px-3 py-2.5 rounded-lg transition-colors">
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2 flex-wrap">
                <input
                  v-model="c.name"
                  class="px-2 py-0.5 text-xs font-semibold text-gray-800 border border-gray-300 rounded focus:outline-none focus:ring-2 focus:ring-blue-200 w-40"
                  placeholder="指标名"
                />
                <input
                  v-model="c.unit"
                  class="px-2 py-0.5 text-xs text-gray-600 border border-gray-300 rounded focus:outline-none focus:ring-2 focus:ring-blue-200 w-16"
                  placeholder="单位"
                />
                <span class="text-[10px] text-gray-400 whitespace-nowrap">命中 {{ c.hit_count }} 次</span>
              </div>
              <div class="mt-1">
                <code class="inline-block font-mono text-[11px] text-blue-700 bg-[#F0F7FF] border border-[#E1F0FF] rounded-md px-1.5 py-0.5">{{ c.expr }}</code>
              </div>
              <div class="text-[11px] text-gray-500 mt-0.5">
                源表：{{ (c.tables || []).join('、') || '—' }}
                <span v-if="(c.dims || []).length"> · 维度：{{ c.dims.join('、') }}</span>
              </div>
              <div class="text-[11px] text-gray-400 mt-0.5">
                别名：{{ (c.aliases || []).join('、') || '—' }}
              </div>
              <div v-if="c.description" class="text-[11px] text-gray-400">{{ c.description }}</div>
              <div v-if="c.sample_question" class="text-[11px] text-gray-400">问法示例：{{ c.sample_question }}</div>
            </div>
            <div class="flex flex-col gap-1.5 shrink-0">
              <button
                @click="adopt(c)"
                class="px-2.5 py-1 text-[11px] bg-blue-600 text-white rounded-lg hover:bg-blue-700 whitespace-nowrap"
              >采纳入库</button>
              <button
                @click="ignore(c)"
                class="px-2.5 py-1 text-[11px] text-gray-500 border border-gray-300 rounded-lg hover:bg-gray-50 whitespace-nowrap"
              >忽略</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- P0-D 定时报表订阅（对标 Tableau Pulse 定时推送）：按间隔自动生成洞察日报推送 -->
    <div class="bg-white border border-gray-200 rounded-xl overflow-hidden">
      <div class="px-3 py-2.5 bg-gray-50 border-b border-gray-200 flex items-center justify-between gap-2">
        <div class="min-w-0">
          <span class="text-xs font-medium text-gray-700">定时报表订阅</span>
          <span class="text-[11px] text-gray-400 ml-1.5">
            按固定间隔自动生成「主动洞察日报」并推送（需先在服务端启用 NOTIFY_ENABLED 并配置推送通道）
          </span>
        </div>
        <button
          @click="schedOpen = !schedOpen"
          class="px-2.5 py-1 text-xs border border-gray-300 rounded-lg hover:bg-gray-50 whitespace-nowrap"
        >{{ schedOpen ? '收起' : '管理' }}</button>
      </div>
      <div v-if="schedOpen" class="p-3">
        <div v-if="!schedules.length" class="text-[11px] text-gray-400 text-center py-2">暂无订阅，新增一条即可开始</div>
        <div v-else class="divide-y divide-gray-100">
          <div v-for="s in schedules" :key="s.id" class="flex items-center justify-between py-1.5">
            <div class="min-w-0">
              <span class="text-xs font-medium text-gray-700">{{ s.name }}</span>
              <span class="text-[10.5px] text-gray-400 ml-2">
                每 {{ s.interval_min }} 分钟 ·
                {{ s.next_in_sec >= 60 ? Math.round(s.next_in_sec / 60) + ' 分钟后' : s.next_in_sec + ' 秒后' }}
              </span>
            </div>
            <button @click="delSchedule(s.id)" class="text-[11px] text-rose-500 hover:text-rose-700 whitespace-nowrap">删除</button>
          </div>
        </div>
        <div class="mt-2.5 flex items-center gap-2">
          <input v-model="schedName" placeholder="订阅名称（如：每日洞察日报）"
                 class="flex-1 px-2 py-1.5 text-xs border border-gray-300 rounded-lg" />
          <input v-model.number="schedInterval" type="number" min="1" placeholder="间隔分钟"
                 class="w-24 px-2 py-1.5 text-xs border border-gray-300 rounded-lg" />
          <button @click="addSchedule"
                  class="px-2.5 py-1.5 text-xs bg-blue-600 text-white rounded-lg hover:bg-blue-700 whitespace-nowrap">新增</button>
        </div>
      </div>
    </div>

    <!-- 指标列表
         列宽用 table-fixed + colgroup 定死比例：口径 SQL 这类长文本按列宽换行/截断，
         不再把表格撑宽。原先的 overflow-x-auto 会在卡片底部留一条横向滚动条，
         现已移除——表格恒定等于容器宽度，8 列宽度比例合计 100%。 -->
    <div v-if="loading" class="text-xs text-gray-400 py-6 text-center">加载中…</div>
    <div v-else class="bg-white border border-gray-200 rounded-xl">
      <table class="w-full table-fixed text-left text-xs">
        <colgroup>
          <col class="w-[17%]" />
          <col class="w-[10%]" />
          <col class="w-[5%]" />
          <col class="w-[19%]" />
          <col class="w-[13%]" />
          <col class="w-[13%]" />
          <col class="w-[11%]" />
          <col class="w-[12%]" />
        </colgroup>
        <thead class="bg-gray-50 text-gray-500">
          <tr>
            <th class="px-3 py-2 font-medium">指标</th>
            <th class="px-3 py-2 font-medium">别名</th>
            <th class="px-2 py-2 font-medium whitespace-nowrap">单位</th>
            <th class="px-3 py-2 font-medium">口径 SQL</th>
            <th class="px-3 py-2 font-medium">业务公式</th>
            <th class="px-3 py-2 font-medium">适用表</th>
            <th class="px-3 py-2 font-medium">维度</th>
            <th class="px-3 py-2 font-medium">操作</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-100">
          <tr v-for="m in metrics" :key="m.name" class="align-top hover:bg-gray-50/60">
            <td class="px-3 py-2">
              <div class="flex items-center gap-1">
                <span class="flex-1 min-w-0 truncate font-semibold text-gray-800" :title="m.name">{{ m.name }}</span>
                <span v-if="builtinNames.includes(m.name)" class="shrink-0 text-[10px] text-gray-400 border border-gray-200 rounded px-1">内置</span>
              </div>
              <div class="text-gray-400 mt-0.5 truncate" :title="m.description || ''">{{ m.description }}</div>
            </td>
            <td class="px-3 py-2 text-gray-600">
              <div class="line-clamp-2 text-[11px]" :title="(m.aliases || []).join('、')">{{ (m.aliases || []).join('、') }}</div>
            </td>
            <td class="px-2 py-2 text-gray-600">
              <div class="truncate" :title="m.unit || ''">{{ m.unit }}</div>
            </td>
            <td class="px-3 py-2 font-mono text-[11px] text-blue-700">
              <div class="line-clamp-2 break-all" :title="m.sql_expression || ''">{{ m.sql_expression }}</div>
            </td>
            <td class="px-3 py-2 text-gray-600">
              <div class="line-clamp-2" :title="m.formula || ''">{{ m.formula }}</div>
            </td>
            <td class="px-3 py-2 text-gray-600">
              <div class="line-clamp-2 break-all font-mono text-[10px] text-gray-500" :title="(m.tables || []).join(', ')">{{ (m.tables || []).join(', ') }}</div>
            </td>
            <td class="px-3 py-2 text-gray-600">
              <div class="line-clamp-2" :title="(m.dims || []).join('、')">{{ (m.dims || []).join('、') }}</div>
            </td>
            <td class="px-3 py-2">
              <template v-if="!builtinNames.includes(m.name)">
                <div class="flex items-center gap-2 whitespace-nowrap">
                  <button @click="openEdit(m)" class="text-blue-600 hover:underline">编辑</button>
                  <button @click="removeMetric(m.name)" class="text-red-500 hover:underline">删除</button>
                </div>
              </template>
              <span v-else class="text-gray-300">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 指标血缘（改造6）：派生指标 → 依赖指标 -->
    <div v-if="lineage.nodes.length" class="bg-white border border-gray-200 rounded-xl p-4">
      <div class="flex items-center justify-between mb-3">
        <h3 class="text-sm font-bold text-gray-800"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <line x1="18" x2="18" y1="20" y2="10" /> <line x1="12" x2="12" y1="20" y2="4" /> <line x1="6" x2="6" y1="20" y2="14" /> </svg></span> 指标血缘图</h3>
        <span class="text-[10px] text-gray-400">派生指标 = 由基础指标计算得出，口径联动</span>
      </div>
      <div class="flex flex-wrap gap-2">
        <div
          v-for="n in lineage.nodes.filter((x: any) => x.kind === 'derived')"
          :key="'d-' + n.name"
          class="border border-blue-200 bg-blue-50 rounded-lg px-3 py-2 min-w-[140px]"
        >
          <div class="text-xs font-semibold text-blue-800">{{ n.name }}</div>
          <div class="text-[10px] text-blue-600 mt-0.5">{{ n.formula || n.unit }}</div>
          <div class="mt-1.5 text-[10px] text-gray-500">
            <template v-if="lineage.edges.filter((e: any) => e.target === n.name).length">
              ← {{ lineage.edges.filter((e: any) => e.target === n.name).map((e: any) => e.source).join('、') }}
            </template>
            <template v-else class="text-gray-300">依赖指标未注册</template>
          </div>
        </div>
      </div>
    </div>

    <!-- 编辑弹窗
         必须 Teleport 到 body：工作台壳层 .workspace-shell 带入场动画，动画播完后
         Chrome 仍把 transform 保留成 matrix(1,0,0,1,0,0)（fill-mode:both 的副作用），
         壳层因此成为 position:fixed 的包含块——弹窗会被按「整页高度」居中，
         落到页面很下方，表现就是"弹窗没固定住"。传送到 body 后按视口定位。
         结构改成 头/身/脚 三段：表单区自己滚，标题和「保存」始终可见。 -->
    <Teleport to="body">
      <div
        v-if="editing !== null"
        class="fixed inset-0 z-[999] bg-black/30 flex items-center justify-center p-4"
        @click.self="editing = null"
      >
        <div class="bg-white rounded-xl w-[560px] max-w-full max-h-[85vh] flex flex-col shadow-xl">
          <div class="flex items-center justify-between px-5 pt-4 pb-3 border-b border-gray-100 shrink-0">
            <h3 class="text-sm font-bold text-gray-800">{{ typeof editing === 'object' ? '编辑指标' : '新增指标' }}</h3>
            <button
              @click="editing = null"
              class="w-7 h-7 grid place-items-center rounded-lg text-gray-400 hover:text-gray-600 hover:bg-gray-100"
              aria-label="关闭"
            ><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M18 6 6 18" /> <path d="m6 6 12 12" /> </svg></span></button>
          </div>

          <div class="flex-1 overflow-y-auto px-5 py-4">
            <div class="grid grid-cols-2 gap-3 text-xs">
              <label class="block">
                <span class="text-gray-500">指标名 *</span>
                <input v-model="form.name" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-200" />
              </label>
              <label class="block">
                <span class="text-gray-500">单位</span>
                <input v-model="form.unit" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg" />
              </label>
              <label class="block col-span-2">
                <span class="text-gray-500">别名（逗号分隔）</span>
                <input v-model="aliasesText" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg" placeholder="合格率, 良品率" />
              </label>
              <label class="block col-span-2">
                <span class="text-gray-500">口径 SQL 表达式 *</span>
                <input v-model="form.sql_expression" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg font-mono" placeholder="SUM(good_qty) / NULLIF(SUM(input_qty), 0) * 100" />
              </label>
              <label class="block col-span-2">
                <span class="text-gray-500">业务公式（给 LLM 看的自然语言口径）</span>
                <input v-model="form.formula" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg" placeholder="合格数 / 投入数 × 100%" />
              </label>
              <label class="block col-span-2">
                <span class="text-gray-500">适用表（逗号分隔）</span>
                <input v-model="tablesText" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg" placeholder="mes_process_output" />
              </label>
              <label class="block col-span-2">
                <span class="text-gray-500">支持维度（逗号分隔）</span>
                <input v-model="dimsText" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg" placeholder="工序, 产线, 产品, 日期" />
              </label>
              <label class="block col-span-2">
                <span class="text-gray-500">说明</span>
                <textarea v-model="form.description" rows="2" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg"></textarea>
              </label>
              <label class="block col-span-1">
                <span class="text-gray-500">生效日期（可选，ISO）</span>
                <input v-model="form.valid_from" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg" placeholder="2026-09-01（留空=至今）" />
              </label>
              <label class="block col-span-1">
                <span class="text-gray-500">失效日期（可选，不含）</span>
                <input v-model="form.valid_to" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg" placeholder="2026-12-31（留空=永久）" />
              </label>
            </div>
          </div>

          <div class="flex justify-end gap-2 px-5 py-3 border-t border-gray-100 shrink-0">
            <button @click="editing = null" class="px-3 py-1.5 text-xs bg-gray-100 hover:bg-gray-200 rounded-lg">取消</button>
            <button @click="saveMetric" class="px-3 py-1.5 text-xs text-white bg-blue-600 hover:bg-blue-700 rounded-lg">保存</button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'

interface Metric {
  name: string
  aliases?: string[]
  unit?: string
  tables?: string[]
  sql_expression?: string
  formula?: string
  description?: string
  dims?: string[]
  valid_from?: string
  valid_to?: string
}

const metrics = ref<Metric[]>([])
const loading = ref(true)
const searchQ = ref('')
const searchResult = ref<any>(null)
const editing = ref<null | 'create' | Metric>(null)

// ── P0-4 指标候选（自动挖掘 + 人工审核）──────────────────
const candidates = ref<any[]>([])
const mining = ref(false)
const candErr = ref('')

const loadCandidates = async () => {
  try {
    const d = await api('/api/metrics/candidates')
    candidates.value = d.candidates || []
  } catch {
    candidates.value = []   // 候选拉取失败不影响主列表使用
  }
}

const doMine = async () => {
  mining.value = true
  candErr.value = ''
  try {
    const d = await api('/api/metrics/mine?limit=10', { method: 'POST' })
    candidates.value = d.candidates || []
    if (!candidates.value.length && d.error) candErr.value = d.error
  } catch (e: any) {
    candErr.value = e?.message || '挖掘失败'
  } finally {
    mining.value = false
  }
}

// ── P0-1 语义层自动构建：扫描库表结构 → 候选指标（human-in-loop 人工采纳）──
const scanning = ref(false)
const doScanSchema = async () => {
  scanning.value = true
  candErr.value = ''
  try {
    const d = await api('/api/metrics/scan-schema?limit=20', { method: 'POST' })
    candidates.value = d.candidates || []
    if (!candidates.value.length && d.error) candErr.value = d.error
  } catch (e: any) {
    candErr.value = e?.message || '库表扫描失败'
  } finally {
    scanning.value = false
  }
}

// ── P0-D 定时报表订阅（对标 Tableau Pulse 定时推送）────────
const schedOpen = ref(false)
const schedules = ref<any[]>([])
const schedName = ref('')
const schedInterval = ref(1440)

const loadSchedules = async () => {
  try {
    const d = await api('/api/report/schedules')
    schedules.value = d.schedules || []
  } catch { schedules.value = [] }
}

const addSchedule = async () => {
  if (!schedName.value.trim()) return
  try {
    await api('/api/report/schedules', {
      method: 'POST',
      body: JSON.stringify({
        name: schedName.value.trim(),
        interval_min: Math.max(1, schedInterval.value || 1440),
        report_type: 'insights',
      }),
    })
    schedName.value = ''
    await loadSchedules()
  } catch (e: any) {
    alert(e?.message || '新增订阅失败')
  }
}

const delSchedule = async (id: string) => {
  if (!confirm('确认删除该定时订阅？')) return
  try {
    await api(`/api/report/schedules/${id}`, { method: 'DELETE' })
    await loadSchedules()
  } catch { /* 忽略删除失败 */ }
}

const adopt = async (c: any) => {
  try {
    await api(`/api/metrics/candidates/${c.id}/adopt`, {
      method: 'POST',
      body: JSON.stringify({ name: c.name, aliases: c.aliases, unit: c.unit, description: c.description }),
    })
    candidates.value = candidates.value.filter((x) => x.id !== c.id)
    await load()            // 入库后刷新列表 + 重建向量库（后端已重建，这里只刷展示）
  } catch (e: any) {
    alert(e?.message || '采纳失败')
  }
}

const ignore = async (c: any) => {
  try {
    await api(`/api/metrics/candidates/${c.id}`, { method: 'DELETE' })
    candidates.value = candidates.value.filter((x) => x.id !== c.id)
  } catch (e: any) {
    alert(e?.message || '忽略失败')
  }
}

const builtinNames = ['产量', '良率', '不良率', '合格数', '不良数', '库存量', '安全库存', '缺货量', '工单数']

const form = ref<Metric>({ name: '', aliases: [], unit: '', tables: [], sql_expression: '', formula: '', description: '', dims: [] })
const aliasesText = ref('')
const tablesText = ref('')
const dimsText = ref('')

const api = async (path: string, opts: any = {}) => {
  const resp = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => null)
    throw new Error(err?.detail || `请求失败(${resp.status})`)
  }
  return resp.json()
}

const load = async () => {
  loading.value = true
  try {
    const d = await api('/api/metrics')
    metrics.value = d.metrics || []
  } catch (e: any) {
    alert(e.message)
  } finally {
    loading.value = false
  }
  loadLineage()
}

// ── 指标血缘（改造6）──────────────────────────────
const lineage = ref<{ nodes: any[]; edges: any[] }>({ nodes: [], edges: [] })
const loadLineage = async () => {
  try {
    const d = await api('/api/metrics/lineage')
    // 契约防御：后端结构变动/空响应时归一化，避免 edges.filter 抛 TypeError 白屏
    lineage.value = {
      nodes: Array.isArray(d?.nodes) ? d.nodes : [],
      edges: Array.isArray(d?.edges) ? d.edges : [],
    }
  } catch (e) { /* 血缘加载失败不影响主列表 */ }
}

let searchSeq = 0

const doSearch = async () => {
  const seq = ++searchSeq
  if (!searchQ.value.trim()) { searchResult.value = null; return }
  try {
    const r = await api(`/api/metrics/search?q=${encodeURIComponent(searchQ.value)}`)
    if (seq === searchSeq) searchResult.value = r
  } catch (e: any) {
    if (seq === searchSeq) alert(e.message)
  }
}

const openCreate = () => {
  form.value = { name: '', aliases: [], unit: '', tables: [], sql_expression: '', formula: '', description: '', dims: [], valid_from: '', valid_to: '' }
  aliasesText.value = ''; tablesText.value = ''; dimsText.value = ''
  editing.value = 'create'
}

const openEdit = (m: Metric) => {
  form.value = { ...m, aliases: [...(m.aliases || [])], tables: [...(m.tables || [])], dims: [...(m.dims || [])] }
  aliasesText.value = (m.aliases || []).join(',')
  tablesText.value = (m.tables || []).join(',')
  dimsText.value = (m.dims || []).join(',')
  editing.value = m
}

const saveMetric = async () => {
  if (!form.value.name.trim()) { alert('指标名不能为空'); return }
  const body = {
    ...form.value,
    name: form.value.name.trim(),
    aliases: aliasesText.value.split(/[,，]/).map((s: string) => s.trim()).filter(Boolean),
    tables: tablesText.value.split(/[,，]/).map((s: string) => s.trim()).filter(Boolean),
    dims: dimsText.value.split(/[,，]/).map((s: string) => s.trim()).filter(Boolean),
  }
  try {
    if (editing.value === 'create') {
      await api('/api/metrics', { method: 'POST', body: JSON.stringify(body) })
    } else {
      // PUT 的路径名必须是「原指标名」（后端按路径名定位记录）；body.name 才是新名（允许改名）
      const originalName = (editing.value as Metric).name
      await api(`/api/metrics/${encodeURIComponent(originalName)}`, { method: 'PUT', body: JSON.stringify(body) })
    }
    editing.value = null
    await load()
  } catch (e: any) {
    alert(e.message)
  }
}

const removeMetric = async (name: string) => {
  if (!confirm(`确认删除指标「${name}」？`)) return
  try {
    await api(`/api/metrics/${encodeURIComponent(name)}`, { method: 'DELETE' })
    await load()
  } catch (e: any) {
    alert(e.message)
  }
}

// ── 弹窗打开期间：锁住页面滚动 + Esc 关闭 ──────────────
// 不锁的话滚轮会继续滚背后的长页面，视觉上像"弹窗在跟着页面跑"。
const _prevBodyOverflow = ref('')
watch(
  () => editing.value !== null,
  (open) => {
    if (open) {
      _prevBodyOverflow.value = document.body.style.overflow
      document.body.style.overflow = 'hidden'
    } else {
      document.body.style.overflow = _prevBodyOverflow.value
    }
  },
)
const onKeydown = (e: KeyboardEvent) => {
  if (e.key === 'Escape' && editing.value !== null) editing.value = null
}

onMounted(() => {
  load()
  loadCandidates()
  loadSchedules()
  window.addEventListener('keydown', onKeydown)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  // 组件被切走时若弹窗还开着，把 body 的滚动还回去，避免整站滚不动
  if (editing.value !== null) document.body.style.overflow = _prevBodyOverflow.value
})
</script>
