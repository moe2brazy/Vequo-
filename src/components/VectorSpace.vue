<template>
  <section class="vector-space-shell">
    <div class="vector-space-toolbar">
      <div>
        <div class="eyebrow">EMBEDDING SPACE</div>
        <h2>向量检索空间</h2>
        <p>拖拽、缩放并点选知识点，观察查询向量与数据语义簇的距离。</p>
      </div>
      <div class="vector-space-actions">
        <label class="vector-search">
          <span><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <circle cx="11" cy="11" r="8" /> <path d="m21 21-4.3-4.3" /> </svg></span></span>
          <input v-model="query" placeholder="输入检索问题，例如：设备停机原因" @keyup.enter="runSearch" />
        </label>
        <button class="vector-button" @click="runSearch">检索</button>
      </div>
    </div>

    <div class="vector-space-main">
      <div class="vector-canvas" @mousemove="moveQueryPoint" @mouseleave="hovered = null">
        <div class="vector-grid"></div>
        <div class="axis axis-x"></div><div class="axis axis-y"></div>
        <div v-for="point in visiblePoints" :key="point.id" class="vector-point" :class="{ matched: point.matched, selected: selected?.id === point.id }" :style="point.style" @mouseenter="hovered = point" @click="selected = point">
          <span class="point-core"></span>
          <span v-if="hovered?.id === point.id" class="point-tooltip">{{ point.label }}<small>{{ point.group }} · 相似度 {{ point.score }}</small></span>
        </div>
        <div class="query-point" :style="queryPointStyle"><span>Q</span></div>
        <div class="canvas-legend"><span><i class="legend-dot all"></i>全部知识</span><span><i class="legend-dot match"></i>匹配结果</span><span><i class="legend-dot query"></i>查询向量</span></div>
        <div class="canvas-hint">基于 PCA 二维投影 · 当前展示 {{ visiblePoints.length }} 个向量</div>
      </div>
      <aside class="vector-detail">
        <div class="detail-label">检索摘要</div>
        <div class="detail-query">{{ query || '最近一周设备停机原因' }}</div>
        <div class="detail-score"><strong>{{ matchedCount }}</strong><span>个相关向量</span><b>82%</b></div>
        <div class="detail-list">
          <button v-for="item in matchedPoints.slice(0, 4)" :key="item.id" :class="{ active: selected?.id === item.id }" @click="selected = item">
            <span class="rank">0{{ item.rank }}</span><span class="detail-name">{{ item.label }}<small>{{ item.group }}</small></span><span class="similarity">{{ item.score }}</span>
          </button>
        </div>
        <div v-if="selected" class="detail-selected"><span>当前选中</span><strong>{{ selected.label }}</strong><p>{{ selected.description }}</p></div>
        <button class="outline-button" @click="resetSearch">清除高亮</button>
      </aside>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

type VectorPoint = { id: number; label: string; group: string; score: string; x: number; y: number; matched: boolean; rank: number; description: string; style: Record<string, string> }

const query = ref('')
const hovered = ref<VectorPoint | null>(null)
const selected = ref<VectorPoint | null>(null)
const basePoints = [
  ['设备停机记录', '设备分析', '0.94', 18, 29], ['停机原因字典', '业务规则', '0.89', 24, 34], ['设备运行效率', '设备分析', '0.86', 28, 26],
  ['工序良率', '质量分析', '0.91', 57, 26], ['不良品检验明细', '质量分析', '0.87', 62, 31], ['质量异常规则', '业务规则', '0.83', 66, 22],
  ['生产订单', '生产分析', '0.71', 38, 62], ['产量趋势', '生产分析', '0.76', 44, 68], ['工序主数据', '业务对象', '0.74', 49, 59],
  ['库存快照', '库存分析', '0.68', 80, 58], ['安全库存规则', '业务规则', '0.73', 87, 64], ['物料主数据', '业务对象', '0.65', 84, 72],
  ['设备保养计划', '设备分析', '0.79', 13, 76], ['供应商档案', '业务对象', '0.48', 71, 82], ['订单交付主题', '分析主题', '0.52', 56, 84],
]
const points = ref<VectorPoint[]>(basePoints.map((item, index) => ({ id: index, label: item[0] as string, group: item[1] as string, score: item[2] as string, x: item[3] as number, y: item[4] as number, matched: index < 6, rank: index + 1, description: `${item[1]}知识片段，包含与当前查询相关的表结构、字段定义和业务语义。`, style: { left: `${item[3]}%`, top: `${item[4]}%` } })))
const visiblePoints = computed(() => points.value)
const matchedPoints = computed(() =>
  points.value.filter(point => point.matched)
    .sort((a, b) => Number(b.score) - Number(a.score))
    .map((p, i) => ({ ...p, rank: i + 1 })),  // rank 跟随排序结果，序号与相似度一致
)
const matchedCount = computed(() => matchedPoints.value.length)
const queryPointStyle = ref<Record<string, string>>({ left: '52%', top: '38%' })

function runSearch() { points.value = points.value.map((point, index) => ({ ...point, matched: index < (query.value ? 5 : 6) })); queryPointStyle.value = { left: query.value ? '55%' : '52%', top: query.value ? '34%' : '38%' }; selected.value = matchedPoints.value[0] || null }
function resetSearch() { query.value = ''; points.value = points.value.map(point => ({ ...point, matched: point.id < 6 })); queryPointStyle.value = { left: '52%', top: '38%' }; selected.value = null }
function moveQueryPoint(event: MouseEvent) { const target = event.currentTarget as HTMLElement; const rect = target.getBoundingClientRect(); if (!query.value) return; const x = Math.max(12, Math.min(88, ((event.clientX - rect.left) / rect.width) * 100)); const y = Math.max(12, Math.min(88, ((event.clientY - rect.top) / rect.height) * 100)); queryPointStyle.value = { left: `${x}%`, top: `${y}%` } }
</script>

<style scoped>
.vector-space-shell { overflow: hidden; border: 1px solid #dce5ee; border-radius: 28px; background: rgba(255,255,255,.86); box-shadow: 0 24px 70px rgba(24, 43, 70, .08); }
.vector-space-toolbar { display: flex; justify-content: space-between; gap: 24px; align-items: flex-end; padding: 28px 30px 22px; border-bottom: 1px solid #edf1f5; }
.eyebrow, .detail-label { color: #6a7b90; font-size: 10px; font-weight: 800; letter-spacing: .18em; }
.vector-space-toolbar h2 { margin-top: 6px; color: #142337; font-size: 24px; letter-spacing: -.04em; }
.vector-space-toolbar p { margin-top: 6px; color: #7b8a9c; font-size: 12px; }
.vector-space-actions { display: flex; gap: 8px; }
.vector-search { display: flex; align-items: center; gap: 8px; width: 280px; padding: 10px 13px; border: 1px solid #dfe7ef; border-radius: 12px; background: #f8fafc; color: #96a4b5; }
.vector-search input { width: 100%; border: 0; outline: 0; background: transparent; color: #1c2d43; font-size: 12px; }
.vector-button, .outline-button { border: 0; border-radius: 12px; padding: 0 18px; color: white; background: #142337; font-size: 12px; font-weight: 700; cursor: pointer; }
.vector-space-main { display: grid; grid-template-columns: minmax(0, 1fr) 255px; min-height: 430px; }
.vector-canvas { position: relative; min-height: 430px; overflow: hidden; background: radial-gradient(circle at 50% 42%, #f9fcff, #eef4f9 72%); cursor: crosshair; }
.vector-grid { position: absolute; inset: 0; opacity: .55; background-image: linear-gradient(rgba(145, 167, 190, .13) 1px, transparent 1px), linear-gradient(90deg, rgba(145, 167, 190, .13) 1px, transparent 1px); background-size: 44px 44px; mask-image: radial-gradient(circle, black 15%, transparent 80%); }
.axis { position: absolute; background: #d1dce6; opacity: .8; }.axis-x { left: 8%; right: 8%; top: 50%; height: 1px; }.axis-y { top: 10%; bottom: 10%; left: 50%; width: 1px; }
.vector-point, .query-point { position: absolute; z-index: 2; transform: translate(-50%, -50%); }.vector-point { width: 15px; height: 15px; border-radius: 50%; background: #c5e1f8; box-shadow: 0 0 0 5px rgba(197, 225, 248, .13); cursor: pointer; transition: transform .2s, background .2s, box-shadow .2s; }.vector-point:hover, .vector-point.selected { z-index: 6; transform: translate(-50%, -50%) scale(1.5); }.vector-point.matched { background: #7655f5; box-shadow: 0 0 0 5px rgba(118, 85, 245, .13), 0 0 18px rgba(118, 85, 245, .35); }.point-tooltip { position: absolute; left: 14px; bottom: 14px; width: 150px; padding: 9px 11px; border-radius: 10px; background: #142337; color: white; font-size: 11px; white-space: nowrap; box-shadow: 0 12px 28px rgba(20, 35, 55, .2); }.point-tooltip small { display: block; margin-top: 4px; color: #a7b6c6; font-size: 9px; }
.query-point { width: 28px; height: 28px; border: 2px solid #f03f64; border-radius: 50%; background: rgba(255,255,255,.8); box-shadow: 0 0 0 8px rgba(240, 63, 100, .13), 0 0 24px rgba(240, 63, 100, .32); pointer-events: none; transition: left .2s, top .2s; }.query-point span { display: grid; height: 100%; place-items: center; color: #f03f64; font-size: 10px; font-weight: 900; }
.canvas-legend { position: absolute; left: 22px; bottom: 20px; display: flex; gap: 14px; color: #6c7d90; font-size: 10px; }.canvas-legend span { display: flex; align-items: center; gap: 5px; }.legend-dot { width: 7px; height: 7px; display: inline-block; border-radius: 50%; }.legend-dot.all { background: #c5e1f8; }.legend-dot.match { background: #7655f5; }.legend-dot.query { background: #f03f64; }.canvas-hint { position: absolute; right: 20px; bottom: 20px; color: #95a3b1; font-size: 10px; }
.vector-detail { padding: 28px 22px; border-left: 1px solid #edf1f5; background: rgba(250, 252, 254, .82); }.detail-query { margin-top: 10px; color: #1c2d43; font-size: 15px; font-weight: 700; line-height: 1.4; }.detail-score { display: flex; align-items: baseline; gap: 7px; margin-top: 18px; padding-bottom: 17px; border-bottom: 1px solid #e7edf2; }.detail-score strong { color: #7655f5; font-size: 30px; }.detail-score span { color: #7e8c9a; font-size: 10px; }.detail-score b { margin-left: auto; padding: 5px 7px; border-radius: 7px; color: #118b72; background: #e3f8f1; font-size: 10px; }.detail-list { margin-top: 12px; }.detail-list button { display: flex; align-items: center; width: 100%; gap: 8px; padding: 9px 4px; border: 0; border-radius: 9px; background: transparent; text-align: left; cursor: pointer; }.detail-list button:hover, .detail-list button.active { background: #edf0ff; }.rank { color: #a4b1bf; font-size: 10px; }.detail-name { min-width: 0; flex: 1; color: #314357; font-size: 11px; font-weight: 700; }.detail-name small { display: block; margin-top: 3px; color: #9aa8b7; font-size: 9px; font-weight: 500; }.similarity { color: #7655f5; font-family: monospace; font-size: 10px; }.detail-selected { margin-top: 16px; padding: 12px; border-radius: 12px; background: #fff; border: 1px solid #e6eaf2; }.detail-selected span { color: #8e9aaa; font-size: 9px; }.detail-selected strong { display: block; margin-top: 5px; color: #25384e; font-size: 12px; }.detail-selected p { margin-top: 7px; color: #8795a4; font-size: 10px; line-height: 1.6; }.outline-button { width: 100%; margin-top: 20px; padding: 10px; color: #536477; border: 1px solid #d9e2eb; background: white; }.outline-button:hover { border-color: #a6b7c8; }
@media (max-width: 900px) { .vector-space-toolbar { align-items: stretch; flex-direction: column; }.vector-space-actions { width: 100%; }.vector-search { flex: 1; width: auto; }.vector-space-main { grid-template-columns: 1fr; }.vector-detail { border-top: 1px solid #edf1f5; border-left: 0; } }
</style>
<style scoped>
.vector-space-shell{border-color:rgba(146,183,207,.13);background:rgba(10,17,29,.76);box-shadow:0 24px 70px rgba(0,0,0,.23),inset 0 1px rgba(255,255,255,.025)}.vector-space-toolbar{border-color:rgba(146,183,207,.09)}.eyebrow,.detail-label{color:#60d8c6}.vector-space-toolbar h2,.detail-query{color:#e4eef4}.vector-space-toolbar p{color:#6b7f8e}.vector-search{border-color:rgba(146,183,207,.13);background:rgba(4,10,19,.62);color:#607484}.vector-search input{color:#d8e5ed}.vector-button{color:#06110f;background:#82e8d8}.vector-canvas{background:radial-gradient(circle at 50% 42%,#15263a,#08111e 72%)}.vector-grid{opacity:.35;background-image:linear-gradient(rgba(130,176,204,.13) 1px,transparent 1px),linear-gradient(90deg,rgba(130,176,204,.13) 1px,transparent 1px)}.axis{background:#31485b}.vector-point{background:#46768f;box-shadow:0 0 0 5px rgba(75,132,158,.1)}.vector-point.matched{background:#8c72ff;box-shadow:0 0 0 5px rgba(140,114,255,.12),0 0 20px rgba(140,114,255,.45)}.query-point{background:rgba(8,17,30,.9)}.canvas-legend,.canvas-hint{color:#5f7484}.vector-detail{border-color:rgba(146,183,207,.09);background:rgba(5,11,20,.56)}.detail-score{border-color:rgba(146,183,207,.1)}.detail-score b{color:#70ddcb;background:rgba(38,167,147,.1)}.detail-list button:hover,.detail-list button.active{background:rgba(119,91,255,.11)}.detail-name{color:#b5c5cf}.detail-name small,.rank{color:#607483}.similarity{color:#9b87ff}.detail-selected{border-color:rgba(146,183,207,.11);background:rgba(255,255,255,.025)}.detail-selected strong{color:#dce8ef}.detail-selected p{color:#738695}.outline-button{color:#8295a3;border-color:rgba(146,183,207,.13);background:rgba(255,255,255,.025)}
</style>
