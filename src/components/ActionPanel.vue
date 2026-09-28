<template>
  <!-- 行动面板（对标 Fabric operations agents / Sigma Agents）：洞察 → 动作 → 执行闭环。
       写操作永远走「编译预览 → 二次确认 → 执行」三道闸门，参数化绑定无注入。 -->
  <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" @click.self="emit('close')">
    <div class="w-full max-w-lg bg-white rounded-2xl shadow-xl border border-gray-200 max-h-[86vh] flex flex-col">
      <!-- 头部 -->
      <div class="flex items-center justify-between px-4 py-3 border-b border-gray-100 shrink-0">
        <div>
          <h3 class="text-sm font-semibold text-gray-800">行动面板</h3>
          <p class="text-[11px] text-gray-400 mt-0.5">把数据结论转化为可执行动作 · SQL 由注册表确定性编译</p>
        </div>
        <button @click="emit('close')" class="text-gray-400 hover:text-gray-600 text-lg leading-none px-1">×</button>
      </div>

      <div class="p-4 overflow-y-auto flex-1 space-y-3">
        <!-- 1. 动作选择 -->
        <div>
          <label class="text-[11px] text-gray-500 font-medium">动作</label>
          <select v-model="actionId" @change="onActionChange"
                  class="mt-1 w-full px-2.5 py-2 text-xs border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-200 bg-white">
            <option v-for="a in actions" :key="a.id" :value="a.id">{{ a.name }}</option>
          </select>
          <p v-if="currentAction?.description" class="text-[11px] text-gray-400 mt-1">{{ currentAction.description }}</p>
        </div>

        <!-- 2. 参数表单（按 schema 渲染） -->
        <div v-if="currentAction && paramsSchema.length" class="space-y-2">
          <label class="text-[11px] text-gray-500 font-medium block">参数</label>
          <div v-for="p in paramsSchema" :key="p.key">
            <label class="text-[11px] text-gray-500">{{ p.label }}{{ p.required ? ' *' : '' }}</label>
            <input
              v-model="params[p.key]"
              :type="p.type === 'number' || p.type === 'int' ? 'number' : 'text'"
              :placeholder="p.label"
              class="mt-0.5 w-full px-2.5 py-1.5 text-xs border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-200"
            />
          </div>
        </div>

        <!-- 3. 编译预览 -->
        <div>
          <button @click="preview" :disabled="previewing"
                  class="px-3 py-1.5 text-xs border border-gray-300 rounded-lg hover:bg-gray-50 disabled:opacity-50">
            {{ previewing ? '编译中…' : (previewSql ? '重新编译' : '预览 SQL') }}
          </button>
          <pre v-if="previewSql" class="mt-2 p-2.5 rounded-lg bg-slate-900 text-slate-100 text-[11px] font-mono overflow-x-auto whitespace-pre-wrap break-all">{{ previewSql }}</pre>
          <p v-if="previewErr" class="mt-1 text-[11px] text-rose-600">{{ previewErr }}</p>
        </div>

        <!-- 4. 结果 -->
        <div v-if="result" :class="result.success ? 'bg-blue-50 border-blue-200 text-blue-700' : 'bg-rose-50 border-rose-200 text-rose-700'"
             class="px-3 py-2 rounded-lg border text-[11.5px] leading-relaxed">
          <div v-if="result.success"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /> <polyline points="22 4 12 14.01 9 11.01" /> </svg></span> 执行成功：影响 {{ result.rows_affected }} 行（{{ result.target_table }}）</div>
          <div v-else><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <circle cx="12" cy="12" r="10" /> <path d="m15 9-6 6" /> <path d="m9 9 6 6" /> </svg></span> {{ result.error }}</div>
          <p v-if="result.error && /未启用|WRITE_BACK/.test(result.error)" class="mt-1 text-[10.5px] opacity-80">
            写回能力默认关闭，需在服务端设置环境变量 WRITE_BACK_ENABLED=1 后重启生效。
          </p>
        </div>
      </div>

      <!-- 底部：执行 -->
      <div class="px-4 py-3 border-t border-gray-100 flex items-center justify-between shrink-0">
        <span class="text-[10.5px] text-gray-400">写操作需二次确认，动作仅限注册表白名单</span>
        <button @click="execute" :disabled="executing || !actionId"
                class="px-4 py-2 text-xs bg-rose-600 text-white rounded-lg hover:bg-rose-700 disabled:opacity-50">
          {{ executing ? '执行中…' : '确认执行' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'

const props = defineProps<{ prefillAction?: string }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'executed', r: any): void }>()

const actions = ref<any[]>([])
const actionId = ref(props.prefillAction || '')
const params = ref<Record<string, any>>({})
const previewSql = ref('')
const previewErr = ref('')
const previewing = ref(false)
const executing = ref(false)
const result = ref<any>(null)

const currentAction = computed(() => actions.value.find(a => a.id === actionId.value))
// 参数 schema 来自后端 list_actions 已合并的 params 字段（key -> {label, type, required}，
// 由 set_columns 与 where_columns 合并而来）。
// 修复（P1）：此前前端只读 set_columns/where_columns 而真实后端返回 params，
// 导致参数表单永远空白、动作面板整体不可用。
const paramsSchema = computed(() => {
  const a = currentAction.value
  if (!a) return []
  const merged: Record<string, any> = {}
  for (const [k, v] of Object.entries((a as any).params || {})) merged[k] = v
  // 兼容旧结构（若后端某动作仍直接带 set_columns/where_columns）
  for (const [k, v] of Object.entries((a as any).set_columns || {})) merged[k] = v
  for (const [k, v] of Object.entries((a as any).where_columns || {})) merged[k] = v
  return Object.entries(merged).map(([key, v]: any) => ({
    key, label: v?.label || key, type: v?.type || 'string',
    required: v?.required !== false,
  }))
})

async function api(path: string, opts: any = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data?.detail || `请求失败(${res.status})`)
  return data
}

async function loadActions() {
  try {
    const d = await api('/api/actions')
    actions.value = d.actions || []
    if (!actionId.value && actions.value.length) actionId.value = actions.value[0].id
    onActionChange()
  } catch (e: any) {
    previewErr.value = e?.message || '动作清单加载失败'
  }
}

function onActionChange() {
  params.value = {}
  previewSql.value = ''
  previewErr.value = ''
  result.value = null
}

async function preview() {
  previewing.value = true
  previewErr.value = ''
  try {
    const d = await api('/api/actions/compile', {
      method: 'POST',
      body: JSON.stringify({ action: actionId.value, params: params.value }),
    })
    if (d.success) previewSql.value = d.sql
    else previewErr.value = d.error || '编译失败'
  } catch (e: any) {
    previewErr.value = e?.message || '编译失败'
  } finally {
    previewing.value = false
  }
}

async function execute() {
  if (!window.confirm('确认执行该写操作？此操作将修改数据库数据。')) return
  executing.value = true
  result.value = null
  try {
    const d = await api('/api/actions/execute', {
      method: 'POST',
      body: JSON.stringify({ action: actionId.value, params: params.value, confirmed: true }),
    })
    result.value = d
    if (d.success) emit('executed', d)
  } catch (e: any) {
    result.value = { success: false, error: e?.message || '执行失败' }
  } finally {
    executing.value = false
  }
}

watch(() => props.prefillAction, (v) => {
  if (v && v !== actionId.value) {
    actionId.value = v
    onActionChange()
  }
})

loadActions()
</script>
