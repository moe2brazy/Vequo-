import { ref, computed, watch } from 'vue'
import { getUser } from '../auth'
import { PROFILE_STORAGE_KEY } from '../profile'
import { matchFaq } from '../assistantFaq'
import { askAgentStream } from '../assistantAi'
import { safeSetItem } from '../storage'

/**
 * 消息中心（浏览器 localStorage 持久化，与 AskPage 历史/业务知识收藏同一套本地存储模式）
 *
 * 默认固定两个会话：
 *  - AI 管理员（conv-ai-assistant）：与悬浮 AI 客服同源 —— FAQ 命中本地秒回，未命中调
 *    /api/agent/stream 单轮 AI 快答（无记忆）
 *  - 系统管理员（conv-admin）：给系统管理员留言；管理员处理后在原会话回复
 *
 * 演示环境下"管理员"与普通员工共用同一浏览器存储，通过 senderKey 区分身份并计算未读
 *（同一浏览器内切换账号即可看到彼此的留言）。
 */

export interface ChatMessage {
  id: string
  convId: string
  senderKey: string
  senderName: string
  senderRole: string
  text: string
  ts: number
  tag?: string
  /** AI 正在生成中（占位气泡，展示思考中/步骤） */
  pending?: boolean
}

export interface Conversation {
  id: string
  key: string           // ai / admin：左侧唯一选中标识
  title: string
  subtitle: string
  peerName: string      // 消息中"对方"展示名
  icon: string          // AppIcon 名称
  pinned?: boolean
  createdAt: number
}

interface ChatData {
  convs: Conversation[]
  messages: Record<string, ChatMessage[]>
  /** 每个身份(key)的已读进度: convId -> 已读到的最大 ts */
  read: Record<string, Record<string, number>>
}

const STORAGE_KEY = 'ops.chat.message.v1'

export function currentIdentity(): { key: string; name: string; role: string } {
  const u = getUser()
  if (u && u.username) {
    return { key: u.username, name: u.display_name || u.username, role: u.role || 'viewer' }
  }
  try {
    const raw = localStorage.getItem(PROFILE_STORAGE_KEY)
    if (raw) {
      const p = JSON.parse(raw)
      if (p?.name) return { key: p.name, name: p.name, role: p.role || 'viewer' }
    }
  } catch { /* ignore */ }
  return { key: 'guest', name: '访客', role: 'viewer' }
}

function uid(): string {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 8)
}

const now = Date.now()

function seed(): ChatData {
  return {
    convs: [
      {
        id: 'conv-ai-assistant',
        key: 'ai',
        title: 'AI 管理员',
        subtitle: '平台使用与业务知识答疑',
        peerName: 'AI 管理员',
        icon: 'bot',
        pinned: true,
        createdAt: now,
      },
      {
        id: 'conv-admin',
        key: 'admin',
        title: '系统管理员',
        subtitle: '数据平台维护与业务咨询',
        peerName: '系统管理员',
        icon: 'settings',
        createdAt: now - 1000 * 60 * 60 * 24 * 2,
      },
    ],
    messages: {
      'conv-ai-assistant': [
        {
          id: uid(),
          convId: 'conv-ai-assistant',
          senderKey: 'ai-assistant',
          senderName: 'AI 管理员',
          senderRole: 'system',
          text: '你好，我是 AI 管理员。关于平台怎么用、业务知识常见问题都可以直接问我；数据查询与多轮分析请前往「智能问析」。',
          ts: now,
        },
      ],
      'conv-admin': [
        {
          id: uid(),
          convId: 'conv-admin',
          senderKey: 'admin',
          senderName: '系统管理员',
          senderRole: 'admin',
          text: '你好，欢迎使用数据平台。需要申请数据权限或反馈问题，可以在这里给我留言，我会尽快处理并回复。',
          ts: now - 1000 * 60 * 60 * 24 * 2,
        },
      ],
    },
    read: {},
  }
}

function load(): ChatData {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const d = JSON.parse(raw) as ChatData
      if (d && Array.isArray(d.convs) && d.messages) {
        // 老数据升级：确保两个默认会话都在
        const hasAi = d.convs.some((c) => c.id === 'conv-ai-assistant')
        const hasAdmin = d.convs.some((c) => c.id === 'conv-admin')
        if (!hasAi || !hasAdmin) return seed()
        return d
      }
    }
  } catch { /* ignore */ }
  return seed()
}

const data = ref<ChatData>(load())

// 落盘合并（性能修复）：data 是整个消息中心——两个会话 + 全部消息 + 各身份的已读进度。
// 原实现是 deep watch 里直接 JSON.stringify 全量写 localStorage，任一字段变化都触发一次，
// 而且 AI 流式作答期间 AiAssistantFab 的 onStep 每个步骤都在改 placeholder.text，
// 于是每步都要序列化「全部历史」，消息越多越卡。
// 改为 300ms 合并写入：连续变更只写最后一次；再用 pagehide / 页面转入后台做兜底 flush，
// 保证最后 300ms 内的消息不会因为用户直接关页而丢。
let persistTimer: ReturnType<typeof setTimeout> | null = null

/** 立即把内存中的消息中心写入本地存储（取消挂起的合并写入） */
export function flushChatPersist(): void {
  if (persistTimer) {
    clearTimeout(persistTimer)
    persistTimer = null
  }
  safeSetItem(STORAGE_KEY, JSON.stringify(persistSnapshot()))
}

/**
 * 2026-10-01 修复：落盘副本每会话消息封顶 200 条（内存不裁剪）。
 * 原实现 messages 从不清理、写失败被静默吞掉 → localStorage 无限增长，
 * 写满配额后最近消息反而丢失。
 */
function persistSnapshot(): ChatData {
  const d = data.value
  const messages: Record<string, ChatMessage[]> = {}
  for (const k of Object.keys(d.messages)) {
    const arr = d.messages[k]
    messages[k] = arr.length > 200 ? arr.slice(arr.length - 200) : arr
  }
  return { ...d, messages }
}

watch(
  data,
  () => {
    if (persistTimer) clearTimeout(persistTimer)
    persistTimer = setTimeout(() => {
      persistTimer = null
      safeSetItem(STORAGE_KEY, JSON.stringify(persistSnapshot()))
    }, 300)
  },
  { deep: true },
)

if (typeof window !== 'undefined') {
  window.addEventListener('pagehide', flushChatPersist)
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flushChatPersist()
  })
}

export const conversations = computed<Conversation[]>(() =>
  [...data.value.convs].sort((a, b) => {
    const pin = Number(b.pinned ? 1 : 0) - Number(a.pinned ? 1 : 0)
    if (pin !== 0) return pin
    return b.createdAt - a.createdAt
  }),
)

export function findConversation(key: string): Conversation | undefined {
  return data.value.convs.find((c) => c.key === key)
}

export function messagesOf(convId: string): ChatMessage[] {
  return data.value.messages[convId] || []
}

function readBucket(): Record<string, number> {
  const k = currentIdentity().key
  return data.value.read[k] || {}
}
function ensureReadBucket(): Record<string, number> {
  const k = currentIdentity().key
  if (!data.value.read[k]) data.value.read[k] = {}
  return data.value.read[k]
}

function pushMsg(convId: string, m: ChatMessage): void {
  if (!data.value.messages[convId]) data.value.messages[convId] = []
  data.value.messages[convId].push(m)
  // 自己发出的消息自动视为已读
  ensureReadBucket()[convId] = m.ts
}

export function unreadOf(convId: string): number {
  const me = currentIdentity().key
  const last = readBucket()[convId] || 0
  return (data.value.messages[convId] || []).filter((m) => m.senderKey !== me && m.ts > last).length
}

export function totalUnread(): number {
  return data.value.convs.reduce((sum, c) => sum + unreadOf(c.id), 0)
}

/** 供侧栏/入口展示的响应式总未读数 */
export const chatUnreadTotal = computed(() => totalUnread())

export function markRead(convId: string): void {
  const msgs = data.value.messages[convId] || []
  ensureReadBucket()[convId] = msgs.length ? msgs[msgs.length - 1].ts : Date.now()
}

/** 给系统管理员（或任意对方）发一条留言 —— 调用方身份即发送人 */
export function sendMessage(key: string, text: string): ChatMessage | null {
  const conv = findConversation(key)
  const t = (text || '').trim()
  if (!conv || !t) return null
  const me = currentIdentity()
  const msg: ChatMessage = {
    id: uid(),
    convId: conv.id,
    senderKey: me.key,
    senderName: me.name,
    senderRole: me.role,
    text: t,
    ts: Date.now(),
  }
  pushMsg(conv.id, msg)
  return msg
}

/** 管理员在「系统管理员」会话中的回复/回执（senderKey=admin） */
export function adminReply(text: string): void {
  const conv = findConversation('admin')
  if (!conv) return
  pushMsg(conv.id, {
    id: uid(),
    convId: conv.id,
    senderKey: 'admin',
    senderName: '系统管理员',
    senderRole: 'admin',
    text,
    ts: Date.now(),
  })
}

/**
 * 向 AI 管理员提问 —— 与悬浮 AI 客服行为一致：
 *  - FAQ 关键词命中：本地秒回（带"常见问答"标识）
 *  - 未命中：调用共享 askAgentStream（/api/agent/stream 单轮），期间插入"正在生成…"占位气泡，
 *    完成后替换为 AI 回答；失败给出与悬浮球一致的错误提示。
 */
export async function askAiStream(key: string, text: string): Promise<void> {
  const conv = findConversation(key)
  const t = (text || '').trim()
  if (!conv || !t) return
  const me = currentIdentity()
  pushMsg(conv.id, {
    id: uid(),
    convId: conv.id,
    senderKey: me.key,
    senderName: me.name,
    senderRole: me.role,
    text: t,
    ts: Date.now(),
  })

  const hit = matchFaq(t)
  if (hit) {
    window.setTimeout(() => {
      pushMsg(conv.id, {
        id: uid(),
        convId: conv.id,
        senderKey: 'ai-assistant',
        senderName: 'AI 管理员',
        senderRole: 'system',
        text: hit.a,
        tag: '常见问答',
        ts: Date.now(),
      })
    }, 260)
    return
  }

  const placeholder: ChatMessage = {
    id: uid(),
    convId: conv.id,
    senderKey: 'ai-assistant',
    senderName: 'AI 管理员',
    senderRole: 'system',
    text: '正在思考…',
    pending: true,
    ts: Date.now(),
  }
  pushMsg(conv.id, placeholder)
  try {
    const { text: answer } = await askAgentStream(t, {
      onStep: (label) => { placeholder.text = label },
    })
    placeholder.text = answer
  } catch (e: any) {
    placeholder.text = `请求失败：${e?.message || '未知错误'}\n\n请确认后端服务已启动；也可以直接前往「智能问析」重试该问题。`
  } finally {
    placeholder.pending = false
  }
}
