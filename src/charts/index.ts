/**
 * 图表渲染统一入口 —— Apache ECharts + AntV G2Plot 双引擎按类型分工
 *
 * 路由规则（自动，用户无感知）：
 *   ECharts  → bar / barh / line / area / stacked / pie / donut / scatter
 *              （常规统计图表，成熟稳定、按需引入体积可控）
 *   G2Plot   → radar / funnel / gauge / liquid / rose / heatmap / sunburst / treemap /
 *              waterfall / sankey / box / histogram / radial-bar / ring-progress /
 *              bullet / wordcloud
 *              （ECharts 未注册或表现力更强的扩展图表，动态 import 懒加载）
 *
 * 兜底链：目标引擎渲染失败 → 另一个引擎尝试 → 后端 matplotlib SVG，
 * 保证任何数据形态下结果都可见，绝不出现空白图表区。
 */
import echarts from '../echarts'
import { isG2PlotType, renderG2Plot, PALETTE, OPS_PALETTE, hexToRgba } from './g2plot'

export { PALETTE, OPS_PALETTE }

// 与 g2plot.ts 保持一致的文本/坐标轴灰阶
const TEXT_COLOR = '#5c6b7a'
const AXIS_COLOR = '#c9d3df'

// 两个引擎的实例需要分别记录：ECharts 用 echarts.getInstanceByDom 反查，
// G2Plot 实例挂到 DOM 上才能统一 dispose（否则 KeepAlive 切换后残留 canvas）。
const G2PLOT_KEY = '__g2plot_instance__'

const isNumCol = (col: string, rows: any[]): boolean => {
  for (const r of rows.slice(0, 8)) {
    const v = r?.[col]
    if (v === null || v === undefined || v === '') continue
    if (typeof v === 'number') return true
    const s = String(v).trim().replace(/,/g, '')
    if (s === '') continue
    return !isNaN(Number(s))
  }
  return false
}

// 数值解析（P2 修复）：兼容千分位字符串 "1,234"（G2Plot 侧已剥离逗号，
// ECharts 侧此前 Number("1,234")=NaN → 画成 0，两引擎视觉不一致）
const toNum = (v: any): number => {
  if (v === null || v === undefined || v === '') return 0
  const n = Number(String(v).replace(/,/g, '').trim())
  return Number.isFinite(n) ? n : 0
}

/**
 * 构建 ECharts option —— 视觉重做版（现代 BI 风格）
 *
 * 设计要点（相比朴素默认样式）：
 * - 渐变填充：柱状/面积用同色系竖向渐变，比纯色更有层次
 * - 圆角：柱体顶部（横向柱为右侧）6px 圆角，饼/环图扇区间留白 + 圆角
 * - 坐标轴弱化：隐藏轴线与刻度，仅保留淡色虚线分割线，画面更干净
 * - 折线平滑 + 加粗，拐点用小圆点，堆叠图顶部圆角
 * - Tooltip 卡片化：白底 + 圆角 + 阴影，视觉更轻
 * - 统一 800ms cubicOut 入场动画
 */
/** ECharts 侧显式支持的图型白名单（修复用途，见 buildEChartOption 内注释） */
const ECHARTS_TYPES = new Set([
  'bar', 'barh', 'line', 'area', 'stacked', 'pie', 'donut', 'scatter',
])

export function buildEChartOption(type: string, cols: string[], rows: any[], palette?: string[]): any {
  // 修复：原实现是"排除法"——最后一段是无条件柱状图兜底，没有 default 分支。
  // 于是后端新增/改名图型、或传入 'table'/空串/大小写混写时，会被静默画成柱状图。
  // 这比空白更危险（用户会依据"看起来正常但语义错误"的图做决策，回归测试也发现不了）。
  // 现在改为白名单：未命中一律返回 null，交由调用方回退 SVG。
  const _t = String(type || '').toLowerCase()
  if (!ECHARTS_TYPES.has(_t)) return null
  type = _t
  const pal = palette || PALETTE
  const numCols = cols.filter((c) => isNumCol(c, rows))
  const catCol = cols.find((c) => !isNumCol(c, rows)) || cols[0]

  // 通用卡片化 tooltip
  const tooltip = {
    trigger: (type === 'pie' || type === 'donut' || type === 'scatter') ? 'item' : 'axis',
    backgroundColor: 'rgba(255,255,255,0.97)',
    borderColor: '#eef2f7',
    borderWidth: 1,
    padding: [10, 14],
    borderRadius: 10,
    textStyle: { color: TEXT_COLOR, fontSize: 12 },
    extraCssText: 'box-shadow:0 6px 24px rgba(20,40,80,.12);',
    axisPointer: { type: 'shadow', shadowStyle: { color: 'rgba(91,143,249,0.07)' } },
  }
  const legend = {
    top: 0,
    icon: 'roundRect',
    itemWidth: 10,
    itemHeight: 10,
    itemGap: 16,
    textStyle: { color: TEXT_COLOR, fontSize: 12 },
  }
  // 弱化坐标轴：不画轴线/刻度，只留虚线分割线
  const valueAxis = {
    type: 'value',
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: AXIS_COLOR, fontSize: 11 },
    splitLine: { lineStyle: { color: '#f1f5f9', type: 'dashed' } },
    nameTextStyle: { color: TEXT_COLOR, fontSize: 11, padding: [0, 0, 6, 0] },
  }

  // ── 饼图 / 环形图：扇区圆角 + 白色间隙 ──
  if (type === 'pie' || type === 'donut') {
    // 修复：原为 `numCols[0] || cols[cols.length - 1]`，无度量列时退化取最后一列；
    // 若该列是文本，toNum 全部返回 0 → 静默绘出一张空白饼图（无扇区、无报错），
    // 恰好违反本文件"绝不出现空白图表区"的承诺。此处与 scatter/柱线分支对齐：不成图即返回 null。
    const vCol = numCols[0]
    if (!vCol) return null
    if (rows.length && rows.every((r) => !toNum(r?.[vCol]))) return null
    return {
      color: pal,
      animationDuration: 800,
      animationEasing: 'cubicOut',
      tooltip: { ...tooltip, formatter: '{b}<br/>{c}（{d}%）' },
      legend: { ...legend, bottom: 0, top: 'auto' },
      series: [{
        type: 'pie',
        radius: type === 'donut' ? ['48%', '72%'] : ['0%', '68%'],
        center: ['50%', type === 'donut' ? '46%' : '48%'],
        avoidLabelOverlap: true,
        itemStyle: { borderRadius: 8, borderColor: '#fff', borderWidth: 2 },
        label: {
          color: TEXT_COLOR, fontSize: 11,
          formatter: '{b}\n{d}%',
          lineHeight: 16,
        },
        labelLine: { length: 10, length2: 10, lineStyle: { color: '#dbe3ec' } },
        emphasis: {
          scaleSize: 6,
          itemStyle: { shadowBlur: 18, shadowColor: 'rgba(20,40,80,0.18)' },
        },
        data: rows.map((r) => ({ name: String(r?.[catCol] ?? ''), value: toNum(r?.[vCol]) })),
      }],
    }
  }

  // ── 散点图：半透明圆点 + 描边 ──
  if (type === 'scatter') {
    const x = numCols[0], y = numCols[1]
    if (!x || !y) return null
    return {
      color: pal,
      animationDuration: 800,
      tooltip,
      grid: { left: 56, right: 28, top: 24, bottom: 44 },
      xAxis: { ...valueAxis, name: x, splitLine: { show: false } },
      yAxis: { ...valueAxis, name: y },
      series: [{
        type: 'scatter',
        symbolSize: 12,
        itemStyle: {
          color: hexToRgba(pal[0], 0.75),
          borderColor: '#fff',
          borderWidth: 1.5,
        },
        emphasis: { itemStyle: { color: pal[0], borderColor: '#fff', borderWidth: 2 } },
        data: rows.map((r) => [toNum(r?.[x]), toNum(r?.[y])]),
      }],
    }
  }

  if (numCols.length === 0) return null
  const cats = rows.map((r) => String(r?.[catCol] ?? ''))
  const horizontal = type === 'barh'
  const isLine = type === 'line' || type === 'area'
  const single = numCols.length === 1   // 单系列才加渐变（多系列渐变易显脏）

  const series = numCols.map((c, i) => {
    const color = pal[i % pal.length]
    const base: any = { name: c, type: isLine ? 'line' : 'bar' }
    if (isLine) {
      base.smooth = true                       // 平滑曲线
      base.symbol = 'circle'
      base.symbolSize = 7
      base.showSymbol = rows.length <= 30      // 点太密时隐藏拐点
      base.lineStyle = { width: 3, color }
      base.itemStyle = { color, borderColor: '#fff', borderWidth: 2 }
      if (type === 'area') {
        base.areaStyle = {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: hexToRgba(color, 0.42) },
            { offset: 1, color: hexToRgba(color, 0.02) },
          ]),
        }
      }
    } else {
      // 柱状：顶部圆角 + 竖向渐变
      base.barMaxWidth = 34
      base.itemStyle = {
        borderRadius: horizontal ? [0, 6, 6, 0] : [6, 6, 0, 0],
        color: single
          ? new echarts.graphic.LinearGradient(
              horizontal ? 0 : 0, horizontal ? 0 : 0,
              horizontal ? 1 : 0, horizontal ? 0 : 1,
              [
                { offset: 0, color: hexToRgba(color, 0.95) },
                { offset: 1, color: hexToRgba(color, 0.45) },
              ])
          : color,
      }
      base.emphasis = { itemStyle: { color } }
      if (type === 'stacked') base.stack = 'total'
    }
    base.data = rows.map((r) => toNum(r?.[c]))
    return base
  })

  // 类目轴：倾斜标签只对「横轴」成立。横向条形图里类目轴是纵轴，再给它加 rotate
  // 会让每个标签多占近一倍行高，13 个类目在 240px 画布里叠成一团黑（实测 id 类
  // 标签互相压字、完全读不出）。纵轴不倾斜，交给 ECharts 自己按高度抽稀。
  const catAxis = {
    type: 'category',
    data: cats,
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: AXIS_COLOR, fontSize: 11 },
  }
  const catAxisX = {
    ...catAxis,
    axisLabel: { ...catAxis.axisLabel, rotate: cats.length > 8 ? 30 : 0 },
  }

  return {
    color: pal,
    animationDuration: 800,
    animationEasing: 'cubicOut',
    tooltip,
    legend: numCols.length > 1 ? legend : { show: false },
    grid: {
      left: 20, right: 24, top: numCols.length > 1 ? 40 : 28, bottom: 12,
      containLabel: true,
    },
    xAxis: horizontal ? valueAxis : catAxisX,
    yAxis: horizontal ? catAxis : valueAxis,
    series,
  }
}

/** 销毁容器上可能存在的两个引擎实例 */
export function disposeChart(el: HTMLElement) {
  try {
    echarts.getInstanceByDom(el)?.dispose()
  } catch { /* 忽略 */ }
  try {
    const p = (el as any)[G2PLOT_KEY]
    if (p) {
      p.destroy?.()
      ;(el as any)[G2PLOT_KEY] = null
    }
  } catch { /* 忽略 */ }
}

/** 容器尺寸变化时让当前实例自适应（两个引擎 API 不同，分别处理） */
export function resizeChart(el: HTMLElement) {
  try {
    echarts.getInstanceByDom(el)?.resize()
  } catch { /* 忽略 */ }
  try {
    ;(el as any)[G2PLOT_KEY]?.changeSize?.(el.clientWidth, el.clientHeight)
  } catch { /* 忽略 */ }
}

/**
 * 渲染单个图表容器。
 * @param el 容器 DOM（需有固定高度）
 * @param type 图表类型
 * @param cols 列名
 * @param rows 数据行
 * @returns 实际使用的引擎名（'echarts' | 'g2plot' | null 表示全部失败需回退 SVG）
 */
/** 渲染序号：同一容器上的并发渲染，只有最后一次允许写结果 */
const RENDER_SEQ = new WeakMap<HTMLElement, number>()

export async function renderChart(
  el: HTMLElement,
  type: string,
  cols: string[],
  rows: any[],
  palette?: string[],
): Promise<'echarts' | 'g2plot' | null> {
  const t = String(type || '').toLowerCase()
  if (!rows.length || !cols.length) return null

  // 修复：renderChart 是 async，首次渲染要等动态 import（可达数百 ms）。
  // 原实现无并发保护，用户快速切换图型时，后完成的"旧"渲染会覆盖"新"渲染 →
  // 图表与当前查询条件不一致，且 G2Plot 与 ECharts 会在同一容器叠加。
  const seq = (RENDER_SEQ.get(el) || 0) + 1
  RENDER_SEQ.set(el, seq)
  const isStale = () => RENDER_SEQ.get(el) !== seq

  const wantG2 = isG2PlotType(t)
  // 首选引擎
  if (wantG2) {
    const plot = await renderG2Plot(el, t, cols, rows, palette)
    if (plot) {
      if (isStale()) {
        // 已被更新的渲染取代：销毁本次结果，避免两个引擎在同一容器叠加
        try { (plot as any)?.destroy?.() } catch { /* ignore */ }
        return null
      }
      ;(el as any)[G2PLOT_KEY] = plot
      return 'g2plot'
    }
    // 修复：G2Plot 构造阶段已插入 canvas，render 抛错时实例不会 destroy →
    // 必须先清场再用 ECharts 兜底，否则出现双 canvas 残影与监听器泄漏。
    disposeChart(el)
    // G2Plot 失败 → ECharts 兜底（用 bar 这种通用类型，至少结果可见）
    const fb = buildEChartOption('bar', cols, rows, palette)
    if (fb) {
      try {
        echarts.init(el).setOption(fb)
        if (isStale()) return null
        return 'echarts'
      } catch { /* 继续回退 */ }
    }
    return null
  }

  // ECharts 常规图：失败即回退后端 SVG（常规图失败概率低，且后端 SVG 一定能显示，
  // 不再做 G2Plot 二次兜底，避免无谓的 chunk 加载）
  const option = buildEChartOption(t, cols, rows, palette)
  if (option) {
    try {
      disposeChart(el) // 清掉残留旧实例，避免 echarts 在"已销毁实例的 DOM"上复用
      echarts.init(el).setOption(option)
      if (isStale()) return null
      return 'echarts'
    } catch { /* 回退 SVG */ }
  }
  return null
}
