<template>
  <!-- 图谱节点中文解释面板：与图谱**左右分栏**（图谱占剩余宽度、面板固定宽度），
       同一屏内直接可见 —— 不用往下滑，也不用遮罩/弹框（遮罩弹框会盖住图谱，曾表现为「点节点图谱就没了」）。 -->
  <aside class="kb-node-detail">
    <div class="kb-node-detail-head">
      <span><AppIcon name="info" :size="14" /> {{ title }}</span>
      <button class="kb-node-close" title="关闭节点说明" @click="emit('close')"><AppIcon name="x" :size="14" /></button>
    </div>
    <div class="kb-node-detail-body">
      <div class="kg-zh">
        <div class="kg-zh-head">
          <span class="kg-zh-badge">{{ kind }}</span>
          <span class="kg-zh-id mono">{{ nodeId }}</span>
          <span v-if="zh?.table" class="kg-zh-id mono">{{ zh.table }}</span>
        </div>
        <p class="kg-zh-desc">
          {{ zh?.desc || '暂未收录该节点的中文说明，下方为知识图谱原始描述。' }}
        </p>

        <div v-if="facts.length" class="kg-zh-grid">
          <div v-for="f in facts" :key="f.label">
            <span>{{ f.label }}</span><b :class="{ mono: f.mono }">{{ f.value }}</b>
          </div>
        </div>

        <div v-if="zh?.formula" class="kg-zh-block">
          <div class="kg-zh-block-title"><AppIcon name="function-square" :size="12" /> 指标口径</div>
          <pre class="kg-zh-code mono">{{ zh.formula }}</pre>
        </div>

        <div v-if="zh?.points?.length" class="kg-zh-block">
          <div class="kg-zh-block-title"><AppIcon name="list" :size="12" /> 说明要点</div>
          <ul class="kg-zh-points">
            <li v-for="(p, pi) in zh.points" :key="pi">{{ p }}</li>
          </ul>
        </div>

        <div v-if="zh?.columns?.length" class="kg-zh-block">
          <div class="kg-zh-block-title"><AppIcon name="columns" :size="12" /> 字段中文含义（{{ zh.columns.length }}）</div>
          <table class="kg-zh-table">
            <thead>
              <tr><th>字段</th><th>类型</th><th>键</th><th>中文含义</th></tr>
            </thead>
            <tbody>
              <tr v-for="col in zh.columns" :key="col.name">
                <td class="mono">{{ col.name }}</td>
                <td class="mono">{{ col.type }}</td>
                <td>
                  <span v-if="col.key === 'PK'" class="kg-key-badge kg-key-pk">PK</span>
                  <span v-else-if="col.key === 'FK'" class="kg-key-badge kg-key-fk" title="外键">FK</span>
                  <span v-else>—</span>
                </td>
                <td>{{ col.zh }}<span v-if="col.ref" class="kg-zh-ref">→ {{ col.ref }}</span></td>
              </tr>
            </tbody>
          </table>
        </div>

        <div v-if="tables.length" class="kg-zh-block">
          <div class="kg-zh-block-title"><AppIcon name="database" :size="12" /> 涉及数据表</div>
          <div class="kg-zh-chips">
            <button v-for="t in tables" :key="t" class="kg-zh-chip mono" @click="emit('open-table', t)">{{ t }}</button>
          </div>
        </div>

        <div class="kg-zh-block">
          <div class="kg-zh-block-title"><AppIcon name="git-fork" :size="12" /> 关联关系（{{ relations.length }}）</div>
          <div v-if="!relations.length" class="kg-zh-empty">该节点暂无关联关系</div>
          <div v-else class="kg-zh-rels">
            <button v-for="(r, ri) in relations" :key="ri" class="kg-zh-rel" @click="emit('pick', r.otherId)">
              <span class="kg-zh-rel-dir" :class="{ out: r.out }">{{ r.out ? '→' : '←' }}</span>
              <span class="kg-zh-rel-main">
                <b>{{ r.otherLabel }}</b>
                <em>{{ r.desc || r.relType }}</em>
              </span>
              <span class="kg-zh-rel-id mono">{{ r.otherId }}</span>
            </button>
          </div>
        </div>

        <div v-if="!zh" class="kg-zh-origin">
          <div class="kg-zh-block-title">知识图谱原始描述</div>
          <p>{{ rawDesc || '—' }}</p>
        </div>
      </div>
    </div>
  </aside>
</template>

<script setup lang="ts">
import AppIcon from './AppIcon.vue'
import type { ZhEntry } from '../data/knowledgeGraphZh'

defineProps<{
  /** 面板标题（如「产品主数据 · 数据表」） */
  title: string
  /** 知识类型（业务对象 / 数据表 / 业务指标…） */
  kind: string
  /** 节点 id（展示用） */
  nodeId: string
  /** 内置中文解释（`data/knowledgeGraphZh.ts`），查不到传 null */
  zh: ZhEntry | null
  /** 图谱原始描述（没有中文解释时兜底展示） */
  rawDesc?: string
  /** 事实卡：数据表 / 行数 / 业务场景 / 单位… */
  facts: Array<{ label: string; value: string; mono?: boolean }>
  /** 节点涉及的数据表（点击可跳回场景知识） */
  tables: string[]
  /** 关联关系（点击可切到对端节点） */
  relations: Array<{ otherId: string; otherLabel: string; relType: string; out: boolean; desc?: string }>
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'pick', id: string): void
  (e: 'open-table', table: string): void
}>()
</script>

<style scoped>
/* 面板外壳：与图谱左右分栏（右侧），高度撑满整行、内部自己滚动
   —— 同一屏内直接可见，不用往下滑。
   宽度固定、flex:none：图谱画布占剩余宽度（面板展开只会让画布变窄，
   LightRagGraph 的 ResizeObserver 会同步重绘，不会出现空白画布）。 */
.kb-node-detail {
  flex: none;
  display: flex;
  flex-direction: column;
  width: min(420px, 40%);
  border-left: 1px solid #e5e6eb;
  background: #ffffff;
}
.kb-node-detail-head {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 11px 16px;
  border-bottom: 1px solid #f2f3f5;
  font-size: 13px;
  font-weight: 600;
  color: #1d2129;
}
.kb-node-close {
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: #c9cdd4;
  cursor: pointer;
  transition: background .15s ease, color .15s ease;
}
.kb-node-close:hover { background: #f2f3f5; color: #1d2129; }
.kb-node-detail-body { flex: 1; min-height: 0; overflow-y: auto; padding: 14px 16px 16px; }

/* 中文口径正文（原 KnowledgePage 的 .kg-zh-* 样式，随面板一起搬过来） */
.mono { font-family: 'Rajdhani', Consolas, monospace; }
.kg-zh { font-size: 12px; color: #1d2129; }
.kg-zh-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.kg-zh-badge { display: inline-flex; align-items: center; padding: 2px 9px; border-radius: 999px; font-size: 11px; font-weight: 500; color: #1677ff; background: #e8f3ff; border: 1px solid #bcd8ff; }
.kg-zh-id { font-size: 11px; color: #86909c; background: #f2f3f5; border-radius: 6px; padding: 2px 6px; }
.kg-zh-desc { margin: 0 0 12px; font-size: 12.5px; line-height: 1.75; color: #4e5969; }
.kg-zh-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; padding: 12px; border: 1px solid #e5e6eb; border-radius: 10px; background: #f7f8fa; }
.kg-zh-grid span { display: block; color: #86909c; font-size: 10px; margin-bottom: 3px; }
.kg-zh-grid b { color: #1d2129; font-weight: 500; word-break: break-all; }
.kg-zh-block { margin-top: 14px; }
.kg-zh-block-title { display: flex; align-items: center; gap: 5px; margin-bottom: 7px; font-size: 12px; font-weight: 500; color: #1d2129; }
.kg-zh-code { margin: 0; padding: 10px 12px; border: 1px solid #e5e6eb; border-radius: 10px; background: #f7f8fa; color: #1d2129; font-size: 12px; white-space: pre-wrap; word-break: break-word; }
.kg-zh-points { margin: 0; padding-left: 18px; color: #4e5969; line-height: 1.85; }
.kg-zh-table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
.kg-zh-table th { padding: 7px 8px; text-align: left; font-weight: 500; color: #86909c; background: #f7f8fa; border-bottom: 1px solid #e5e6eb; white-space: nowrap; }
.kg-zh-table td { padding: 7px 8px; border-bottom: 1px solid #f2f3f5; color: #1d2129; vertical-align: top; }
.kg-zh-table tr:last-child td { border-bottom: 0; }
.kg-key-badge { display: inline-block; padding: 0 5px; border-radius: 4px; font-size: 10px; font-weight: 600; line-height: 16px; }
.kg-key-pk { color: #a8641d; background: #fdf6dd; border: 1px solid #efd88a; }
.kg-key-fk { color: #1677ff; background: #e8f3ff; border: 1px solid #bcd8ff; }
.kg-zh-ref { margin-left: 6px; color: #86909c; font-size: 10.5px; }
.kg-zh-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.kg-zh-chip { padding: 3px 9px; border: 1px solid #c9cdd4; border-radius: 999px; background: #fff; font-size: 11px; color: #4e5969; cursor: pointer; transition: border-color .15s ease, color .15s ease; }
.kg-zh-chip:hover { border-color: #1677ff; color: #1677ff; }
.kg-zh-rels { display: flex; flex-direction: column; gap: 6px; }
.kg-zh-rel { display: flex; align-items: center; gap: 8px; width: 100%; padding: 8px 10px; border: 1px solid #e5e6eb; border-radius: 10px; background: #fff; text-align: left; cursor: pointer; transition: border-color .15s ease, background .15s ease; }
.kg-zh-rel:hover { border-color: #1677ff; background: #f7fbff; }
.kg-zh-rel-dir { color: #ec4899; font-weight: 700; }
.kg-zh-rel-dir.out { color: #6366f1; }
.kg-zh-rel-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.kg-zh-rel-main b { font-size: 12px; font-weight: 500; color: #1d2129; }
.kg-zh-rel-main em { font-style: normal; font-size: 11px; color: #86909c; }
.kg-zh-rel-id { font-size: 10.5px; color: #a9aeb8; white-space: nowrap; }
.kg-zh-empty { padding: 10px; color: #a9aeb8; font-size: 11.5px; background: #f7f8fa; border-radius: 8px; }
.kg-zh-origin { margin-top: 14px; padding: 10px 12px; border: 1px dashed #e5e6eb; border-radius: 10px; background: #fafbfc; color: #86909c; font-size: 11.5px; line-height: 1.65; }
.kg-zh-origin p { margin: 0; }
</style>
