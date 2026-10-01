<template>
  <div class="chart-card">
    <div class="chart-card-head">
      <div class="chart-card-title">{{ chart.title }}</div>
      <div class="chart-card-meta">
        {{ chart.table_label }}<span v-if="chart.dim_label"> · 按「{{ chart.dim_label }}」统计</span>
        <button v-if="lineage" class="chart-card-lineage" @click.stop="lineageOpen = true" title="查看数据来源（表 / 字段 / SQL）">
          <AppIcon name="link-2" :size="11" /> 溯源
        </button>
      </div>
    </div>
    <div ref="el" class="chart-card-canvas"></div>
    <div class="chart-card-interp">
      <AppIcon name="lightbulb" :size="14" class="chart-card-interp-icon" />
      <p>{{ chart.interpretation }}</p>
    </div>

    <!-- 数据溯源弹窗：来源表 + 涉及字段 + 生成 SQL -->
    <div v-if="lineageOpen" class="lineage-mask" @click.self="lineageOpen = false">
      <div class="lineage-dialog">
        <div class="lineage-head">
          <span class="lineage-title"><AppIcon name="database" :size="14" /> 数据溯源</span>
          <button class="lineage-close" @click="lineageOpen = false">×</button>
        </div>
        <div class="lineage-body">
          <div class="lineage-row">
            <span class="lineage-label">来源表</span>
            <span class="lineage-value">
              {{ lineage.table_label || '—' }}
              <span v-if="lineage.table" class="lineage-table">（{{ lineage.table }}）</span>
            </span>
          </div>
          <div class="lineage-row">
            <span class="lineage-label">涉及字段</span>
            <div v-if="lineage.fields && lineage.fields.length" class="lineage-fields">
              <span v-for="(f, i) in lineage.fields" :key="i" class="lineage-field">
                <span class="lineage-field-name">{{ f.label || f.name }}</span>
                <span v-if="f.name && f.name !== f.label" class="lineage-field-raw">{{ f.name }}</span>
                <span class="lineage-field-role" :class="'role-' + roleClass(f.role)">{{ f.role || '字段' }}</span>
              </span>
            </div>
            <span v-else class="lineage-value lineage-empty">—</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import echarts from '../echarts'
import AppIcon from './AppIcon.vue'
import { renderChart, disposeChart, resizeChart, OPS_PALETTE } from '../charts'

const props = defineProps<{ chart: any }>()

const el = ref<HTMLElement>()
// 注：原先在此缓存 `let inst` —— 已移除。disposeChart 会销毁同一 DOM 上的实例而缓存变量仍 truthy，
// 导致"通用图 → 结构化图"切换后向已销毁实例 setOption（图表静默空白）。现统一按 DOM 反查实例。

// 通用图表（后端返回 columns+rows，走统一 renderChart 双引擎）vs 结构化图表（后端返回 data）
const isGeneric = computed(() => !!props.chart.columns && !!props.chart.rows)

// 数据溯源：来源表 + 涉及字段 + 生成 SQL（后端 chart.lineage）
const lineage = computed(() => props.chart.lineage || null)
const lineageOpen = ref(false)
// 字段角色 → 样式类（避免中文 class 名带来的转义问题）
const ROLE_CLASS: Record<string, string> = { '维度': 'dim', '度量': 'measure', '时间': 'time' }
const roleClass = (r: string) => ROLE_CLASS[r || ''] || 'field'

// 品牌蓝色阶色板（2026-09-14 UI 精修 v2：深→浅品牌蓝 + 单墨点锚色，
// 替代原「多近黑块」配色——饼图/旭日图大片黑色与蓝白界面不协调）
const COLORS = ['#2E7CF0', '#4D9EFF', '#8FC5FF', '#1F66D6', '#5FAEFF', '#B9DCFF', '#1677ff', '#1d2129']

const AXIS_COLOR = '#e5e6eb'
const LABEL_COLOR = '#4e5969'
const SPLIT_COLOR = '#f2f3f5'

function buildOption(chart: any) {
  switch (chart.type) {
    case 'gauge': return buildGauge(chart)
    case 'funnel': return buildFunnel(chart)
    case 'sunburst': return buildSunburst(chart)
    case 'pareto': return buildPareto(chart)
    case 'stock-health': return buildStockHealth(chart)
    case 'stacked-bar': return buildStackedBar(chart)
    case 'pie': return buildPie(chart)
    case 'line': return buildLine(chart)
    default: return buildBar(chart)
  }
}

// 仪表盘：综合良率等单值 KPI
// 返回类型显式标 any：animationEasing 等字面量在对象推断里会被放宽成 string，
// 直接进 setOption 会撞 ECBasicOption 的 AnimationEasing 联合类型（vue-tsc 报 TS2769）。
function buildGauge(chart: any): any {
  const d = chart.data || {}
  const value = d.value ?? 0
  const gradeColor = d.grade === '优秀' ? '#1d2129' : d.grade === '良好' ? '#1677ff' : '#d97706'
  return {
    // 指针扫过表盘这一段动画由 ECharts 的 enter 动画驱动（GaugeView 用 initProps 把 rotation
    // 从起始角推到终值）。显式写死时长/缓动，避免上游任何一处改动把它静默关掉。
    animation: true,
    animationDuration: 1000,
    animationDurationUpdate: 700,
    animationEasing: 'cubicOut',
    animationEasingUpdate: 'cubicOut',
    series: [{
      type: 'gauge',
      startAngle: 210,
      endAngle: -30,
      min: d.min ?? 0,
      max: d.max ?? 100,
      radius: '95%',
      center: ['50%', '58%'],
      axisLine: {
        lineStyle: {
          width: 18,
          color: [
            [0.95, '#f0f2e9'],
            [0.98, '#e8eecb'],
            [1, '#1677ff'],
          ],
        },
      },
      axisTick: { show: false },
      splitLine: { length: 14, lineStyle: { color: '#c9cfc2', width: 2 } },
      axisLabel: { color: '#a9aeb8', fontSize: 10, distance: 18 },
      pointer: { width: 5, length: '62%', itemStyle: { color: '#1d2129' } },
      detail: {
        valueAnimation: true,
        fontSize: 30,
        fontWeight: 600,
        color: gradeColor,
        offsetCenter: [0, '42%'],
        formatter: (v: number) => `${v}${d.unit || '%'}`,
      },
      title: { color: '#a9aeb8', fontSize: 12, offsetCenter: [0, '78%'] },
      data: [{ value, name: d.grade || '综合良率' }],
    }],
  }
}

// 漏斗图：工序损耗
function buildFunnel(chart: any) {
  const data = chart.data || []
  return {
    color: ['#2E7CF0', '#4D9EFF', '#5FAEFF', '#1F66D6', '#8FC5FF', '#1677ff', '#B9DCFF', '#1d2129'],
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { bottom: 0, type: 'scroll', textStyle: { color: LABEL_COLOR, fontSize: 11 } },
    series: [{
      type: 'funnel',
      left: '8%',
      right: '8%',
      top: 10,
      bottom: 36,
      minSize: '18%',
      maxSize: '100%',
      sort: 'descending',
      gap: 3,
      label: { show: true, position: 'inside', color: '#1d2129', fontSize: 11, formatter: '{b}' },
      itemStyle: { borderColor: '#fff', borderWidth: 1 },
      emphasis: { label: { fontSize: 13, fontWeight: 600 } },
      data: data.map((d: any) => ({ name: d.name, value: d.value })),
    }],
  }
}

// 旭日图：缺陷严重度→类型
function buildSunburst(chart: any) {
  return {
    color: ['#2E7CF0', '#4D9EFF', '#8FC5FF', '#1F66D6', '#5FAEFF', '#B9DCFF', '#1677ff', '#1d2129'],
    tooltip: { trigger: 'item', formatter: '{b}: {c}' },
    series: [{
      type: 'sunburst',
      radius: ['16%', '88%'],
      center: ['50%', '52%'],
      sort: 'desc',
      label: {
        rotate: 'radial',
        color: '#1d2129',
        fontSize: 11,
        // 深色切片上的可读性：白描边光晕（深浅切片通用，比单色文字稳妥）
        textBorderColor: 'rgba(255, 255, 255, .9)',
        textBorderWidth: 2,
        formatter: (p: any) => (p.depth === 0 ? '' : p.name),
      },
      itemStyle: { borderColor: '#fff', borderWidth: 2, borderRadius: 6 },
      emphasis: { itemStyle: { borderWidth: 3 } },
      data: chart.data || [],
    }],
  }
}

// 帕累托图：柱 + 累计百分比折线（双 Y 轴）
function buildPareto(chart: any) {
  const data = chart.data || []
  const names = data.map((d: any) => d.name)
  const values = data.map((d: any) => d.value)
  const total = values.reduce((a: number, b: number) => a + b, 0) || 1
  let acc = 0
  const pct = values.map((v: number) => { acc += v; return +(acc / total * 100).toFixed(1) })
  return {
    color: COLORS,
    tooltip: {
      trigger: 'axis',
      formatter: (ps: any[]) => {
        if (!ps.length) return ''
        const name = ps[0].axisValue
        const bar = ps.find((p) => p.seriesType === 'bar')
        return `${name}<br/>数量：${bar ? bar.value : ''}<br/>累计占比：${pct[ps[0].dataIndex]}%`
      },
    },
    grid: { left: 10, right: 10, top: 24, bottom: 6, containLabel: true },
    xAxis: {
      type: 'category',
      data: names,
      axisLine: { lineStyle: { color: AXIS_COLOR } },
      axisTick: { show: false },
      axisLabel: { color: LABEL_COLOR, fontSize: 10, rotate: names.length > 5 ? 22 : 0, interval: 0 },
    },
    yAxis: [
      {
        type: 'value',
        splitLine: { lineStyle: { color: SPLIT_COLOR } },
        axisLabel: { color: '#a9aeb8', fontSize: 10 },
      },
      {
        type: 'value', max: 100,
        splitLine: { show: false },
        axisLabel: { color: '#a9aeb8', fontSize: 10, formatter: '{value}%' },
      },
    ],
    series: [
      {
        name: '数量', type: 'bar', data: values,
        barWidth: '52%',
        itemStyle: { color: '#1677ff', borderRadius: [6, 6, 0, 0] },
      },
      {
        name: '累计占比', type: 'line', yAxisIndex: 1, data: pct,
        smooth: true, symbol: 'circle', symbolSize: 6,
        lineStyle: { color: '#1d2129', width: 2 },
        itemStyle: { color: '#1d2129' },
        label: { show: true, fontSize: 9, color: '#1d2129', formatter: '{c}%' },
      },
    ],
  }
}

// 库存健康度：横向条形 + 安全库存标线 + 告警红
function buildStockHealth(chart: any) {
  const data = chart.data || []
  const names = data.map((d: any) => d.name)
  const values = data.map((d: any) => d.value)
  const safes = data.map((d: any) => d.safe)
  return {
    color: COLORS,
    tooltip: {
      trigger: 'axis',
      formatter: (ps: any[]) => {
        if (!ps.length) return ''
        const i = ps[0].dataIndex
        const d = data[i]
        const ratioText = d.ratio == null ? '—' : `${d.ratio.toFixed(0)}%` // null = 安全库存为 0，无达成率
        return `${d.name}<br/>可用库存：${d.value}<br/>安全库存：${d.safe}<br/>达成率：${ratioText}${d.alert ? '<br/><span style="color:#e11d48">⚠ 低于安全线</span>' : ''}`
      },
    },
    legend: { top: 0, textStyle: { color: LABEL_COLOR, fontSize: 11 } },
    grid: { left: 10, right: 44, top: 30, bottom: 6, containLabel: true },
    xAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: SPLIT_COLOR } },
      axisLabel: { color: '#a9aeb8', fontSize: 10 },
    },
    yAxis: {
      type: 'category', data: names, inverse: true,
      axisLine: { lineStyle: { color: AXIS_COLOR } },
      axisTick: { show: false },
      axisLabel: { color: LABEL_COLOR, fontSize: 10 },
    },
    series: [
      {
        name: '可用库存',
        type: 'bar',
        data: values.map((v: any, i: number) => ({
          value: v,
          itemStyle: { color: data[i].alert ? '#fda4af' : '#1677ff', borderRadius: [0, 6, 6, 0] },
        })),
        barWidth: '55%',
        label: { show: true, position: 'right', fontSize: 10, color: '#4e5969', formatter: '{c}' },
      },
      {
        name: '安全库存',
        type: 'line',
        data: safes,
        symbol: 'diamond',
        symbolSize: 9,
        lineStyle: { color: '#f59e0b', type: 'dashed', width: 1.5 },
        itemStyle: { color: '#f59e0b' },
        label: { show: true, position: 'right', fontSize: 9, color: '#b45309', formatter: '{c}' },
      },
    ],
  }
}

// 堆叠构成：合格 / 不良
function buildStackedBar(chart: any) {
  const d = chart.data || {}
  const cats = d.categories || []
  const series = (d.series || []).map((s: any, i: number) => ({
    name: s.name,
    type: 'bar',
    stack: 'total',
    data: s.data,
    barWidth: '50%',
    itemStyle: { color: i === 0 ? '#1677ff' : '#fda4af', borderRadius: i === 1 ? [6, 6, 0, 0] : 0 },
  }))
  return {
    color: ['#1677ff', '#fda4af'],
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { top: 0, textStyle: { color: LABEL_COLOR, fontSize: 11 } },
    grid: { left: 10, right: 16, top: 34, bottom: 6, containLabel: true },
    xAxis: {
      type: 'category', data: cats,
      axisLine: { lineStyle: { color: AXIS_COLOR } },
      axisTick: { show: false },
      axisLabel: { color: LABEL_COLOR, fontSize: 10, rotate: cats.length > 6 ? 22 : 0 },
    },
    yAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: SPLIT_COLOR } },
      axisLabel: { color: '#a9aeb8', fontSize: 10 },
    },
    series,
  }
}

// 饼图（环形）
function buildPie(chart: any) {
  return {
    color: COLORS,
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { bottom: 0, type: 'scroll', textStyle: { color: LABEL_COLOR, fontSize: 11 } },
    series: [{
      type: 'pie',
      radius: ['42%', '68%'],
      center: ['50%', '42%'],
      avoidLabelOverlap: true,
      itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
      label: { color: LABEL_COLOR, fontSize: 11 },
      data: (chart.data || []).map((d: any) => ({ name: d.name, value: d.value })),
    }],
  }
}

// 折线图
function buildLine(chart: any) {
  const names = (chart.data || []).map((d: any) => d.name)
  const values = (chart.data || []).map((d: any) => d.value)
  return {
    color: COLORS,
    tooltip: { trigger: 'axis', axisPointer: { type: 'line' } },
    grid: { left: 10, right: 16, top: 20, bottom: 6, containLabel: true },
    xAxis: {
      type: 'category', data: names,
      axisLine: { lineStyle: { color: AXIS_COLOR } },
      axisTick: { show: false },
      axisLabel: { color: LABEL_COLOR, fontSize: 10, rotate: names.length > 6 ? 22 : 0 },
    },
    yAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: SPLIT_COLOR } },
      axisLabel: { color: '#a9aeb8', fontSize: 10 },
    },
    series: [{
      type: 'line', data: values, smooth: true,
      itemStyle: { color: '#1d2129' },
      lineStyle: { color: '#1d2129', width: 2 },
      // 面积填充与线同色相（原来用的是 rgba(53,74,55,.08) 偏绿的灰，和全线蓝/深灰的体系对不上）
      areaStyle: { color: 'rgba(29,33,41,0.06)' },
      symbolSize: 5,
    }],
  }
}

// 普通柱状图
function buildBar(chart: any) {
  const names = (chart.data || []).map((d: any) => d.name)
  const values = (chart.data || []).map((d: any) => d.value)
  return {
    color: COLORS,
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 10, right: 16, top: 20, bottom: 6, containLabel: true },
    xAxis: {
      type: 'category', data: names,
      axisLine: { lineStyle: { color: AXIS_COLOR } },
      axisTick: { show: false },
      axisLabel: { color: LABEL_COLOR, fontSize: 10, rotate: names.length > 6 ? 22 : 0 },
    },
    yAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: SPLIT_COLOR } },
      axisLabel: { color: '#a9aeb8', fontSize: 10 },
    },
    series: [{
      type: 'bar', data: values,
      barWidth: '52%',
      itemStyle: { color: '#1677ff', borderRadius: [6, 6, 0, 0] },
    }],
  }
}

async function render() {
  if (!el.value) return
  // 通用图表：交给统一双引擎（ECharts + G2Plot）渲染，类型多样且与主题色板一致
  if (isGeneric.value) {
    disposeChart(el.value)
    await renderChart(el.value, props.chart.type, props.chart.columns, props.chart.rows, OPS_PALETTE)
    return
  }
  // 结构化图表（gauge/sunburst/pareto/stock-health/funnel/stacked-bar 等）：保留专用 builder
  // 修复（P1）：不再缓存实例到模块变量。disposeChart 内部就是
  // echarts.getInstanceByDom(el)?.dispose()，销毁的正是缓存的 inst，但本地变量仍 truthy，
  // 于是"通用图 → 结构化图"切换后会在已销毁实例上 setOption → 图表静默空白。
  // 改为每次按 DOM 反查，拿不到才 init。
  const chart = echarts.getInstanceByDom(el.value) ?? echarts.init(el.value)
  chart.setOption(buildOption(props.chart), true)
}

// 修复（P1）：原实现用 deep:true 监听整个 chart 对象（含上千行 rows 与嵌套 lineage），
// 既在首次挂载时递归建立全量依赖，又让任一内部字段变化触发整图重建。改为按标识 + 数据指纹比较。
watch(
  () => [props.chart?.id, props.chart?.type, props.chart?.rows?.length, props.chart?.data] as const,
  () => { nextTick(render) },
)

// 修复：接入 ResizeObserver。原实现没有任何尺寸自适应（AskPage 的共享 observer 只观察 .echart，
// 而本组件用的是 .chart-card-canvas），侧栏折叠动画/窗口缩放/栅格换列后画布会被裁切或拉伸模糊。
//
// 2026-09-28 修复（仪表盘指针不转的根因）：observer 挂上后浏览器会立刻回调一次（初始尺寸通知），
// 此时尺寸其实没变，但 resizeChart() → chart.resize() → ECharts 内部用
// `update({type:'resize', animation:{duration:0}})` 整图重绘一次 —— 入场动画被当场掐断：
// 仪表盘指针直接落在终值（实测旋转角从第 1 帧起就是 -8.2754 恒定），柱/线/旭日图同样没有入场动画。
// 所以这里只在「尺寸真的变了」时才 resize。
let ro: ResizeObserver | null = null
let lastW = -1
let lastH = -1
onMounted(() => {
  nextTick(render)
  if (el.value) {
    lastW = el.value.clientWidth
    lastH = el.value.clientHeight
    ro = new ResizeObserver(() => {
      const node = el.value
      if (!node) return
      const w = node.clientWidth
      const h = node.clientHeight
      if (w === lastW && h === lastH) return   // 尺寸未变 → 不 resize，保住入场动画
      lastW = w
      lastH = h
      resizeChart(node)
    })
    ro.observe(el.value)
  }
})
onBeforeUnmount(() => {
  try { ro?.disconnect() } catch { /* ignore */ }
  ro = null
  // disposeChart 已同时回收 ECharts 与 G2Plot 实例，无需再单独 dispose 缓存变量
  if (el.value) disposeChart(el.value)
})
</script>

<style scoped>
.chart-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 18px;
  border: 1px solid #e5e6eb;
  border-radius: 16px;
  background: #fff;
  box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04), 0 10px 28px -12px rgba(16, 24, 40, 0.08);
  transition: box-shadow .28s ease, border-color .28s ease, transform .28s ease;
}
.chart-card:hover {
  border-color: #cfe4ff;
  box-shadow: 0 2px 4px rgba(16, 24, 40, 0.04), 0 18px 44px -14px rgba(77, 158, 255, 0.22);
}
.chart-card-head {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.chart-card-title {
  font-size: 14.5px;
  font-weight: 600;
  color: #1d2129;
}
.chart-card-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: #a9aeb8;
}
.chart-card-lineage {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 1px 8px;
  border: 1px solid #e5e6eb;
  border-radius: 999px;
  background: #fff;
  color: #2E7CF0;
  font-size: 10.5px;
  line-height: 1.6;
  cursor: pointer;
  transition: all 0.18s ease;
}
.chart-card-lineage:hover {
  border-color: #4D9EFF;
  background: #4D9EFF;
  color: #ffffff;
}
.chart-card-canvas {
  width: 100%;
  height: 240px;
}
.chart-card-interp {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  border-radius: 10px;
  background: #f0f7ff;
  border: 1px solid #e1f0ff;
}
.chart-card-interp-icon {
  margin-top: 1px;
  flex-shrink: 0;
  color: #2E7CF0;
}
.chart-card-interp p {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.7;
  color: #4e5969;
}

/* ── 数据溯源弹窗（OPS 主题：米白卡片 + 灰绿边框 + 黄绿点缀）── */
.lineage-mask {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(29, 33, 41, 0.32);
  animation: chart-fade 0.18s ease both;
}
.lineage-dialog {
  width: min(560px, 92vw);
  max-height: 78vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid rgba(0, 0, 0, 0.06);
  border-radius: 16px;
  background: #fdfcf7;
  box-shadow: 0 18px 50px rgba(0, 0, 0, 0.18);
  animation: chart-pop 0.2s cubic-bezier(0.22, 0.61, 0.36, 1) both;
}
.lineage-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid #f2f3f5;
}
.lineage-title {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: 13.5px;
  font-weight: 600;
  color: #1d2129;
}
.lineage-title .app-icon {
  color: #1677ff;
}
.lineage-close {
  border: none;
  background: none;
  color: #a9aeb8;
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
  padding: 2px 6px;
  border-radius: 8px;
}
.lineage-close:hover {
  background: #f2f3f5;
  color: #1d2129;
}
.lineage-body {
  flex: 1;
  overflow-y: auto;
  padding: 14px 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.lineage-row {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.lineage-label {
  flex-shrink: 0;
  width: 56px;
  padding-top: 2px;
  font-size: 11px;
  color: #a9aeb8;
}
.lineage-value {
  flex: 1;
  font-size: 12.5px;
  color: #1d2129;
  line-height: 1.6;
}
.lineage-table {
  color: #a9aeb8;
  font-size: 11px;
  font-family: 'Rajdhani', 'SF Mono', Consolas, monospace;
}
.lineage-empty {
  color: #b7bdb4;
}
.lineage-fields {
  flex: 1;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.lineage-field {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 9px;
  border: 1px solid #e2e6da;
  border-radius: 999px;
  background: #fff;
  font-size: 11.5px;
}
.lineage-field-name {
  color: #1d2129;
  font-weight: 500;
}
.lineage-field-raw {
  color: #a9aeb8;
  font-size: 10.5px;
  font-family: 'Rajdhani', 'SF Mono', Consolas, monospace;
}
.lineage-field-role {
  padding: 1px 7px;
  border-radius: 999px;
  font-size: 10px;
  background: #f2f3f5;
  color: #4e5969;
}
/* 标签底色原来用橄榄绿 rgba(146,166,62)/rgba(53,74,55)，与字色（蓝/深灰）不同色相，像没改完的旧主题 */
.lineage-field-role.role-dim {
  background: rgba(22, 119, 255, 0.12);
  color: #1677ff;
}
.lineage-field-role.role-measure {
  background: rgba(29, 33, 41, 0.08);
  color: #1d2129;
}
.lineage-field-role.role-time {
  background: rgba(245, 158, 11, 0.14);
  color: #b45309;
}
@keyframes chart-fade {
  from { opacity: 0; }
  to { opacity: 1; }
}
@keyframes chart-pop {
  from { opacity: 0; transform: translateY(10px) scale(0.98); }
  to { opacity: 1; transform: none; }
}
</style>
