// ECharts 按需引入（bundle 体积优化）
// 全量引入 echarts 会把 400+ 图表/组件打进主包（主 chunk 1.37MB / gzip 455KB）。
// 这里只注册本项目实际用到的图表与组件，主包显著缩小。
//
// 使用方式：页面里 `import * as echarts from 'echarts'` 改为 `import echarts from '../echarts'`
// （echarts.init / echarts.ECharts 类型 / setOption 均可用）。
import * as echarts from 'echarts/core'
import {
  BarChart, LineChart, PieChart, ScatterChart, GraphChart,
  FunnelChart, SunburstChart, GaugeChart,
} from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  DataZoomComponent,
  GraphicComponent,
} from 'echarts/components'
import { LabelLayout, UniversalTransition } from 'echarts/features'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  BarChart, LineChart, PieChart, ScatterChart, GraphChart,
  FunnelChart, SunburstChart, GaugeChart,
  GridComponent, TooltipComponent, LegendComponent, TitleComponent,
  DataZoomComponent, GraphicComponent,
  LabelLayout, UniversalTransition,
  CanvasRenderer,
])

// 类型上伪装为完整 echarts（仅 TS 编译期需要 ECharts 等类型；运行时走 core 已注册组件）
const full = echarts as unknown as typeof import('echarts')
export default full
// 显式导出实例类型，供页面标注变量（default 导入无法用 echarts.ECharts 命名空间）
export type { EChartsType } from 'echarts/core'
