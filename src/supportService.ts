/**
 * 人工客服服务层 —— 与后端 /api/support/* 对接
 *
 * 职责：
 *  1. 客户端身份 clientId（未登录游客用，持久化 localStorage，保证同一浏览器能找回自己的工单）
 *  2. 会话/消息 CRUD + 已读标记
 *  3. 全局未读轮询（供悬浮球红点/角标使用）
 *
 * 与「智能问析」/消息中心 chat.ts 的边界：本模块是服务端持久化的人工工单，
 * 用户与管理员真正互通（不再只存本地 localStorage）。
 * 人工客服界面位于悬浮球内的「人工客服」视图（AiAssistantFab.vue），不单独成页。
 */

import { ref, computed } from 'vue'
import { getUser, isAdmin } from './auth'
import { PROFILE_STORAGE_KEY } from './profile'

// ====== 类型 ======
/** 「业务知识有误」反馈的载荷（随消息一起存储，前端渲染为大号待办卡） */
export interface SupportFeedbackMeta {
  item_key?: string
  item_kind?: string
  item_title?: string
  item_scene?: string
  item_table?: string
  item_desc?: string
  /** 用户填写的问题说明 */
  note?: string
  /**
   * open = 未处理；done = 已处理（知识已修正）；
   * rejected = 反馈不成立（经核实该知识无误，通常附管理员回复告知用户）
   */
  status?: FeedbackStatus
  admin_note?: string
  handled_at?: number
  handled_by?: string
}

export type FeedbackStatus = 'open' | 'done' | 'rejected'

export interface SupportMessage {
  id: string
  sender: 'user' | 'agent'
  sender_name: string
  text: string
  ts: number
  /** text = 普通对话；feedback = 业务知识反馈（大号待办卡） */
  kind?: 'text' | 'feedback'
  meta?: SupportFeedbackMeta
}

export interface SupportConversation {
  id: string
  user_key: string
  user_name: string
  status: 'open' | 'closed'
  created_at: number
  updated_at: number
  user_read_ts?: number
  admin_read_ts?: number
  messages?: SupportMessage[]
}

export interface SupportConvoSummary {
  id: string
  user_key: string
  user_name: string
  status: string
  created_at: number
  updated_at: number
  last_text: string
  message_count: number
  unread?: number
  /** 管理员视角：该会话中尚未处理的知识反馈数 */
  pending_feedback?: number
}

// ====== 客户端身份（游客侧标识）======
const CLIENT_ID_KEY = 'ops.support.clientId'

export function getClientId(): string {
  try {
    let id = localStorage.getItem(CLIENT_ID_KEY)
    if (!id) {
      id = 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10)
      localStorage.setItem(CLIENT_ID_KEY, id)
    }
    return id
  } catch {
    return 'anonymous'
  }
}

function headers(extra: Record<string, string> = {}): Record<string, string> {
  return { 'Content-Type': 'application/json', 'X-Client-Id': getClientId(), ...extra }
}

async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, init)
  if (!resp.ok) {
    const err = await resp.json().catch(() => null)
    // 修复：把 HTTP 状态码带到 Error 上，供轮询层区分 401/403（token 失效）与其他错误
    const e = new Error((err as any)?.detail || `请求失败(${resp.status})`) as Error & { status?: number }
    e.status = resp.status
    throw e
  }
  return resp.json() as Promise<T>
}

/** 当前身份展示名（登录账号 > 本地资料 > 游客） */
export function currentSupportName(): string {
  const u = getUser()
  if (u?.display_name) return u.display_name
  if (u?.username) return u.username
  try {
    const p = JSON.parse(localStorage.getItem(PROFILE_STORAGE_KEY) || 'null')
    if (p?.name) return p.name
  } catch { /* ignore */ }
  return '访客'
}

// ====== API ======

/** 客服 AI 多轮问答（会话内记忆，history 由调用方维护；不落库） */
export async function askSupportAi(
  messages: Array<{ role: 'user' | 'assistant'; content: string }>,
  signal?: AbortSignal,
): Promise<{ answer: string; need_human: boolean }> {
  const data = await api<{ answer: string; need_human: boolean }>('/api/support/ai-chat', {
    method: 'POST',
    headers: headers(),
    body: JSON.stringify({ messages, client_id: getClientId() }),
    signal,
  })
  return { answer: data.answer, need_human: !!data.need_human }
}

export async function listConversations(): Promise<{ conversations: SupportConvoSummary[]; is_admin: boolean; me: string }> {
  return api('/api/support/conversations', { headers: headers() })
}

export async function createConversation(summary = ''): Promise<SupportConversation> {
  const data = await api<{ conversation: SupportConversation }>('/api/support/conversations', {
    method: 'POST',
    headers: headers(),
    body: JSON.stringify({ summary, user_name: currentSupportName(), client_id: getClientId() }),
  })
  return data.conversation
}

export async function fetchConversation(cid: string): Promise<SupportConversation> {
  const data = await api<{ conversation: SupportConversation }>(
    `/api/support/conversations/${encodeURIComponent(cid)}/messages`,
    { headers: headers() },
  )
  return data.conversation
}

export async function sendMessage(cid: string, text: string): Promise<SupportMessage> {
  const data = await api<{ message: SupportMessage }>(
    `/api/support/conversations/${encodeURIComponent(cid)}/messages`,
    { method: 'POST', headers: headers(), body: JSON.stringify({ text, client_id: getClientId() }) },
  )
  return data.message
}

export async function markRead(cid: string): Promise<void> {
  await api(`/api/support/conversations/${encodeURIComponent(cid)}/read`, {
    method: 'POST',
    headers: headers(),
  })
}

export async function closeConversation(cid: string): Promise<void> {
  await api(`/api/support/conversations/${encodeURIComponent(cid)}/close`, {
    method: 'POST',
    headers: headers(),
  })
}

export async function fetchUnread(): Promise<number> {
  const data = await api<{ unread: number }>('/api/support/unread', { headers: headers() })
  return data.unread || 0
}

/**
 * 「业务知识有误」反馈 —— 作为一条大号待办消息，追加进当前用户已有的人工客服会话
 * （后端复用未关闭会话，**不新开聊天框**）。
 */
export async function sendKnowledgeFeedback(payload: {
  item_key: string
  item_kind: string
  item_title: string
  item_scene?: string
  item_table?: string
  item_desc?: string
  note: string
}): Promise<{ conversation: SupportConversation; message: SupportMessage }> {
  return api('/api/support/knowledge-feedback', {
    method: 'POST',
    headers: headers(),
    body: JSON.stringify({ ...payload, user_name: currentSupportName(), client_id: getClientId() }),
  })
}

/**
 * 管理员处理知识反馈：
 *  - `done`     已处理（管理员已修正知识）
 *  - `rejected` 反馈不成立（经核实该知识无误）
 *  - `open`     退回未处理
 * 传 `reply` 时会把回复作为一条「人工客服」消息真正发给用户（用户侧会产生未读提醒）。
 */
export async function updateFeedbackStatus(
  mid: string,
  status: FeedbackStatus,
  adminNote = '',
  reply = '',
): Promise<SupportMessage> {
  const data = await api<{ message: SupportMessage }>(
    `/api/support/feedback/${encodeURIComponent(mid)}/status`,
    {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify({ status, admin_note: adminNote, reply }),
    },
  )
  return data.message
}

// ====== 全局未读（浮动球红点 / 页面入口角标）======
export const supportUnread = ref(0)
export const supportIsAdmin = computed(() => isAdmin())

let pollTimer: number | null = null
let pollRefs = 0
const DEFAULT_POLL_MS = 5000
/**
 * 修复（P0）：token 失效后必须停止轮询。
 * 原实现的死循环：轮询间隔 5s，而 auth.ts 的 401 去抖窗口只有 2s →
 * 每个轮询周期都重新广播 auth:unauthorized → App.vue 反复弹出登录框，
 * 用户点「取消」后 5 秒又被弹一次，页面无法正常使用，后端日志也被 401 淹没。
 */
let pollingDisabled = false

async function poll(): Promise<void> {
  if (pollingDisabled) return
  try {
    if (document.hidden) return // 后台标签页不轮询，省请求
    supportUnread.value = await fetchUnread()
  } catch (e: any) {
    // 401（未登录/token 过期）与 403（无权限）：立即停表，避免 401 风暴与登录框弹出循环
    if (e?.status === 401 || e?.status === 403) stopSupportPolling()
    /* 其他错误（后端未启动等）静默失败，不影响工作台 */
  }
}

/** 停止未读轮询（登出 / token 失效时由认证层显式调用） */
export function stopSupportPolling(): void {
  pollingDisabled = true
  pollRefs = 0
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
}

/** 重新允许未读轮询（重新登录成功后由认证层调用） */
export function resetSupportPolling(): void {
  pollingDisabled = false
  if (pollRefs > 0 && pollTimer === null) {
    void poll()
    pollTimer = window.setInterval(poll, DEFAULT_POLL_MS)
  }
}

/** 开启未读轮询（引用计数：悬浮球与客服页同时挂载也只跑一个定时器） */
export function startSupportPolling(intervalMs = DEFAULT_POLL_MS): () => void {
  pollingDisabled = false // 重新挂载视为一次全新的尝试
  pollRefs += 1
  if (pollTimer === null) {
    void poll()
    pollTimer = window.setInterval(poll, intervalMs)
  }
  return () => {
    pollRefs = Math.max(0, pollRefs - 1)
    if (pollRefs === 0 && pollTimer !== null) {
      window.clearInterval(pollTimer)
      pollTimer = null
    }
  }
}

