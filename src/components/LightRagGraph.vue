<template>
  <div class="lightrag-graph" :style="height ? { height: height + 'px' } : undefined">
    <div ref="containerRef" class="lightrag-graph-canvas"></div>
    <div v-if="error" class="lightrag-graph-error">{{ error }}</div>
  </div>
</template>

<!--
  知识图谱组件：vis-network 物理模拟布局（barnesHut + 稳定化动画）
  - 节点可拖拽、滚轮缩放、悬停显示 title 提示（名称 + 描述）
  - 动态平滑连线（smooth: dynamic），边色继承节点色
  - 数据来源（2026-10-03 改）：**默认走实时接口 /api/tables/relationships**，
    读 PostgreSQL information_schema 的真实外键关系，随当前库自动变化。
    原实现默认读 public/knowledge_graph.json —— 那是 LightRAG 离线工具导出的
    静态快照（entity_type=data、file_path=unknown_source），与当前数据库**无任何连接**，
    演示时节点与实际库表对不上。静态文件仅在显式传 src 时才使用（保留为离线兜底/对照）。
-->
<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'

const props = withDefaults(
  defineProps<{
    /**
     * 图谱数据来源。为空（默认）→ 走**实时** /api/tables/relationships（真实外键）。
     * 显式传 URL（如 '/knowledge_graph.json'）→ 读该静态 JSON（离线兜底/对照用）。
     */
    src?: string
    /** 画布高度（px），不传则填满父容器 */
    height?: number
    /**
     * 适配视图后的放大倍数（初始加载 / 重置均生效）：
     * vis-network 的 fit() 会把全图缩到刚好放下，节点多时标签过小；
     * 1 = 完全适配（不放大），1.4 = 在适配基础上再放大 40%。
     */
    fitZoom?: number
  }>(),
  { src: '', height: 0, fitZoom: 1 },
)

const emit = defineEmits<{ (e: 'node-click', id: string): void }>()

const containerRef = ref<HTMLDivElement | null>(null)
const error = ref('')
let network: any = null
// 卸载/重入守卫：draw() 里有两处 await（fetch + 动态 import），等待期间组件完全可能被卸载。
// 卸载时 onBeforeUnmount 只能销毁「当时已存在」的实例（此刻还是 null），
// await 结束后若继续 new Network，就会造出一个永远不会被 destroy 的孤儿实例——
// canvas、事件绑定、barnesHut 物理模拟循环全部常驻内存，进出该页几次就累积几个。
let disposed = false
let abortCtrl: AbortController | null = null

// 与 knowledge_graph.html 中完全一致的 vis-network options
const buildOptions = () => ({
  configure: { enabled: false },
  edges: {
    color: { inherit: true },
    smooth: { enabled: true, type: 'dynamic' },
  },
  interaction: {
    dragNodes: true,
    hideEdgesOnDrag: false,
    hideNodesOnDrag: false,
  },
  physics: {
    enabled: true,
    stabilization: {
      enabled: true,
      // 关掉 vis 自带的“稳定后 fit”，改由 applyFit() 在稳定完成事件里执行
      // 「先 fit 再乘 fitZoom」——否则内部 fit 可能在放大之后把视图又拉回纯适配
      fit: false,
      iterations: 1000,
      onlyDynamicEdges: false,
      updateInterval: 50,
    },
  },
})

/** 实时接口：读 PG information_schema 的真实表与外键（2026-10-03）。 */
const LIVE_API = '/api/tables/relationships'

/** 按业务域上色，让图谱一眼能区分生产/质量/设备/库存/基础数据。 */
const SCENE_COLOR: Record<string, string> = {
  生产: '#4f9cf9',
  质量: '#f59e0b',
  设备: '#ef4444',
  库存: '#10b981',
  销售: '#8b5cf6',
  基础数据: '#64748b',
}

/**
 * 把 /api/tables/relationships 的返回转成 vis-network 的 {nodes, edges}。
 * 后端结构：{nodes:[{id,name,label,columns,scene,nodeType}], relationships:
 *           [{source_table,source_column,target_table,target_column,type,description}]}
 */
const adaptLive = (data: any) => {
  const rawNodes: any[] = data?.nodes || []
  const rels: any[] = data?.relationships || []
  const nodes = rawNodes.map((n) => {
    const scene = String(n.scene || '')
    const color = SCENE_COLOR[scene] || '#64748b'
    return {
      id: String(n.id ?? n.name),
      label: String(n.label || n.name || n.id),
      // 字段多的表画大一点，图谱层次更清楚
      size: Math.min(46, 16 + Math.sqrt(Number(n.columns) || 1) * 7),
      color,
      shape: 'dot',
      title: `${n.label || n.name}\n表名：${n.name}\n字段数：${n.columns ?? '-'}\n业务域：${scene || '-'}`,
    }
  })
  const known = new Set(nodes.map((n) => n.id))
  // 只保留两端都在节点集里的边，避免悬空连线（跨 schema 的外键会出现这种情况）
  const edges = rels
    .filter((r) => known.has(String(r.source_table)) && known.has(String(r.target_table)))
    .map((r) => {
      // type: 'foreign_key' = information_schema 里的真实外键（实线加粗）
      //       'inferred'   = 按同名 _id 字段推导（虚线细线，置信度低）
      //       'business'   = 预定义业务关系
      const inferred = r.type === 'inferred'
      return {
        from: String(r.source_table),
        to: String(r.target_table),
        title: r.description || `${r.source_column} → ${r.target_column}`,
        dashes: inferred,
        width: inferred ? 1 : 2,
      }
    })
  return { nodes, edges }
}

const draw = async () => {
  if (!containerRef.value) return
  // 取消上一次仍在途的加载（组件重建 / 手动重绘时）
  abortCtrl?.abort()
  const ac = new AbortController()
  abortCtrl = ac
  try {
    // 默认实时接口；显式 src 时才读静态 JSON（离线兜底/对照）
    const url = props.src || LIVE_API
    const res = await fetch(url, { signal: ac.signal })
    if (!res.ok) throw new Error(`加载 ${url} 失败（HTTP ${res.status}）`)
    const data = await res.json()

    let rawNodes: any[]; let rawEdges: any[]
    if (props.src) {
      // 静态 LightRAG 导出格式：本身就是 vis.DataSet 的 {nodes, edges}
      rawNodes = data.nodes || []
      rawEdges = data.edges || []
    } else {
      const adapted = adaptLive(data)
      rawNodes = adapted.nodes
      rawEdges = adapted.edges
    }
    if (!rawNodes.length) throw new Error('图谱数据为空')

    // 【守卫】两处 await 之后、动手建图之前：卸载了 / 本次加载已被更新的加载取代 / 容器没了，
    // 三种情况都直接放弃，绝不在失效容器上创建 Network。
    if (disposed || ac !== abortCtrl || !containerRef.value) return

    // 动态加载 vis-network（与 KnowledgeGraph.vue 相同的方式，避免主包体积膨胀）
    const { Network } = await import('vis-network')
    const { DataSet } = await import('vis-data')

    if (disposed || ac !== abortCtrl || !containerRef.value) return

    // 直接使用 LightRAG 导出的原始字段（color/label/title/shape/size/width），
    // 保证与 knowledge_graph.html 的渲染效果完全一致
    const nodes = new DataSet(rawNodes)
    const edges = new DataSet(rawEdges)

    const inst = new Network(containerRef.value, { nodes, edges } as any, buildOptions() as any)
    // 极端情况：import 完成后才被卸载 → 立刻销毁刚建好的实例，不留孤儿
    if (disposed) {
      try { inst.destroy() } catch { /* ignore */ }
      return
    }
    network = inst

    network.on('click', (params: any) => {
      if (disposed) return
      if (params.nodes && params.nodes.length > 0) emit('node-click', String(params.nodes[0]))
    })

    // 物理布局稳定后统一「适配 + 放大」（stabilization.fit 已关闭，这里就是初始视图的唯一入口）
    network.once('stabilizationIterationsDone', () => {
      if (disposed) return
      applyFit({ duration: 500, easingFunction: 'easeOutQuad' })
    })
  } catch (e: any) {
    if (e?.name === 'AbortError' || disposed) return
    error.value = e?.message || '知识图谱加载失败'
    console.error('[LightRagGraph] 加载失败:', e)
  }
}

/**
 * 适配视图：先瞬时 fit 到「全图刚好放下」，再按 props.fitZoom 放大。
 * 之所以先 fit，是因为用户手动缩放/平移后 getScale() 不再等于适配比例，
 * 先归位再乘倍数才能保证初始视图与「重置」后的视图一致。
 */
const applyFit = (animation: any = false) => {
  if (!network) return
  try {
    network.fit({ animation: false })
    const factor = Number(props.fitZoom) || 1
    if (factor === 1) {
      if (animation) network.fit({ animation })
      return
    }
    network.moveTo({
      scale: network.getScale() * factor,
      animation: animation || undefined,
    })
  } catch { /* ignore */ }
}

/** 重置视图（供父组件通过 ref 调用，与旧 KnowledgeGraph 的 fitView 对齐） */
const fitView = () => {
  applyFit({ duration: 400, easingFunction: 'easeOutQuad' })
}

/** 重新运行物理布局 */
const relayout = () => {
  try {
    network?.stabilize(500)
  } catch { /* ignore */ }
  applyFit({ duration: 400, easingFunction: 'easeOutQuad' })
}

/** 导出 PNG（白底，与 knowledge_graph.html 的白色画布一致） */
const downloadPng = () => {
  if (!containerRef.value) return
  const canvases = containerRef.value.querySelectorAll('canvas')
  if (!canvases.length) return
  const first = canvases[0] as HTMLCanvasElement
  const target = document.createElement('canvas')
  target.width = first.width
  target.height = first.height
  const ctx = target.getContext('2d')
  if (!ctx) return
  ctx.fillStyle = '#ffffff'
  ctx.fillRect(0, 0, target.width, target.height)
  canvases.forEach((c) => ctx.drawImage(c, 0, 0))
  const link = document.createElement('a')
  link.download = 'lightrag-knowledge-graph.png'
  link.href = target.toDataURL('image/png')
  link.click()
}

// getNodePositions：返回每个节点在**页面坐标系**里的中心点。
// 用途：① 自动化测试要按真实坐标点击节点（网格盲扫点不中，测出来的结论不可信）；
//      ② 后续若要做「点击画布空白处关闭详情」之类的交互，也需要节点位置。
// 返回 [{id, label, x, y}]，坐标已加上画布相对页面的偏移，可直接喂给
// dispatchEvent 的 clientX/clientY。
const getNodePositions = () => {
  const out: Array<{ id: string; label: string; x: number; y: number }> = []
  if (!network || !containerRef.value) return out
  try {
    const canvasRect = containerRef.value.getBoundingClientRect()
    // vis-network 把节点集挂在 body.data.nodes（DataSet 实例）。
    // 不同版本暴露形式不同：可能是 DataSet（有 .get()/.length），也可能已是数组，
    // 这里两种都兼容，避免拿到 undefined 后整个函数静默返回空数组。
    const ds: any = (network as any).body?.data?.nodes
    let ids: string[] = []
    if (Array.isArray(ds)) {
      ids = ds.map((n: any) => String(n.id))
    } else if (ds && typeof ds.get === 'function') {
      const arr = ds.get() || []
      ids = arr.map((n: any) => String(n.id ?? n))
    } else if (ds && typeof ds.length === 'number') {
      ids = Array.from({ length: ds.length }, (_, i) => String(ds[i]?.id ?? i))
    }
    for (const id of ids) {
      if (!id) continue
      const p = network.getPosition(id)
      if (!p) continue
      const domPos = network.canvasToDOM(p)
      if (!domPos) continue
      out.push({
        id,
        label: id,
        x: canvasRect.left + domPos.x,
        y: canvasRect.top + domPos.y,
      })
    }
  } catch (e) {
    console.warn('[LightRagGraph] getNodePositions 失败:', e)
  }
  return out
}

defineExpose({ fitView, relayout, downloadPng, getNodePositions })

// 容器尺寸自适应：vis-network 自带的 autoResize 只监听 window resize，
// 父级布局变化（「节点中文解释」面板展开把图挤小/恢复、右侧面板收起等）不会触发重绘，
// 画布可能停在旧尺寸甚至变空白。这里用 ResizeObserver 跟随容器尺寸同步画布。
let ro: ResizeObserver | null = null
const syncSize = () => {
  if (disposed || !network) return
  const w = containerRef.value?.clientWidth || 0
  const h = containerRef.value?.clientHeight || 0
  if (w <= 0 || h <= 0) return // 容器暂时不可见：保持原尺寸，等下次尺寸变正再同步
  try {
    network.setSize(`${w}px`, `${h}px`)
    network.redraw()
  } catch { /* ignore */ }
}

onMounted(() => {
  draw()
  if (typeof ResizeObserver !== 'undefined' && containerRef.value) {
    ro = new ResizeObserver(() => syncSize())
    ro.observe(containerRef.value)
  }
})
onBeforeUnmount(() => {
  disposed = true
  try { ro?.disconnect() } catch { /* ignore */ }
  ro = null
  try { abortCtrl?.abort() } catch { /* ignore */ }
  abortCtrl = null
  try { network?.destroy() } catch { /* ignore */ }
  network = null
})
</script>

<style scoped>
.lightrag-graph {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 420px;
  overflow: hidden;
  background-color: #ffffff;
  border: 1px solid lightgray;
  border-radius: 8px;
}
.lightrag-graph-canvas {
  width: 100%;
  height: 100%;
}
.lightrag-graph-error {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #b91c1c;
  background: #fff;
  font-size: 13px;
}
</style>
