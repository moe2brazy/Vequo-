<template>
  <div ref="root" class="ops-landing">
    <OpsWavyField />
    <header class="ops-nav">
      <button class="ops-brand" aria-label="返回首页顶部" @click="scrollTo('top')"><b aria-hidden="true"></b><span>Vequo 维阔</span><small>DATA OPERATIONS</small></button>
      <nav aria-label="首页导航"><button @click="scrollTo('story')">怎么答的</button><button @click="scrollTo('photos')">能做什么</button><button @click="scrollTo('about')">关于 Vequo</button></nav>
      <button class="nav-enter" @click="emit('enter')">进入工作台 <span>↗</span></button>
    </header>

    <main>
      <section id="top" class="ops-hero" data-reveal>
        <div class="hero-copy">
          <p class="section-code"><span>01</span> DATA, IN MOTION</p>
          <h1>把复杂数据，<br><em>重新组织成行动。</em></h1>
          <p class="hero-intro">从结构、语义到查询结果，让每一次分析都拥有清晰的上下文和可以追溯的路径。</p>
          <div class="hero-actions"><button class="primary-action" @click="scrollTo('story')">开始探索 <span>↓</span></button></div>
          <div class="hero-stats"><span><b>05</b> 数据视图</span><span><b>24/7</b> 持续观察</span><span><b>SQL</b> 可追溯答案</span></div>
        </div>

        <div class="hero-svg-wrap" aria-hidden="true">
          <svg class="hero-system" viewBox="0 0 640 640" role="img">
            <defs><filter id="ops-soft"><feGaussianBlur stdDeviation="9" /></filter><linearGradient id="ops-signal" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#1677ff"/><stop offset="1" stop-color="#ff765f"/></linearGradient></defs>
            <g class="svg-aura"><circle cx="320" cy="320" r="194" fill="#1677ff" opacity=".2" filter="url(#ops-soft)"/></g>
            <g class="svg-orbits" fill="none" stroke="currentColor"><ellipse cx="320" cy="320" rx="250" ry="134"/><ellipse cx="320" cy="320" rx="126" ry="258" transform="rotate(32 320 320)"/><circle cx="320" cy="320" r="196" stroke-dasharray="5 12"/></g>
            <g class="svg-panels">
              <rect x="203" y="216" width="234" height="208" rx="36" fill="#f7f8fa" stroke="#1d2129"/>
              <path d="M237 278h166M237 316h112M237 354h142" fill="none" stroke="#1d2129" stroke-width="7" stroke-linecap="round"/>
              <rect x="237" y="379" width="76" height="14" rx="7" fill="#1677ff"/><rect x="322" y="379" width="45" height="14" rx="7" fill="#ff765f"/>
            </g>
            <g class="svg-nodes">
              <circle cx="92" cy="289" r="25" fill="#1d2129"/><circle cx="532" cy="205" r="30" fill="#ff765f"/><circle cx="505" cy="476" r="22" fill="#1677ff" stroke="#1d2129"/><circle cx="183" cy="504" r="16" fill="#8ba8ff"/>
              <path d="M114 289C172 286 171 258 203 256M437 258c43-16 61-32 73-42M437 390c42 30 56 54 61 68M226 424c-10 35-23 57-33 68" fill="none" stroke="#1d2129" stroke-width="2" stroke-dasharray="4 8"/>
            </g>
            <g class="svg-signal"><path d="M65 414c77-85 113 54 180-18s100-30 151 9 92 28 180-62" fill="none" stroke="url(#ops-signal)" stroke-width="9" stroke-linecap="round"/></g>
            <g class="svg-labels" fill="#1d2129"><text x="63" y="251">SOURCE</text><text x="520" y="164">SCHEMA</text><text x="503" y="522">ANSWER</text></g>
          </svg>
          <div class="hero-svg-caption"><i></i><span>LIVE SEMANTIC FIELD</span><b>数据关系正在生成</b></div>
        </div>
      </section>

      <section id="story" ref="horizontalSection" class="horizontal-story">
        <div class="horizontal-sticky">
          <div class="horizontal-heading"><p class="section-code"><span>02</span> HOW IT ANSWERS</p><h2>你问一句话，<br>系统在背后做四件事。</h2><div class="story-progress"><i :style="{ transform: `scaleX(${horizontalProgress})` }"></i></div></div>
          <div ref="horizontalTrack" class="horizontal-track" :style="{ transform: `translate3d(${-trackOffset}px,0,0)` }">
            <article v-for="(slice, index) in storySlices" :key="slice.no" class="story-panel" :class="`story-${slice.tone}`">
              <div class="story-panel-top"><span>{{ slice.no }}</span><small>{{ slice.en }}</small></div>
              <div class="story-symbol" aria-hidden="true">
                <svg viewBox="0 0 220 220">
                  <g v-if="index === 0" class="symbol-source"><circle v-for="n in 6" :key="n" cx="110" cy="110" :r="18 + n * 14"/></g>
                  <g v-else-if="index === 1" class="symbol-schema"><rect v-for="n in 4" :key="n" :x="35 + n * 26" :y="42 + n * 24" width="92" height="70" rx="12"/></g>
                  <g v-else-if="index === 2" class="symbol-relation"><path d="M45 165 91 62l78 34-22 78-102-9Z"/><circle cx="45" cy="165" r="11"/><circle cx="91" cy="62" r="11"/><circle cx="169" cy="96" r="11"/><circle cx="147" cy="174" r="11"/></g>
                  <g v-else class="symbol-answer"><path d="M33 144c37-8 40-72 76-64s35 68 78 21"/><path d="m159 81 28 20-25 23"/></g>
                </svg>
              </div>
              <h3>{{ slice.title }}</h3><p>{{ slice.detail }}</p><small class="panel-meta">{{ slice.meta }}</small>
            </article>
          </div>
          <div class="horizontal-hint"><span>SCROLL TO EXPLORE</span><b>→</b></div>
        </div>
      </section>

      <section id="photos" class="photo-section" data-reveal>
        <div class="photo-heading"><div><p class="section-code"><span>03</span> CAPABILITIES / 能做什么</p><h2>五张牌，<br>五个能用的东西。</h2></div><p>点牌面翻动。展开当前这张，看它具体管哪一块。</p></div>
        <div class="photo-layout">
          <div class="poker-copy"><small>CURRENT FRAME</small><strong>{{ activePhoto.title }}</strong><p>{{ activePhoto.detail }}</p><div class="poker-controls"><button aria-label="上一张" @click="rotatePoker(-1)">←</button><span>{{ String(activeIndex + 1).padStart(2,'0') }} / {{ String(photoItems.length).padStart(2,'0') }}</span><button aria-label="下一张" @click="rotatePoker(1)">→</button></div></div>
          <div class="poker-stage" @pointerdown="startPokerDrag" @pointerup="endPokerDrag">
            <article v-for="(photo, index) in photoItems" :key="photo.id" class="poker-card" :class="[{ 'is-front': slots[index] === photoItems.length - 1, 'is-wrapping': wrappingId === photo.id }, `photo-${photo.tone}`]" :style="pokerStyle(slots[index])" :aria-label="`${photo.title}卡片`" @click="slots[index] === photoItems.length - 1 && rotatePoker(1)">
              <div class="card-head"><span>{{ photo.no }}</span><small>{{ photo.en }}</small></div>
              <svg class="card-art" viewBox="0 0 360 440" aria-hidden="true">
                <g v-if="photo.kind === 'lines'" class="art-lines"><path v-for="n in 14" :key="n" :d="`M ${-40 + n * 34} 440 L ${100 + n * 34} 0`"/></g>
                <g v-else-if="photo.kind === 'grid'" class="art-grid"><path v-for="n in 9" :key="`v${n}`" :d="`M ${n * 45} 0 V 440`"/><path v-for="n in 11" :key="`h${n}`" :d="`M 0 ${n * 44} H 360`"/></g>
                <g v-else-if="photo.kind === 'nodes'" class="art-nodes"><path d="M28 330 112 96l155 54 65 190-168 55Z"/><circle cx="28" cy="330" r="9"/><circle cx="112" cy="96" r="13"/><circle cx="267" cy="150" r="8"/><circle cx="332" cy="340" r="14"/><circle cx="164" cy="395" r="10"/></g>
                <g v-else-if="photo.kind === 'rings'" class="art-rings"><circle v-for="n in 12" :key="n" cx="180" cy="220" :r="n * 18"/></g>
                <g v-else class="art-bars"><rect v-for="(height,n) in [82,145,112,206,168,250]" :key="n" :x="24 + n * 54" :y="386 - height" width="30" :height="height" rx="15"/><path d="M23 128c49 58 80-22 127 25s76-54 184-61"/></g>
              </svg>
              <div class="card-title"><h3>{{ photo.title }}</h3><p>{{ photo.meta }}</p></div>
              <button v-if="slots[index] === photoItems.length - 1" class="card-expand" @click.stop="openPhoto(photo.id)">展开 <span>↗</span></button>
            </article>
          </div>
        </div>
      </section>

      <section id="about" class="about-section" data-reveal>
        <div class="about-mark" aria-hidden="true"><svg viewBox="104 143 308 227" role="presentation"><path fill="currentColor" d="M104 143h71l84 160-35 67L104 143Z" /><path fill="currentColor" d="M340 143h72L263 370h-78l155-227Z" /></svg></div><div><p class="section-code"><span>04</span> ABOUT VEQUO</p><h2>散在各处的定义，<br>收在一处。</h2></div><p>表结构、业务术语、指标算法、谁能看哪几列——这些平时分开维护的东西，Vequo 收在一处。车间的人问数、质检的人核数、管理员改口径，用的是同一套说法。</p>
      </section>

      <section class="landing-cta" data-reveal><small>READY / 05</small><h2>让你的数据<br>开始工作。</h2><button @click="emit('enter')">进入工作台 <span>↗</span></button></section>
    </main>
    <footer><span>VEQUO 维阔 © 2026</span><span>LIGHT OPERATIONS EDITION</span><button @click="scrollTo('top')">回到顶部 ↑</button></footer>

    <div v-if="expandedPhoto" class="photo-modal" role="dialog" aria-modal="true" :aria-label="`${expandedPhoto.title}详情`" @click.self="expandedPhotoId = null">
      <div class="photo-modal-card" :class="`photo-${expandedPhoto.tone}`"><button class="modal-close" aria-label="关闭" @click="expandedPhotoId = null">×</button><span>{{ expandedPhoto.no }} / {{ expandedPhoto.en }}</span><h2>{{ expandedPhoto.title }}</h2><p>{{ expandedPhoto.detail }}</p><div class="modal-route"><small>INPUT</small><i></i><small>CONTEXT</small><i></i><small>ACTION</small></div><button class="modal-enter" @click="emit('enter')">在工作台中查看 ↗</button></div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import OpsWavyField from './OpsWavyField.vue'

const emit = defineEmits<{ (event: 'enter'): void }>()
const root = ref<HTMLElement | null>(null)
const horizontalSection = ref<HTMLElement | null>(null)
const horizontalTrack = ref<HTMLElement | null>(null)
const horizontalProgress = ref(0)
const trackOffset = ref(0)
const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
let scrollFrame = 0
let observer: IntersectionObserver | null = null
let pokerTimer = 0
let sectionTimer = 0
let dragStartX = 0

const storySlices = [
  { no:'01', en:'UNDERSTAND', title:'先听懂问题', detail:'「产线」「良率」这些词，系统先拿去业务知识里对一遍。它得确认，你嘴里的这个词，对应库里哪张表、哪个字段。', meta:'术语 · 字段 · 口径', tone:'orange' },
  { no:'02', en:'JOIN', title:'自己接表', detail:'这些表之间一条外键都没建。关系是系统读字段名读出来的——认得出两边的 line_id 说的是同一个东西，才敢接。', meta:'同名字段 · 推导', tone:'blue' },
  { no:'03', en:'METRIC', title:'认准口径', detail:'「良率」在系统里是一条口径，好品数除以投料数。要改只改这一处，全厂跟着变。', meta:'口径 · 一处维护', tone:'green' },
  { no:'04', en:'ANSWER', title:'给出处', detail:'生成的 SQL 先在库里试跑，跑不通就不端上来。答案旁边永远挂着它用过的表和口径。', meta:'SQL · 试跑 · 来源', tone:'red' },
]
const photoItems = [
  { id:'ask', no:'01', en:'ASK', title:'一句话问数', detail:'用自然语言直接问数。系统生成 SQL、跑出结果，并把用到的表和口径一并附在后面。', meta:'ASK / 智能问析', tone:'orange', kind:'lines' },
  { id:'schema', no:'02', en:'SCHEMA', title:'表与字段', detail:'接进来的每张表、每个字段都摊开在这里：什么类型、什么含义、能不能关联，可查可改。', meta:'SCHEMA / 数据资源', tone:'blue', kind:'grid' },
  { id:'knowledge', no:'03', en:'KNOWLEDGE', title:'知识口径', detail:'业务术语、指标算法、分析规则集中在一处维护。改一个口径，全系统的问答跟着变。', meta:'KNOWLEDGE / 业务知识', tone:'green', kind:'nodes' },
  { id:'report', no:'04', en:'REPORT', title:'结果能带走', detail:'问答结果可以导出成报告，图表、数字连同它引用的口径一起带走。', meta:'REPORT / 报告导出', tone:'red', kind:'rings' },
  { id:'access', no:'05', en:'ACCESS', title:'权限分岗', detail:'车间、质检、设备各看各的。敏感字段自动打码，放到哪一级由管理员定。', meta:'ACCESS / 权限管理', tone:'lime', kind:'bars' },
]
const slots = ref(photoItems.map((_, index) => index))
const wrappingId = ref<string | null>(null)
const expandedPhotoId = ref<string | null>(null)
const activeIndex = computed(() => slots.value.findIndex(slot => slot === photoItems.length - 1))
const activePhoto = computed(() => photoItems[Math.max(0, activeIndex.value)])
const expandedPhoto = computed(() => photoItems.find(item => item.id === expandedPhotoId.value) || null)

function scrollTo(id: string) { document.getElementById(id)?.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block:'start' }) }
function pokerStyle(slot: number) { return { '--slot': slot, '--depth': photoItems.length - 1 - slot } as Record<string, number> }
function rotatePoker(direction: 1 | -1) {
  if (wrappingId.value) return
  const edgeSlot = direction > 0 ? photoItems.length - 1 : 0
  const edgeIndex = slots.value.findIndex(slot => slot === edgeSlot)
  wrappingId.value = photoItems[edgeIndex]?.id || null
  slots.value = slots.value.map(slot => direction > 0 ? (slot + 1) % photoItems.length : (slot - 1 + photoItems.length) % photoItems.length)
  window.clearTimeout(pokerTimer)
  pokerTimer = window.setTimeout(() => { wrappingId.value = null }, reduced ? 20 : 430)
}
function openPhoto(id: string) { expandedPhotoId.value = id }
function startPokerDrag(event: PointerEvent) { dragStartX = event.clientX }
function endPokerDrag(event: PointerEvent) { const delta = event.clientX - dragStartX; if (Math.abs(delta) > 45) rotatePoker(delta < 0 ? 1 : -1) }
function updateHorizontal() {
  scrollFrame = 0
  if (!horizontalSection.value || !horizontalTrack.value || window.innerWidth < 760) { horizontalProgress.value = 0; trackOffset.value = 0; return }
  const rect = horizontalSection.value.getBoundingClientRect(), distance = Math.max(1, rect.height - window.innerHeight)
  horizontalProgress.value = Math.max(0, Math.min(1, -rect.top / distance))
  trackOffset.value = horizontalProgress.value * Math.max(0, horizontalTrack.value.scrollWidth - window.innerWidth + window.innerWidth * .08)
}
function handleScroll() { if (!scrollFrame) scrollFrame = requestAnimationFrame(updateHorizontal) }
function handleResize() { updateHorizontal() }
function handleKey(event: KeyboardEvent) { if (event.key === 'Escape') expandedPhotoId.value = null; if (event.key === 'ArrowRight' && document.activeElement?.closest('.photo-section')) rotatePoker(1); if (event.key === 'ArrowLeft' && document.activeElement?.closest('.photo-section')) rotatePoker(-1) }

onMounted(() => {
  observer = new IntersectionObserver(entries => entries.forEach(entry => entry.target.classList.toggle('is-visible', entry.isIntersecting)), { threshold:.18 })
  root.value?.querySelectorAll('[data-reveal]').forEach(element => observer?.observe(element))
  window.addEventListener('scroll', handleScroll, { passive:true }); window.addEventListener('resize', handleResize); window.addEventListener('keydown', handleKey)
  updateHorizontal()
  const requestedSection = new URLSearchParams(window.location.search).get('section')
  if (requestedSection && ['story','photos','about'].includes(requestedSection)) {
    sectionTimer = window.setTimeout(() => {
      const target = document.getElementById(requestedSection)
      if (!target) return
      target.classList.add('is-visible')
      document.documentElement.style.scrollBehavior = 'auto'
      window.scrollTo(0, target.getBoundingClientRect().top + window.scrollY)
      requestAnimationFrame(() => {
        document.documentElement.style.removeProperty('scroll-behavior')
      })
    }, 650)
  }
})
onBeforeUnmount(() => { cancelAnimationFrame(scrollFrame); window.clearTimeout(pokerTimer); window.clearTimeout(sectionTimer); observer?.disconnect(); window.removeEventListener('scroll', handleScroll); window.removeEventListener('resize', handleResize); window.removeEventListener('keydown', handleKey) })
</script>

<style scoped>
.ops-landing{position:relative;isolation:isolate;min-height:100vh;overflow:clip;color:#1d2129;background:#f7f8fa;font-family:Arial,"Noto Sans CJK SC",sans-serif}.ops-landing:before{position:fixed;z-index:0;inset:0;content:'';pointer-events:none;background:radial-gradient(circle at 78% 18%,rgba(77, 158, 255,.3),transparent 28%),radial-gradient(circle at 12% 64%,rgba(255,118,95,.12),transparent 26%)}button{color:inherit}.ops-nav{position:absolute;z-index:20;top:0;right:0;left:0;display:grid;grid-template-columns:1fr auto 1fr;align-items:center;padding:25px clamp(22px,4.5vw,72px);border-bottom:1px solid rgba(29, 33, 41,.16)}.ops-brand{display:flex;align-items:center;justify-self:start;gap:10px;border:0;background:transparent}.ops-brand b{display:block;width:46px;height:46px;background:url('/brand/vequo-app-icon.svg') center/contain no-repeat}.ops-brand span{font:700 20px Rajdhani,sans-serif;letter-spacing:.18em}.ops-brand small{padding-left:10px;border-left:1px solid rgba(29, 33, 41,.24);font:600 8px Rajdhani,sans-serif;letter-spacing:.13em;opacity:.58}.ops-nav nav{display:flex;gap:32px}.ops-nav nav button,.nav-enter{min-height:44px;border:0;background:transparent;font-size:12px}.ops-nav nav button{position:relative}.ops-nav nav button:after{position:absolute;right:0;bottom:8px;left:0;height:1px;content:'';background:#1d2129;transform:scaleX(0);transition:transform .25s;transform-origin:right}.ops-nav nav button:hover:after{transform:scaleX(1);transform-origin:left}.nav-enter{justify-self:end;padding:0 18px;border:1px solid #1d2129!important;border-radius:24px!important}.nav-enter span{margin-left:18px}.nav-enter:hover{color:#f7f8fa;background:#1d2129}
main,footer{position:relative;z-index:2}.ops-hero{display:grid;grid-template-columns:minmax(0,1.03fr) minmax(420px,.97fr);align-items:center;min-height:100svh;padding:125px clamp(24px,6.2vw,96px) 64px}.section-code{display:flex;align-items:center;gap:12px;margin:0 0 25px;font:600 9px Rajdhani,sans-serif;letter-spacing:.18em;text-transform:uppercase}.section-code span{display:grid;width:25px;height:25px;place-items:center;border:1px solid currentColor;border-radius:50%;letter-spacing:0}.hero-copy h1,.photo-heading h2,.about-section h2,.landing-cta h2,.horizontal-heading h2{margin:0;font-weight:400;letter-spacing:-.065em}.hero-copy h1{max-width:790px;font-size:clamp(58px,7vw,112px);line-height:.91}.hero-copy h1 em{color:#1677ff;font-family:Georgia,"Songti SC",serif;font-weight:400}.hero-intro{max-width:570px;margin:34px 0 0;color:#4e5969;font-size:15px;line-height:1.85}.hero-actions{display:flex;align-items:center;gap:14px;margin-top:38px}.primary-action{min-height:52px;padding:0 23px;border:1px solid #4D9EFF;border-radius:28px;background:#4D9EFF;color:#ffffff;font-size:13px}.primary-action span{margin-left:35px}.primary-action:hover{color:#fff;background:#1d2129}.hero-stats{display:flex;gap:34px;margin-top:60px;padding-top:19px;border-top:1px solid rgba(29, 33, 41,.2);font-size:10px}.hero-stats span{display:grid;gap:4px;color:#4e5969}.hero-stats b{color:#1d2129;font:600 20px Rajdhani,sans-serif}.hero-svg-wrap{position:relative;display:grid;place-items:center;min-width:0}.hero-system{width:min(100%,640px);overflow:visible;color:#1d2129}.svg-orbits{transform-origin:center;animation:svg-orbit 24s linear infinite}.svg-orbits ellipse{opacity:.34}.svg-panels{transform-origin:center;animation:svg-panel-float 5.4s ease-in-out infinite}.svg-nodes circle{transform-box:fill-box;transform-origin:center;animation:svg-node-pulse 3.8s ease-in-out infinite}.svg-nodes circle:nth-of-type(2){animation-delay:-1.2s}.svg-nodes circle:nth-of-type(3){animation-delay:-2.1s}.svg-signal path{stroke-dasharray:520;stroke-dashoffset:520;animation:svg-signal-draw 4.2s ease-in-out infinite}.svg-labels{font:600 10px Rajdhani,sans-serif;letter-spacing:.16em}.hero-svg-caption{position:absolute;right:4%;bottom:10%;display:grid;grid-template-columns:auto 1fr;gap:3px 8px;padding:13px 16px;border:1px solid rgba(29, 33, 41,.24);border-radius:14px;background:rgba(247, 248, 250,.68);backdrop-filter:blur(12px)}.hero-svg-caption i{grid-row:1/3;width:7px;height:7px;margin-top:4px;border-radius:50%;background:#4D9EFF;box-shadow:0 0 0 5px rgba(77, 158, 255,.14)}.hero-svg-caption span{font:600 7px Rajdhani,sans-serif;letter-spacing:.15em}.hero-svg-caption b{font-size:11px;font-weight:500}
.horizontal-story{position:relative;height:400vh;background:#1d2129;color:#f7f8fa}.horizontal-sticky{position:sticky;top:0;height:100vh;overflow:hidden}.horizontal-heading{position:absolute;z-index:2;top:6vh;left:6vw}.horizontal-heading h2{font-size:clamp(35px,4vw,64px);line-height:1}.story-progress{width:min(300px,48vw);height:2px;margin-top:22px;background:rgba(255,255,255,.16)}.story-progress i{display:block;width:100%;height:100%;background:#4D9EFF;transform-origin:left}.horizontal-track{position:absolute;top:31vh;left:7vw;display:flex;gap:30px;width:max-content;will-change:transform}.story-panel{position:relative;width:min(68vw,880px);height:58vh;min-height:390px;padding:26px 31px;overflow:hidden;border-radius:26px;color:#1d2129}.story-orange{background:#ed975b}.story-blue{background:#a6bee4}.story-green{background:#a7c0e4}.story-red{background:#dd746e}.story-panel-top{display:flex;justify-content:space-between;font:600 10px Rajdhani,sans-serif;letter-spacing:.15em}.story-symbol{position:absolute;top:13%;right:5%;width:min(43%,280px)}.story-symbol svg{width:100%;overflow:visible}.story-symbol g{fill:none;stroke:#1d2129;stroke-width:2}.symbol-source circle{transform-origin:center;animation:symbol-ring 6s ease-in-out infinite}.symbol-schema rect{transform-origin:center;animation:symbol-cards 5s ease-in-out infinite}.symbol-relation circle{fill:#1d2129}.symbol-answer path{stroke-width:6;stroke-linecap:round;stroke-linejoin:round}.story-panel h3{position:absolute;bottom:22%;left:31px;margin:0;font-size:clamp(46px,6vw,86px);font-weight:400;letter-spacing:-.05em}.story-panel>p{position:absolute;right:31px;bottom:8%;max-width:340px;margin:0;font-size:13px;line-height:1.7}.panel-meta{position:absolute;bottom:8%;left:31px;font:600 8px Rajdhani,sans-serif;letter-spacing:.15em}.horizontal-hint{position:absolute;right:5vw;bottom:4vh;display:flex;gap:18px;align-items:center;font:600 8px Rajdhani,sans-serif;letter-spacing:.15em}.horizontal-hint b{font-size:22px;color:#1677ff}
.photo-section{min-height:110vh;padding:13vh clamp(24px,6vw,92px);background:#f7f8fa}.photo-heading{display:grid;grid-template-columns:1fr minmax(260px,380px);gap:40px;align-items:end}.photo-heading h2{font-size:clamp(50px,7vw,108px);line-height:.88}.photo-heading>p{margin:0 0 8px;color:#4e5969;font-size:14px;line-height:1.8}.photo-layout{display:grid;grid-template-columns:minmax(210px,.55fr) minmax(600px,1.45fr);gap:4vw;align-items:center;margin-top:11vh}.poker-copy small{font:600 8px Rajdhani,sans-serif;letter-spacing:.16em}.poker-copy strong{display:block;margin-top:16px;font:400 clamp(38px,4vw,62px) Georgia,"Songti SC",serif}.poker-copy p{max-width:330px;color:#4e5969;font-size:13px;line-height:1.8}.poker-controls{display:flex;align-items:center;gap:15px;margin-top:30px}.poker-controls button{width:44px;height:44px;border:1px solid #1d2129;border-radius:50%;background:transparent}.poker-controls button:hover{background:#2E7CF0;color:#ffffff}.poker-controls span{font:600 9px Rajdhani,sans-serif;letter-spacing:.15em}.poker-stage{position:relative;height:min(64vw,670px);min-height:560px;touch-action:pan-y;user-select:none}.poker-card{--slot:0;position:absolute;top:50%;left:7%;width:min(41vw,430px);aspect-ratio:.78;padding:22px;overflow:hidden;border:1px solid rgba(29, 33, 41,.8);border-radius:24px;box-shadow:0 18px 42px rgba(29, 33, 41,.12);transform-origin:bottom left;transform:translate3d(calc(var(--slot) * 16%),calc((var(--slot) - 2) * -3%),0) rotate(calc((var(--slot) - 2) * 3.4deg)) translateY(-52%);transition:transform .42s cubic-bezier(.22,.8,.2,1),filter .42s;z-index:calc(var(--slot) + 1);cursor:pointer}.poker-card:not(.is-front){filter:saturate(.78) brightness(.93)}.poker-card.is-front:hover{transform:translate3d(calc(var(--slot) * 16% + 8px),calc((var(--slot) - 2) * -3% - 8px),0) rotate(calc((var(--slot) - 2) * 3.4deg)) translateY(-52%)}.poker-card.is-wrapping{transition:none}.photo-orange{background:#ee9b5d}.photo-blue{background:#a7c0e4}.photo-green{background:#bcd3f5}.photo-red{background:#dd7770}.photo-lime{background:#4D9EFF}.card-head{position:relative;z-index:2;display:flex;justify-content:space-between;font:600 9px Rajdhani,sans-serif;letter-spacing:.13em}.card-art{position:absolute;inset:0;width:100%;height:100%}.card-art g{fill:none;stroke:rgba(29, 33, 41,.58);stroke-width:1.3}.art-grid{transform:rotate(8deg);transform-origin:center}.art-nodes path{stroke-width:3}.art-nodes circle{fill:#1d2129;stroke:none}.art-rings circle{stroke:rgba(255,255,255,.65)}.art-bars rect{fill:rgba(29, 33, 41,.76);stroke:none}.art-bars path{stroke-width:4}.card-title{position:absolute;z-index:2;right:22px;bottom:22px;left:22px}.card-title h3{margin:0;font-size:clamp(34px,4vw,58px);font-weight:400;letter-spacing:-.04em}.card-title p{margin:6px 0 0;font:600 8px Rajdhani,sans-serif;letter-spacing:.15em}.card-expand{position:absolute;z-index:4;right:20px;top:50%;min-height:44px;padding:0 17px;border:1px solid #1d2129;border-radius:24px;background:rgba(247, 248, 250,.78);font-size:11px;backdrop-filter:blur(8px)}.card-expand span{margin-left:18px}.card-expand:hover{background:#fff}
.about-section{display:grid;grid-template-columns:auto minmax(330px,1fr) minmax(280px,.72fr);gap:6vw;align-items:center;padding:15vh 7vw;color:#f7f8fa;background:#7884c8}.about-mark{display:grid;width:130px;height:130px;place-items:center;border:1px solid rgba(255,255,255,.72);border-radius:50%}.about-mark svg{display:block;width:62%;height:auto}.about-section h2{font-size:clamp(36px,4.2vw,70px);line-height:.92}.about-section>p{font-size:14px;line-height:1.9}.landing-cta{display:grid;min-height:86vh;place-items:center;align-content:center;text-align:center;background:#4D9EFF}.landing-cta small{font:600 9px Rajdhani,sans-serif;letter-spacing:.18em}.landing-cta h2{margin:25px 0 35px;font-size:clamp(72px,11vw,176px);line-height:.8}.landing-cta button{min-height:58px;padding:0 26px;border:1px solid #1d2129;border-radius:30px;background:transparent}.landing-cta button span{margin-left:50px}.landing-cta button:hover{color:#f7f8fa;background:#1d2129}footer{display:flex;justify-content:space-between;padding:30px 5vw;border-top:1px solid rgba(29, 33, 41,.2);background:#4D9EFF;font:600 9px Rajdhani,sans-serif;letter-spacing:.14em}footer button{border:0;background:transparent;font:inherit}
[data-reveal]{opacity:0;transform:translateY(35px);transition:opacity .8s ease,transform .8s cubic-bezier(.2,.75,.2,1)}[data-reveal].is-visible{opacity:1;transform:none}.ops-hero[data-reveal]{opacity:1;transform:none}.photo-modal{position:fixed;z-index:1000;inset:0;display:grid;padding:5vw;place-items:center;background:rgba(29, 33, 41,.34);backdrop-filter:blur(13px);animation:modal-in .25s ease}.photo-modal-card{position:relative;width:min(760px,90vw);padding:clamp(30px,6vw,78px);border:1px solid #1d2129;border-radius:30px;box-shadow:0 30px 100px rgba(29, 33, 41,.24)}.photo-modal-card>span{font:600 9px Rajdhani,sans-serif;letter-spacing:.15em}.photo-modal-card h2{margin:45px 0 20px;font:400 clamp(60px,9vw,120px)/.85 Georgia,"Songti SC",serif}.photo-modal-card>p{max-width:560px;font-size:15px;line-height:1.9}.modal-close{position:absolute;top:22px;right:22px;width:46px;height:46px;border:1px solid #1d2129;border-radius:50%;background:transparent;font-size:25px}.modal-route{display:flex;align-items:center;gap:12px;margin-top:55px}.modal-route small{font:600 8px Rajdhani,sans-serif;letter-spacing:.13em}.modal-route i{width:60px;height:1px;background:#1d2129}.modal-enter{min-height:48px;margin-top:26px;padding:0 20px;border:1px solid #1d2129;border-radius:24px;background:rgba(255,255,255,.55)}
@keyframes svg-orbit{to{transform:rotate(360deg)}}@keyframes svg-panel-float{50%{transform:translateY(-12px) rotate(.7deg)}}@keyframes svg-node-pulse{50%{transform:scale(1.2)}}@keyframes svg-signal-draw{0%{stroke-dashoffset:520}45%,70%{stroke-dashoffset:0}100%{stroke-dashoffset:-520}}@keyframes symbol-ring{50%{transform:scale(.88)}}@keyframes symbol-cards{50%{transform:translate(8px,-6px)}}@keyframes modal-in{from{opacity:0}.photo-modal-card{transform:translateY(20px)}to{opacity:1}.photo-modal-card{transform:none}}
@media(max-width:980px){.ops-nav{grid-template-columns:1fr auto}.ops-nav nav{display:none}.ops-hero{grid-template-columns:1fr;padding-top:140px}.hero-svg-wrap{margin-top:40px}.photo-layout{grid-template-columns:1fr}.poker-copy{display:grid;grid-template-columns:1fr 1fr;gap:0 25px}.poker-copy p{grid-column:2;grid-row:1/4}.poker-controls{grid-column:1}.poker-stage{height:650px}.poker-card{left:12%;width:min(58vw,430px)}.about-section{grid-template-columns:auto 1fr}.about-section>p{grid-column:2}}
@media(max-width:759px){.ops-nav{padding:18px}.ops-brand small{display:none}.nav-enter{padding:0 13px}.nav-enter span{margin-left:8px}.ops-hero{display:block;padding:130px 20px 70px}.hero-copy h1{font-size:clamp(55px,18vw,78px)}.hero-stats{gap:18px}.hero-svg-wrap{margin-top:30px}.horizontal-story{height:auto;padding:90px 0;color:#f7f8fa;background:#1d2129}.horizontal-sticky{position:relative;height:auto;overflow:auto}.horizontal-heading{position:relative;top:auto;left:auto;padding:0 20px}.horizontal-heading h2{font-size:40px}.horizontal-track{position:relative;top:auto;left:auto;width:auto;margin-top:50px;padding:0 20px;overflow-x:auto;scroll-snap-type:x mandatory;transform:none!important}.story-panel{width:82vw;min-width:82vw;height:520px;scroll-snap-align:center}.story-panel h3{font-size:52px}.horizontal-hint{display:none}.photo-section{padding:100px 20px}.photo-heading{grid-template-columns:1fr}.photo-heading h2{font-size:60px}.photo-layout{margin-top:50px}.poker-copy{display:block}.poker-stage{height:510px;min-height:510px}.poker-card{left:0;width:min(72vw,350px);padding:17px;border-radius:19px;transform:translate3d(calc(var(--slot) * 8%),calc((var(--slot) - 2) * -2%),0) rotate(calc((var(--slot) - 2) * 2.6deg)) translateY(-52%)}.card-expand{right:14px;top:43%;padding:0 12px}.about-section{grid-template-columns:1fr;padding:100px 20px}.about-mark{width:82px;height:82px}.about-section>p{grid-column:auto}.landing-cta{min-height:75vh;padding:70px 20px}.landing-cta h2{font-size:76px}footer{gap:12px;flex-wrap:wrap}footer span:nth-child(2){display:none}.photo-modal{padding:14px}.photo-modal-card{padding:55px 25px 30px;border-radius:22px}.photo-modal-card h2{font-size:65px}.modal-route i{width:24px}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.001ms!important;animation-iteration-count:1!important;scroll-behavior:auto!important}.poker-card,[data-reveal]{transition-duration:.01ms}}
</style>

<style scoped>
.photo-modal-card{animation:modal-card-in .32s cubic-bezier(.2,.75,.2,1)}
@keyframes modal-in{from{opacity:0}to{opacity:1}}
@keyframes modal-card-in{from{opacity:0;transform:translateY(22px) scale(.98)}to{opacity:1;transform:none}}
</style>
