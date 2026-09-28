<template>
  <div class="lightrag-graph" :style="height ? { height: height + 'px' } : undefined">
    <div ref="containerRef" class="lightrag-graph-canvas"></div>
    <div v-if="error" class="lightrag-graph-error">{{ error }}</div>
  </div>
</template>

<!--
  LightRAG 知识图谱组件：1:1 复刻 D:\LightRAG\LightRAG\knowledge_graph.html 的渲染效果
  - vis-network 物理模拟布局（barnesHut + 稳定化动画）
  - 节点可拖拽、滚轮缩放、悬停显示 title 提示（名称 + 描述）
  - 动态平滑连线（smooth: dynamic），边色继承节点色
  - 数据来源：public/knowledge_graph.json（由 knowledge_graph.html 内嵌的 vis.DataSet 导出）
-->
<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 图谱数据地址（默认从 public 目录加载 LightRAG 导出的 JSON） */
    src?: string
    /** 画布高度（px），不传则填满父容器 */
    height?: number
    /**
     * 适配视图后的放大倍数（初始加载 / 重置均生效）：
     * vis-network 的 fit() 会把全图缩到刚好放下，44 个节点时标签过小；
     * 1 = 完全适配（不放大），1.4 = 在适配基础上再放大 40%。
     */
    fitZoom?: number
  }>(),
  { src: '/knowledge_graph.json', height: 0, fitZoom: 1 },
)

const emit = defineEmits<{ (e: 'node-click', id: string): void }>()

const containerRef = ref<HTMLDivElement | null>(null)
const error = ref('')
let network: any = null

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

const draw = async () => {
  if (!containerRef.value) return
  try {
    const res = await fetch(props.src)
    if (!res.ok) throw new Error(`加载 ${props.src} 失败（HTTP ${res.status}）`)
    const data = await res.json()
    const rawNodes: any[] = data.nodes || []
    const rawEdges: any[] = data.edges || []
    if (!rawNodes.length) throw new Error('图谱数据为空')

    // 动态加载 vis-network（与 KnowledgeGraph.vue 相同的方式，避免主包体积膨胀）
    const { Network } = await import('vis-network')
    const { DataSet } = await import('vis-data')

    // 直接使用 LightRAG 导出的原始字段（color/label/title/shape/size/width），
    // 保证与 knowledge_graph.html 的渲染效果完全一致
    const nodes = new DataSet(rawNodes)
    const edges = new DataSet(rawEdges)

    network = new Network(containerRef.value, { nodes, edges } as any, buildOptions() as any)

    network.on('click', (params: any) => {
      if (params.nodes && params.nodes.length > 0) emit('node-click', String(params.nodes[0]))
    })

    // 物理布局稳定后统一「适配 + 放大」（stabilization.fit 已关闭，这里就是初始视图的唯一入口）
    network.once('stabilizationIterationsDone', () => {
      applyFit({ duration: 500, easingFunction: 'easeOutQuad' })
    })
  } catch (e: any) {
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

defineExpose({ fitView, relayout, downloadPng })

onMounted(draw)
onBeforeUnmount(() => {
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
