<template>
  <div class="page-lazy" role="status" aria-live="polite">
    <div class="dots"><i /><i /><i /></div>
    <p>正在加载页面…</p>
  </div>
</template>

<style scoped>
.page-lazy {
  display: flex;
  min-height: 55vh;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
  color: #86909c;
  font-size: 13px;
}
.dots { display: flex; gap: 7px; }
.dots i {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: #4D9EFF;
  opacity: .35;
  animation: page-lazy-bounce 1s ease-in-out infinite;
}
.dots i:nth-child(2) { animation-delay: .15s; }
.dots i:nth-child(3) { animation-delay: .3s; }
@keyframes page-lazy-bounce {
  0%, 60%, 100% { opacity: .3; transform: translateY(0); }
  30% { opacity: 1; transform: translateY(-5px); }
}
</style>
