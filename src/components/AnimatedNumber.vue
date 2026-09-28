<template>
  <span class="tabular-nums">{{ display.toLocaleString() }}</span>
</template>

<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'

/** 数字滚动动画：值变化时从当前值平滑过渡到目标值（ease-out cubic） */
const props = withDefaults(
  defineProps<{
    value: number
    duration?: number
  }>(),
  { duration: 900 },
)

const display = ref(0)
let raf = 0

const animate = (to: number) => {
  cancelAnimationFrame(raf)
  const from = display.value
  const start = performance.now()
  const tick = (now: number) => {
    const t = Math.min((now - start) / props.duration, 1)
    const eased = 1 - Math.pow(1 - t, 3)
    display.value = Math.round(from + (to - from) * eased)
    if (t < 1) raf = requestAnimationFrame(tick)
  }
  raf = requestAnimationFrame(tick)
}

watch(() => props.value, (v) => animate(Number(v) || 0), { immediate: true })

onBeforeUnmount(() => cancelAnimationFrame(raf))
</script>
