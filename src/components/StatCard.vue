<script setup lang="ts">
import type { PropType } from 'vue'

export interface StatItem {
  label: string
  value: number
  change: number
  icon: string
}

const props = defineProps({
  stat: {
    type: Object as PropType<StatItem>,
    required: true,
  },
})

const isPositive = props.stat.change >= 0
</script>

<template>
  <div
    class="stat-card group bg-white rounded-2xl border border-gray-100 p-5 hover:-translate-y-1 transition-all duration-300"
    style="box-shadow: var(--shadow-card)"
  >
    <div class="flex items-start justify-between">
      <div class="min-w-0">
        <p class="text-xs text-gray-400 font-medium uppercase tracking-wider">{{ stat.label }}</p>
        <p class="text-[28px] leading-tight font-bold text-gray-900 mt-2 tabular-nums tracking-tight">
          {{ stat.value.toLocaleString() }}
        </p>
      </div>
      <span
        class="grid h-10 w-10 flex-none place-items-center rounded-xl text-lg transition-transform duration-300 group-hover:scale-110"
        style="background: var(--brand-050); color: var(--brand-600)"
      >{{ stat.icon }}</span>
    </div>
    <div class="flex items-center gap-1.5 mt-4 pt-3 border-t border-gray-50">
      <span
        class="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-semibold"
        :style="isPositive ? 'background:var(--brand-050);color:var(--brand-600)' : 'background:#FDECEC;color:#F53F3F'"
      >
        {{ isPositive ? '↑' : '↓' }} {{ Math.abs(stat.change).toFixed(2) }}%
      </span>
      <span class="text-xs text-gray-400">较上期</span>
    </div>
  </div>
</template>
