<template>
  <div ref="root" class="ops-wavy-field" aria-hidden="true">
    <svg ref="svg" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

type WavePoint = { x: number; y: number; ox: number; oy: number; vx: number; vy: number; phase: number }

const root = ref<HTMLElement | null>(null)
const svg = ref<SVGSVGElement | null>(null)
const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
const pointer = { x: -9999, y: -9999, sx: -9999, sy: -9999, lx: -9999, ly: -9999, speed: 0, angle: 0, active: false }
let lines: WavePoint[][] = []
let paths: SVGPathElement[] = []
let bounds = { left: 0, top: 0, width: 1, height: 1 }
let observer: ResizeObserver | null = null
let frame = 0

function moved(point: WavePoint) {
  return { x: point.x + point.ox, y: point.y + point.oy }
}
function pathFor(points: WavePoint[]) {
  const first = moved(points[0])
  let data = `M ${first.x.toFixed(1)} ${first.y.toFixed(1)}`
  for (let index = 1; index < points.length - 1; index += 1) {
    const point = moved(points[index]), next = moved(points[index + 1])
    data += ` Q ${point.x.toFixed(1)} ${point.y.toFixed(1)} ${((point.x + next.x) / 2).toFixed(1)} ${((point.y + next.y) / 2).toFixed(1)}`
  }
  const last = moved(points[points.length - 1])
  return `${data} L ${last.x.toFixed(1)} ${last.y.toFixed(1)}`
}
function redraw() {
  lines.forEach((points, index) => paths[index]?.setAttribute('d', pathFor(points)))
}
async function setup() {
  if (!root.value || !svg.value) return
  bounds = root.value.getBoundingClientRect()
  const xGap = bounds.width < 720 ? 28 : 22, yGap = bounds.width < 720 ? 48 : 38
  const columns = Math.ceil((bounds.width + 180) / xGap), rows = Math.ceil((bounds.height + 120) / yGap)
  const startX = (bounds.width - columns * xGap) / 2, startY = -60
  lines = Array.from({ length: columns + 1 }, (_, column) => Array.from({ length: rows + 1 }, (_, row) => ({
    x: startX + column * xGap,
    y: startY + row * yGap,
    ox: 0,
    oy: 0,
    vx: 0,
    vy: 0,
    phase: column * .31 + row * .17,
  })))
  svg.value.replaceChildren()
  paths = lines.map((_, index) => {
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path')
    path.classList.add(index % 7 === 0 ? 'wave-accent' : 'wave-line')
    svg.value?.appendChild(path)
    return path
  })
  await nextTick()
  redraw()
  cancelAnimationFrame(frame)
  if (!reduced) frame = requestAnimationFrame(tick)
}
function tick(time: number) {
  const targetX = pointer.active ? pointer.x : bounds.width * .58
  const targetY = pointer.active ? pointer.y : bounds.height * .42
  if (pointer.sx < -1000) { pointer.sx = targetX; pointer.sy = targetY; pointer.lx = targetX; pointer.ly = targetY }
  pointer.sx += (targetX - pointer.sx) * .14
  pointer.sy += (targetY - pointer.sy) * .14
  const mouseDx = targetX - pointer.lx, mouseDy = targetY - pointer.ly
  const rawSpeed = Math.hypot(mouseDx, mouseDy)
  pointer.speed += (rawSpeed - pointer.speed) * .16
  pointer.speed = Math.min(70, pointer.speed)
  if (rawSpeed > .1) pointer.angle = Math.atan2(mouseDy, mouseDx)
  pointer.lx = targetX; pointer.ly = targetY

  for (const points of lines) for (const point of points) {
    const dx = point.x + point.ox - pointer.sx, dy = point.y + point.oy - pointer.sy
    const distance = Math.max(1, Math.hypot(dx, dy)), radius = 115 + pointer.speed * 1.4
    if (pointer.active && distance < radius) {
      const falloff = 1 - distance / radius, force = falloff * pointer.speed * .035
      point.vx += (Math.cos(pointer.angle) * .72 + dx / distance * .28) * force
      point.vy += (Math.sin(pointer.angle) * .28 + dy / distance * .08) * force
    }
    const ambient = Math.sin(time * .00045 + point.phase) * 2.2
    point.vx += (ambient - point.ox) * .012
    point.vy += -point.oy * .015
    point.vx *= .91; point.vy *= .91
    point.ox = Math.max(-48, Math.min(48, point.ox + point.vx))
    point.oy = Math.max(-24, Math.min(24, point.oy + point.vy))
  }
  redraw()
  frame = requestAnimationFrame(tick)
}
function handlePointer(event: PointerEvent) {
  if (!root.value || reduced) return
  const rect = root.value.getBoundingClientRect()
  pointer.x = event.clientX - rect.left; pointer.y = event.clientY - rect.top; pointer.active = true
}
function resetPointer() { pointer.active = false; pointer.speed = 0 }

onMounted(() => {
  setup()
  observer = new ResizeObserver(setup)
  if (root.value) observer.observe(root.value)
  window.addEventListener('pointermove', handlePointer, { passive: true })
  window.addEventListener('blur', resetPointer)
  document.documentElement.addEventListener('mouseleave', resetPointer)
})
onBeforeUnmount(() => {
  cancelAnimationFrame(frame)
  observer?.disconnect()
  window.removeEventListener('pointermove', handlePointer)
  window.removeEventListener('blur', resetPointer)
  document.documentElement.removeEventListener('mouseleave', resetPointer)
})
</script>

<style scoped>
.ops-wavy-field{position:fixed;z-index:0;inset:0;overflow:hidden;pointer-events:none;opacity:.42;mask-image:linear-gradient(90deg,#000 0 44%,rgba(0,0,0,.5) 72%,transparent)}
svg{width:100%;height:100%}:deep(path){fill:none;vector-effect:non-scaling-stroke}:deep(.wave-line){stroke:rgba(27,31,27,.18);stroke-width:.7}:deep(.wave-accent){stroke:rgba(77,158,255,.3);stroke-width:1}
@media(max-width:760px){.ops-wavy-field{opacity:.26;mask-image:linear-gradient(180deg,#000,transparent 82%)}}
@media(prefers-reduced-motion:reduce){.ops-wavy-field{opacity:.2}}
</style>
