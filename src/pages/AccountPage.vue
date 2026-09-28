<template>
  <div class="max-w-5xl mx-auto space-y-5">
    <!-- 头部（page-hero）：与全站各页统一的开场壳（2026-09-27 补，此前是唯一没有页头的页面） -->
    <div class="page-hero">
      <h2>账户设置</h2>
      <p class="text-xs text-gray-500 mt-1">
        头像、昵称与登录密码都在这一页改；改完即时生效，不用重新登录。
      </p>
    </div>
    <!-- ====== 基本信息 ====== -->
    <div class="bg-white rounded-xl border border-gray-100 shadow-sm p-6">
      <h3 class="text-sm font-bold text-gray-800 mb-4">基本信息</h3>
      <div class="flex items-start gap-5">
        <!-- 头像 -->
        <div class="flex flex-col items-center gap-2">
          <div class="w-20 h-20 rounded-full bg-primary flex items-center justify-center text-white text-2xl font-bold shadow-md overflow-hidden">
            <img v-if="avatarPreview" :src="avatarPreview" class="w-full h-full object-cover" alt="头像" />
            <span v-else>{{ avatarChar }}</span>
          </div>
          <label class="text-xs text-primary cursor-pointer hover:underline">
            上传头像
            <input type="file" accept="image/*" class="hidden" @change="onAvatarChange" />
          </label>
          <button v-if="avatarPreview" class="text-xs text-gray-400 hover:text-red-500" @click="clearAvatar">清除</button>
        </div>
        <!-- 表单 -->
        <div class="flex-1 space-y-3">
          <div>
            <label class="text-xs text-gray-400">用户名（不可修改）</label>
            <input :value="user?.username || ''" disabled class="w-full mt-1 px-3 py-2 bg-gray-50 border border-gray-200 rounded-lg text-sm text-gray-500" />
          </div>
          <div>
            <label class="text-xs text-gray-400">昵称</label>
            <input v-model="displayName" placeholder="输入昵称" class="w-full mt-1 px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-primary/40" />
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="text-xs text-gray-400">邮箱</label>
              <input v-model="profileExtra.email" placeholder="name@example.com" class="w-full mt-1 px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-primary/40" />
            </div>
            <div>
              <label class="text-xs text-gray-400">手机</label>
              <input v-model="profileExtra.phone" placeholder="手机号" class="w-full mt-1 px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-primary/40" />
            </div>
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="text-xs text-gray-400">部门</label>
              <input v-model="profileExtra.department" placeholder="如：质量部" class="w-full mt-1 px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-primary/40" />
            </div>
            <div>
              <label class="text-xs text-gray-400">职位</label>
              <input v-model="profileExtra.title" placeholder="如：生产计划主管" class="w-full mt-1 px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-primary/40" />
            </div>
          </div>
          <div>
            <label class="text-xs text-gray-400">个人简介</label>
            <textarea v-model="profileExtra.bio" placeholder="一句话介绍自己" rows="2" class="w-full mt-1 px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-primary/40"></textarea>
          </div>
          <div class="flex items-center gap-3">
            <button class="px-4 py-2 text-sm text-white bg-primary hover:bg-primary-dark rounded-lg disabled:opacity-50" :disabled="saving" @click="saveProfile">{{ saving ? '保存中…' : '保存资料' }}</button>
            <span class="text-xs text-gray-400">角色：{{ roleLabel }}</span>
            <span v-if="saveMsg" class="text-xs" :class="saveMsgOk ? 'text-blue-500' : 'text-red-500'">{{ saveMsg }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- ====== 修改密码 ====== -->
    <div class="bg-white rounded-xl border border-gray-100 shadow-sm p-6">
      <div class="flex items-center justify-between">
        <div>
          <h3 class="text-sm font-bold text-gray-800">修改密码</h3>
          <p class="text-xs text-gray-400 mt-1">定期更新密码可提升账号安全性，修改后将重新登录</p>
        </div>
        <button class="px-4 py-2 text-sm text-white bg-primary hover:bg-primary-dark rounded-lg" @click="openPwModal">修改密码</button>
      </div>
    </div>

    <!-- ====== 我的痕迹：收藏 / 最近登录 ====== -->
    <div class="bg-white rounded-xl border border-gray-100 shadow-sm p-6">
      <h3 class="text-sm font-bold text-gray-800 mb-4">我的痕迹</h3>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
        <!-- 收藏 -->
        <div>
          <h4 class="text-xs font-bold text-gray-600 mb-2">我的收藏（{{ favorites.length }}）</h4>
          <div v-if="favorites.length" class="space-y-1 max-h-56 overflow-y-auto">
            <div v-for="f in favorites" :key="`${f.kind}:${f.key}`" class="group flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-gray-50 text-xs">
              <AppIcon :name="favIcon(f.kind)" :size="13" class="text-gray-400 shrink-0" />
              <span class="flex-1 min-w-0 truncate text-gray-700">
                <span class="text-gray-400 mr-1">{{ favTypeLabel(f.kind) }}</span>{{ f.label }}
                <span v-if="f.scene" class="text-gray-300 ml-1">· {{ f.scene }}</span>
              </span>
              <button class="text-gray-300 hover:text-red-500 opacity-0 group-hover:opacity-100 transition" title="取消收藏" @click="removeFavoriteLocal(f.kind, f.key)"><AppIcon name="x" :size="12" /></button>
            </div>
          </div>
          <div v-else class="text-xs text-gray-400">暂无收藏，去业务知识页对对象/指标/规则点星标即可加入。</div>
          <button v-if="favorites.length" class="mt-2 text-[11px] text-gray-400 hover:text-red-500" @click="clearAllFavorites">清空收藏</button>
        </div>
        <!-- 最近登录 -->
        <div>
          <div class="flex items-center justify-between mb-2">
            <h4 class="text-xs font-bold text-gray-600">最近登录</h4>
            <span class="text-[10px] text-gray-300">仅本机</span>
          </div>
          <div v-if="loginEntries.length" class="space-y-1 max-h-56 overflow-y-auto">
            <div v-for="h in loginEntries" :key="h.ts" class="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-gray-50 text-xs">
              <AppIcon name="log-in" :size="13" class="text-gray-400 shrink-0" />
              <span class="flex-1 truncate text-gray-700">
                <span class="font-medium">{{ h.user }}</span>
                <span v-if="h.role" class="ml-1 text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-500">{{ roleLabelOf(h.role) }}</span>
              </span>
              <span class="text-[11px] text-gray-400 shrink-0">{{ formatLoginTime(h.ts) }}</span>
            </div>
          </div>
          <div v-else class="text-xs text-gray-400">暂无登录记录。退出再登录一次后这里会显示。</div>
          <button v-if="loginEntries.length" class="mt-2 text-[11px] text-gray-400 hover:text-red-500" @click="clearLoginLog">清空记录</button>
        </div>
      </div>
    </div>

    <!-- ====== 管理入口（仅管理员可见；原先的「进入管理员模式」二次确认已移除） ====== -->
    <div v-if="isAdmin" class="bg-white rounded-xl border border-gray-100 shadow-sm p-6">
      <div class="flex items-center justify-between mb-3">
        <h3 class="text-sm font-bold text-gray-800">管理</h3>
      </div>
      <p class="text-xs text-gray-500 mb-3">
        成员与角色、数据权限、审批与审计都在「权限管理」页；这里只留数据库连接配置。
      </p>
      <div class="flex gap-2">
        <button class="px-3 py-1.5 text-xs text-[#1d2129] border border-[#c9cdd4] bg-white hover:bg-[#e8f3ff] hover:text-[#2E7CF0] hover:border-[#4D9EFF] rounded-lg" @click="goSettings">数据库配置</button>
      </div>
    </div>

    <!-- ====== 退出登录 ====== -->
    <div class="text-center pt-2">
      <button class="px-6 py-2 text-sm text-red-500 border border-red-200 rounded-lg hover:bg-red-50" @click="doLogout">退出登录</button>
    </div>

    <!-- ====== 修改密码弹窗 ====== -->
    <div v-if="showPw" class="fixed inset-0 z-[999] flex items-center justify-center bg-black/30 backdrop-blur-sm" @click.self="showPw = false">
      <div class="w-96 bg-white rounded-2xl shadow-2xl p-6">
        <h3 class="text-sm font-bold text-gray-800 mb-1">修改密码</h3>
        <p class="text-xs text-gray-400 mb-4">输入旧密码与新密码以更新账号密码</p>
        <div class="space-y-3">
          <input v-model="pw.old" type="password" placeholder="旧密码" class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-primary/40" />
          <input v-model="pw.n1" type="password" placeholder="新密码（至少 6 位）" class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-primary/40" />
          <input v-model="pw.n2" type="password" placeholder="确认新密码" class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-primary/40" @keyup.enter="changePassword" />
          <p v-if="pwMsg" class="text-xs" :class="pwMsgOk ? 'text-blue-500' : 'text-red-500'">{{ pwMsg }}</p>
        </div>
        <div class="flex gap-2 mt-4">
          <button class="flex-1 px-3 py-2 text-sm text-[#1d2129] border border-[#c9cdd4] bg-white hover:bg-[#e8f3ff] hover:text-[#2E7CF0] hover:border-[#4D9EFF] rounded-lg" @click="showPw = false">取消</button>
          <button class="flex-1 px-3 py-2 text-sm text-white bg-primary hover:bg-primary-dark rounded-lg disabled:opacity-50" :disabled="pwSaving" @click="changePassword">{{ pwSaving ? '提交中…' : '确认修改' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import { getUser, isAdmin as checkIsAdmin, logout, updateLocalUser } from '../auth'
import { knowledgeFavorites, removeFavorite as removeFavoriteStore, clearFavorites } from '../stores/favorites'
import { loginLog, formatRelative, clearLoginLog } from '../stores/loginLog'

const emit = defineEmits<{ (e: 'account-updated', payload: { display_name?: string; avatar?: string }): void }>()

const user = ref(getUser())
const isAdmin = computed(() => checkIsAdmin())

const displayName = ref(user.value?.display_name || user.value?.username || '')
const avatar = ref(user.value?.avatar || '')
const avatarPreview = ref(user.value?.avatar || '')
const profileExtra = ref({
  email: user.value?.email || '',
  phone: user.value?.phone || '',
  department: user.value?.department || '',
  title: user.value?.title || '',
  bio: user.value?.bio || '',
})
const saving = ref(false)
const saveMsg = ref('')
const saveMsgOk = ref(true)

const avatarChar = computed(() => (displayName.value || user.value?.username || '')[0]?.toUpperCase() || 'U')
const roleLabel = computed(() => {
  const map: Record<string, string> = { admin: '管理员', viewer: '普通员工', guest: '游客' }
  return map[user.value?.role || ''] || user.value?.role || ''
})

// ── 资料保存 ──
// 修复：原实现只校验体积，不校验类型/内容 —— accept="image/*" 只是文件选择器的提示，
// 用户可切到"所有文件"或改扩展名绕过，于是任意文件会被读成 data URL 落库+落 localStorage。
// 一旦后续出现非 <img> 的渲染路径（如报表内联 HTML）即形成存储型 XSS。此处做三重校验。
const ALLOWED_AVATAR_TYPES = ['image/png', 'image/jpeg', 'image/webp', 'image/gif']
const onAvatarChange = async (e: Event) => {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (!file) return
  if (!ALLOWED_AVATAR_TYPES.includes(file.type)) {
    saveMsg.value = '仅支持 PNG / JPG / WEBP / GIF 格式的图片'
    saveMsgOk.value = false
    return
  }
  if (file.size > 200 * 1024) {
    saveMsg.value = '头像图片不能超过 200KB'
    saveMsgOk.value = false
    return
  }
  // 进一步验证真的能被解码为图片（防伪造 MIME 的任意文件）
  try {
    await createImageBitmap(file)
  } catch {
    saveMsg.value = '该文件不是有效图片'
    saveMsgOk.value = false
    return
  }
  const reader = new FileReader()
  reader.onload = () => {
    avatar.value = String(reader.result || '')
    avatarPreview.value = avatar.value
    saveMsg.value = ''
  }
  reader.readAsDataURL(file)
}

const clearAvatar = () => {
  avatar.value = ''
  avatarPreview.value = ''
}

const saveProfile = async () => {
  const name = displayName.value.trim()
  if (!name) {
    saveMsg.value = '昵称不能为空'
    saveMsgOk.value = false
    return
  }
  saving.value = true
  saveMsg.value = ''
  try {
    const resp = await fetch('/api/auth/me', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display_name: name, avatar: avatar.value, ...profileExtra.value }),
    })
    const data = await resp.json().catch(() => ({}))
    if (!resp.ok) throw new Error((data as any).detail || '保存失败')
    // 同步本地 + 通知 App.vue 更新右上角
    user.value = updateLocalUser({ display_name: name, avatar: avatar.value, ...profileExtra.value })
    emit('account-updated', { display_name: name, avatar: avatar.value })
    saveMsg.value = '资料已保存'
    saveMsgOk.value = true
  } catch (e: any) {
    saveMsg.value = e?.message || '保存失败'
    saveMsgOk.value = false
  } finally {
    saving.value = false
  }
}

// ── 修改密码 ──
const pw = ref({ old: '', n1: '', n2: '' })
const pwMsg = ref('')
const pwMsgOk = ref(true)
const pwSaving = ref(false)
const showPw = ref(false)

const openPwModal = () => {
  pw.value = { old: '', n1: '', n2: '' }
  pwMsg.value = ''
  pwMsgOk.value = true
  showPw.value = true
}

const changePassword = async () => {
  pwMsg.value = ''
  if (!pw.value.old || !pw.value.n1) {
    pwMsg.value = '请填写旧密码和新密码'
    pwMsgOk.value = false
    return
  }
  if (pw.value.n1 !== pw.value.n2) {
    pwMsg.value = '两次输入的新密码不一致'
    pwMsgOk.value = false
    return
  }
  if (pw.value.n1.length < 6) {
    pwMsg.value = '新密码长度不能少于 6 位'
    pwMsgOk.value = false
    return
  }
  pwSaving.value = true
  try {
    const resp = await fetch('/api/auth/password', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old_password: pw.value.old, new_password: pw.value.n1 }),
    })
    const data = await resp.json().catch(() => ({}))
    if (!resp.ok) throw new Error((data as any).detail || '修改失败')
    pwMsg.value = '密码已修改，正在退出登录…'
    pwMsgOk.value = true
    setTimeout(() => {
      logout()
      window.location.reload()
    }, 1200)
  } catch (e: any) {
    pwMsg.value = e?.message || '修改失败'
    pwMsgOk.value = false
  } finally {
    pwSaving.value = false
  }
}

// ── 我的痕迹：收藏 / 最近登录（前端 localStorage） ──
const favorites = knowledgeFavorites
const loginEntries = loginLog
const removeFavoriteLocal = (kind: string, key: string) => removeFavoriteStore(kind, key)
const clearAllFavorites = () => { if (window.confirm('确认清空所有收藏？')) clearFavorites() }
const formatLoginTime = (ts: number) => formatRelative(ts)
const favIcon = (kind: string) => {
  const m: Record<string, string> = {
    '业务对象': 'table', '业务指标': 'bar-chart-2', '业务规则': 'ruler',
    '分析主题': 'target', '术语': 'book-open', object: 'table', metric: 'bar-chart-2',
    rule: 'ruler', topic: 'target', term: 'book-open',
  }
  return m[kind] || 'star'
}
const favTypeLabel = (kind: string) => {
  const m: Record<string, string> = {
    '业务对象': '对象', '业务指标': '指标', '业务规则': '规则',
    '分析主题': '主题', '术语': '术语',
    object: '对象', metric: '指标', rule: '规则', topic: '主题', term: '术语',
  }
  return m[kind] || kind
}
const roleLabelOf = (role: string) => ({ admin: '管理员', viewer: '普通员工' } as Record<string,string>)[role] || role

// ── 其他 ──
const goSettings = () => {
  // 跳转到系统设置页（数据库配置在设置页）
  window.dispatchEvent(new CustomEvent('nav:settings'))
}

const doLogout = () => {
  logout()
  window.location.reload()
}

onMounted(() => {
  // 我的收藏 / 最近登录 由 stores 初始化时从 localStorage 读取（响应式）。
  // 管理相关的数据加载已随「管理员模式」一并移除——成员/权限/审批/审计都在权限管理页。
})
</script>
