/**
 * AntV G2Plot 渲染封装 — 与 Apache ECharts 分工协作
 *
 * 分工原则（按图表类型自动路由，用户无感知，各自发挥优势）：
 * - Apache ECharts：常规统计图表（柱状/条形/折线/面积/堆叠/饼图/环形/散点），
 *   已有成熟实现且按需引入，体积可控、交互稳定。
 * - AntV G2Plot：ECharts 未注册或表现力更强的扩展图表 —— 雷达图、漏斗图、仪表盘、
 *   玫瑰图、水波图、热力图、旭日图、矩形树图、瀑布图、桑基图、箱线图、直方图、
 *   玉玦图、环形进度、子弹图、词云等。
 *
 * 体积控制：G2Plot 包体较大，统一走动态 import() 懒加载 —— 只有实际命中扩展图表
 * 类型时 Vite 才去加载对应 chunk，不进主包、不影响首屏。
 *
 * 兜底：任何一步失败（类型不支持 / 数据不足 / 渲染异常）都返回 null，
 * 由调用方回退到后端 matplotlib 生成的 SVG，保证结果永远可见。
 */

// 统一配色（AntV 经典色板，比原色更柔和高级）：ECharts 与 G2Plot 共用，切换引擎视觉不割裂
export const PALETTE = [
  '#5B8FF9', '#4FB3FF', '#F6BD16', '#E8684A', '#6DC8EC',
  '#9270CA', '#FF9D4D', '#269A99', '#FF99C3', '#A78BFA',
]

// OPS 主题色板（总览页/青柠+森林绿）：贴合前端 style.css 设计语言，与 PALETTE 区分，
// 供总览页等需要与品牌视觉统一的位置按需传入，不影响问答页既有蓝系配色。
// 修复：原色板 8 个槽位里 '#1677ff' 重复 5 次、'#1d2129' 重复 2 次（仅 3 个唯一色），
// 导致多系列/多分类图（多指标雷达、多系列柱、饼图/玫瑰图）出现多个分类同色、图例无法对应。
// 现改为同一品牌色系的不同亮度，保证每个槽位唯一。
export const OPS_PALETTE = [
  '#1d2129', '#1677ff', '#3b82f6', '#60a5fa',
  '#93c5fd', '#0e7490', '#4D9EFF', '#64748b',
]

// 通用文本色（坐标轴/图例），统一灰阶让画面更干净
const TEXT_COLOR = '#5c6b7a'
const AXIS_COLOR = '#c9d3df'

// 2026-10-01 清理：删除死导出 verticalGradient——全项目无任何调用方，
// ECharts 侧渐变都是各自实现的（charts/index.ts 内联 LinearGradient）。

/** #RRGGBB → rgba(r,g,b,a) */
export function hexToRgba(hex: string, alpha = 1): string {
  const h = (hex || '').replace('#', '')
  const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h
  const num = parseInt(full || '000000', 16)
  const r = (num >> 16) & 255
  const g = (num >> 8) & 255
  const b = num & 255
  return `rgba(${r},${g},${b},${alpha})`
}

/** 由 AntV G2Plot 承接的扩展图表类型（ECharts 侧不处理这些） */
export const G2PLOT_TYPES = [
  'radar', 'funnel', 'gauge', 'liquid', 'rose', 'heatmap',
  'sunburst', 'treemap', 'waterfall', 'sankey', 'box', 'histogram',
  'radial-bar', 'ring-progress', 'bullet', 'wordcloud',
]

/**
 * 各图表类型的**按需加载器**：只 import 对应 plot 目录，而不是整个 '@antv/g2plot'。
 *
 * 为什么不用 `import('@antv/g2plot')` 全量引入：
 * 1) 全量会把 30+ 种 plot 全打进来（含本项目用不到的 scatter/stock/violin 等），
 *    单 chunk 达 800KB+，而实际同时只用到一种；
 * 2) 部分 plot 依赖额外三方包（如 scatter 依赖 d3-regression），全量引入会连带
 *    要求这些依赖存在，缺一个就整个构建失败。
 *
 * 按需引入后 Vite 为每种 plot 生成独立 chunk，共享的 G2 核心自动提取为公共 chunk，
 * 命中哪类图才下载哪类，首屏零成本。
 *
 * 注意：路径必须是静态字符串（Vite 才能静态分析并 code-split），故用显式映射表。
 */
const LOADERS: Record<string, () => Promise<any>> = {
  'radar': () => import('@antv/g2plot/esm/plots/radar'),
  'funnel': () => import('@antv/g2plot/esm/plots/funnel'),
  'gauge': () => import('@antv/g2plot/esm/plots/gauge'),
  'liquid': () => import('@antv/g2plot/esm/plots/liquid'),
  'rose': () => import('@antv/g2plot/esm/plots/rose'),
  'heatmap': () => import('@antv/g2plot/esm/plots/heatmap'),
  'sunburst': () => import('@antv/g2plot/esm/plots/sunburst'),
  'treemap': () => import('@antv/g2plot/esm/plots/treemap'),
  'waterfall': () => import('@antv/g2plot/esm/plots/waterfall'),
  'sankey': () => import('@antv/g2plot/esm/plots/sankey'),
  'box': () => import('@antv/g2plot/esm/plots/box'),
  'histogram': () => import('@antv/g2plot/esm/plots/histogram'),
  'radial-bar': () => import('@antv/g2plot/esm/plots/radial-bar'),
  'ring-progress': () => import('@antv/g2plot/esm/plots/ring-progress'),
  'bullet': () => import('@antv/g2plot/esm/plots/bullet'),
  'wordcloud': () => import('@antv/g2plot/esm/plots/word-cloud'),
}

export function isG2PlotType(type: string): boolean {
  return !!type && G2PLOT_TYPES.includes(String(type).toLowerCase())
}

// ── 列类型判定（与 charts/index.ts 的 isNumCol 同口径：采样前 8 行，空值跳过；
// 2026-10-01 修复：剥离千分位逗号，此前 "1,234" 在本侧判为类别列、ECharts 侧判为数值列，
// 两引擎 catCol/numCols 选择分叉导致维度错位）──
const isNumCol = (col: string, rows: any[]): boolean => {
  // 2026-10-01 修复：与 charts/index.ts 同口径——整窗多数决（≥70% 可转数字才算数值列），
  // 原实现第一个非空值就 return，前 8 行全 NULL 的数值列会被误判为类目轴
  let numCount = 0
  let nonEmpty = 0
  for (const r of rows.slice(0, 8)) {
    const v = r?.[col]
    if (v === null || v === undefined || String(v).trim() === '') continue
    nonEmpty++
    if (typeof v === 'number' && Number.isFinite(v)) { numCount++; continue }
    const s = String(v).trim().replace(/,/g, '')
    if (s !== '' && !isNaN(Number(s))) numCount++
  }
  if (nonEmpty === 0) return false
  return numCount / nonEmpty >= 0.7
}

const numColsOf = (cols: string[], rows: any[]) => cols.filter((c) => isNumCol(c, rows))
const catColOf = (cols: string[], rows: any[]) => cols.find((c) => !isNumCol(c, rows)) || cols[0]

/** 安全取数：无法转数字时返回 0（G2Plot 遇到 NaN/null 会渲染空白） */
const n = (v: any): number => {
  if (v === null || v === undefined || v === '') return 0
  const x = Number(String(v).replace(/,/g, ''))
  return isNaN(x) ? 0 : x
}

/**
 * 修复：区分"缺失"与"真实的 0"。
 * 原实现用 n() 取数做统计，缺失值被塌缩成 0，而随后的 filter(Number.isFinite) 是死代码
 * （n() 永远返回有限数）→ 缺失值以"真实 0 观测"进入箱线图/直方图：
 * 箱线图的下限被钉在 0、Q1/中位数左移，直方图最左侧多出一根虚假的 0 值尖峰。
 * 质量/设备类指标（如 downtime_minutes 大量空值）会被画成"下限为 0、分布左偏"的假象。
 * 统计类图必须用本函数，把缺失值真正剔除。
 */
const nOrNull = (v: any): number | null => {
  if (v === null || v === undefined || v === '') return null
  const x = Number(String(v).replace(/,/g, ''))
  return isFinite(x) ? x : null
}

/** 单值型图表（仪表盘/水波/环形进度）的百分比：0~1
 *  - 值本身在 (0,1] 且是比率语义 → 直接用；
 *  - 否则按「首个值 / 该列最大值」归一，避免 100 分制被当成 10000%。 */
function percentOf(rows: any[], valueCol: string): number | null {
  const vals = rows.map((r) => n(r?.[valueCol])).filter((v) => Number.isFinite(v))
  if (!vals.length) return null
  const first = vals[0]
  if (first > 0 && first <= 1) return first
  // 2026-10-02 修复（潜伏准确性 bug）：值在 (1,100] 区间（如综合良率 97.56）是
  // 百分比语义，直接 /100 使用。原实现走 first/max 归一——单行数据 first===max
  // 恒等于 1，仪表盘把 97.56% 画成 100%（"看起来正常但语义错误"，比空白更危险）。
  // >100 的非比率量（产量/数量等）才按列最大值归一。
  if (first > 1 && first <= 100) return Math.min(1, Math.max(0, first / 100))
  const max = Math.max(...vals.map(Math.abs))
  if (!max) return null
  return Math.min(1, Math.max(0, first / max))
}

/** 分位数（箱线图用）：线性插值法，q ∈ [0,1] */
function quantile(sorted: number[], q: number): number {
  if (!sorted.length) return 0
  const pos = (sorted.length - 1) * q
  const base = Math.floor(pos)
  const rest = pos - base
  if (sorted[base + 1] !== undefined) {
    return sorted[base] + rest * (sorted[base + 1] - sorted[base])
  }
  return sorted[base]
}

/**
 * 构建 G2Plot 图表配置。
 * 返回 null 表示「该类型无法用当前数据渲染」，调用方应回退 SVG。
 */
// 2026-10-01 清理：取消 export（仅本文件内部使用），避免死导出扩大模块公共面
function buildG2PlotConfig(type: string, cols: string[], rows: any[], palette?: string[]): Record<string, any> | null {
  const t = String(type || '').toLowerCase()
  if (!rows.length || !cols.length) return null
  const pal = palette || PALETTE
  const nums = numColsOf(cols, rows)
  const cat = catColOf(cols, rows)

  // 统一基础配置：现代 BI 风格（平滑动画、弱化网格、卡片化 tooltip）
  const base: Record<string, any> = {
    autoFit: true,
    color: pal,
    appendPadding: [16, 16, 16, 16],
    animation: { appear: { animation: 'wave-in', duration: 700 }, update: { duration: 400 } },
    // 坐标轴弱化：隐藏轴线/刻度，只留淡色虚线网格
    xAxis: {
      line: { style: { stroke: 'transparent' } },
      tickLine: null,
      label: { style: { fill: AXIS_COLOR, fontSize: 11 } },
      grid: null,
    },
    yAxis: {
      line: null,
      tickLine: null,
      label: { style: { fill: AXIS_COLOR, fontSize: 11 } },
      grid: { line: { style: { stroke: '#f1f5f9', lineDash: [4, 4] } } },
    },
    tooltip: { showMarkers: true, shared: true },
    legend: { position: 'top-left', itemName: { style: { fill: TEXT_COLOR, fontSize: 12 } } },
  }

  switch (t) {
    // 雷达图：维度做角度轴、各数值列做系列，适合「多指标横评」
    case 'radar': {
      if (!nums.length) return null
      const data: any[] = []
      for (const r of rows.slice(0, 30)) {
        for (const c of nums) {
          data.push({ name: String(r?.[cat] ?? ''), type: c, value: n(r?.[c]) })
        }
      }
      return {
        ...base,
        data,
        xField: 'name',
        yField: 'value',
        seriesField: 'type',
        meta: { value: { min: 0 } },
        xAxis: { line: null, tickLine: null, grid: { line: { style: { lineDash: null } } } },
        yAxis: { label: false, grid: { alternateColor: 'rgba(0,0,0,0.04)' } },
        // 雷达图细节：粗线 + 白色描边圆点，单系列时叠加半透明填充
        line: { style: { lineWidth: 2.5 } },
        point: { size: 3.5, style: { fill: '#fff', lineWidth: 2 } },
        area: nums.length === 1 ? { style: { fillOpacity: 0.16 } } : undefined,
      }
    }

    // 漏斗图：适用于「逐级递减的转化/数量」场景
    case 'funnel': {
      const v = nums[0]
      if (!v) return null
      const data = rows.slice(0, 12).map((r) => ({ stage: String(r?.[cat] ?? ''), value: n(r?.[v]) }))
      return {
        ...base,
        data,
        xField: 'stage',
        yField: 'value',
        // 白字标签 + 圆角漏斗 + 右侧转化率标注
        label: {
          formatter: (d: any) => `${d.stage}  ${d.value}`,
          fill: '#fff', fontSize: 12,
        },
        conversionTag: {
          offsetX: 8,
          text: { fill: TEXT_COLOR, fontSize: 11 },
          spacing: 4,
        },
        funnelStyle: { stroke: '#fff', lineWidth: 2, radius: [6, 6, 6, 6] },
      }
    }

    // 仪表盘 / 水波图 / 环形进度：单一指标的达成率
    case 'gauge':
    case 'liquid':
    case 'ring-progress': {
      const v = nums[0]
      if (!v) return null
      const percent = percentOf(rows, v)
      if (percent === null) return null
      if (t === 'liquid') {
        return { ...base, percent, outline: { border: 2, distance: 4 }, wave: { length: 128 } }
      }
      if (t === 'ring-progress') {
        return { ...base, percent, color: [pal[0], '#e8e8e8'] }
      }
      return {
        ...base,
        percent,
        range: {
          color: `l(0) 0:${hexToRgba(pal[0], 0.2)} 1:${pal[0]}`,
          ticks: [0, percent, 1],
        },
        statistic: {
          content: {
            formatter: () => `${Math.round(percent * 100)}%`,
            style: { fontSize: 28, fontWeight: 600, fill: '#334155' },
          },
        },
        indicator: {
          pointer: { style: { stroke: '#94a3b8', lineWidth: 2 } },
          pin: { style: { stroke: '#94a3b8', lineWidth: 2 } },
        },
        axis: {
          label: { formatter: (v: any) => `${Math.round(Number(v) * 100)}%` },
          subTickLine: { count: 3 },
        },
      }
    }

    // 玫瑰图（南丁格尔玫瑰）：占比类数据，比饼图更有面积对比
    case 'rose': {
      const v = nums[0]
      if (!v) return null
      const data = rows.slice(0, 12).map((r) => ({ type: String(r?.[cat] ?? ''), value: n(r?.[v]) }))
      return {
        ...base,
        data,
        xField: 'type',
        yField: 'value',
        seriesField: 'type',
        radius: 0.92,
        innerRadius: 0.22,
        // 扇区白色描边 + 圆角，更有"设计感"的玫瑰图
        sectorStyle: { stroke: '#fff', lineWidth: 1.5 },
        label: { offset: 10, fields: ['type'], style: { fill: TEXT_COLOR, fontSize: 11 } },
      }
    }

    // 热力图：需要 2 个维度 + 1 个数值；只有一个维度时用行号兜底做 y
    case 'heatmap': {
      const v = nums[0]
      if (!v) return null
      const cats = cols.filter((c) => !isNumCol(c, rows))
      const xCol = cats[0] || cat
      const yCol = cats[1]
      const data = rows.slice(0, 200).map((r, i) => ({
        x: String(r?.[xCol] ?? ''),
        y: yCol ? String(r?.[yCol] ?? '') : `#${i + 1}`,
        value: n(r?.[v]),
      }))
      return {
        ...base,
        data,
        xField: 'x',
        yField: 'y',
        colorField: 'value',
        // 现代冷色调渐变（浅色 → 主题色），格子圆角更柔和
        color: [hexToRgba(pal[0], 0.12), pal[0]],
        meta: { x: { type: 'cat' }, y: { type: 'cat' } },
        shape: 'square',
        cellStyle: { radius: 3 },
      }
    }

    // 旭日图 / 矩形树图：层级占比（SQL 结果是扁平的，这里构建一层 root → 各项）
    case 'sunburst':
    case 'treemap': {
      const v = nums[0]
      if (!v) return null
      const children = rows.slice(0, 20).map((r) => ({
        name: String(r?.[cat] ?? ''),
        value: Math.max(0, n(r?.[v])),
      }))
      const data = { name: '总计', children }
      return {
        ...base,
        data,
        ...(t === 'sunburst'
          ? { innerRadius: 0.25, label: { fields: ['name'] } }
          : { colorField: 'name', legend: false }),
      }
    }

    // 瀑布图：逐项的增减贡献（正累积/负递减）
    case 'waterfall': {
      const v = nums[0]
      if (!v) return null
      const data = rows.slice(0, 20).map((r) => ({
        x: String(r?.[cat] ?? ''),
        y: n(r?.[v]),
      }))
      return {
        ...base,
        data,
        xField: 'x',
        yField: 'y',
        // 连接线 + 顶部数值标签，柱体白色描边
        label: { fields: ['y'], style: { fill: TEXT_COLOR, fontSize: 11 } },
        leaderLine: { style: { stroke: '#c9d3df', lineDash: [3, 3] } },
        columnStyle: { stroke: '#fff', lineWidth: 1.5, radius: [4, 4, 0, 0] },
      }
    }

    // 桑基图：需要「两个类别列（源→目标）+ 一个权重列」；
    // 若只有 1 个类别列，不能拿数值列当 target（会把 "100" 这种数字当成节点名，语义全错）。
    case 'sankey': {
      const cats = cols.filter((c) => !isNumCol(c, rows))
      const w = nums[0]
      if (!w || cats.length < 2) return null
      const [src, tgt] = cats
      const data = rows.slice(0, 60).map((r) => ({
        source: String(r?.[src] ?? ''),
        target: String(r?.[tgt] ?? ''),
        value: Math.max(0, n(r?.[w])),
      }))
      return { ...base, data, sourceField: 'source', targetField: 'target', weightField: 'value' }
    }

    // 箱线图：对每个数值列计算五数概括（min/Q1/median/Q3/max）
    case 'box': {
      if (!nums.length) return null
      const data = nums.slice(0, 8).map((c) => {
        // 修复：改用 nOrNull 真正剔除缺失值（原 n() 会把 null 变 0，且 filter 是死代码）
        const vals = rows.map((r) => nOrNull(r?.[c])).filter((v): v is number => v !== null).sort((a, b) => a - b)
        if (vals.length < 5) return null // 样本过少时箱线图统计量不可信
        return {
          x: c,
          low: vals[0],
          q1: quantile(vals, 0.25),
          median: quantile(vals, 0.5),
          q3: quantile(vals, 0.75),
          high: vals[vals.length - 1],
        }
      }).filter(Boolean)
      if (!data.length) return null
      return { ...base, data, xField: 'x', yField: ['low', 'q1', 'median', 'q3', 'high'] }
    }

    // 直方图：单个数值列的分布（自动分箱）
    case 'histogram': {
      const v = nums[0]
      if (!v) return null
      // 修复：剔除缺失值，避免最左侧出现虚假的 0 值尖峰
      const data = rows
        .map((r) => nOrNull(r?.[v]))
        .filter((x): x is number => x !== null)
        .map((x) => ({ value: x }))
      if (!data.length) return null
      return {
        ...base,
        data,
        binField: 'value',
        binNumber: Math.min(20, Math.max(5, Math.ceil(data.length / 3))),
        // 圆角柱 + 主题色填充
        columnStyle: { fill: hexToRgba(pal[0], 0.85), radius: [4, 4, 0, 0] },
      }
    }

    // 玉玦图（环形柱状）：排名类数据的另一种呈现
    case 'radial-bar': {
      const v = nums[0]
      if (!v) return null
      const data = rows.slice(0, 12).map((r) => ({ name: String(r?.[cat] ?? ''), value: n(r?.[v]) }))
      return {
        ...base,
        data,
        xField: 'name',
        yField: 'value',
        maxAngle: 270,
        radius: 0.85,
        innerRadius: 0.25,
        // 圆角条 + 白色描边
        barStyle: { stroke: '#fff', lineWidth: 1.5, radius: 6 },
        label: { text: 'name', fill: TEXT_COLOR, fontSize: 11 },
      }
    }

    // 子弹图：实际值 vs 目标值（用该列均值作目标、最大值作区间）
    case 'bullet': {
      const v = nums[0]
      if (!v) return null
      const vals = rows.map((r) => n(r?.[v])).filter((x) => Number.isFinite(x))
      if (!vals.length) return null
      const max = Math.max(...vals)
      const avg = vals.reduce((a, b) => a + b, 0) / vals.length
      const data = rows.slice(0, 10).map((r) => ({
        title: String(r?.[cat] ?? ''),
        ranges: [max],
        measures: [n(r?.[v])],
        target: [avg],
      }))
      return { ...base, data, rangeField: 'ranges', measureField: 'measures', targetField: 'target', xField: 'title' }
    }

    // 词云：文本维度 + 数值权重
    case 'wordcloud': {
      const v = nums[0]
      if (!v) return null
      const data = rows.slice(0, 50).map((r) => ({
        word: String(r?.[cat] ?? ''),
        weight: Math.max(0, n(r?.[v])),
      })).filter((d) => d.word)
      if (!data.length) return null
      return { ...base, data, wordField: 'word', weightField: 'weight', color: pal, wordStyle: { fontFamily: 'Microsoft YaHei' } }
    }

    default:
      return null
  }
}

/**
 * 用 G2Plot 渲染图表到容器。
 * @returns 成功返回 plot 实例（需调用 .destroy() 释放），失败返回 null（调用方回退 SVG）
 */
export async function renderG2Plot(
  el: HTMLElement,
  type: string,
  cols: string[],
  rows: any[],
  palette?: string[],
): Promise<any | null> {
  const t = String(type).toLowerCase()
  const config = buildG2PlotConfig(t, cols, rows, palette)
  if (!config) return null
  const loader = LOADERS[t]
  if (!loader) return null

  // 修复：构造前校验容器尺寸。autoFit:true 依赖容器已有宽高，
  // 折叠面板/Tab 未激活/弹窗刚插入等 0×0 场景下 render() 会抛错，
  // 表现为"扩展图静默降级成柱状图"，且没有任何日志可查。这里等一帧重试，仍为 0 则放弃。
  if (el.clientWidth < 8 || el.clientHeight < 8) {
    // 2026-10-02 修复：后台/隐藏 Tab 里 requestAnimationFrame 不触发，纯 rAF 等待会
    // 永久挂起——DashboardPanel.renderCards 用 Promise.all 等全部 renderChart，
    // 用户渲染期间切走 Tab → 看板 loading 永远不结束。改为 rAF 与 setTimeout 竞速
    // （后台下 setTimeout 被节流到 ≥1s 但仍会触发，保证等待有上界）。
    await Promise.race([
      new Promise((r) => requestAnimationFrame(() => r(null))),
      new Promise((r) => setTimeout(r, 120)),
    ])
    if (el.clientWidth < 8 || el.clientHeight < 8) return null
  }

  let plot: any = null
  try {
    // 按需动态 import：每个 plot 独立 chunk，命中哪类图才下载哪类
    const mod: any = await loader()
    const PlotClass = mod?.default ?? mod  // esm/plots/* 以 default 导出
    if (typeof PlotClass !== 'function') {
      console.warn('[chart] g2plot 模块形态异常，无法取得构造函数：', t)
      return null
    }
    plot = new PlotClass(el, config)
    plot.render()
    return plot
  } catch (e) {
    // 修复：构造函数阶段已向容器插入 canvas 并注册监听器，渲染失败时若直接 return
    // 会留下一个既不返回、也不销毁的半成品实例（随后 ECharts 兜底 → 双 canvas 残影 + 监听器泄漏）。
    try { plot?.destroy?.() } catch { /* ignore */ }
    try { el.innerHTML = '' } catch { /* ignore */ }
    // 修复：原实现是 `catch { return null }`，把降级原因完全吞掉 →
    // 线上只能看到"扩展图表永远显示成柱状图"，无法区分 chunk 404 / 模块形态不符 / render 抛错。
    console.warn('[chart] g2plot 渲染失败，已回退 ECharts：', { type: t, error: e })
    return null
  }
}
