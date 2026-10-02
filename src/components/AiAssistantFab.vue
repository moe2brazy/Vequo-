<template>
  <!-- Teleport 到 body 顶层渲染：悬浮球必须固定在整个视口右下角，不能受页面容器层
       （workspace-shell 的进入动画 / 内部滚动容器等）影响——否则 position:fixed 会被
       祖先的 transform/containing-block 劫持，页面滚动时球跟着滑。 -->
  <Teleport to="body">
  <div class="ai-fab-root">
    <!-- ====== 聊天面板（点开悬浮球出现） ====== -->
    <Transition name="ai-fab-pop">
      <div v-if="open" class="ai-fab-panel" :class="{ expanded }" :style="panelStyle">
        <!-- ---------- 头部（随视图切换） ---------- -->
        <header class="ai-panel-head">
          <div class="ai-panel-title">
            <button v-if="view === 'human'" class="ai-back" title="返回 AI 客服" @click="view = 'ai'">
              <AppIcon name="chevron-left" :size="14" />
            </button>
            <span class="ai-panel-avatar" :class="{ human: view === 'human' }">
              <AppIcon :name="view === 'human' ? 'message-circle' : 'bot'" :size="16" />
            </span>
            <div class="ai-panel-name">
              <b>{{ view === 'human' ? '人工客服' : 'Vequo 智能客服' }}</b>
              <small>
                <i class="ai-online-dot"></i>
                <span v-if="view === 'human'">
                  {{ adminView ? `管理员视角 · ${convos.length} 个会话` : '留言后管理员会尽快回复' }}
                </span>
                <span v-else>在线 · 会话内连续对话</span>
              </small>
            </div>
          </div>
          <div class="ai-panel-actions">
            <!-- AI 视图：人工客服入口（小星标左侧），有客服新回复时显示红点 + 未读数量 -->
            <button
              v-if="view === 'ai'"
              class="ai-act-human"
              :class="{ has: supportUnread > 0 }"
              :title="supportUnread > 0 ? `人工客服（${supportUnread} 条未读）` : '人工客服会话'"
              @click="openHuman()"
            >
              <AppIcon name="message-circle" :size="15" />
              <span v-if="supportUnread > 0" class="ai-act-dot">
                {{ supportUnread > 99 ? '99+' : supportUnread }}
              </span>
            </button>
            <button
              v-if="view === 'ai'"
              title="前往智能问析（接入完整 AI，支持多轮分析）"
              @click="goAsk"
            >
              <AppIcon name="sparkles" :size="15" />
            </button>
            <!-- 浮窗放大 / 还原（放大到 680×720，仍只占屏幕一角，不铺满页面） -->
            <button :title="expanded ? '还原窗口' : '放大窗口'" @click="toggleExpand">
              <AppIcon :name="expanded ? 'minimize-2' : 'maximize-2'" :size="15" />
            </button>
            <button title="收起（清空本次 AI 对话）" @click="open = false">
              <AppIcon name="x" :size="15" />
            </button>
          </div>
        </header>

        <!-- ---------- AI 客服视图 ---------- -->
        <template v-if="view === 'ai'">
          <div ref="msgList" class="ai-panel-msgs">
            <!-- 欢迎语 + 常见问答 -->
            <div v-if="!chat.length" class="ai-welcome">
              <div class="ai-welcome-hi">
                <AppIcon name="bot" :size="14" />
                <span>你好，我是 Vequo 智能客服</span>
              </div>
              <p class="ai-welcome-tip">业务知识、平台使用问题都可以直接问我，本窗口内支持连续追问；涉及权限审批、账号异常等需要管理员处理的问题，我可以帮你转接人工客服。</p>
              <div class="ai-faq">
                <div class="ai-faq-tabs">
                  <button
                    v-for="g in FAQ_GROUPS"
                    :key="g.key"
                    :class="{ on: faqTab === g.key }"
                    @click="faqTab = g.key"
                  >{{ g.label }}</button>
                </div>
                <button v-for="(f, i) in currentFaq" :key="i" class="ai-faq-item" @click="askFaq(f)">
                  <AppIcon name="message-circle" :size="12" />
                  <span>{{ f.q }}</span>
                </button>
              </div>
            </div>

            <!-- 对话气泡 -->
            <div v-for="(m, i) in chat" :key="i" class="ai-row" :class="{ mine: m.role === 'user' }">
              <div class="ai-bubble">
                <div class="ai-bubble-text">{{ m.text }}</div>
                <div v-if="m.tag" class="ai-bubble-tag">
                  <AppIcon name="sparkles" :size="10" />
                  <span>{{ m.tag }}</span>
                </div>
                <!-- AI 判断需人工处理：气泡下方直接给出转人工按钮 -->
                <button v-if="m.needHuman" class="ai-transfer-btn" @click="transferToHuman(m)">
                  <AppIcon name="message-circle" :size="11" />
                  <span>转接人工客服</span>
                </button>
              </div>
            </div>

            <!-- 生成中 -->
            <div v-if="loading" class="ai-row">
              <div class="ai-bubble ai-bubble-loading">
                <AppIcon name="loader-2" :size="13" class="ai-spin" />
                <span>{{ loadingStep || '正在思考…' }}</span>
              </div>
            </div>
          </div>

          <div class="ai-panel-input">
            <input
              v-model="draft"
              type="text"
              placeholder="提问业务知识或使用问题…"
              autocomplete="off"
              :disabled="loading"
              @keydown.enter.prevent="send"
            />
            <button class="ai-send" :disabled="!draft.trim() || loading" title="发送" @click="send">
              <AppIcon name="send" :size="14" />
            </button>
          </div>

          <footer class="ai-panel-foot">
            <button @click="goAskWithLast">
              <AppIcon name="sparkles" :size="12" />
              <span>{{ lastQuestion ? '携带该问题前往智能问析（多轮分析）' : '前往智能问析使用接入的 AI' }}</span>
            </button>
          </footer>
        </template>

        <!-- ---------- 人工客服视图（服务端工单，用户/管理员互通） ---------- -->
        <div v-else class="ai-human" :class="{ row: expanded }">
          <!-- 会话列表：管理员看全部用户，普通用户只看自己的 -->
          <div class="ai-human-list">
            <div class="ai-human-list-head">
              <span>{{ adminView ? '用户会话' : '我的咨询' }}</span>
              <button title="刷新" @click="loadConvos"><AppIcon name="refresh-cw" :size="11" /></button>
            </div>
            <div class="ai-human-list-body">
              <div v-if="!convos.length" class="ai-human-empty">
                {{ adminView ? '暂无用户咨询' : '还没有咨询，发送第一条消息即可创建' }}
              </div>
              <button
                v-for="c in convos"
                :key="c.id"
                class="ai-human-item"
                :class="{ on: activeId === c.id }"
                @click="openConvo(c.id)"
              >
                <div class="ai-human-item-top">
                  <span class="truncate">{{ adminView ? (c.user_name || c.user_key) : '人工客服' }}</span>
                  <em v-if="(c.unread || 0) > 0">{{ (c.unread || 0) > 99 ? '99+' : c.unread }}</em>
                </div>
                <div class="ai-human-item-sub truncate">{{ c.last_text || '（暂无消息）' }}</div>
                <!-- 管理员视角：该会话还有未处理的业务知识反馈 -->
                <div v-if="adminView && (c.pending_feedback || 0) > 0" class="ai-human-item-fb">
                  <AppIcon name="alert-triangle" :size="9" />
                  {{ c.pending_feedback }} 条知识反馈待处理
                </div>
              </button>
            </div>
          </div>

          <!-- 消息区 -->
          <div class="ai-human-chat">
            <div v-if="humanError" class="ai-human-err">{{ humanError }}</div>
            <div ref="humanBox" class="ai-human-msgs">
              <div v-if="!activeId" class="ai-human-empty">
                {{ adminView ? '左侧选择用户会话开始回复' : '还没有咨询，直接发送消息即可开始' }}
              </div>
              <template v-else>
                <div v-if="!activeConvo?.messages?.length" class="ai-human-empty">还没有消息，发送第一条内容开始对话。</div>
                <template v-for="m in activeConvo?.messages || []" :key="m.id">
                  <!-- 业务知识「有误」反馈：大号显著待办卡（比普通气泡大），点击打开屏幕居中详情浮窗 -->
                  <div
                    v-if="m.kind === 'feedback'"
                    class="ai-fb-card"
                    :class="{ done: fbStatus(m) === 'done', rejected: fbStatus(m) === 'rejected' }"
                    :title="adminView ? '点击查看详情并处理' : '点击查看处理进度'"
                    @click="openFeedbackDetail(m)"
                  >
                    <div class="ai-fb-card-top">
                      <span class="ai-fb-tag"><AppIcon name="alert-triangle" :size="11" /> 业务知识反馈</span>
                      <span class="ai-fb-state" :class="fbStatus(m)">{{ fbLabel(m) }}</span>
                    </div>
                    <div class="ai-fb-item">
                      <em v-if="m.meta?.item_kind">{{ m.meta.item_kind }}</em>
                      <b>{{ m.meta?.item_title || '（未知知识条目）' }}</b>
                      <small v-if="m.meta?.item_scene">{{ m.meta.item_scene }}</small>
                    </div>
                    <div class="ai-fb-note">{{ m.text }}</div>
                    <div class="ai-fb-card-foot">
                      <span>{{ m.sender_name }} · {{ fmtTime(m.ts) }}</span>
                      <span class="ai-fb-more">查看详情 <AppIcon name="arrow-right" :size="10" /></span>
                    </div>
                  </div>
                  <div v-else class="ai-hrow" :class="{ mine: isMine(m) }">
                    <div class="ai-hbubble">
                      <div class="ai-hbubble-meta">{{ m.sender_name }} · {{ fmtTime(m.ts) }}</div>
                      <div class="ai-hbubble-text">{{ m.text }}</div>
                    </div>
                  </div>
                </template>
              </template>
            </div>
            <div class="ai-panel-input">
              <input
                v-model="humanDraft"
                type="text"
                :disabled="humanSending || activeConvo?.status === 'closed'"
                :placeholder="activeConvo?.status === 'closed'
                  ? '会话已结束'
                  : (adminView ? '以人工客服身份回复…' : '描述你的问题，管理员会尽快回复…')"
                autocomplete="off"
                @keydown.enter.prevent="sendHuman"
              />
              <button
                class="ai-send"
                :disabled="!humanDraft.trim() || humanSending || activeConvo?.status === 'closed'"
                title="发送"
                @click="sendHuman"
              >
                <AppIcon name="send" :size="14" />
              </button>
            </div>
            <div v-if="adminView && activeId && activeConvo?.status === 'open'" class="ai-human-foot">
              <button @click="closeConvo">结束会话</button>
            </div>
          </div>
        </div>
      </div>
    </Transition>

    <!-- ====== 可拖动悬浮球 ====== -->
    <button
      class="ai-fab-ball"
      :class="{ open, dragging: isDragging }"
      :style="ballStyle"
      :title="open ? '收起智能客服' : '智能客服（可拖动）'"
      @pointerdown="onPointerDown"
    >
      <AppIcon :name="open ? 'x' : 'bot'" :size="24" />
      <span v-if="!open && supportUnread > 0" class="ai-fab-badge">
        {{ supportUnread > 99 ? '99+' : supportUnread }}
      </span>
      <span v-else-if="!open" class="ai-fab-dot"></span>
    </button>
  </div>

  <!-- ====== 知识反馈详情浮窗（屏幕居中，管理员在此处理） ====== -->
  <div v-if="fbDetail" class="ai-fb-mask" @click.self="fbDetail = null">
    <div class="ai-fb-modal" role="dialog" aria-label="业务知识反馈详情">
      <header class="ai-fb-modal-head">
        <span><AppIcon name="alert-triangle" :size="15" /> 业务知识反馈</span>
        <button title="关闭" @click="fbDetail = null"><AppIcon name="x" :size="14" /></button>
      </header>

      <div class="ai-fb-modal-body">
        <div class="ai-fb-mrow">
          <label>处理状态</label>
          <span class="ai-fb-state" :class="fbStatus(fbDetail)">{{ fbLabel(fbDetail) }}</span>
        </div>
        <div class="ai-fb-mrow">
          <label>知识条目</label>
          <b>{{ fbDetail.meta?.item_title || '（未知知识条目）' }}</b>
        </div>
        <div class="ai-fb-mrow">
          <label>知识类型</label>
          <span>{{ fbDetail.meta?.item_kind || '—' }}</span>
        </div>
        <div class="ai-fb-mrow">
          <label>业务场景</label>
          <span>{{ fbDetail.meta?.item_scene || '—' }}</span>
        </div>
        <div v-if="fbDetail.meta?.item_table" class="ai-fb-mrow">
          <label>数据表</label>
          <code>{{ fbDetail.meta.item_table }}</code>
        </div>
        <div v-if="fbDetail.meta?.item_key" class="ai-fb-mrow">
          <label>知识标识</label>
          <code class="ai-fb-key">{{ fbDetail.meta.item_key }}</code>
        </div>
        <div v-if="fbDetail.meta?.item_desc" class="ai-fb-mcol">
          <label>条目原描述</label>
          <p>{{ fbDetail.meta.item_desc }}</p>
        </div>
        <div class="ai-fb-mcol">
          <label>用户反馈内容</label>
          <p class="ai-fb-hl">{{ fbDetail.text }}</p>
        </div>

        <!-- 管理员：处理备注（点「知识无误」时会作为客服回复发给用户） -->
        <div v-if="adminView" class="ai-fb-mcol">
          <label>处理备注{{ fbStatus(fbDetail) === 'rejected' ? ' / 已回复用户' : '' }}</label>
          <textarea
            v-model="fbDraft"
            rows="3"
            maxlength="500"
            placeholder="已修正：例如 已按工单去重修正口径公式并重新发布；&#10;知识无误：例如 经核实口径无误，你看到的是旧版本"
          ></textarea>
          <p v-if="fbStatus(fbDetail) === 'open'" class="ai-fb-hint">
            <AppIcon name="info" :size="11" />
            点「知识无误 · 回复用户」时，上面这段文字会作为一条「人工客服」消息发给用户（留空则用默认话术）。
          </p>
        </div>
        <!-- 用户：查看管理员的说明（反馈不成立时这段也已作为客服消息发给你） -->
        <div v-else-if="fbDetail.meta?.admin_note" class="ai-fb-mcol">
          <label>{{ fbStatus(fbDetail) === 'rejected' ? '管理员说明（已回复你）' : '管理员处理说明' }}</label>
          <p>{{ fbDetail.meta.admin_note }}</p>
        </div>

        <p class="ai-fb-meta-line">
          <span>{{ fbDetail.sender_name }} · {{ fmtTime(fbDetail.ts) }}</span>
          <span v-if="fbDetail.meta?.handled_by">
            由 {{ fbDetail.meta.handled_by }} 处理 · {{ fmtTime(fbDetail.meta.handled_at || 0) }}
          </span>
        </p>
        <p v-if="fbError" class="ai-fb-err">{{ fbError }}</p>
      </div>

      <footer class="ai-fb-modal-foot">
        <template v-if="adminView">
          <button class="ai-fb-ghost" @click="fbDetail = null">关闭</button>
          <template v-if="fbStatus(fbDetail) === 'open'">
            <!-- 用户反馈可能是误报：这个出口把「知识无误」的结论直接回复给用户 -->
            <button
              class="ai-fb-warn"
              :disabled="fbBusy"
              title="经核实该知识无误：驳回反馈，并把备注作为客服回复发给用户"
              @click="rejectFeedback"
            >
              <AppIcon name="x" :size="11" /> 知识无误 · 回复用户
            </button>
            <button class="ai-fb-primary" :disabled="fbBusy" @click="markFeedback('done')">
              {{ fbBusy ? '处理中…' : '标记已处理' }}
            </button>
          </template>
          <button v-else class="ai-fb-ghost" :disabled="fbBusy" @click="markFeedback('open')">
            退回未处理
          </button>
        </template>
        <button v-else class="ai-fb-primary" @click="fbDetail = null">知道了</button>
      </footer>
    </div>
  </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, inject, onMounted, onUnmounted, nextTick, watch } from 'vue'
import AppIcon from './AppIcon.vue'

/**
 * AI 客服悬浮球（工作台内全局可用）—— 两个视图 + 可放大
 *  1) AI 客服视图：常见问答本地秒回；其余走 /api/support/ai-chat，携带窗口内多轮 history
 *     （有记忆、可连续追问），不落库；退出浮窗即清空历史（防缓存/上下文膨胀）
 *  2) 人工客服视图：服务端工单，用户与管理员真正互通；AI 自评 need_human 时气泡内出现
 *     「转接人工客服」，携带问题摘要建单后自动切到本视图
 *  - 未读：悬浮球角标 + 头部入口红点（全局轮询，引用计数）
 *  - 放大：面板 344×462 → 680×720（只占屏幕一角，不铺满整页），放大态人工视图左右分栏
 */

const emit = defineEmits<{
  (e: 'navigate-ask', question?: string): void
}>()

// ====== 常见问答库（共享模块 assistantFaq.ts，与消息中心 AI 管理员同步） ======
import { FAQ_GROUPS, FAQS, matchFaq, type FaqItem } from '../assistantFaq'
import {
  askSupportAi, supportUnread, startSupportPolling,
  listConversations, createConversation, fetchConversation, sendMessage,
  closeConversation, markRead, fetchUnread, updateFeedbackStatus,
  type SupportConversation, type SupportConvoSummary, type SupportMessage,
  type FeedbackStatus,
} from '../supportService'
import { getUser } from '../auth'

const faqTab = ref<string>('usage')
const currentFaq = computed(() => FAQS[faqTab.value] || [])

// ====== 视图与窗口 ======
type View = 'ai' | 'human'
const view = ref<View>('ai')
const expanded = ref(false)

function toggleExpand(): void {
  expanded.value = !expanded.value
}

// ====== AI 对话状态（仅组件内存，关闭面板即清空：会话内记忆、不跨窗口累积） ======
interface FabMessage {
  role: 'user' | 'assistant'
  text: string
  tag?: string
  /** AI 判断该问题需要人工介入（展示转人工按钮） */
  needHuman?: boolean
}
const chat = ref<FabMessage[]>([])
const draft = ref('')
const loading = ref(false)
const loadingStep = ref('')
const open = ref(false)
const msgList = ref<HTMLElement | null>(null)
let abortCtrl: AbortController | null = null
// 修复（P0）：拖动收起浮窗与「用户点 × 主动关闭」都会把 open 置 false，
// 必须用标记区分，否则拖动球也会走下方 else 分支 → 静默清空整段对话并中断流式请求。
let suppressClearOnClose = false
// 常见问答的"模拟思考延迟"定时器：需在关闭/卸载时清理，避免向已清空的会话推送"幽灵答案"
let faqTimer: number | null = null

// 记忆生命周期 = 面板会话：打开时全新对话，退出时立即清空历史。
// 2026-10-01 修复：拖动收起（suppressClearOnClose）保留的对话，重开时被无条件清空，
// 在途回答随后凭空出现（幽灵消息）。改为：重开时若上一次是"保留式收起"则继续原会话。
let retainOnReopen = false
watch(open, (v) => {
  if (v) {
    if (!retainOnReopen) {
      chat.value = []
      draft.value = ''
      loading.value = false
      loadingStep.value = ''
      abortCtrl?.abort()
      abortCtrl = null
    }
    retainOnReopen = false
    nextTick(scrollBottom)
  } else {
    // 修复（P0）：因拖动而收起时，只收起悬浮面板，保留对话与在途请求。
    // 原先拖动会命中下面这段"退出即清空"逻辑 → 随手拖一下球就丢失整段多轮对话（不可恢复）。
    if (suppressClearOnClose) {
      suppressClearOnClose = false
      retainOnReopen = true
      return
    }
    retainOnReopen = false
    // 退出浮窗：终止在途请求 + 清空问答历史（不写入任何持久化存储）
    abortCtrl?.abort()
    abortCtrl = null
    chat.value = []
    draft.value = ''
    loading.value = false
    loadingStep.value = ''
    view.value = 'ai'
    expanded.value = false
    stopHumanTimer()
    if (faqTimer !== null) { window.clearTimeout(faqTimer); faqTimer = null }
  }
})

const lastQuestion = computed(() => [...chat.value].reverse().find((m) => m.role === 'user')?.text || '')

function askFaq(f: FaqItem): void {
  if (loading.value) return
  // 点击预设问句：用户气泡显示预设问句本身
  answerWithFaq(f.q, f)
}

/**
 * 以预设答案回复 —— 用户气泡 + 预设回答都在这里产生（唯一推送点）。
 *  - 点「常见问答」条目：userText = 预设问句
 *  - 手输内容命中相似问题：userText = 用户输入原文（绝不改写成预设问句，避免同一问题出现两次）
 *  - 相似度不足时不会走到这里，由 send() 转交客服 AI 接口
 */
function answerWithFaq(userText: string, f: FaqItem): void {
  chat.value.push({ role: 'user', text: userText })
  scrollBottom()
  // 本地秒回（带"常见问答"标识），模拟一点思考延迟；
  // 手输内容与预设问句不同时，标注出实际匹配到的预设问题，便于用户理解
  const tag = userText.trim() === f.q ? '常见问答' : `常见问答 · 匹配「${f.q}」`
  if (faqTimer !== null) window.clearTimeout(faqTimer)
  faqTimer = window.setTimeout(() => {
    faqTimer = null
    chat.value.push({ role: 'assistant', text: f.a, tag })
    scrollBottom()
  }, 260)
}

/** 客服 AI（带本窗口多轮记忆）：把已有对话 history 一并传给 /api/support/ai-chat */
async function askAgent(query: string): Promise<void> {
  loading.value = true
  loadingStep.value = '正在思考…'
  const controller = new AbortController()
  abortCtrl = controller
  try {
    // 记忆窗口：只携带最近 12 条往返（约 6 轮），避免上下文无上限膨胀
    const history = chat.value
      .filter((m) => m.text && !m.needHuman)
      .slice(-12)
      .map((m) => ({ role: m.role, content: m.text }))
    if (!history.some((h) => h.content === query)) {
      history.push({ role: 'user', content: query })
    }
    const { answer, need_human } = await askSupportAi(history, controller.signal)
    if (controller.signal.aborted) return
    chat.value.push({ role: 'assistant', text: answer, needHuman: need_human })
  } catch (e: any) {
    if (e?.name === 'AbortError') {
      chat.value.push({ role: 'assistant', text: '已停止生成。' })
    } else {
      chat.value.push({
        role: 'assistant',
        text: `请求失败：${e?.message || '未知错误'}\n\n请确认后端服务已启动；也可以直接联系人工客服。`,
        needHuman: true,
      })
    }
  } finally {
    loading.value = false
    loadingStep.value = ''
    abortCtrl = null
    scrollBottom()
  }
}

/**
 * 发送用户输入：
 *  - 与预设问答相似（关键词/问句打分命中）→ 复用预设回答，用户气泡 = 用户输入原文
 *  - 其余问题 → 走接入的客服 AI 接口（/api/support/ai-chat，带窗口内记忆）
 */
function send(): void {
  const q = draft.value.trim()
  if (!q || loading.value) return
  draft.value = ''
  const hit = matchFaq(q)
  if (hit) {
    answerWithFaq(q, hit)   // 内部推送用户气泡 + 预设回答（不重复推送）
  } else {
    chat.value.push({ role: 'user', text: q })
    scrollBottom()
    void askAgent(q)
  }
}

function scrollBottom(): void {
  nextTick(() => {
    if (msgList.value) msgList.value.scrollTop = msgList.value.scrollHeight
  })
}

// ====== 人工客服视图 ======
const authUser = inject<any>('authUser', ref(null))
const adminView = computed(() => (authUser?.value?.role || getUser()?.role) === 'admin')

const convos = ref<SupportConvoSummary[]>([])
const activeId = ref('')
const activeConvo = ref<SupportConversation | null>(null)
const humanDraft = ref('')
const humanSending = ref(false)
const humanError = ref('')
const humanBox = ref<HTMLElement | null>(null)

function fmtTime(ts: number): string {
  if (!ts) return ''
  const d = new Date(ts)
  const pad = (n: number) => String(n).padStart(2, '0')
  const today = new Date(); today.setHours(0, 0, 0, 0)
  if (d.getTime() >= today.getTime()) return `${pad(d.getHours())}:${pad(d.getMinutes())}`
  return `${d.getMonth() + 1}/${d.getDate()}`
}

/** 管理员视角下 agent=我，用户视角下 user=我 */
function isMine(m: SupportMessage): boolean {
  return adminView.value ? m.sender === 'agent' : m.sender === 'user'
}

function humanScroll(): void {
  nextTick(() => {
    if (humanBox.value) humanBox.value.scrollTop = humanBox.value.scrollHeight
  })
}

async function loadConvos(): Promise<void> {
  try {
    const d = await listConversations()
    convos.value = d.conversations || []
  } catch (e: any) {
    humanError.value = `会话列表加载失败：${e?.message || e}`
  }
}

async function openConvo(id: string): Promise<void> {
  activeId.value = id
  try {
    activeConvo.value = await fetchConversation(id)  // 打开即已读（服务端标记）
    humanScroll()
    await syncUnread()
  } catch (e: any) {
    humanError.value = `消息加载失败：${e?.message || e}`
  }
}

async function sendHuman(): Promise<void> {
  const text = humanDraft.value.trim()
  if (!text || humanSending.value) return
  humanSending.value = true
  humanDraft.value = ''
  try {
    // 用户首条消息即自动建单（已移除「新建咨询」按钮：无会话时直接发消息即可）
    if (!activeId.value) {
      const c = await createConversation(text)
      await loadConvos()
      activeId.value = c.id
      activeConvo.value = c
    } else {
      await sendMessage(activeId.value, text)
      activeConvo.value = await fetchConversation(activeId.value)
    }
    await loadConvos()
    await syncUnread()
    humanScroll()
    humanError.value = ''
  } catch (e: any) {
    humanError.value = `发送失败：${e?.message || e}`
    humanDraft.value = text
  } finally {
    humanSending.value = false
  }
}

async function closeConvo(): Promise<void> {
  if (!activeId.value) return
  try {
    await closeConversation(activeId.value)
    activeConvo.value = await fetchConversation(activeId.value)
    await loadConvos()
  } catch (e: any) {
    humanError.value = `操作失败：${e?.message || e}`
  }
}

// ====== 业务知识反馈：会话流中的大号待办卡 + 屏幕居中的详情浮窗 ======
// 反馈由业务知识页「👎 知识有误」提交，后端追加进本用户的人工客服会话（不另开聊天框）。
// 这里负责展示与（管理员）处理：标记已处理 / 反馈不成立（知识无误）+ 回复用户 / 退回未处理。
// 注意：用户反馈可能是误报，所以除了「已修正」还需要「知识无误」这一出口。
const fbDetail = ref<SupportMessage | null>(null)
const fbDraft = ref('')
const fbBusy = ref(false)
const fbError = ref('')

const FB_LABEL: Record<FeedbackStatus, string> = {
  open: '未处理',
  done: '已处理',
  rejected: '反馈不成立',
}
/** 未处理 / 已处理 / 反馈不成立（缺省或非法值按未处理） */
const fbStatus = (m: SupportMessage | null): FeedbackStatus => {
  const s = m?.meta?.status
  return s === 'done' || s === 'rejected' ? s : 'open'
}
const fbLabel = (m: SupportMessage | null): string => FB_LABEL[fbStatus(m)]

function openFeedbackDetail(m: SupportMessage): void {
  fbDetail.value = m            // 直接引用列表中的响应式对象，处理后就地刷新
  fbDraft.value = m.meta?.admin_note || ''
  fbError.value = ''
}

/**
 * 处理反馈。status='rejected' 时把 reply 作为「人工客服」消息真正回复给用户
 * （用户会看到新消息 + 未读提醒），reply 为空则用默认话术。
 */
async function markFeedback(status: FeedbackStatus, reply = ''): Promise<void> {
  const cur = fbDetail.value
  if (!cur || fbBusy.value) return
  fbBusy.value = true
  fbError.value = ''
  try {
    const updated = await updateFeedbackStatus(cur.id, status, fbDraft.value, reply)
    Object.assign(cur, updated)          // 就地更新：卡片状态徽标立即变化
    fbDetail.value = null
    if (reply && activeId.value) {
      // 回复消息要出现在消息流里，整会话重拉一次
      activeConvo.value = await fetchConversation(activeId.value)
      humanScroll()
    }
    await loadConvos()                   // 刷新左侧「N 条知识反馈待处理」角标
  } catch (e: any) {
    fbError.value = `处理失败：${e?.message || e}`
  } finally {
    fbBusy.value = false
  }
}

/** 「知识无误」：驳回反馈并回复用户。备注留空时用默认话术，避免管理员漏写导致用户不知情。 */
const FB_DEFAULT_REJECT_REPLY =
  '经核实，该知识条目内容无误，你的反馈我们已记录，感谢反馈。'
function rejectFeedback(): void {
  const note = fbDraft.value.trim()
  void markFeedback('rejected', note || FB_DEFAULT_REJECT_REPLY)
}

// 切换会话 / 收起面板时关闭详情浮窗，避免指向已失效的消息
watch([activeId, view, open], () => { fbDetail.value = null })

async function syncUnread(): Promise<void> {
  try { supportUnread.value = await fetchUnread() } catch { /* ignore */ }
}

/** 进入人工客服视图：加载会话；用户自动打开最近一条，管理员打开第一条 */
async function openHuman(pendingSummary = ''): Promise<void> {
  view.value = 'human'
  humanError.value = ''
  activeId.value = ''
  activeConvo.value = null
  await loadConvos()
  if (pendingSummary && !adminView.value) {
    try {
      const c = await createConversation(pendingSummary)
      await loadConvos()
      await openConvo(c.id)
    } catch (e: any) {
      humanError.value = `转人工失败：${e?.message || e}`
    }
  } else if (convos.value.length) {
    await openConvo(convos.value[0].id)
  }
  startHumanTimer()
}

/** 转人工：把本轮问题作为上下文摘要带过去，用户不用重新描述 */
function transferToHuman(m: FabMessage): void {
  const idx = chat.value.indexOf(m)
  const prevUser = idx > 0 ? [...chat.value.slice(0, idx)].reverse().find((x) => x.role === 'user') : undefined
  const summary = prevUser ? `【AI 客服未能解决】用户问题：${prevUser.text}` : '【用户主动转人工】'
  expanded.value = true   // 转人工后放大窗口，便于查看会话与回复
  void openHuman(summary)
}

// 人工视图打开期间：定时刷新会话与消息，收到新消息自动标已读
let humanTimer: number | null = null
function startHumanTimer(): void {
  stopHumanTimer()
  humanTimer = window.setInterval(async () => {
    if (document.hidden || view.value !== 'human' || !open.value) return
    await loadConvos()
    if (activeId.value) {
      const before = activeConvo.value?.messages?.length || 0
      try {
        activeConvo.value = await fetchConversation(activeId.value)
        const after = activeConvo.value?.messages?.length || 0
        if (after > before) {
          humanScroll()
          await markRead(activeId.value)
        }
      } catch { /* ignore */ }
    }
    await syncUnread()
  }, 5000)
}
function stopHumanTimer(): void {
  if (humanTimer !== null) {
    window.clearInterval(humanTimer)
    humanTimer = null
  }
}

// ====== 跳转入口 ======
function goAsk(): void {
  open.value = false
  emit('navigate-ask')
}

function goAskWithLast(): void {
  const q = lastQuestion.value
  open.value = false
  emit('navigate-ask', q || undefined)
}

// ====== 悬浮球拖动（pointer 事件，点击/拖动自动区分） ======
const BALL_SIZE = 56
const POS_KEY = 'ops.assistant.fab.pos'

const viewport = ref({ w: window.innerWidth, h: window.innerHeight })
const pos = ref({ left: viewport.value.w - BALL_SIZE - 28, top: viewport.value.h - BALL_SIZE - 28 })
const isDragging = ref(false)

// 面板尺寸：普通 344×462；放大 680×720（均按视口收敛，不铺满整页）
const panelW = computed(() => (expanded.value ? Math.min(680, viewport.value.w - 40) : Math.min(344, viewport.value.w - 24)))
const panelH = computed(() => (expanded.value ? Math.min(720, viewport.value.h - 100) : Math.min(462, viewport.value.h - 120)))

function clampPos(left: number, top: number): { left: number; top: number } {
  const margin = 8
  const maxX = Math.max(margin, viewport.value.w - BALL_SIZE - margin)
  const maxY = Math.max(margin, viewport.value.h - BALL_SIZE - margin)
  return {
    left: Math.min(Math.max(left, margin), maxX),
    top: Math.min(Math.max(top, margin), maxY),
  }
}

function loadPos(): void {
  try {
    const raw = localStorage.getItem(POS_KEY)
    if (raw) {
      const p = JSON.parse(raw)
      if (typeof p?.left === 'number' && typeof p?.top === 'number') {
        pos.value = clampPos(p.left, p.top)
        return
      }
    }
  } catch { /* 回退默认右下角 */ }
}

function savePos(): void {
  try {
    localStorage.setItem(POS_KEY, JSON.stringify(pos.value))
  } catch { /* ignore */ }
}

let dragStartPt = { x: 0, y: 0 }
let dragStartBall = { left: 0, top: 0 }
let dragMoved = false

function onPointerDown(e: PointerEvent): void {
  if (e.button !== 0) return
  dragStartPt = { x: e.clientX, y: e.clientY }
  dragStartBall = { ...pos.value }
  dragMoved = false
  window.addEventListener('pointermove', onPointerMove)
  window.addEventListener('pointerup', onPointerUp)
  window.addEventListener('pointercancel', onPointerUp)
}

function onPointerMove(e: PointerEvent): void {
  const dx = e.clientX - dragStartPt.x
  const dy = e.clientY - dragStartPt.y
  if (!dragMoved && Math.hypot(dx, dy) < 6) return
  dragMoved = true
  isDragging.value = true
  if (open.value) {
    suppressClearOnClose = true // 标记「因拖动收起」，watch 关闭分支据此保留对话
    open.value = false
  }
  pos.value = clampPos(dragStartBall.left + dx, dragStartBall.top + dy)
}

function onPointerUp(): void {
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerup', onPointerUp)
  window.removeEventListener('pointercancel', onPointerUp)
  isDragging.value = false
  if (dragMoved) {
    savePos()
  } else {
    // 位移 < 6px 视为点击：展开/收起面板
    open.value = !open.value
    if (open.value) scrollBottom()
  }
}

const ballStyle = computed(() => {
  // 2026-09-03 修复：原来用 left/top，浏览器宽度变化或历史 saved 坐标越界时球会被钉在屏幕顶端
  // （被 header 压住），且 css 没有 right/bottom 兜底。改为 right/bottom 表达，根据当前 viewport
  // 与 saved left/top 反算——saved 越界时自动收敛到视口内任意合法位置（不再跑到屏幕外）。
  const right = Math.max(8, viewport.value.w - pos.value.left - BALL_SIZE)
  const bottom = Math.max(8, viewport.value.h - pos.value.top - BALL_SIZE)
  return { right: `${right}px`, bottom: `${bottom}px` }
})

/** 面板位置：默认出现在悬浮球上方；球在屏幕上半部时改到下方 */
const panelStyle = computed(() => {
  const gap = 12
  const margin = 10
  const w = panelW.value
  const h = panelH.value
  const left = Math.min(
    Math.max(pos.value.left + BALL_SIZE / 2 - w / 2, margin),
    Math.max(margin, viewport.value.w - w - margin),
  )
  const aboveTop = pos.value.top - h - gap
  const top = aboveTop >= margin ? aboveTop : Math.min(pos.value.top + BALL_SIZE + gap, viewport.value.h - h - margin)
  return {
    left: `${Math.max(margin, Math.min(left, viewport.value.w - w - margin))}px`,
    top: `${Math.max(margin, top)}px`,
    width: `${w}px`,
    height: `${h}px`,
  }
})

function onResize(): void {
  viewport.value = { w: window.innerWidth, h: window.innerHeight }
  pos.value = clampPos(pos.value.left, pos.value.top)
}

// 人工客服未读轮询（引用计数：与全局只跑一个定时器）
let stopPolling: (() => void) | null = null

onMounted(() => {
  loadPos()
  window.addEventListener('resize', onResize)
  stopPolling = startSupportPolling()
})

onUnmounted(() => {
  window.removeEventListener('resize', onResize)
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerup', onPointerUp)
  window.removeEventListener('pointercancel', onPointerUp)
  abortCtrl?.abort()
  stopPolling?.()
  stopPolling = null
  stopHumanTimer()
})
</script>

<style scoped>
/* ====== 悬浮球 ====== */
.ai-fab-ball {
  position: fixed;
  z-index: 900;
  display: grid;
  width: 56px;
  height: 56px;
  place-items: center;
  border: 0;
  border-radius: 50%;
  background: linear-gradient(135deg, #4D9EFF, #2E7CF0);
  color: #fff;
  box-shadow: 0 10px 30px rgba(46, 124, 240, .35), 0 0 0 6px rgba(77, 158, 255, .10);
  cursor: grab;
  user-select: none;
  touch-action: none;
  transition: transform .18s ease, box-shadow .18s ease;
}
.ai-fab-ball:hover {
  transform: translateY(-2px) scale(1.04);
  box-shadow: 0 14px 36px rgba(46, 124, 240, .42), 0 0 0 8px rgba(77, 158, 255, .14);
}
.ai-fab-ball.dragging { cursor: grabbing; transform: scale(1.06); transition: none; }
.ai-fab-ball.open { background: linear-gradient(135deg, #1677ff, #2E7CF0); }
.ai-fab-dot {
  position: absolute;
  right: 4px;
  bottom: 4px;
  width: 11px;
  height: 11px;
  border-radius: 50%;
  background: #9ad0ff;
  border: 2px solid #2E7CF0;
}
/* 人工客服未读数角标（悬浮球右上角） */
.ai-fab-badge {
  position: absolute;
  top: -3px;
  right: -3px;
  display: grid;
  min-width: 19px;
  height: 19px;
  padding: 0 4px;
  place-items: center;
  border-radius: 10px;
  background: #e8453c;
  border: 2px solid #fff;
  color: #fff;
  font-size: 10px;
  font-weight: 700;
  line-height: 1;
}

/* ====== 面板 ====== */
.ai-fab-panel {
  position: fixed;
  z-index: 901;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid #dbe7f7;
  border-radius: 18px;
  background: #fff;
  box-shadow: 0 24px 64px rgba(42, 88, 160, .22);
}
.ai-fab-panel.expanded { border-radius: 20px; box-shadow: 0 28px 80px rgba(42, 88, 160, .26); }

/* 头部 */
.ai-panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 12px 14px;
  border-bottom: 1px solid #e6eef8;
  background: linear-gradient(180deg, #f2f7fe, #fff);
}
.ai-panel-title { display: flex; align-items: center; gap: 10px; min-width: 0; }
.ai-back {
  display: grid;
  flex: none;
  width: 24px;
  height: 24px;
  place-items: center;
  border: 1px solid #d8e5f5;
  border-radius: 7px;
  background: #fff;
  color: #5b6c85;
  cursor: pointer;
}
.ai-back:hover { border-color: #8fbdfa; color: #2E7CF0; }
.ai-panel-avatar {
  display: grid;
  flex: none;
  width: 34px;
  height: 34px;
  place-items: center;
  border-radius: 50%;
  background: linear-gradient(135deg, #4D9EFF, #2E7CF0);
  color: #fff;
}
.ai-panel-avatar.human { background: linear-gradient(135deg, #34c38f, #1fa97a); }
.ai-panel-name { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.ai-panel-name b { font-size: 13px; color: #1d3a63; }
.ai-panel-name small {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 10px;
  color: #7b8ba3;
}
.ai-online-dot { width: 6px; height: 6px; border-radius: 50%; background: #4D9EFF; }
.ai-panel-actions { display: flex; flex: none; gap: 4px; }
.ai-panel-actions button {
  position: relative;
  display: grid;
  width: 28px;
  height: 28px;
  place-items: center;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: #7c8aa0;
  cursor: pointer;
}
.ai-panel-actions button:hover {
  border-color: #b7d6ff;
  background: #e8f3ff;
  color: #2E7CF0;
}
/* 人工客服入口（星标左侧）：有未读时高亮 + 右上角红点计数 */
.ai-act-human.has {
  border-color: #ffd0cf;
  background: #fff1f0;
  color: #e8453c;
}
.ai-act-dot {
  position: absolute;
  top: -3px;
  right: -3px;
  min-width: 15px;
  height: 15px;
  padding: 0 3px;
  border-radius: 8px;
  background: #e8453c;
  border: 1.5px solid #fff;
  color: #fff;
  font-size: 9px;
  font-weight: 700;
  line-height: 12px;
  text-align: center;
}

/* 消息区 */
.ai-panel-msgs {
  flex: 1;
  overflow-y: auto;
  padding: 14px;
  background:
    radial-gradient(420px 160px at 100% -30px, rgba(77, 158, 255, .08), transparent 60%),
    #f6f9fd;
}

/* 欢迎语 + FAQ */
.ai-welcome { display: flex; flex-direction: column; gap: 10px; }
.ai-welcome-hi {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 700;
  color: #1d3a63;
}
.ai-welcome-hi :deep(.app-icon) { color: #4D9EFF; }
.ai-welcome-tip { margin: 0; font-size: 11px; line-height: 1.7; color: #7b8ba3; }
.ai-faq { display: flex; flex-direction: column; gap: 8px; }
.ai-faq-tabs { display: flex; gap: 4px; padding: 3px; border-radius: 10px; background: #e9f1fa; }
.ai-faq-tabs button {
  flex: 1;
  padding: 5px 0;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: #7c8aa0;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
}
.ai-faq-tabs button.on {
  background: #fff;
  color: #2E7CF0;
  box-shadow: 0 1px 4px rgba(42, 88, 160, .12);
}
.ai-faq-item {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 8px 11px;
  border: 1px solid #dce7f6;
  border-radius: 11px;
  background: #fff;
  color: #3f5878;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
  transition: border-color .15s, background .15s, transform .15s;
}
.ai-faq-item:hover { border-color: #8fbdfa; background: #f2f8ff; transform: translateX(2px); }
.ai-faq-item :deep(.app-icon) { flex: none; color: #4D9EFF; }

/* 气泡 */
.ai-row { display: flex; margin-bottom: 10px; }
.ai-row.mine { justify-content: flex-end; }
.ai-bubble {
  position: relative;
  max-width: 86%;
  padding: 8px 11px;
  border: 1px solid #dde6f2;
  border-radius: 13px;
  background: #fff;
  box-shadow: 0 1px 3px rgba(42, 88, 160, .05);
}
.ai-row.mine .ai-bubble { background: #2E7CF0; border-color: #2E7CF0; }
.ai-row.mine .ai-bubble-text { color: #fff; }
.ai-bubble-text {
  font-size: 12px;
  line-height: 1.65;
  color: #2b3a4e;
  white-space: pre-wrap;
  word-break: break-word;
}
.ai-bubble-tag {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  margin-top: 5px;
  padding: 2px 7px;
  border-radius: 8px;
  background: #e8f3ff;
  color: #2E7CF0;
  font-size: 10px;
  font-weight: 600;
}
/* 气泡内「转接人工客服」按钮 */
.ai-transfer-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-top: 7px;
  padding: 4px 9px;
  border: 1px solid #ffc9c6;
  border-radius: 9px;
  background: #fff5f5;
  color: #e8453c;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  transition: background .15s, border-color .15s;
}
.ai-transfer-btn:hover { background: #ffe8e6; border-color: #e8453c; }
.ai-bubble-loading { display: flex; align-items: center; gap: 7px; color: #7c8aa0; font-size: 12px; }
.ai-spin { animation: ai-spin 1s linear infinite; }
@keyframes ai-spin { to { transform: rotate(360deg); } }

/* 输入区 */
.ai-panel-input {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border-top: 1px solid #e6eef8;
  background: #fff;
}
.ai-panel-input input {
  flex: 1;
  min-width: 0;
  height: 36px;
  padding: 0 12px;
  border: 1px solid #cdddf0;
  border-radius: 18px;
  background: #f6f9fd;
  font-size: 12px;
  color: #2b3a4e;
  outline: none;
}
.ai-panel-input input:focus {
  border-color: #4D9EFF;
  background: #fff;
  box-shadow: 0 0 0 3px rgba(77, 158, 255, .15);
}
.ai-send {
  display: grid;
  flex: none;
  width: 36px;
  height: 36px;
  place-items: center;
  border: 0;
  border-radius: 50%;
  background: #2E7CF0;
  color: #fff;
  cursor: pointer;
}
.ai-send:disabled { opacity: .35; cursor: not-allowed; }
.ai-send:not(:disabled):hover { background: #1677ff; }

/* 底部跳转 */
.ai-panel-foot { padding: 0 12px 10px; background: #fff; }
.ai-panel-foot button {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  width: 100%;
  padding: 7px 0;
  border: 1px dashed #b7d6ff;
  border-radius: 10px;
  background: #f4f9ff;
  color: #2E7CF0;
  font-size: 11px;
  cursor: pointer;
  transition: background .15s, border-color .15s;
}
.ai-panel-foot button:hover { background: #e4f0ff; border-color: #4D9EFF; }

/* ====== 人工客服视图 ====== */
.ai-human {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 0;
}
/* 放大态：左右分栏（左会话列表 / 右消息） */
.ai-human.row { flex-direction: row; }
.ai-human.row .ai-human-list {
  width: 176px;
  flex: none;
  border-right: 1px solid #e6eef8;
  border-bottom: 0;
  max-height: none;
}
.ai-human-list {
  display: flex;
  flex-direction: column;
  max-height: 116px;
  border-bottom: 1px solid #e6eef8;
  background: #f8fbff;
}
.ai-human-list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 10px;
  border-bottom: 1px solid #eaf1fa;
  font-size: 11px;
  font-weight: 600;
  color: #5b6c85;
}
.ai-human-list-head button {
  display: grid;
  width: 20px;
  height: 20px;
  place-items: center;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: #7c8aa0;
  cursor: pointer;
}
.ai-human-list-head button:hover { background: #e8f3ff; color: #2E7CF0; }
.ai-human-list-body { flex: 1; overflow-y: auto; }
.ai-human-empty { padding: 14px 10px; text-align: center; font-size: 11px; color: #9aa8bd; line-height: 1.6; }
.ai-human-item {
  display: block;
  width: 100%;
  padding: 7px 10px;
  border: 0;
  border-bottom: 1px solid #f0f5fb;
  background: transparent;
  text-align: left;
  cursor: pointer;
}
.ai-human-item:hover { background: #eef6ff; }
.ai-human-item.on { background: #e4f0ff; }
.ai-human-item-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  color: #2b3a4e;
}
.ai-human-item-top em {
  flex: none;
  min-width: 16px;
  height: 15px;
  padding: 0 4px;
  border-radius: 8px;
  background: #e8453c;
  color: #fff;
  font-size: 9.5px;
  font-style: normal;
  font-weight: 700;
  line-height: 15px;
  text-align: center;
}
.ai-human-item-sub { margin-top: 2px; font-size: 10.5px; color: #9aa8bd; }
/* 会话列表：待处理知识反馈提示 */
.ai-human-item-fb {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  margin-top: 3px;
  padding: 1px 6px;
  border-radius: 7px;
  background: #fff4e6;
  color: #b26a00;
  font-size: 9.5px;
  font-weight: 600;
}
.ai-human-item-fb :deep(.app-icon) { flex: none; }

.ai-human-chat { display: flex; flex: 1; flex-direction: column; min-height: 0; }
.ai-human-err {
  padding: 5px 10px;
  background: #fff1f0;
  border-bottom: 1px solid #ffd0cf;
  color: #c0392b;
  font-size: 10.5px;
}
.ai-human-msgs {
  flex: 1;
  overflow-y: auto;
  padding: 10px 12px;
  background: #f6f9fd;
}
.ai-hrow { display: flex; margin-bottom: 9px; }
.ai-hrow.mine { justify-content: flex-end; }
.ai-hbubble { max-width: 82%; }
.ai-hbubble-meta { margin-bottom: 2px; font-size: 9.5px; color: #a3b0c4; }
.ai-hrow.mine .ai-hbubble-meta { text-align: right; }
.ai-hbubble-text {
  padding: 7px 10px;
  border: 1px solid #dde6f2;
  border-radius: 12px;
  background: #fff;
  color: #2b3a4e;
  font-size: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}
.ai-hrow.mine .ai-hbubble-text {
  background: #2E7CF0;
  border-color: #2E7CF0;
  color: #fff;
}
.ai-human-foot { padding: 0 12px 8px; background: #fff; }
.ai-human-foot button {
  width: 100%;
  padding: 5px 0;
  border: 1px solid #d8e5f5;
  border-radius: 9px;
  background: #f8fbff;
  color: #6b7c93;
  font-size: 11px;
  cursor: pointer;
}
.ai-human-foot button:hover { border-color: #8fbdfa; color: #2E7CF0; }

/* ====== 业务知识反馈：会话流里的大号显著待办卡 ====== */
.ai-fb-card {
  margin: 6px 0 12px;
  padding: 11px 13px;
  border: 1px solid #ffd9a8;
  border-left: 4px solid #f5a524;
  border-radius: 13px;
  background: linear-gradient(180deg, #fffaf1, #fff);
  box-shadow: 0 4px 14px rgba(190, 130, 20, .10);
  cursor: pointer;
  transition: box-shadow .16s ease, transform .16s ease, border-color .16s ease;
}
.ai-fb-card:hover {
  border-color: #f5a524;
  box-shadow: 0 8px 22px rgba(190, 130, 20, .18);
  transform: translateY(-1px);
}
/* 已处理：降饱和，避免和未处理抢注意力 */
.ai-fb-card.done {
  border-color: #d6e6da;
  border-left-color: #34c38f;
  background: linear-gradient(180deg, #f7fcf9, #fff);
  box-shadow: 0 3px 10px rgba(40, 130, 90, .08);
}
.ai-fb-card.done:hover { border-color: #34c38f; }
/* 反馈不成立（知识无误）：蓝灰，与「已处理」的绿区分，表示结论是「无需修改」 */
.ai-fb-card.rejected {
  border-color: #d3dded;
  border-left-color: #7c8aa0;
  background: linear-gradient(180deg, #f7f9fc, #fff);
  box-shadow: 0 3px 10px rgba(60, 80, 120, .08);
}
.ai-fb-card.rejected:hover { border-color: #7c8aa0; }
.ai-fb-card.rejected .ai-fb-note { border-color: #dbe4f0; color: #45536b; }
.ai-fb-card-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}
.ai-fb-tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 999px;
  background: #fff0d6;
  color: #b26a00;
  font-size: 10.5px;
  font-weight: 700;
}
.ai-fb-card.done .ai-fb-tag { background: #e3f6ec; color: #1f8a5c; }
.ai-fb-tag :deep(.app-icon) { flex: none; }
.ai-fb-state {
  flex: none;
  padding: 2px 9px;
  border-radius: 999px;
  font-size: 10.5px;
  font-weight: 700;
}
.ai-fb-state.open { background: #ffe9e6; color: #d4380d; }
.ai-fb-state.done { background: #e3f6ec; color: #1f8a5c; }
.ai-fb-state.rejected { background: #eaeef5; color: #5b6c85; }
/* 反馈不成立时，卡片上的标签也改中性色 */
.ai-fb-card.rejected .ai-fb-tag { background: #eaeef5; color: #5b6c85; }
.ai-fb-item {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 7px;
}
.ai-fb-item em {
  padding: 1px 7px;
  border-radius: 6px;
  background: #eef3fb;
  color: #5b6c85;
  font-size: 10px;
  font-style: normal;
  font-weight: 600;
}
.ai-fb-item b { font-size: 13.5px; color: #1d3a63; }
.ai-fb-item small { font-size: 10.5px; color: #9aa8bd; }
.ai-fb-note {
  padding: 8px 10px;
  border-radius: 9px;
  background: #fff;
  border: 1px dashed #ffd9a8;
  color: #4a3a20;
  font-size: 12px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.ai-fb-card.done .ai-fb-note { border-color: #cfe9d9; color: #3f5a4b; }
.ai-fb-card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 8px;
  font-size: 10px;
  color: #a3b0c4;
}
.ai-fb-more {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  color: #2E7CF0;
  font-weight: 600;
}

/* ====== 反馈详情浮窗（屏幕正中） ====== */
.ai-fb-mask {
  position: fixed;
  inset: 0;
  z-index: 1400;
  display: grid;
  place-items: center;
  padding: 20px;
  background: rgba(15, 33, 61, .42);
  backdrop-filter: blur(2px);
}
.ai-fb-modal {
  display: flex;
  flex-direction: column;
  width: min(520px, 94vw);
  max-height: min(86vh, 720px);
  overflow: hidden;
  border: 1px solid #dbe7f7;
  border-radius: 18px;
  background: #fff;
  box-shadow: 0 28px 80px rgba(20, 45, 90, .34);
  animation: ai-fb-pop .18s ease;
}
@keyframes ai-fb-pop {
  from { opacity: 0; transform: translateY(12px) scale(.97); }
  to { opacity: 1; transform: none; }
}
.ai-fb-modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 13px 16px;
  border-bottom: 1px solid #e6eef8;
  background: linear-gradient(180deg, #fff7ea, #fff);
  font-size: 13px;
  font-weight: 700;
  color: #1d3a63;
}
.ai-fb-modal-head > span { display: inline-flex; align-items: center; gap: 6px; }
.ai-fb-modal-head > span :deep(.app-icon) { color: #f5a524; }
.ai-fb-modal-head button {
  display: grid;
  width: 26px;
  height: 26px;
  place-items: center;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: #7c8aa0;
  cursor: pointer;
}
.ai-fb-modal-head button:hover { background: #eef4ff; color: #2E7CF0; }
.ai-fb-modal-body {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 9px;
  overflow-y: auto;
  padding: 14px 16px;
}
.ai-fb-mrow {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
  color: #2b3a4e;
}
.ai-fb-mrow label { flex: none; width: 62px; color: #86909c; font-size: 11.5px; }
.ai-fb-mrow b { font-size: 13.5px; color: #1d3a63; }
.ai-fb-mrow code {
  padding: 1px 6px;
  border-radius: 5px;
  background: #f2f6fc;
  color: #3f5878;
  font-size: 11px;
}
.ai-fb-key { word-break: break-all; }
.ai-fb-mcol { display: flex; flex-direction: column; gap: 5px; }
.ai-fb-mcol label { color: #86909c; font-size: 11.5px; }
.ai-fb-mcol p {
  margin: 0;
  padding: 9px 11px;
  border: 1px solid #e8eef7;
  border-radius: 10px;
  background: #f9fbfe;
  color: #2b3a4e;
  font-size: 12px;
  line-height: 1.75;
  white-space: pre-wrap;
  word-break: break-word;
}
.ai-fb-mcol p.ai-fb-hl {
  border-color: #ffd9a8;
  background: #fffaf1;
  color: #4a3a20;
}
.ai-fb-mcol textarea {
  padding: 9px 11px;
  border: 1px solid #cdddf0;
  border-radius: 10px;
  background: #f9fbfe;
  color: #2b3a4e;
  font-size: 12px;
  line-height: 1.6;
  font-family: inherit;
  resize: vertical;
  outline: none;
}
.ai-fb-mcol textarea:focus {
  border-color: #4D9EFF;
  background: #fff;
  box-shadow: 0 0 0 3px rgba(77, 158, 255, .15);
}
.ai-fb-meta-line {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  margin: 2px 0 0;
  font-size: 10.5px;
  color: #a3b0c4;
}
.ai-fb-err { margin: 0; color: #d4380d; font-size: 11.5px; }
.ai-fb-hint {
  display: flex;
  align-items: flex-start;
  gap: 5px;
  margin: 0;
  font-size: 10.5px;
  line-height: 1.6;
  color: #9aa8bd;
}
.ai-fb-hint :deep(.app-icon) { flex: none; margin-top: 2px; color: #4D9EFF; }
.ai-fb-modal-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 16px;
  border-top: 1px solid #eef3fa;
  background: #fff;
}
.ai-fb-modal-foot button {
  padding: 7px 15px;
  border-radius: 10px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}
.ai-fb-ghost {
  border: 1px solid #d8e5f5;
  background: #fff;
  color: #5b6c85;
}
.ai-fb-ghost:hover:not(:disabled) { border-color: #8fbdfa; color: #2E7CF0; }
.ai-fb-primary {
  border: 1px solid #2E7CF0;
  background: #2E7CF0;
  color: #fff;
}
.ai-fb-primary:hover:not(:disabled) { background: #1677ff; }
/* 「知识无误 · 回复用户」：橙色调，与蓝色的「标记已处理」区分开 */
.ai-fb-warn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 1px solid #f0b429;
  background: #fff8e8;
  color: #a86a00;
}
.ai-fb-warn:hover:not(:disabled) { background: #ffefd0; border-color: #d99b12; }
.ai-fb-modal-foot button:disabled { opacity: .5; cursor: not-allowed; }

/* 面板弹出动画 */
.ai-fab-pop-enter-active { transition: opacity .2s ease, transform .2s ease; }
.ai-fab-pop-leave-active { transition: opacity .14s ease, transform .14s ease; }
.ai-fab-pop-enter-from,
.ai-fab-pop-leave-to { opacity: 0; transform: translateY(10px) scale(.97); }

.truncate { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
</style>
