<template>
  <div class="page-lazy-error" role="alert">
    <div class="box">
      <span class="chip" aria-hidden="true">!</span>
      <h3>页面加载失败</h3>
      <p>这个页面的代码没能加载出来。常见原因：开发服务器重新预构建依赖后旧资源地址失效（Vite Re-optimizing dependencies）、网络抖动，或该页面自身运行时报错。</p>
      <pre v-if="detail">{{ detail }}</pre>
      <div class="actions">
        <button type="button" class="primary" @click="reload">重新加载页面</button>
        <button type="button" class="ghost" @click="retry" v-if="canRetry">仅重试本页</button>
      </div>
      <small>如果刷新后仍失败，请把上面这段错误信息发给开发者（同时也打印在浏览器控制台）。</small>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

const props = defineProps<{ error?: unknown }>()
const canRetry = ref(true)

const detail = computed(() => {
  const e = props.error as { message?: string } | string | undefined
  const msg = typeof e === 'string' ? e : (e?.message || '')
  return String(msg).slice(0, 300)
})

function reload() {
  window.location.reload()
}

// 单页重试：回到初始页，让动态 import 重新发起一次（不整页刷新，保留登录态与输入）
function retry() {
  canRetry.value = false
  window.dispatchEvent(new CustomEvent('ops:retry-page'))
}
</script>

<style scoped>
.page-lazy-error {
  display: flex;
  min-height: 55vh;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.box {
  max-width: 560px;
  padding: 26px 28px;
  border: 1px solid rgba(245, 34, 45, .22);
  border-radius: 20px;
  background: #fff;
  box-shadow: 0 18px 44px rgba(29, 33, 41, .08);
  text-align: left;
}
.chip {
  display: grid;
  width: 30px;
  height: 30px;
  place-items: center;
  border-radius: 50%;
  background: rgba(245, 34, 45, .1);
  color: #d03050;
  font-weight: 700;
}
.box h3 { margin: 14px 0 6px; font-size: 16px; font-weight: 600; color: #1d2129; }
.box p { margin: 0; color: #4e5969; font-size: 13px; line-height: 1.75; }
.box pre {
  max-height: 130px;
  margin: 14px 0 0;
  padding: 11px 13px;
  overflow: auto;
  border-radius: 12px;
  background: #f7f8fa;
  color: #d03050;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}
.actions { display: flex; gap: 10px; margin-top: 18px; }
.actions button {
  min-height: 40px;
  padding: 0 18px;
  border-radius: 12px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background .18s, color .18s;
}
.actions .primary { border: 0; background: #4D9EFF; color: #fff; }
.actions .primary:hover { background: #2E7CF0; }
.actions .ghost { border: 1px solid #c9cdd4; background: #fff; color: #1d2129; }
.actions .ghost:hover:not(:disabled) { background: #e8f3ff; color: #2E7CF0; }
.actions .ghost:disabled { opacity: .5; cursor: not-allowed; }
.box small { display: block; margin-top: 14px; color: #86909c; font-size: 11px; line-height: 1.7; }
</style>
