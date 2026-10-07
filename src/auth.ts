// 认证（Phase 4.1 前端）：token 管理 + 全局 fetch 包装
// - 登录成功把 JWT 存 localStorage，之后所有 fetch 自动带 Authorization 头
// - 401 → 广播 'auth:unauthorized'（App.vue 跳登录页）
// - 403 → 广播 'auth:forbidden'（仅提示权限不足，绝不弹登录框，避免死循环）
// - 后端默认开放模式（AUTH_REQUIRED=0）：未登录以 guest 身份查询，管理功能需登录

const TOKEN_KEY = 'sqlbot_auth_token'
const USER_KEY = 'sqlbot_auth_user'

import { safeSetItem } from './storage'

export interface AuthUser {
  username: string
  role: string
  display_name?: string
  avatar?: string
  actions?: string[]   // 操作级权限（export/share/download），供前端按钮显隐
  email?: string
  phone?: string
  department?: string
  title?: string
  bio?: string
  preferences?: Record<string, any>
  enabled?: boolean
  last_login?: string
  created_at?: string
}

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export function getUser(): AuthUser | null {
  // 2026-10-06 修复（P0·登录页闪烁）：原实现只读 localStorage 的 USER_KEY，
  // **不要求 TOKEN_KEY 同时存在**。于是任何「有用户记录、没 token」的残留状态
  // （登出被中断 / 旧版本遗留 / 手工清过其中一个 key）都会被判为「已登录」，
  // App.vue 的 enterWorkspace() 直接切到工作台；工作台首个请求带上这个无效/缺失
  // 凭证 → 后端 401 → patchFetch 广播 auth:unauthorized → onUnauthorized 跳登录页。
  // 用户看到的就是「先进工作台，1~2 秒后被弹回登录」。
  // 用户身份必须与 token 成对存在，否则一律视为未登录。
  if (!getToken()) return null
  try {
    const raw = localStorage.getItem(USER_KEY)
    if (!raw) return null
    const u = JSON.parse(raw) as AuthUser
    // 结构性校验：解析成功但字段缺失/类型不对时也当作无身份，
    // 避免半个对象让界面进入「已登录」外观（右上角出现空用户名、菜单错乱）。
    if (!u || typeof u !== 'object' || !u.username) return null
    return u
  } catch {
    return null
  }
}

export function isLoggedIn(): boolean {
  return !!getToken()
}

/**
 * 只解析 JWT 载荷做本地有效性预检（**不验签**，签名只能由后端判定）。
 * 用途：进工作台前先拦掉「token 结构已过期」的情况，避免白进一趟再被 401 弹回。
 * 解析失败或不是标准 JWT（格式/段数不对、payload 不是 JSON）一律视为无效。
 */
export function isTokenFresh(): boolean {
  const t = getToken()
  if (!t) return false
  const parts = t.split('.')
  if (parts.length !== 3) return false
  try {
    // 补 padding：JWT 用的是 base64url（-_ 替�� +/）且省略末尾 =）
    const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    const json = decodeURIComponent(
      atob(b64 + '='.repeat((4 - (b64.length % 4)) % 4))
        .split('')
        .map((ch) => '%' + ('00' + ch.charCodeAt(0).toString(16)).slice(-2))
        .join('')
    )
    const payload = JSON.parse(json)
    if (typeof payload !== 'object' || payload === null) return false
    if (typeof payload.exp === 'number' && payload.exp * 1000 <= Date.now()) return false
    return true
  } catch {
    return false
  }
}

export function isAdmin(): boolean {
  const u = getUser()
  return !!u && !!getToken() && u.role === 'admin'
}

/** 可编辑角色（admin）：可修改数据库数据、维护指标口径 */
export function isEditor(): boolean {
  const u = getUser()
  return !!u && !!getToken() && u.role === 'admin'
}

/** 判断当前用户是否有某操作权限（export/share/download）；admin 全放行，未登录/未配置则拒绝（fail-close） */
export function canDo(action: string): boolean {
  const u = getUser()
  if (!u || !getToken()) return false
  if (u.role === 'admin') return true
  return (u.actions || []).includes(action)
}

/** 改昵称/头像后同步本地用户信息（右上角即时刷新） */
export function updateLocalUser(partial: Partial<AuthUser>): AuthUser | null {
  const cur = getUser()
  if (!cur) return null   // 真正未登录 → null
  const next = { ...cur, ...partial }
  if (!safeSetItem(USER_KEY, JSON.stringify(next))) {
    // 2026-10-03 修复（P2）：原实现写入失败也 return null，而 null 有两种语义
    // （「未登录」与「localStorage 已满」），调用方（App.vue onAccountUpdated）
    // 不区分、一律覆盖 authUser → 全站权限外观塌成未登录：侧栏「管理专区」整组消失、
    // 管理员徽标消失、停在权限页会被弹回总览。用户会以为权限被收了，实际只是
    // 一个本地 key 没写进去，刷新一下又回来。
    // 现在区分语义：写入失败时保留内存态可用（仅提示刷新后生效），只有真未登录才 null。
    console.warn('[auth] 更新本地用户信息失败（本地存储已满，已尝试自动清理）；本次会话内生效，刷新后可能丢失')
    return next
  }
  return next
}


/** 发送邮箱注册验证码的返回结果 */
export interface SendCodeResult {
  /** 重发冷却秒数（默认 60） */
  cooldown: number
  /** 是否已真实投递到邮箱。false = 后端处于调试模式，验证码只打在后端控制台 */
  sent: boolean
  /** 未真实投递的原因（如「MAIL_SMTP_PASSWORD 未填写完整」） */
  hint: string
}

/** 发送邮箱注册验证码 */
export async function sendEmailCode(email: string): Promise<SendCodeResult> {
  const resp = await fetch('/api/auth/email/send-code', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    throw new Error((data as any).detail || `验证码发送失败（HTTP ${resp.status}）`)
  }
  return {
    cooldown: (data as any).cooldown || 60,
    // 后端旧版本没有 sent 字段，此时按「已发送」处理，避免误报调试模式
    sent: (data as any).sent !== false,
    hint: (data as any).mail_hint || '',
  }
}

export async function login(email: string, password: string): Promise<AuthUser> {
  const resp = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    throw new Error((data as any).detail || `登录失败（HTTP ${resp.status}）`)
  }
  const user: AuthUser = {
    username: data.username || email,
    role: data.role || 'viewer',
    display_name: data.display_name || data.username || email,
    avatar: data.avatar || '',
    actions: data.actions || [],
  }
  // 仅在后端确实返回 token 时才写入，避免空串覆盖已有有效 token
  if (data.token) {
    // P0-修复（2026-09-03）：写入受配额保护——历史数据把 localStorage 写满时，
    // 自动清理后再写，杜绝登录抛 "setItem exceeded the quota"
    const okT = safeSetItem(TOKEN_KEY, data.token)
    const okU = safeSetItem(USER_KEY, JSON.stringify(user))
    if (!okT || !okU) {
      throw new Error('本地存储空间不足（已尝试自动清理问析历史）。请清除本站浏览器数据后重新登录。')
    }
  }
  return user
}

/** 注册新账户（邮箱验证码 + 密码）：默认「待授权」角色（pending），管理员授权前无数据权限。注册即登录（后端签发 token）。 */
export async function register(email: string, code: string, password: string, display_name: string): Promise<AuthUser> {
  const resp = await fetch('/api/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, code, password, display_name }),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    throw new Error((data as any).detail || `注册失败（HTTP ${resp.status}）`)
  }
  const user: AuthUser = {
    username: data.username || email,
    role: data.role || 'pending',
    display_name: data.display_name || email,
    avatar: data.avatar || '',
    actions: data.actions || [],
  }
  if (data.token) {
    const okT = safeSetItem(TOKEN_KEY, data.token)
    const okU = safeSetItem(USER_KEY, JSON.stringify(user))
    if (!okT || !okU) {
      throw new Error('本地存储空间不足（已尝试自动清理问析历史）。请清除本站浏览器数据后重新注册。')
    }
  }
  return user
}

/** 登出：清 token/身份；silent=true 时不派发 'auth:logout' 事件。
 *  token 过期等被动登出场景应静默清理（仅清本地凭证 + 弹登录框），
 *  不应走完整登出流程（清本地注册身份 + 踢回 intro 页），否则用户登录成功后回不到工作台。 */
export function logout(silent = false): void {
  try {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
  } catch {
  }
  if (!silent) {
    window.dispatchEvent(new CustomEvent('auth:logout'))
  }
}

/** 认证相关端点：401 是业务错误（密码错等），不应触发登录弹框。
 *  注意：`/api/auth/me` 是会话/身份校验端点，401 表示 token 过期或未登录，
 *  应触发重新登录，故不在此排除列表内。 */
function isAuthEndpoint(url: string): boolean {
  return /\/api\/auth\/(login|register|send-code|verify-password|password)/.test(url)
}

// 包装 window.fetch：自动附带 Authorization 头；401/403 分流广播
export function patchFetch(): void {
  const origFetch = window.fetch.bind(window)
  // 401 去抖：多个并发请求同时 401 时只广播一次，避免重复弹登录框/重复登出打断操作
  let unauthorizedDispatched = false
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : (input as Request).url || ''
    const token = getToken()
    const requestUrl = new URL(url, window.location.origin)
    const isSameOriginApi = requestUrl.origin === window.location.origin && requestUrl.pathname.startsWith('/api')
    const headers = new Headers(init?.headers || {})
    if (token && isSameOriginApi) {
      headers.set('Authorization', `Bearer ${token}`)
    }
    const resp = await origFetch(input, { ...init, headers })
    if (isSameOriginApi && resp.status === 401 && !isAuthEndpoint(url)) {
      // token 过期或后端强制认证 → 清身份并弹登录框（2 秒内只触发一次）
      if (!unauthorizedDispatched) {
        unauthorizedDispatched = true
        window.dispatchEvent(new CustomEvent('auth:unauthorized'))
        window.setTimeout(() => { unauthorizedDispatched = false }, 2000)
      }
    } else if (isSameOriginApi && resp.status === 403) {
      // 权限不足：只提示，不弹登录框（避免"登录了还是 403 → 再弹登录"死循环）
      window.dispatchEvent(new CustomEvent('auth:forbidden'))
    }
    return resp
  }
}
