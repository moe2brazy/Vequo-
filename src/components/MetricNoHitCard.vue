<template>
  <div class="mt-2 px-3 py-2.5 bg-amber-50 border border-amber-200 rounded-lg text-xs text-amber-800 leading-relaxed">
    <div class="flex items-start gap-2">
      <span class="mt-0.5"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" /> <path d="M12 9v4" /> <path d="M12 17h.01" /> </svg></span></span>
      <div class="flex-1 min-w-0">
        <div>
          未能从口径库识别您问题中的业务口径<template v-if="hints.length">（疑似：{{ hints.join('、') }}）</template>。
          您可以直接换个说法重新提问，或先定义该指标口径。
        </div>
        <div class="mt-1.5 flex items-center gap-2 flex-wrap">
          <button
            @click="$emit('infer')"
            :disabled="inferring"
            class="px-2 py-0.5 bg-indigo-100 text-indigo-700 rounded-full hover:bg-indigo-200 transition disabled:opacity-50"
            :title="'让 AI 分析口径并生成推荐查询方案，弹窗确认后执行'"
          ><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z" /> <path d="M5 3v4" /> <path d="M19 17v4" /> <path d="M3 5h4" /> <path d="M17 19h4" /> </svg></span>{{ inferring ? '正在生成方案（AI 推断中，最长约 45 秒）…' : ' 推荐 LLM 执行' }}</button>
          <button
            @click="$emit('define')"
            class="px-2 py-0.5 bg-amber-100 text-amber-700 rounded-full hover:bg-amber-200 transition"
          ><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z" /> <polyline points="14 2 14 8 20 8" /> <line x1="16" x2="8" y1="13" y2="13" /> <line x1="16" x2="8" y1="17" y2="17" /> <line x1="10" x2="8" y1="9" y2="9" /> </svg></span> 我来定义口径</button>
          <button
            @click="$emit('dismiss')"
            class="px-2 py-0.5 text-amber-600 hover:bg-amber-100 rounded-full transition"
          ><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M18 6 6 18" /> <path d="m6 6 12 12" /> </svg></span> 忽略</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
defineProps<{ hints: string[]; inferring?: boolean }>()
defineEmits<{ (e: 'define'): void; (e: 'dismiss'): void; (e: 'infer'): void }>()
</script>
