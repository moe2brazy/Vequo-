<template>
  <div ref="containerRef" class="w-full" :style="{ height: height + 'px' }"></div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'

const props = withDefaults(defineProps<{
  nodes?: Array<{
    id: string
    name: string
    label: string
    columns: number
    connected: boolean
    nodeType?: string
    scene?: string
    subFields?: Array<{ name: string; type: string; ktype: string }>
    rowCount?: number
    icon?: string
    desc?: string
  }>
  relationships: Array<{
    source_table: string
    source_column: string
    target_table: string
    target_column: string
    type: string
    description?: string
  }>
  height?: number
}>(), {
  height: 560,
})

const emit = defineEmits<{ (e: 'node-click', id: string): void }>()

const containerRef = ref<HTMLElement | null>(null)
let network: any = null

async function renderGraph() {
  if (!containerRef.value || !(props.nodes?.length || props.relationships.length)) return

  await nextTick()
  await new Promise((r) => requestAnimationFrame(() => r(null)))

  const { Network } = await import('vis-network')
  const { DataSet } = await import('vis-data')

  const nodeSet = new Set<string>()
  props.relationships.forEach((r) => {
    nodeSet.add(r.source_table)
    nodeSet.add(r.target_table)
  })

  const nodeDetails = new Map((props.nodes || []).map((node) => [node.id, node]))
  if (props.nodes?.length) {
    props.nodes.forEach((node) => nodeSet.add(node.id))
  }

  const connectedSet = new Set<string>()
  props.relationships.forEach((r) => {
    connectedSet.add(r.source_table)
    connectedSet.add(r.target_table)
  })

  const sceneAnchorNames = Array.from(nodeSet).filter((n) => nodeDetails.get(n)?.nodeType === '业务场景')
  const sceneAnchorCount = Math.max(1, sceneAnchorNames.length)
  const anchorRadius = Math.max(600, sceneAnchorCount * 300)
  const sceneAnchorPos = new Map<string, { x: number; y: number }>()
  sceneAnchorNames.forEach((n, i) => {
    const ang = (2 * Math.PI * i) / sceneAnchorCount - Math.PI / 2
    sceneAnchorPos.set(n, { x: anchorRadius * Math.cos(ang), y: anchorRadius * Math.sin(ang) })
  })

  const ruleAnchorNames = Array.from(nodeSet).filter((n) => nodeDetails.get(n)?.nodeType === '业务规则')
  const ruleAnchorPos = new Map<string, { x: number; y: number }>()
  ruleAnchorNames.forEach((n, i) => {
    const y = (i - (ruleAnchorNames.length - 1) / 2) * 80
    ruleAnchorPos.set(n, { x: anchorRadius + 200, y })
  })

  const nodes = new DataSet(
    Array.from(nodeSet).map((name) => {
      const detail = nodeDetails.get(name)
      const kt = detail?.nodeType || '数据表'
      const isTable = kt === '数据表'
      const connected = connectedSet.has(name)
      const subInfo = detail?.subFields
        ? detail.subFields.slice(0, 8).map((f: any) => `${f.name}:${f.type}`).join(', ')
        : ''

      const shortType = (t: string) => {
        if (!t) return ''
        const s = t.toLowerCase()
        if (s.startsWith('character varying')) return 'varchar'
        if (s.startsWith('character')) return 'char'
        return t.replace(/\(.*?\)/g, '')
      }
      const buildTableLabel = (nodeName: string, nodeDetail: any) => {
        const cols: any[] = nodeDetail?.columnsFull || []
        const lines = [nodeDetail?.label || nodeName]
        if (!cols.length) return lines.join('\n')
        const pkNames = new Set<string>(nodeDetail?.pkColumns || [])
        const fkRows = props.relationships
          .filter((r) => r.source_table === nodeName)
          .map((r) => ({ name: r.source_column, target: r.target_table.split('.').pop() || r.target_table }))
        const fkNames = new Set(fkRows.map((f) => f.name))
        const others = cols.filter((c) => !pkNames.has(c.name) && !fkNames.has(c.name))
        const rows: string[] = []
        cols.filter((c) => pkNames.has(c.name)).forEach((c) => rows.push(`[PK] ${c.name} : ${shortType(c.type)}`))
        fkRows.forEach((f) => rows.push(`[FK] ${f.name} → ${f.target}`))
        others.slice(0, Math.max(0, 6 - rows.length)).forEach((c) => rows.push(`  ${c.name} : ${shortType(c.type)}`))
        lines.push(...rows)
        if (cols.length > rows.length) lines.push(`… 共 ${cols.length} 字段`)
        return lines.join('\n')
      }
      const wrapFieldName = (n: string) => {
        if (!n) return ''
        if (n.length <= 11) return n
        const seg = Math.floor(n.length / 2)
        for (let i = seg; i < n.length - 3; i++) {
          if (n[i] === '_') return `${n.slice(0, i)}\n${n.slice(i + 1)}`
        }
        return `${n.slice(0, seg)}\n${n.slice(seg)}`
      }

      // ===== 节点 value 缩小（半径减小） =====
    let nodeValue = 8
if (kt === '业务场景') nodeValue = 30
else if (kt === '数据字段') nodeValue = 6
else if (connected) nodeValue = isTable ? 12 : 4
else nodeValue = 3

      // ===== 字体调大 =====
      let fontSize = 15
      if (kt === '业务场景') fontSize = 40
      else if (kt === '数据字段') fontSize = 19
      else if (isTable) fontSize = 15
      else fontSize = 25

      // ===== 节点尺寸约束（更紧凑） =====
      let widthMin = 100, widthMax = 200
      let heightMin = 30, heightMax = 60
      if (kt === '业务场景') { widthMin = 120; widthMax = 200; heightMin = 40; heightMax = 65 }
      else if (isTable) { widthMin = 140; widthMax = 260; heightMin = 36; heightMax = 75 }
      else if (kt === '数据字段') { widthMin = 70; widthMax = 120; heightMin = 24; heightMax = 40 }

      return {
        id: name,
        label: kt === '数据字段' ? wrapFieldName(detail?.label || name) : (isTable ? buildTableLabel(name, detail) : (detail?.label || name)),
        title: [
          `${getNodeIcon(kt)} ${detail?.label || name}`,
          `节点类型：${kt}`,
          detail?.scene ? `业务场景：${detail.scene}` : '',
          isTable ? `字段数：${detail?.columns || 0}` : '',
          isTable && detail?.rowCount ? `数据行数：${detail.rowCount.toLocaleString()}` : '',
          detail?.desc ? `📝 ${detail.desc}` : '',
          connected ? '🔗 存在关联' : '',
          subInfo ? `📌 关键字段：${subInfo}` : '',
        ].filter(Boolean).join('\n'),
        shape: TYPE_SHAPES[kt] || 'ellipse',
        color: getTypeColor(kt),
        borderWidth: connected ? (isTable ? 2 : 1.5) : 0.8,
        borderRadius: kt === '业务规则' || isTable ? 4 : 14,
        font: { color: kt === '业务场景' ? '#0f172a' : '#1f2937', size: fontSize, face: 'Microsoft YaHei' },
        margin: { top: isTable ? 10 : 8, bottom: isTable ? 10 : 8, left: isTable ? 12 : 10, right: isTable ? 12 : 10 },
        widthConstraint: { minimum: widthMin, maximum: widthMax },
        heightConstraint: { minimum: heightMin, maximum: heightMax },
        value: nodeValue,
        fixed: (kt === '业务场景' || kt === '业务规则') ? { x: true, y: true } : false,
        x: kt === '业务规则' ? ruleAnchorPos.get(name)?.x : sceneAnchorPos.get(name)?.x,
        y: kt === '业务规则' ? ruleAnchorPos.get(name)?.y : sceneAnchorPos.get(name)?.y,
        physics: true,
      }
    })
  )

  const edges = new DataSet(
    props.relationships.map((r, idx) => {
      const typeMap: Record<string, string> = {
        foreign_key: ' 关联 ',
        object_table: ' 映射 ',
        scene_object: ' 包含 ',
        table_field: ' 包含 ',
        object_metric: ' 计算 ',
        rule_metric: ' 影响 ',
        default: ' 关联 ',
      }
      const relationText = typeMap[r.type || 'default'] || '关联'
      const edgeTitle = r.description || `${r.type || '关联'}：${r.source_table || 'source'} → ${r.target_table || 'target'}`

      return {
        id: idx,
        from: r.source_table,
        to: r.target_table,
        label: relationText,
        title: `${edgeTitle}\n关系类型：${r.type || 'unknown'}`,
        arrows: { to: { enabled: true, type: 'arrow' } },
        color: { color: r.type === 'foreign_key' ? '#94a3b8' : '#cbd5e1', highlight: '#2563eb' },
        font: { 
          size: 22, 
          color: 'gray', 
          strokeWidth: 2, 
          strokeColor: '#ffffff',
           align: 'top',
           offset: 10, 
          },
        width: r.type === 'foreign_key' ? 1.8 : 1.2,
        smooth: { type: 'curvedCW', roundness: 0.08 },
        labelHighlightBold: false,
        fontMultiplier: 1,
        dashes: false,
        selectionWidth: 0,
      }
    })
  )

  const data = { nodes, edges }
  const options = {
    layout: {
      improvedLayout: true,
      hierarchical: false,
    },
    interaction: {
      hover: true,
      tooltipDelay: 150,
      zoomView: true,
      dragView: true,
      dragNodes: true,
      multiselect: true,
      navigationButtons: true,
      hoverConnectedEdges: true
    },
    edges: {
      smooth: { enabled: true, type: 'dynamic', roundness: 0.08 },
      color: { color: '#94a3b8', highlight: '#2563eb', hover: '#2563eb' },
      selectionWidth: 2,
      arrows: {
        to: { enabled: true, scaleFactor: 0.9, type: 'arrow' },
      },
      font: {
        align: 'top',
      },
    },
    nodes: {
      shapeProperties: { borderRadius: 6 },
      scaling: {
        min: 6,
        max: 35,
        label: { enabled: false },
      },
      shadow: { enabled: true, color: 'rgba(15,23,42,0.06)', size: 3, x: 0, y: 2 },
    },
    physics: {
      enabled: true,
      stabilization: { enabled: true, iterations: 400, updateInterval: 200, fit: false },
      barnesHut: {
        gravitationalConstant: -10000,
        centralGravity: 0.1,
        springLength: 200,
        springConstant: 0.1,
        damping: 0.5,
        avoidOverlap: 0.8,
      }
    }
  }

  network = new Network(containerRef.value, data as any, options as any)

  network.on('click', (params: any) => {
    try {
      const id = params?.nodes?.[0]
      if (id) {
        const neighborIds = new Set<string>([id])
        props.relationships.forEach((r) => {
          if (r.source_table === id) neighborIds.add(r.target_table)
          if (r.target_table === id) neighborIds.add(r.source_table)
        })
        network.selectNodes(Array.from(neighborIds), true)
        emit('node-click', String(id))
      } else {
        network.unselectAll()
        emit('node-click', '')
      }
    } catch (e) { /* ignore */ }
  })

  const fitZoomed = () => {
  try {
    network.fit({ animation: false })
    const s = network.getScale()
    const h = containerRef.value?.clientHeight || 560
    const factor = h >= 680 ? 1.2 : 1.5
    network.moveTo({ scale: s * factor, animation: false })
    network.redraw()
    network.fit({ animation: false })
    const s2 = network.getScale()
    network.moveTo({ scale: s2 * factor, animation: false })
    network.redraw()
  } catch (e) { /* ignore */ }
}

  network.once('stabilizationIterationsDone', () => {
    try {
      network.setOptions({ physics: { enabled: false } })
      ;[...sceneAnchorNames, ...ruleAnchorNames].forEach((id) => {
        try { network.body.data.nodes.update({ id, fixed: false }) } catch (e) { /* ignore */ }
      })
      fitZoomed()
    } catch (e) { /* ignore */ }
  })
}

const TYPE_COLORS: Record<string, { background: string; border: string; highlight: { background: string; border: string } }> = {
  '业务场景': { background: '#EAF2FB', border: '#4A90D9', highlight: { background: '#4A90D9', border: '#2E6DA4' } },
  '业务对象': { background: '#E9F9F0', border: '#2ECC71', highlight: { background: '#2ECC71', border: '#1F9D55' } },
  '业务指标': { background: '#FEF5E7', border: '#F39C12', highlight: { background: '#F39C12', border: '#C07F0E' } },
  '业务规则': { background: '#F4ECF7', border: '#9B59B6', highlight: { background: '#9B59B6', border: '#7D3C98' } },
  '数据表':   { background: '#FDEDEC', border: '#E74C3C', highlight: { background: '#E74C3C', border: '#B03A2E' } },
  '数据字段': { background: '#FEF9E7', border: '#F1C40F', highlight: { background: '#F1C40F', border: '#C19D0C' } },
}

const TYPE_SHAPES: Record<string, string> = {
  '业务场景': 'diamond',
  '业务对象': 'ellipse',
  '业务指标': 'box',
  '业务规则': 'box',
  '数据表': 'box',
  '数据字段': 'ellipse',
}

function getTypeColor(kt: string) {
  return TYPE_COLORS[kt] || { background: '#f8fafc', border: '#cbd5e1', highlight: { background: '#475569', border: '#334155' } }
}

function getNodeIcon(kt: string): string {
  const iconMap: Record<string, string> = {
    '业务场景': '🗂️',
    '业务对象': '📦',
    '业务指标': '📊',
    '业务规则': '📏',
    '数据表': '🗄️',
    '数据字段': '🔤',
  }
  return iconMap[kt] || '📄'
}

let renderSeq = 0

onMounted(async () => {
  const seq = ++renderSeq
  await nextTick()
  if (seq !== renderSeq) return
  renderGraph()
})

onBeforeUnmount(() => {
  renderSeq++
  if (network) {
    try { network.destroy() } catch { /* ignore */ }
    network = null
  }
})

watch(() => [props.nodes, props.relationships], async () => {
  const seq = ++renderSeq
  if (network) {
    try { network.destroy() } catch { /* ignore */ }
    network = null
  }
  await nextTick()
  if (seq !== renderSeq) return
  renderGraph()
})

watch(() => props.height, async () => {
  await nextTick()
  if (network) {
    network.redraw()
    network.fit({ animation: true })
  } else {
    renderGraph()
  }
})

const fitView = () => {
  if (network) {
    network.fit({ animation: true })
  }
}

const focusNode = (id: string) => {
  if (!network) return
  try {
    network.selectNodes([id], true)
    network.focus(id, { scale: 1.2, animation: { duration: 400, easingFunction: 'easeInOutQuad' } })
  } catch (e) { /* ignore */ }
}

defineExpose({ fitView, focusNode })
</script>