<template>
  <ModalDialog :visible="visible" title="定义指标口径" width="640px" maxHeight="88vh" @close="$emit('cancel')">
    <div class="space-y-2">
      <div>
        <label class="text-xs text-gray-500 block mb-0.5">指标名称 *</label>
        <input v-model="form.name" placeholder="如：订单满足率" class="w-full px-2.5 py-1.5 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-indigo-400" />
      </div>
      <div>
        <label class="text-xs text-gray-500 block mb-0.5">业务公式（自然语言，必填）</label>
        <input v-model="form.formula" placeholder="如：合格数 ÷ 投入数 × 100%" class="w-full px-2.5 py-1.5 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-indigo-400" />
      </div>
      <div>
        <div class="flex items-center justify-between mb-0.5">
          <label class="text-xs text-gray-500">口径 SQL（聚合表达式，可选）</label>
          <button
            @click="compile"
            :disabled="compiling"
            class="text-[11px] px-2 py-0.5 bg-indigo-50 text-indigo-600 rounded-full hover:bg-indigo-100 transition disabled:opacity-50"
          ><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z" /> <path d="M5 3v4" /> <path d="M19 17v4" /> <path d="M3 5h4" /> <path d="M17 19h4" /> </svg></span>{{ compiling ? '生成中…' : ' 帮我生成' }}</button>
        </div>
        <textarea
          v-model="form.sql_expression"
          rows="2"
          placeholder="SUM(x) / NULLIF(SUM(y),0) * 100（可留空，仅填公式）"
          class="w-full px-2.5 py-1.5 border border-gray-200 rounded-lg text-sm font-mono focus:outline-none focus:border-indigo-400"
        ></textarea>
        <div v-if="compileMsg" class="text-[11px] mt-1" :class="compileErr ? 'text-red-500' : 'text-blue-600'">{{ compileMsg }}</div>
      </div>
      <div class="grid grid-cols-2 gap-2">
        <div>
          <label class="text-xs text-gray-500 block mb-0.5">单位</label>
          <input v-model="form.unit" placeholder="件 / % / 元" class="w-full px-2.5 py-1.5 border border-gray-200 rounded-lg text-sm" />
        </div>
        <div>
          <label class="text-xs text-gray-500 block mb-0.5">适用表（逗号分隔）*</label>
          <input v-model="tablesText" placeholder="mes_process_output, test_orders" class="w-full px-2.5 py-1.5 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-indigo-400" />
        </div>
      </div>
      <div>
        <label class="text-xs text-gray-500 block mb-0.5">说明</label>
        <input v-model="form.description" placeholder="口径描述（可选）" class="w-full px-2.5 py-1.5 border border-gray-200 rounded-lg text-sm" />
      </div>
      <div v-if="error" class="text-xs text-red-500 bg-red-50 border border-red-100 rounded-lg px-2.5 py-1.5">{{ error }}</div>
      <div v-if="submitted" class="text-xs text-blue-600 bg-blue-50 border border-blue-100 rounded-lg px-2.5 py-1.5">{{ submitted }}</div>
      <div class="flex items-center gap-2 pt-0.5">
        <button
          @click="submit"
          :disabled="submitting || !form.name || !form.formula"
          class="px-3 py-1.5 bg-indigo-600 text-white rounded-lg text-xs hover:bg-indigo-700 transition disabled:opacity-40"
        >{{ submitting ? '提交中…' : '保存口径' }}</button>
        <button
          @click="$emit('cancel')"
          class="px-3 py-1.5 border border-gray-300 text-gray-600 rounded-lg text-xs hover:bg-gray-50 transition"
        >取消</button>
      </div>
    </div>
  </ModalDialog>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import ModalDialog from './ModalDialog.vue'

const props = defineProps<{ visible: boolean; prefillName?: string }>()
const emit = defineEmits<{ (e: 'cancel'): void; (e: 'saved'): void }>()

const form = ref({
  name: props.prefillName || '',
  formula: '',
  sql_expression: '',
  unit: '',
  description: '',
})
const tablesText = ref('')
const compiling = ref(false)
const submitting = ref(false)
const compileMsg = ref('')
const compileErr = ref(false)
const error = ref('')
const submitted = ref('')

watch(
  () => props.prefillName,
  (v) => { if (v && !form.value.name) form.value.name = v },
)

const api = async (path: string, opts: any = {}) => {
  const resp = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts })
  if (!resp.ok) {
    const err = await resp.json().catch(() => null)
    throw new Error(err?.detail || `请求失败(${resp.status})`)
  }
  return resp.json()
}

const compile = async () => {
  if (!form.value.formula) {
    compileErr.value = true
    compileMsg.value = '请先填写业务公式'
    return
  }
  compiling.value = true
  compileErr.value = false
  compileMsg.value = ''
  try {
    const d = await api('/api/metrics/compile', {
      method: 'POST',
      body: JSON.stringify({
        query: form.value.name,
        formula: form.value.formula,
        tables: tablesText.value.split(/[,，\s]+/).filter(Boolean),
      }),
    })
    form.value.sql_expression = d.sql || ''
    compileMsg.value = d.compiled
      ? `已命中现有口径「${d.metric}」，表达式已填入（可修改）`
      : '生成成功，请人工核对表达式'
    if (d.unit && !form.value.unit) form.value.unit = d.unit
  } catch (e: any) {
    compileErr.value = true
    compileMsg.value = e.message
  } finally {
    compiling.value = false
  }
}

const submit = async () => {
  if (!form.value.name.trim() || !form.value.formula.trim()) {
    error.value = '指标名称和业务公式必填'
    return
  }
  if (!tablesText.value.trim()) {
    error.value = '请填写适用表（至少一张）——空表定义会让指标无法编译执行，并干扰问题匹配'
    return
  }
  submitting.value = true
  error.value = ''
  submitted.value = ''
  try {
    const tables = tablesText.value.split(/[,，\s]+/).filter(Boolean)
    const body: any = {
      name: form.value.name.trim(),
      formula: form.value.formula.trim(),
      sql_expression: form.value.sql_expression.trim(),
      unit: form.value.unit.trim(),
      description: form.value.description.trim(),
      aliases: [],
      tables,
    }
    const d = await api('/api/metrics', { method: 'POST', body: JSON.stringify(body) })
    submitted.value = d.pending
      ? '已提交，待管理员审核通过后生效'
      : `已保存${d.metric?.name ? `（${d.metric.name}）` : ''}`
    emit('saved')
  } catch (e: any) {
    error.value = e.message
  } finally {
    submitting.value = false
  }
}

// 弹窗每次打开时重置表单（保留 prefillName 预填），避免残留上次定义
watch(
  () => props.visible,
  (v) => {
    if (v) {
      form.value = {
        name: props.prefillName || '',
        formula: '',
        sql_expression: '',
        unit: '',
        description: '',
      }
      tablesText.value = ''
      compileMsg.value = ''
      compileErr.value = false
      error.value = ''
      submitted.value = ''
    }
  },
)
</script>
