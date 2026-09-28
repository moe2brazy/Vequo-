<template>
  <span
    class="app-icon"
    :style="{ width: size + 'px', height: size + 'px' }"
    aria-hidden="true"
    v-html="svg"
  ></span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

/**
 * 统一图标组件
 * 图标资源：Lucide（ISC License）—— 官方发行包 lucide-static@0.263.0
 * 本地化存储于 src/assets/icons/，构建期内联，无外部 CDN 依赖
 * https://lucide.dev
 */
const modules = import.meta.glob('../assets/icons/*.svg', {
  eager: true,
  query: '?raw',
  import: 'default',
}) as Record<string, string>

const ICONS: Record<string, string> = {}
for (const [path, raw] of Object.entries(modules)) {
  const name = path.split('/').pop()!.replace('.svg', '')
  ICONS[name] = raw
}

const props = withDefaults(
  defineProps<{
    name: string
    size?: number
    strokeWidth?: number
  }>(),
  { size: 20, strokeWidth: 1.8 },
)

const svg = computed(() => {
  const raw = ICONS[props.name] || ICONS['info']
  return raw
    .replace('width="24"', `width="${props.size}"`)
    .replace('height="24"', `height="${props.size}"`)
    .replace('stroke-width="2"', `stroke-width="${props.strokeWidth}"`)
})
</script>

<style scoped>
.app-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  line-height: 0;
}
.app-icon :deep(svg) {
  display: block;
}
</style>
