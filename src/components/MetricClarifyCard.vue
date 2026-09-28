<template>
  <div class="mt-2 p-3 bg-white rounded-lg border border-indigo-200 shadow-sm">
    <div class="text-sm text-gray-700 leading-relaxed"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <circle cx="12" cy="12" r="10" /> <path d="M12 16v-4" /> <path d="M12 8h.01" /> </svg></span> {{ answer }}</div>
    <div class="mt-2 space-y-1.5">
      <label
        v-for="(h, i) in hits"
        :key="h.name"
        :class="['flex items-start gap-2 px-3 py-2 rounded-lg border cursor-pointer transition',
                 h.denied ? 'opacity-50 cursor-not-allowed' : (selected === i ? 'border-indigo-400 bg-indigo-50' : 'border-gray-200 hover:bg-gray-50')]"
      >
        <input type="radio" :name="'clarify-' + uid" :value="i" v-model="selected" :disabled="h.denied" class="mt-1" />
        <div class="flex-1 min-w-0">
          <div class="text-sm text-gray-800 font-medium">
            {{ h.name }}
            <span v-if="h.denied" class="ml-1 text-[10px] px-1.5 py-0.5 bg-gray-200 text-gray-500 rounded-full">当前角色不可用</span>
            <span v-else-if="h.unit" class="ml-1 text-[10px] text-gray-400">{{ h.unit }}</span>
            <button
              v-if="h.denied"
              @click.stop="hint = '该口径对您当前角色不可用，请联系管理员在「权限管理 → 指标口径」中为您的角色授权后再查询。'"
              class="ml-1 text-[10px] px-1.5 py-0.5 bg-indigo-50 text-indigo-600 rounded-full hover:bg-indigo-100 transition"
            >申请</button>
          </div>
          <div class="text-xs text-gray-500 mt-0.5">{{ h.formula || h.sql_expression || '—' }}</div>
          <div v-if="h.description" class="text-xs text-gray-400 mt-0.5">{{ h.description }}</div>
        </div>
      </label>
    </div>
    <div v-if="hint" class="mt-2 px-2.5 py-1.5 bg-indigo-50 border border-indigo-100 rounded-lg text-[11px] text-indigo-700 leading-relaxed">{{ hint }}</div>
    <div class="mt-2.5 flex items-center gap-2">
      <button
        @click="pick"
        :disabled="selected === -1"
        class="px-3 py-1.5 bg-indigo-600 text-white rounded-lg text-xs hover:bg-indigo-700 transition disabled:opacity-40"
      >按此口径查询</button>
      <button
        @click="$emit('custom')"
        class="px-3 py-1.5 border border-gray-300 text-gray-600 rounded-lg text-xs hover:bg-gray-50 transition"
      >都不是，自定义</button>
      <button
        v-if="hits.length"
        @click="$emit('pick', hits[0].name)"
        class="px-3 py-1.5 text-indigo-500 rounded-lg text-xs hover:bg-indigo-50 transition"
      >不选择，按「{{ hits[0].name }}」计算</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

const props = defineProps<{ answer: string; hits: any[] }>()
const emit = defineEmits<{ (e: 'pick', name: string): void; (e: 'custom'): void }>()

const uid = Math.random().toString(36).slice(2, 8)
const selected = ref(-1)
const hint = ref('')

const pick = () => {
  if (selected.value >= 0 && !props.hits[selected.value]?.denied) {
    emit('pick', props.hits[selected.value].name)
  }
}
</script>
