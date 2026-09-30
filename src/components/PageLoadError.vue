<template>
  <div class="page-lazy-error" role="alert">
    <div class="box">
      <span class="chip" aria-hidden="true">!</span>
      <h3>页面加载失败</h3>
      <p>这个页面的代码没能加载出来。常见原因：开发服务器重新预构建依赖后旧资源地址失效（Vite Re-optimizing dependencies）、网络抖动，或该页面自身运行时报错。</p>
      <pre v-if="detail">{{ detail }}</pre>
      <div class="actions">
        <button type="button" class="primary" @click="reload">重新加载页面</button>
        <button type="button" class="ghost" @click="retry" v-if="canRetry && !staleChunk">仅重试本页</button>
      </div>
      <small v-if="staleChunk">这类错误是「开发服务器重新预构建依赖后旧资源地址失效」，本页内重试用的还是同一个失效地址，只有整页刷新才能恢复。</small>
      <small v-else>如果刷新后仍失败，请把上面这段错误信息发给开发者（同时也打印在浏览器控制台）。</small>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

const props = defineProps<{ error?: unknown }>()
const canRetry = ref(true)

const rawMessage = computed(() => {
  const e = props.error as { message?: string } | string | undefined
  return typeof e === 'string' ? e : (e?.message || '')
})

const detail = computed(() => String(rawMessage.value).slice(0, 300))

// 依赖重优化 / chunk 失效类错误：Vite 重新预构建依赖（vite config 变更、启动期 optimize）后
// 旧的 dep 地址全部失效，此时「仅重试本页」重新发起的仍是那个已失效的地址 → 必然再次失败；
// 只有整页刷新拿到新的模块图才能恢复（生产环境发新版后旧 chunk 被清理也是同一现象）。
// 实测：dev server 重优化后 DataPage.vue 一直报 Failed to fetch dynamically imported module。
const staleChunk = computed(() =>
  /dynamically imported module|Importing a module script failed/i.test(String(rawMessage.value)))

/** 整页刷新：带时间戳强制绕过 HTML/模块缓存，同时保留原有查询参数（如 ?view= 入口），
 *  避免 location.reload() 命中的还是缓存里的旧 index.html → 刷新后依旧失败。 */
function reload() {
  try {
    const url = new URL(window.location.href)
    url.searchParams.set('_r', Date.now().toString(36))
    window.location.replace(url.toString())
  } catch {
    window.location.reload()
  }
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
