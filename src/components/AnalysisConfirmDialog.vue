<template>
  <ModalDialog :visible="visible" title="需要补充一点信息" width="620px" maxHeight="88vh" @close="onCancel">
    <div class="space-y-3.5">
      <!-- 上次执行失败原因（重开弹窗时展示） -->
      <div
        v-if="error"
        class="px-3 py-2 bg-red-50 border border-red-200 rounded-lg text-xs text-red-700 leading-relaxed"
      >
        <span class="font-medium">上次没查出来：</span>{{ error }}
      </div>

      <!-- 触发原因（人话说明到底哪儿不清楚） -->
      <div class="px-3 py-2.5 rounded-lg text-xs leading-relaxed" :class="reasonBanner.class">
        <span class="font-medium">{{ reasonBanner.title }}</span>
        <div class="mt-0.5">{{ reasonBanner.text }}</div>
      </div>

      <!-- 用户原问题 -->
      <div class="px-3 py-2 bg-gray-50 border border-gray-100 rounded-lg text-sm text-gray-700 leading-relaxed">
        <span class="text-xs text-gray-400">你的问题：</span>{{ query }}
      </div>

      <!-- 澄清输入：用户只说大白话，由 AI 识别 -->
      <div>
        <div class="text-xs text-gray-600 mb-1.5">{{ askLabel }}</div>
        <textarea
          v-model="clarifyText"
          rows="3"
          :placeholder="placeholder"
          @keydown.enter.exact.prevent="onClarify"
          class="w-full text-sm leading-relaxed text-gray-800 bg-white border border-gray-200 rounded-lg p-3 resize-y focus:outline-none focus:ring-2 focus:ring-indigo-200 focus:border-indigo-300"
        ></textarea>
        <div class="mt-1.5 text-[11px] text-gray-400">用大白话说就行，AI 会自己理解；回车可直接提交。</div>
      </div>

      <!-- 操作 -->
      <div class="flex items-center justify-end gap-2 pt-1">
        <button
          @click="onCancel"
          :disabled="submitting"
          class="px-3.5 py-2 border border-gray-200 text-gray-500 rounded-lg text-xs hover:bg-gray-50 transition disabled:opacity-40"
        >稍后处理</button>
        <button
          @click="$emit('define')"
          :disabled="submitting"
          class="px-3.5 py-2 border border-indigo-200 text-indigo-600 rounded-lg text-xs hover:bg-indigo-50 transition disabled:opacity-40"
        >去登记口径</button>
        <button
          @click="$emit('auto')"
          :disabled="submitting || inferring"
          class="px-3.5 py-2 border border-gray-200 text-gray-600 rounded-lg text-xs hover:bg-gray-50 transition disabled:opacity-40"
        >{{ inferring ? 'AI 理解中…' : '就按你的理解查' }}</button>
        <button
          @click="onClarify"
          :disabled="submitting || !clarifyText.trim()"
          class="px-4 py-2 bg-indigo-600 text-white rounded-lg text-xs font-medium hover:bg-indigo-700 transition disabled:opacity-40"
        >补充完，重新查询</button>
      </div>
    </div>
  </ModalDialog>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import ModalDialog from './ModalDialog.vue'

// 定位：这不是「方案确认弹窗」，而是「澄清弹窗」——
// 未定义口径 / 查询模糊时，只问用户一句话，用户用自然语言补充，交给 AI 识别。
// 不再展示置信度、指标公式、SQL 草稿这些业务用户看不懂也不需要看的东西。
const props = defineProps<{
  visible: boolean
  query: string
  reason?: string          // no_hit / ambiguous / complex …
  hints?: string[]         // 疑似未登记的指标词
  analysis?: any           // 仍由父组件持有（「就按你的理解查」要用它的 sql_draft），此处不展示
  error?: string           // 上次执行失败原因
  genError?: string        // 方案生成失败原因
  inferring?: boolean      // 正在生成方案（「就按你的理解查」的 loading 态）
}>()

const emit = defineEmits<{
  (e: 'clarify', text: string): void   // 用户提交了自然语言补充
  (e: 'auto'): void                    // 不补充了，让 AI 按现有理解直接查
  (e: 'define'): void                  // 去登记口径
  (e: 'cancel'): void
}>()

const clarifyText = ref('')
const submitting = ref(false)

const setSubmitting = (v: boolean) => { submitting.value = v }

// 每次打开清空上一次的输入，避免串到别的问题上
watch(() => props.visible, (v) => {
  if (v) {
    clarifyText.value = ''
    submitting.value = false
  }
})

// 依据原因给出「到底哪儿不清楚」的人话说明
const reasonBanner = computed(() => {
  const word = (props.hints || []).filter(Boolean).slice(0, 2).join('、')
  switch (props.reason) {
    case 'ambiguous':
      return {
        title: word ? `「${word}」有几种不同的算法` : '这个问题有几种不同的理解',
        text: '系统里有多个相近口径，请说明按哪个算，例如按什么字段、要不要去重。',
        class: 'bg-indigo-50 border border-indigo-200 text-indigo-700',
      }
    case 'complex':
      return {
        title: '这个问题涉及多张表',
        text: '请补充一下统计范围和条件，比如时间范围、只看哪类数据，AI 才好准确取数。',
        class: 'bg-purple-50 border border-purple-200 text-purple-700',
      }
    default:
      return {
        title: word ? `没查到「${word}」的统计口径` : '没查到对应的统计口径',
        text: '这个词还没登记过算法，麻烦用大白话说明你想统计什么、怎么算。',
        class: 'bg-amber-50 border border-amber-200 text-amber-800',
      }
  }
})

const askLabel = computed(() =>
  props.reason === 'ambiguous' ? '你想按哪种口径算？' : '补充一下，你具体想怎么算？'
)

const placeholder = computed(() =>
  props.reason === 'ambiguous'
    ? '例如：按不良记录数算，同一条记录只算一次'
    : '例如：按工序统计最近 7 天的不良数量，不良类型只算划伤'
)

const onClarify = () => {
  const t = clarifyText.value.trim()
  if (t) emit('clarify', t)
}
const onCancel = () => emit('cancel')

defineExpose({ setSubmitting })
</script>
