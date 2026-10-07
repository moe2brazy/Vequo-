<template>
  <div class="ops-app">
    <!-- 视图切换：初始页 ↔ 登录页 ↔ 注册页 ↔ 工作台（同页淡入淡出，不是跳转新页面）
         登录优先：进入工作台前先进「登录页」；没有账号再从登录页底部去注册页 -->
    <Transition name="ops-switch" mode="out-in">
      <!-- ====== 初始页（主题3 OPS 首页） ====== -->
      <OpsLanding v-if="view === 'intro'" key="intro" @enter="enterWorkspace" />

      <!-- ====== 登录页（独立页面，不再是弹窗；未登录时的默认落点） ====== -->
      <OpsLogin
        v-else-if="view === 'login'"
        key="login"
        :initial-email="loginEmail"
        @success="handleLoginSuccess"
        @register="goRegister"
        @cancel="backHome"
      />

      <!-- ====== 注册页（邮箱 + 验证码创建后端账户；入口在登录页的「没有账号？立即注册」） ====== -->
      <OpsRegister v-else-if="view === 'register'" key="register" @complete="handleRegistered" @cancel="backToLogin" />

      <!-- ====== 工作台（主题3 OPS 壳层 + 原项目全部功能；admin-theme = 管理员专属视觉主题） ====== -->
      <div v-else key="workspace" class="workspace-shell" :class="{ 'admin-theme': isAdminUser }">
      <aside class="workspace-sidebar" :class="{ collapsed: sidebarCollapsed }">
        <div class="sidebar-head">
          <button class="workspace-brand" @click="backHome"><b aria-hidden="true"></b><span>Vequo / OPS</span></button>
          <button
            class="sidebar-toggle"
            type="button"
            :aria-label="sidebarCollapsed ? '展开侧栏' : '收起侧栏'"
            :title="sidebarCollapsed ? '展开侧栏' : '收起侧栏'"
            :aria-expanded="!sidebarCollapsed"
            @click="toggleSidebar"
          >
            <span class="toggle-icon">‹</span>
          </button>
        </div>
        <nav>
          <button v-for="item in commonMenuItems" :key="item.key" :class="{ active: currentPage === item.key }" :title="item.label" @click="currentPage = item.key">
            <i><AppIcon :name="item.icon" :size="15" /></i><span>{{ item.label }}</span>
          </button>
          <!-- 管理专区：仅管理员可见，独立分组 + 盾牌标识 + 琥珀金强调，与普通功能区视觉隔离 -->
          <template v-if="adminMenuItems.length">
            <div class="admin-menu-divider" title="管理员专属功能区">
              <AppIcon name="shield" :size="12" /><span class="divider-label">管理专区</span>
            </div>
            <button v-for="item in adminMenuItems" :key="item.key" class="admin-item" :class="{ active: currentPage === item.key }" :title="item.label" @click="currentPage = item.key">
              <i><AppIcon :name="item.icon" :size="15" /></i><span>{{ item.label }}</span>
            </button>
          </template>
        </nav>
        <button class="back-home" :title="sidebarCollapsed ? '回到首页' : ''" @click="backHome"><i>←</i><span>回到首页</span></button>
      </aside>

      <main>
        <header class="workspace-header">
          <!-- 左侧：面包屑 + 页标题（紧凑，对齐源项目） -->
          <div class="hd-title">
            <small>Vequo / {{ pageTitle }}</small>
            <div class="hd-title-main">
              <h1>{{ pageTitle }}</h1>
              <!-- 管理员身份徽标：放在标题区而非右侧工具栏，窄屏（≤700px 工具栏隐藏）下依然可见 -->
              <span v-if="isAdminUser" class="hd-admin-badge" title="当前为管理员账号，拥有系统全部管理权限">
                <AppIcon name="shield" :size="12" /><span>管理员</span>
              </span>
            </div>
          </div>
          <!-- 右侧：问析快捷入口 + 数据在线 + 实时时钟 + 天气 + 用户（对齐源项目） -->
          <div class="workspace-tools">
            <button class="ask-entry" title="去智能问析提问" @click="navigateToAsk('')"><AppIcon name="search" :size="13" />问析</button>
            <span class="hd-online" title="后端服务与数据库连接正常"><i></i>数据在线</span>
            <div class="hd-clock" :title="clockTitle">
              <AppIcon name="clock" :size="13" />
              <span class="hd-clock-time">{{ clockTime }}</span>
              <span class="hd-clock-date">{{ clockDate }}</span>
            </div>
            <div class="hd-weather" :title="weather.desc || '正在加载…'">
              <AppIcon :name="weather.icon" :size="14" />
              <span class="hd-weather-temp">{{ weather.loaded ? weather.temp + '°' : '--°' }}</span>
              <span class="hd-weather-city">{{ weather.city }}</span>
            </div>
            <!-- 用户入口（原项目认证）：未登录 → 弹登录框；已登录 → 下拉账户设置/退出 -->
            <button class="ops-user-button" :class="{ active: showUserMenu, 'admin-avatar': isAdminUser }" :title="userLabel" @click="authUser ? (showUserMenu = !showUserMenu) : goLogin()">
              <img v-if="authUser && authUser.avatar" :src="authUser.avatar" alt="">
              <b v-else>{{ avatarChar }}</b>
            </button>
            <div v-if="showUserMenu" class="ops-user-menu">
              <div class="ops-user-menu-head" :class="{ 'admin-head': isAdminUser }">{{ userLabel }}</div>
              <button v-if="authUser" @click="goAccount">账户设置</button>
              <button @click="doLogout">退出登录</button>
            </div>
          </div>
        </header>

        <section class="workspace-content">
          <!-- 缓存策略：只缓存核心页（overview/ask/data —— 后台任务不中断），
               其余页直接渲染（切走销毁，避免 8 个重页面常驻内存 + 频繁切换销毁重建导致卡死白屏）。
               注意：KeepAlive 内不能有任何注释节点（@vue/compiler-core 把注释也算子节点会编译报错）；
               移除 Transition：out-in 快速连续切换会卡在过渡状态导致白屏。
               KeepAlive 常驻（组件上 v-if 切换而非 KeepAlive 上），避免切到非缓存页时缓存被卸载清空。 -->
          <KeepAlive :max="3">
            <component
              v-if="isCachedPage"
              :is="currentPageComponent"
              :key="currentPage + '-' + dataVersion"
              :class="currentPage === 'ask' ? '' : 'h-full'"
              :initial-question="currentPage === 'ask' ? askQuestion : undefined"
              :initial-scene="currentPage === 'knowledge' ? knowledgeScene : undefined"
              @navigate="navigateTo"
              @navigate-ask="navigateToAsk"
              @question-consumed="askQuestion = ''"
              @account-updated="onAccountUpdated"
              @database-switched="onDatabaseSwitched"
            />
          </KeepAlive>
          <component
            v-if="!isCachedPage"
            :is="currentPageComponent"
            :key="'plain-' + currentPage + '-' + dataVersion"
            :class="currentPage === 'ask' ? '' : 'h-full'"
            :initial-question="currentPage === 'ask' ? askQuestion : undefined"
            :initial-scene="currentPage === 'knowledge' ? knowledgeScene : undefined"
            @navigate="navigateTo"
            @navigate-ask="navigateToAsk"
            @question-consumed="askQuestion = ''"
            @account-updated="onAccountUpdated"
            @database-switched="onDatabaseSwitched"
          />
        </section>
      </main>

      <!-- AI 客服悬浮球（可拖动，客服 AI 会话内记忆 + 常见问答 + 人工客服视图，仅工作台内显示） -->
      <AiAssistantFab
        @navigate-ask="(q) => navigateToAsk(q || '')"
      />
    </div>
    </Transition>

    <!-- ====== 账户设置弹窗（右上角头像 → 账户设置；保存后头像/昵称即时同步） ====== -->
    <ModalDialog
      :visible="accountDialogVisible"
      title="账户设置"
      width="940px"
      maxHeight="88vh"
      @close="accountDialogVisible = false"
    >
      <AccountPage @account-updated="onAccountUpdated" />
    </ModalDialog>

    <!-- ====== 全局提示（403 权限不足等） ====== -->
    <Transition name="fade-slide">
      <div v-if="toast" class="fixed top-6 left-1/2 -translate-x-1/2 z-[1000] px-4 py-2.5 rounded-xl shadow-lg text-sm bg-red-50 border border-red-200 text-red-600">
        {{ toast }}
      </div>
    </Transition>

    <!-- ====== 命令面板（P0-2 对标 Spotter command palette）：Ctrl+K 全局搜索/跳转/提问 ====== -->
    <CommandPalette
      :visible="showPalette"
      @close="showPalette = false"
      @navigate="(p) => { showPalette = false; currentPage = p }"
      @ask="(q) => { showPalette = false; navigateToAsk(q) }"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, defineAsyncComponent, provide } from 'vue'
import OpsLanding from './components/OpsLanding.vue'
import OpsLogin from './components/OpsLogin.vue'
import OpsRegister from './components/OpsRegister.vue'
import CommandPalette from './components/CommandPalette.vue'
import AppIcon from './components/AppIcon.vue'
import AiAssistantFab from './components/AiAssistantFab.vue'
import ModalDialog from './components/ModalDialog.vue'
import PageLoading from './components/PageLoading.vue'
import PageLoadError from './components/PageLoadError.vue'
// 业务页面改为路由级懒加载（defineAsyncComponent）：首屏仅加载当前页 + echarts/vendor，
// 其余页面按需加载，避免 1.08MB 单体 chunk 全量下发。壳层 Ops 组件保持同步（首屏/登录用）。
//
// ⚠ 必须给 defineAsyncComponent 配 loading/error 组件：否则 loader 失败（Vite 重新预构建依赖后
// 旧 chunk 地址失效、网络抖动、页面内运行时报错）时 Vue 会**静默渲染成空白页**，
// 现象就是「点总览 / 智能问析 后内容区一片空白」，而错误只出现在控制台里。
function lazyPage(loader: () => Promise<any>) {
  return defineAsyncComponent({
    loader,
    delay: 200,
    timeout: 30000,
    loadingComponent: PageLoading,
    errorComponent: PageLoadError,
    onError(error, retry, fail, attempts) {
      console.error('[页面加载失败]', error)
      // 前 2 次自动重试（chunk 拉取失败重试往往就好）；仍失败则交给 PageLoadError 展示
      // 错误详情 + 「重新加载」按钮，不再留空白页。
      if (attempts <= 2) retry()
      else fail()
    },
  })
}
const OverviewPage = lazyPage(() => import('./pages/OverviewPage.vue'))
const AskPage = lazyPage(() => import('./pages/AskPage.vue'))
const DataPage = lazyPage(() => import('./pages/DataPage.vue'))
const KnowledgePage = lazyPage(() => import('./pages/KnowledgePage.vue'))
const MetricsPage = lazyPage(() => import('./pages/MetricsPage.vue'))
const PermissionPage = lazyPage(() => import('./pages/PermissionPage.vue'))
const AccountPage = lazyPage(() => import('./pages/AccountPage.vue'))
const SettingsPage = lazyPage(() => import('./pages/SettingsPage.vue'))
// 页面 → 组件映射：KeepAlive 只接受「单个直接子组件」，用 <component :is> 动态切换；
// 切页时组件实例被缓存（deactivated），后台任务（定时器/轮询/流式请求）不中断。
const pageComponents: Record<string, any> = {
  overview: OverviewPage,
  ask: AskPage,
  data: DataPage,
  knowledge: KnowledgePage,
  metrics: MetricsPage,
  permission: PermissionPage,
  account: AccountPage,
  settings: SettingsPage,
}
const currentPageComponent = computed(() => pageComponents[currentPage.value] || OverviewPage)
// 仅核心页进 KeepAlive 缓存（后台任务不中断）；其余页直接渲染，避免 8 页常驻内存卡死
const isCachedPage = computed(() => ['overview', 'ask', 'data'].includes(currentPage.value))
import { logout as apiLogout, getUser, getToken, isAdmin, isEditor, isTokenFresh, updateLocalUser, type AuthUser } from './auth'
// 修复（P0）：未读轮询必须在 token 失效/登出时停止，否则 5s 一次的 401 会反复跳登录页
import { stopSupportPolling, resetSupportPolling } from './supportService'
import { recordLogin } from './stores/loginLog'

// ====== 视图状态（初始页 / 登录页 / 注册页 / 工作台，同页切换） ======
type View = 'intro' | 'login' | 'register' | 'workspace'
const ENTRY_VIEW = new URLSearchParams(window.location.search).get('view')
const view = ref<View>(ENTRY_VIEW === 'register' ? 'register' : ENTRY_VIEW === 'login' ? 'login' : 'intro')
// 注册成功后回登录页时预填的邮箱（登录表单自己持有邮箱/密码，这里只传初始值）
const loginEmail = ref('')

function enterWorkspace() {
  if (view.value === 'workspace') return
  // 已登录账号 → 直接进入工作台
  // 2026-10-06 修复（P0·先进工作台再跳登录）：
  // 原判断只有 `if (authUser.value)`，而 authUser 来自 getUser() —— 它原先
  // 只读 localStorage 的用户记录。于是「有用户记录、token 已失效或缺失」这种
  // 残留态会**直接切到工作台**：工作台首个请求带无效凭证 → 后端 401 →
  // patchFetch 广播 auth:unauthorized → onUnauthorized 把 view 置回 login。
  // 用户观感就是「先进工作台，1~2 秒后被弹回登录页」。
  // 现在进工作台前先本地预检 token（exp 未到期且结构合法），不通过就当未登录。
  if (authUser.value && isTokenFresh()) {
    view.value = 'workspace'
    return
  }
  // token 不可用但用户记录还在 → 清掉残留，避免后续界面误判「已登录」
  if (authUser.value) {
    authUser.value = null
    apiLogout(true) // 静默清理凭证（不派发 auth:logout，避免把 view 置回 intro）
  }
  // 未登录 → 进登录页（登录优先）；没有账号再从登录页底部去注册
  goLogin()
}

/** 进登录页（未登录时的默认落点） */
function goLogin() {
  view.value = 'login'
}

/** 去注册页（登录页底部「没有账号？立即注册」的唯一注册入口） */
function goRegister() {
  view.value = 'register'
}

/** 注册页返回：回登录页（已有账号的用户在这里回到登录），而不是回初始页 */
function backToLogin() {
  view.value = 'login'
}

function handleRegistered(email: string) {
  // 注册已在 OpsRegister 内通过 /api/auth/register 创建后端「待授权」账户，并且已静默
  // 丢弃接口顺带签发的 token。这里回登录页并预填刚注册的邮箱，
  // 由用户手动登录后才进工作台（不再「注册即进工作台」）。
  authUser.value = getUser()
  loginEmail.value = email
  view.value = 'login'
}

/** 登录页登录成功：写登录态 → 记录最近登录 → 恢复未读轮询 → 进工作台 */
function handleLoginSuccess(user: AuthUser) {
  authUser.value = user
  recordLogin({ user: user.username || user.email || '', role: user.role })
  resetSupportPolling() // 修复：重新登录成功后恢复未读轮询
  view.value = 'workspace'
}

function backHome() {
  if (view.value === 'intro') return
  view.value = 'intro'
  window.scrollTo({ top: 0, behavior: 'auto' })
}

// ====== 认证状态（原项目 Phase 4.1） ======
// 2026-10-06 修复（P0·先进工作台再跳登录）：初始登录态除了「有用户记录」
// 还要过 token 预检。getUser() 现在已要求 token 存在，但**过期 token 仍是本地
// 合法存在的** —— 不预检的话，页面一刷新就直接进工作台，随后第一个 API 401
// 又把用户弹回登录页（同样 1~2 秒）。这里在启动时就清理掉过期凭证，
// 让刷新后落在登录页而不是「闪一下工作台」。
const authUser = ref(isTokenFresh() ? getUser() : null)
if (!isTokenFresh()) {
  // 过期/无效 token：静默清掉，避免 getToken() 继续把它附到请求头里
  apiLogout(true)
}
// 供各页面（如 KnowledgePage 的 canDo）建立登录态响应式依赖；登录/登出后按钮显隐自动刷新
provide('authUser', authUser)
const showUserMenu = ref(false)
const toast = ref('')
let toastTimer: number | undefined

const avatarChar = computed(() => {
  const name = authUser.value?.display_name || authUser.value?.username || ''
  return name[0]?.toUpperCase() || 'U'
})
const userLabel = computed(() => {
  if (authUser.value) {
    const roleMap: Record<string, string> = { admin: '管理员', viewer: '普通员工', pending: '待授权' }
    const name = authUser.value.display_name || authUser.value.username
    return `${name}（${roleMap[authUser.value.role] || authUser.value.role}）`
  }
  return '未登录，点击登录'
})

const showToast = (msg: string) => {
  toast.value = msg
  if (toastTimer) window.clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => { toast.value = '' }, 3000)
}

const doLogout = () => {
  stopSupportPolling() // 修复：先停未读轮询再登出，避免旧 token 继续打 401
  apiLogout()
  authUser.value = null
  showUserMenu.value = false
  // 退出登录 = 清除当前登录态，重新进入工作台时需重新登录。
  // 注册账户已持久化在后端（auth_users.json），退出不影响账户本身，可再次用账号密码登录。
  // 强制整页刷新：账号切换后所有权限相关状态（菜单/页面数据/本地角色）从 localStorage
  // 重新读取，保证完全一致，避免 keep-alive 缓存的页面残留登录态数据。
  window.location.reload()
}

// 账户设置改为「右上角头像 → 账户设置」弹窗打开（不再占用左侧导航页面）
const accountDialogVisible = ref(false)

const goAccount = () => {
  showUserMenu.value = false
  accountDialogVisible.value = true
}

const onNavSettings = () => {
  // AccountPage 管理员模式 → 跳转系统设置（数据库配置）；先关闭账户弹窗
  accountDialogVisible.value = false
  showUserMenu.value = false
  currentPage.value = 'settings'
}

const onAccountUpdated = (partial: { display_name?: string; avatar?: string }) => {
  authUser.value = updateLocalUser(partial)
}

const onUnauthorized = () => {
  // token 失效或后端强制认证 → 静默清理身份并进登录页。
  // 关键：不能调会派发 'auth:logout' 的 apiLogout()，否则触发 onLogout 把 view 置回 intro，
  // 用户登录成功后会落到初始页而不是刚才那一页。
  stopSupportPolling() // 修复（P0）：先停未读轮询，否则每 5s 再拿一次 401 → 反复跳登录页
  if (getToken()) apiLogout(true)
  authUser.value = null
  // 停在管理专区页面 → 跳回总览（菜单已随 authUser 更新隐藏）
  if (ADMIN_ONLY_PAGES.includes(currentPage.value) || currentPage.value === 'account') currentPage.value = 'overview'
  // 进登录页重新登录。注意：workspace 外壳会被卸载（页面级 KeepAlive 缓存随之销毁），
  // 但 currentPage 留在 App 状态里，登录成功后会回到同一页。
  view.value = 'login'
}

const onForbidden = () => {
  // 权限不足：只提示，不弹登录框
  showToast('权限不足：当前账号无权限执行该操作')
}

const onLogout = () => {
  stopSupportPolling() // 修复：登出时停止未读轮询，避免以旧身份继续请求
  authUser.value = null
  // 退出登录 = 清除当前登录态，回到初始页重新登录；
  // 注册账户已持久化在后端，退出不影响账户本身，可再次用账号密码登录。
  if (view.value !== 'intro') view.value = 'intro'
  // 退出后若停在管理专区 / 账户设置 → 跳回总览
  if (ADMIN_ONLY_PAGES.includes(currentPage.value) || currentPage.value === 'account') currentPage.value = 'overview'
}

// ====== 命令面板（P0-2）：Ctrl+K / Cmd+K 全局触发 ======
const showPalette = ref(false)
const onGlobalKeydown = (e: KeyboardEvent) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    // 2026-10-01 修复：输入框/文本域内 Ctrl+K 常是用户自定义语义（如 Markdown 链接），
    // 处于可编辑元素时不拦截，避免误触发命令面板
    const t = e.target as HTMLElement | null
    if (t && (t.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName))) return
    e.preventDefault()
    showPalette.value = !showPalette.value
  }
}

// ====== 顶部栏实时时钟（每秒刷新，对齐源项目） ======
const clockTime = ref('--:--:--')
const clockDate = ref('')
const clockTitle = ref('')
const WEEK_CN = ['日', '一', '二', '三', '四', '五', '六']
function updateClock() {
  const d = new Date()
  const pad = (n: number) => n.toString().padStart(2, '0')
  clockTime.value = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  clockDate.value = `${d.getMonth() + 1}/${d.getDate()} 周${WEEK_CN[d.getDay()]}`
  clockTitle.value = d.toLocaleString('zh-CN')
}
let clockTimer: number | undefined

// ====== 顶部栏天气（wttr.in，无需 Key，CORS 友好，对齐源项目） ======
const WEATHER_CITY = '杭州'  // 与用户所在地一致；如需变更可改为系统设置项
const weather = ref({
  loaded: false,
  icon: 'cloud' as string,
  desc: '正在加载…',
  temp: '--',
  city: WEATHER_CITY,
})
function weatherIconFor(desc: string): string {
  const d = (desc || '').toLowerCase()
  if (/晴|clear|sunny|阳光/.test(d)) return 'sun'
  if (/雷|thunder|storm/.test(d)) return 'cloud-lightning'
  if (/雪|snow|冰雹|冰粒|hail|sleet/.test(d)) return 'cloud-snow'
  if (/雨|rain|shower|阵雨|毛毛/.test(d)) return /小雨|毛毛|drizzle/.test(d) ? 'cloud-drizzle' : 'cloud-rain'
  if (/雾|霾|smog|haze|mist|fog|烟/.test(d)) return 'cloud-fog'
  if (/多云|partly|cloudy|cloud|阴|overcast/.test(d)) return 'cloud'
  return 'cloud'
}
// 用 XHR 而非 fetch：全局 fetch 已被 auth.ts 补丁自动加 Authorization 头，
// 跨域请求会触发 CORS 预检（OPTIONS），wttr.in 不支持 → 登录后天气必然失败。
// XHR 不受补丁影响，为简单跨域 GET，无需预检。
function fetchWeatherRaw(): Promise<any> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('GET', `https://wttr.in/${encodeURIComponent(WEATHER_CITY)}?format=j1&lang=zh`, true)
    xhr.timeout = 8000
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try { resolve(JSON.parse(xhr.responseText)) } catch (e) { reject(e) }
      } else reject(new Error('weather http ' + xhr.status))
    }
    xhr.onerror = () => reject(new Error('weather network'))
    xhr.ontimeout = () => reject(new Error('weather timeout'))
    xhr.send()
  })
}
async function fetchWeather() {
  try {
    const data = await fetchWeatherRaw()
    const cur = data?.current_condition?.[0]
    if (!cur) throw new Error('weather empty')
    const desc = cur.weatherDesc?.[0]?.value || ''
    const temp = cur.temp_C
    const area = data?.nearest_area?.[0]?.areaName?.[0]?.value || WEATHER_CITY
    weather.value = {
      loaded: true,
      icon: weatherIconFor(desc),
      desc: `${desc} · 湿度 ${cur.humidity}% · 风速 ${cur.windspeedKmph} km/h`,
      temp,
      city: area,
    }
  } catch {
    weather.value = { loaded: false, icon: 'cloud', desc: '天气加载失败', temp: '--', city: WEATHER_CITY }
  }
}
let weatherTimer: number | undefined

onMounted(() => {
  window.addEventListener('auth:unauthorized', onUnauthorized)
  window.addEventListener('auth:forbidden', onForbidden)
  window.addEventListener('auth:logout', onLogout)
  window.addEventListener('nav:settings', onNavSettings)
  window.addEventListener('keydown', onGlobalKeydown)
  window.addEventListener('ops:retry-page', onRetryPage)
  // 实时时钟 + 天气（对齐源项目顶栏）
  updateClock()
  clockTimer = window.setInterval(updateClock, 1000)
  fetchWeather()
  weatherTimer = window.setInterval(fetchWeather, 15 * 60 * 1000)
})
onUnmounted(() => {
  window.removeEventListener('auth:unauthorized', onUnauthorized)
  window.removeEventListener('auth:forbidden', onForbidden)
  window.removeEventListener('auth:logout', onLogout)
  window.removeEventListener('nav:settings', onNavSettings)
  window.removeEventListener('keydown', onGlobalKeydown)
  window.removeEventListener('ops:retry-page', onRetryPage)
  if (clockTimer) window.clearInterval(clockTimer)
  if (weatherTimer) window.clearInterval(weatherTimer)
})

const currentPage = ref('overview')
const knowledgeScene = ref('production')
const askQuestion = ref('')
// 数据库切换后递增，强制重建所有缓存页面（避免 keep-alive 展示旧库数据）
const dataVersion = ref(0)

// ====== 侧边栏折叠态（用户偏好，刷新后保留，对齐源项目可收纳导航栏） ======
const SIDEBAR_COLLAPSED_KEY = 'ops.sidebar.collapsed'
const sidebarCollapsed = ref(false)
try {
  sidebarCollapsed.value = window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === '1'
} catch { /* localStorage 不可用时默认展开 */ }
function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value
  try { window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, sidebarCollapsed.value ? '1' : '0') } catch { /* ignore */ }
}

// ====== 菜单配置（2026-09-27 起 icon 用项目自带的 Lucide 图标名，走 AppIcon 渲染；
// 此前的 ▦ ◌ ▤ ⌘ ∑ ☰ ⚙ 是 Unicode 几何符号，⌘ 还是 macOS Command 键，Windows 上语义错位） ======
const menuItems = [
  { key: 'overview', label: '总览', icon: 'layout-grid' },
  { key: 'ask', label: '智能问析', icon: 'bot' },
  { key: 'data', label: '数据资源', icon: 'database' },
  { key: 'knowledge', label: '业务知识', icon: 'book-open' },
  { key: 'metrics', label: '指标口径', icon: 'sigma' },
  { key: 'permission', label: '权限管理', icon: 'users' },
  { key: 'settings', label: '系统设置', icon: 'settings' },
]

// 侧边栏菜单分两组：通用功能区（所有人可见）+ 管理专区（数据资源/指标口径/权限管理/系统设置，仅管理员）。
// 注意：isAdmin()/isEditor() 读 localStorage（非响应式），computed 若不引用 authUser
// 会被缓存导致登录/登出后菜单不更新 —— 用 void authUser.value 建立响应式依赖。
// 「数据资源」页是数据库表结构、字段字典、样例数据这类底层内容 —— 业务人员既看不懂，
// 平时也用不上（要数据直接去问析页提问就行）。2026-09-21 把它从通用区挪进管理专区，
// 只有管理员看得见，跟指标口径 / 权限管理 / 系统设置 摆在一起。
const COMMON_MENU_KEYS = ['overview', 'ask', 'knowledge']
const commonMenuItems = computed(() => menuItems.filter((it) => COMMON_MENU_KEYS.includes(it.key)))
const adminMenuItems = computed(() => {
  void authUser.value
  return menuItems.filter((it) => {
    if (it.key === 'settings' || it.key === 'permission' || it.key === 'data') return isAdmin()
    if (it.key === 'metrics') return isEditor()
    return false
  })
})
// 管理专区页面：非管理员一律进不去（侧栏已不显示，但命令面板 / 事件跳转 / 退出登录瞬间都可能带进去）
const ADMIN_ONLY_PAGES = ['data', 'metrics', 'permission', 'settings']
// 页面 key → 中文名（页面标题与拦截提示共用一个来源，避免两处文案对不上）
const PAGE_LABELS: Record<string, string> = {
  overview: '总览',
  ask: '智能问析',
  data: '数据资源',
  knowledge: '业务知识',
  metrics: '指标口径',
  permission: '权限管理',
  account: '账户设置',
  settings: '系统设置',
}
// 管理员账号标识：驱动 admin-theme 主题（深色侧栏 + 琥珀金强调色 + 管理员徽标），
// 与普通员工的浅蓝白界面形成明显视觉区分（纯视觉层，不影响任何功能逻辑）
const isAdminUser = computed(() => {
  void authUser.value
  return isAdmin()
})

const pageTitle = computed(() => PAGE_LABELS[currentPage.value] || '总览')

// 顶栏副标题句已删（2026-09-27）：原「修改昵称、头像与密码」这类描述放在顶栏 h1 里，
// 与页内 page-hero 的标题/说明重复，还把内容推低一截。开场统一为：顶栏只留页名。

const navigateTo = (page: string) => {
  currentPage.value = page
}

// 兜底守卫：管理专区页面只有管理员能进。侧栏已经不显示了，但还有三条路绕得过去 ——
// 命令面板跳转、页面内事件跳转、管理员退出登录后仍停在那一页 —— 在这里统一拦一道。
watch(currentPage, (page) => {
  if (!ADMIN_ONLY_PAGES.includes(page) || isAdmin()) return
  showToast(`「${PAGE_LABELS[page] || page}」属于管理专区，当前账号没有权限查看`)
  currentPage.value = 'overview'
})

const navigateToAsk = (question: string) => {
  askQuestion.value = question
  currentPage.value = 'ask'
}

// 数据库切换后：递增版本号强制重建页面 + 清空知识页缓存场景
const onDatabaseSwitched = () => {
  dataVersion.value += 1
  knowledgeScene.value = 'production'
  currentPage.value = 'overview'
}

// 页面加载失败时，PageLoadError 的「仅重试本页」会广播此事件：
// 递增 dataVersion 会改掉 <component> 的 :key → 组件实例重建 → defineAsyncComponent 重新发起 import。
const onRetryPage = () => { dataVersion.value += 1 }
</script>

<style scoped>
/* 登录/注册相关样式：登录页样式在同目录 OpsLogin.vue，注册页在 OpsRegister.vue；
   原「身份选择页（gate）」与「登录弹窗」均已移除，对应 .gate-* / .ops-login-overlay 样式一并删除。 */
.ops-switch-enter-active {
  transition: opacity 0.28s ease, transform 0.28s ease;
}
.ops-switch-leave-active {
  transition: opacity 0.18s ease;
}
.ops-switch-enter-from {
  opacity: 0;
  transform: translateY(10px);
}
.ops-switch-leave-to {
  opacity: 0;
}
.fade-slide-enter-active {
  transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
}
.fade-slide-leave-active {
  transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
}
.fade-slide-enter-from {
  opacity: 0;
  transform: translateY(12px);
}
.fade-slide-leave-to {
  opacity: 0;
  transform: translateY(-8px);
}
.ops-user-menu {
  position: absolute;
  right: 0;
  top: calc(100% + 8px);
  width: 176px;
  background: #ffffff;
  border: 1px solid #e5e6eb;
  border-radius: 14px;
  padding: 6px;
  box-shadow: 0 14px 40px -10px rgba(16, 24, 40, .16);
  z-index: 60;
}
.ops-user-menu-head {
  padding: 8px 12px;
  font-size: 12px;
  color: #4e5969;
  border-bottom: 1px solid #e5e9e1;
  margin-bottom: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ops-user-menu button {
  display: block;
  width: 100%;
  padding: 9px 12px;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: #1d2129;
  font-size: 13px;
  text-align: left;
}
.ops-user-menu button:hover {
  background: #e8f3ff;
  color: #2E7CF0;
}
</style>
