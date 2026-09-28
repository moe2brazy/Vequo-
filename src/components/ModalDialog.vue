<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="fixed inset-0 z-50 flex items-center justify-center"
    >
      <!-- 遮罩：点击关闭（挂遮罩上而非外层，否则被遮罩拦截导致 .self 永不命中） -->
      <div class="absolute inset-0 bg-black/40 backdrop-blur-[6px] ui-mask-fade" @click="close"></div>
      <!-- 弹窗主体 -->
      <div
        class="relative bg-white rounded-2xl shadow-2xl border border-gray-200 overflow-hidden flex flex-col ui-dialog-pop"
        style="box-shadow: 0 24px 60px -16px rgba(16,24,40,.28), 0 8px 20px -8px rgba(16,24,40,.12);"
        :style="{ width: width, maxHeight: maxHeight }"
      >
        <!-- 头部 -->
        <div class="flex items-center justify-between px-6 py-4 border-b border-gray-100 flex-shrink-0">
          <h3 class="text-lg font-semibold text-gray-900">{{ title }}</h3>
          <button
            @click="close"
            class="w-8 h-8 flex items-center justify-center rounded-lg text-gray-400 hover:text-gray-600 hover:bg-gray-100 transition-colors"
          >
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
        <!-- 内容 -->
        <div class="flex-1 overflow-y-auto px-6 py-4">
          <slot />
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
defineProps<{
  visible: boolean
  title: string
  width?: string
  maxHeight?: string
}>()

const emit = defineEmits<{
  (e: 'close'): void
}>()

const close = () => emit('close')
</script>

<style scoped>
.ui-mask-fade {
  animation: ui-mask-in .22s ease both;
}
.ui-dialog-pop {
  animation: ui-dialog-in .28s cubic-bezier(.34, 1.3, .64, 1) both;
}
@keyframes ui-mask-in {
  from { opacity: 0; }
  to { opacity: 1; }
}
@keyframes ui-dialog-in {
  from { opacity: 0; transform: translateY(14px) scale(.97); }
  to { opacity: 1; transform: none; }
}
</style>
