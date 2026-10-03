<script setup lang="ts">
/**
 * 主动洞察 + 数据盲点面板（P1-2 / P1-4）
 *
 * 两个能力解决的是互补的两个问题：
 * - **主动洞察**（对标 Tableau SpotIQ）：无需配置，自动扫描「度量 × 维度」组合，
 *   把统计上显著的异常挑出来 —— 解决"不知道该看什么异常"。
 * - **数据盲点**（对标 FineBI 盲点发现）：从「有数据 vs 被问过」的差集里找线索，
 *   提示从没被查询过的表、从没拆解过的维度 —— 解决"不知道自己不知道什么"。
 *
 * 与已有的「指标异动提醒」（monitor）区别：monitor 要人工配规则盯已知指标，
 * 这里两个能力都是零配置自动发现。
 */
import { ref } from 'vue'
import ActionPanel from './ActionPanel.vue'

const emit = defineEmits<{ (e: 'ask', question: string): void }>()

type Tab = 'insights' | 'blinds'
const tab = ref<Tab>('insights')
const loading = ref(false)
const loaded = ref<Record<string, boolean>>({ insights: false, blinds: false })
const error = ref('')
const insights = ref<any[]>([])
const blinds = ref<any[]>([])

// 行动闭环（对标 Fabric operations agents）：洞察 → 动作面板
const showAction = ref(false)
const prefillAction = ref('')
function openAction(actionId: string) {
  prefillAction.value = actionId || ''
  showAction.value = true
}

const TYPE_LABEL: Record<string, string> = {
  outlier: '离群异常',
  imbalance: '分布失衡',
  trend_break: '趋势突变',
  null_rate: '空值预警',
}
const BLIND_LABEL: Record<string, string> = {
  unexplored_table: '未探索表',
  unused_dimension: '未拆解维度',
  unused_measure: '未统计指标',
}

// 2026-10-03 修复（串台）：loading / error 是跨 tab 共享的单值 ref，load 也没有
// in-flight 去重或序号守卫。/api/insights/scan 是重接口（要扫维度组合），
// 返回明显慢于 /api/insights/blind-spots ——「洞察后到、先复位 loading」几乎是必然。
// 于是快速切 tab 时：盲点请求先返回把 loading 置 false，洞察请求后返回再置一次，
// 而此时面板渲染的是 blinds 分支 → loading 消失 + blinds 仍是空数组 → 显示
// 「未发现数据盲点」——把「还没加载完」说成「没有盲点」，是一条明确的错误结论。
// 现在加请求序号守卫：只有最新一次请求能写状态。
let loadSeq = 0
async function load(which: Tab, force = false) {
  // 2026-10-03 修复（P2）：已加载过的 tab 再切回来时原实现直接早退、loadSeq 不推进，
  // 于是上一个 tab 那个还在飞的慢请求（/api/insights/scan 实测 12s，比 blind-spots
  // 慢一个数量级）依然非 stale、仍持有 loading=true；而 loading 是**跨 tab 共享的
  // 单值**、模板 `v-if="loading"` 不看当前 tab → 洞察数据明明已加载好，却被 12 秒的
  // 「正在扫描维度组合…」整块盖住，点刷新都没用（v-else-if 优先级最高）。
  // 与 10-03 修的「未加载完说成没有盲点」同类，只是方向相反。
  // 现在早退时也推进序号：让在途慢请求作废（它本就不是当前 tab 要的），并复位 loading。
  if (loaded.value[which] && !force) {
    loadSeq++
    loading.value = false
    return
  }
  const my = ++loadSeq
  const isStale = () => my !== loadSeq
  loading.value = true
  error.value = ''
  try {
    const url = which === 'insights'
      ? '/api/insights/scan?limit=3&max_insights=6'
      : '/api/insights/blind-spots?limit=5'
    const res = await fetch(url, {
      method: which === 'insights' ? 'POST' : 'GET',
      headers: { 'Content-Type': 'application/json' },
    })
    const data = await res.json().catch(() => ({}))
    if (isStale()) return          // 已有更新的请求在跑，丢弃本次结果
    if (!res.ok) throw new Error(data?.detail || `请求失败(${res.status})`)
    if (which === 'insights') {
      insights.value = data.insights || []
      if (!data.success) error.value = data.error || '未发现显著洞察'
    } else {
      blinds.value = data.spots || []
      if (!data.success) error.value = data.error || '未发现数据盲点'
    }
    loaded.value[which] = true
  } catch (e: any) {
    if (isStale()) return
    error.value = e?.message || '加载失败'
  } finally {
    if (!isStale()) loading.value = false
  }
}

function switchTab(t: Tab) {
  tab.value = t
  error.value = ''
  load(t)
}

function ask(q: string) {
  if (q) emit('ask', q)
}
</script>

<template>
  <div class="bg-white border border-gray-200 rounded-xl overflow-hidden">
    <div class="px-3 py-2.5 border-b border-gray-200 flex items-center justify-between gap-2">
      <div class="min-w-0">
        <span class="text-xs font-medium text-gray-700">主动洞察 / 数据盲点</span>
        <span class="text-[11px] text-gray-400 ml-1.5">零配置自动发现</span>
      </div>
      <div class="flex items-center gap-1 shrink-0">
        <button
          @click="switchTab('insights')"
          :class="tab === 'insights' ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'"
          class="px-2 py-0.5 text-[11px] rounded transition"
        >主动洞察</button>
        <button
          @click="switchTab('blinds')"
          :class="tab === 'blinds' ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'"
          class="px-2 py-0.5 text-[11px] rounded transition"
        >数据盲点</button>
        <button
          v-if="loaded[tab]"
          @click="load(tab, true)"
          :disabled="loading"
          class="px-2 py-0.5 text-[11px] text-gray-500 hover:bg-gray-100 rounded transition disabled:opacity-50"
        >刷新</button>
      </div>
    </div>

    <div v-if="loading" class="px-3 py-5 text-[11px] text-gray-400 text-center">
      {{ tab === 'insights' ? '正在扫描维度组合…' : '正在比对数据资产与查询行为…' }}
    </div>

    <div v-else-if="error" class="px-3 py-4 text-[11px] text-amber-600">{{ error }}</div>

    <!-- 主动洞察 -->
    <div v-else-if="tab === 'insights'" class="divide-y divide-gray-100">
      <div v-if="!insights.length" class="px-3 py-5 text-[11px] text-gray-400 text-center">
        未发现显著异常（各维度组合的度量分布均在正常范围）
      </div>
      <div v-for="(i, idx) in insights" :key="idx" class="px-3 py-2">
        <div class="flex items-center gap-1.5 mb-0.5">
          <span class="px-1.5 py-0.5 text-[10px] rounded bg-indigo-50 text-indigo-600">
            {{ TYPE_LABEL[i.type] || i.type }}
          </span>
          <span class="text-[10px] text-gray-400 truncate">{{ i.table }}</span>
        </div>
        <div class="text-[11.5px] text-gray-700 leading-relaxed">{{ i.message }}</div>
        <!-- 洞察 → 行动（对标 Fabric operations agents）：带 action_suggestion 的异常可一键执行 -->
        <button
          v-if="i.action_suggestion"
          @click="openAction(i.action_suggestion.action)"
          class="mt-1.5 text-[11px] text-rose-600 border border-rose-200 bg-rose-50 rounded-lg px-2 py-1 hover:bg-rose-100"
        ><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" /> </svg></span> {{ i.action_suggestion.name }}</button>
      </div>
    </div>

    <!-- 数据盲点 -->
    <div v-else class="divide-y divide-gray-100">
      <div v-if="!blinds.length" class="px-3 py-5 text-[11px] text-gray-400 text-center">
        未发现盲点（库里的表与字段都已被查询覆盖）
      </div>
      <div v-for="(b, idx) in blinds" :key="idx" class="px-3 py-2">
        <div class="flex items-center gap-1.5 mb-0.5">
          <span class="px-1.5 py-0.5 text-[10px] rounded bg-blue-50 text-blue-700">
            {{ BLIND_LABEL[b.type] || b.type }}
          </span>
          <span class="text-[10px] text-gray-400 truncate">{{ b.label }}</span>
        </div>
        <div class="text-[11.5px] text-gray-700 leading-relaxed">{{ b.detail }}</div>
        <button
          v-if="b.suggestion"
          @click="ask(b.suggestion)"
          class="mt-1 text-[11px] text-blue-600 hover:underline text-left"
        >→ {{ b.suggestion }}</button>
      </div>
    </div>

    <div class="px-3 py-1.5 bg-gray-50 border-t border-gray-200 text-[10px] text-gray-400">
      {{ tab === 'insights'
        ? '统计口径：|z|>2 离群 · 占比>60% 失衡 · 偏离历史均值>30% 突变 · 空值率>30% 预警'
        : '比对「库表元数据」与「历史查询解析结果」的差集得出' }}
    </div>
  </div>

  <!-- 行动面板（洞察 → 执行闭环，对标 Fabric operations agents） -->
  <ActionPanel
    v-if="showAction"
    :prefill-action="prefillAction"
    @close="showAction = false"
    @executed="() => { showAction = false; load('insights', true) }"
  />
</template>
