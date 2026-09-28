<template>
  <div class="space-y-4">
    <div class="page-hero rounded-xl border border-blue-100 bg-blue-50/50 p-4 text-sm text-gray-600">
      支持切换不同的数据库软件（PostgreSQL / MySQL）和不同的数据库工作区。切换成功后，数据表、字段、行数、表关系和分析主题会从新数据库重新读取。
    </div>

    <!-- 运行观测（编译覆盖率 / 成功率 / 耗时 / LLM 兜底率） -->
    <div class="p-4 border border-gray-200 rounded-lg">
      <div class="flex items-center justify-between mb-3">
        <div>
          <div class="text-xs font-medium text-gray-500">运行观测</div>
          <div class="text-[10px] text-gray-400 mt-0.5">近 {{ opsView.window }} 轮查询 · 编译覆盖率低 = 可到指标管理采纳候选口径</div>
        </div>
      </div>
      <div v-if="opsView.total > 0" class="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div class="rounded-lg bg-gray-50 px-4 py-3">
          <div class="text-[11px] text-gray-500">编译覆盖率</div>
          <div class="text-xl font-semibold text-gray-800">{{ Math.round(opsView.compile_rate * 100) }}%</div>
          <div class="text-[10.5px] text-gray-400">确定性编译命中占比</div>
        </div>
        <div class="rounded-lg bg-gray-50 px-4 py-3">
          <div class="text-[11px] text-gray-500">成功率</div>
          <div class="text-xl font-semibold text-gray-800">{{ Math.round(opsView.ok_rate * 100) }}%</div>
          <div class="text-[10.5px] text-gray-400">本轮窗口执行成功</div>
        </div>
        <div class="rounded-lg bg-gray-50 px-4 py-3">
          <div class="text-[11px] text-gray-500">P95 耗时</div>
          <div class="text-xl font-semibold text-gray-800">{{ fmtMs(opsView.p95_ms) }}</div>
          <div class="text-[10.5px] text-gray-400">95% 查询在此之内</div>
        </div>
        <div class="rounded-lg bg-gray-50 px-4 py-3">
          <div class="text-[11px] text-gray-500">LLM 兜底率</div>
          <div class="text-xl font-semibold text-gray-800">{{ Math.round(opsView.llm_rate * 100) }}%</div>
          <div class="text-[10.5px] text-gray-400">未命中编译走 LLM</div>
        </div>
      </div>
      <div v-else class="text-xs text-gray-400 py-2">暂无观测数据（执行查询后生成）</div>
    </div>

    <!-- 数据库软件类型 + 连接配置 -->
    <div class="grid grid-cols-2 gap-4">
      <div class="p-4 border border-gray-200 rounded-lg">
        <label class="text-xs text-gray-400 font-medium">数据库软件类型</label>
        <select
          v-model="form.db_type"
          @change="onDbTypeChange"
          class="w-full mt-1 px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-primary/50 bg-white"
        >
          <option value="postgresql">PostgreSQL</option>
          <option value="mysql">MySQL</option>
          <option value="mssql">SQL Server</option>
          <option value="snowflake">Snowflake</option>
          <option value="clickhouse">ClickHouse</option>
          <option value="bigquery">BigQuery</option>
        </select>
      </div>
      <div class="p-4 border border-gray-200 rounded-lg">
        <label class="text-xs text-gray-400 font-medium">主机地址</label>
        <input v-model="form.host" type="text" placeholder="localhost"
               class="w-full mt-1 px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-primary/50" />
      </div>
      <div class="p-4 border border-gray-200 rounded-lg">
        <label class="text-xs text-gray-400 font-medium">端口</label>
        <input v-model.number="form.port" type="number" placeholder="5432"
               class="w-full mt-1 px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-primary/50" />
      </div>
      <div class="p-4 border border-gray-200 rounded-lg">
        <label class="text-xs text-gray-400 font-medium">数据库名称</label>
        <input v-model="form.database" type="text" placeholder="enterprise_data"
               class="w-full mt-1 px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-primary/50" />
      </div>
      <div class="p-4 border border-gray-200 rounded-lg">
        <label class="text-xs text-gray-400 font-medium">用户名</label>
        <input v-model="form.user" type="text" placeholder="postgres"
               class="w-full mt-1 px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-primary/50" />
      </div>
      <div class="p-4 border border-gray-200 rounded-lg">
        <label class="text-xs text-gray-400 font-medium">密码</label>
        <input v-model="form.password" type="password" placeholder="请输入数据库密码"
               class="w-full mt-1 px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-primary/50" />
      </div>
    </div>

    <div class="flex items-center gap-3">
      <button @click="testConnection" :disabled="loading" class="px-4 py-2 border border-gray-200 text-gray-600 rounded-lg text-sm hover:bg-gray-50 transition disabled:opacity-50">
        测试连接
      </button>
      <button @click="saveDbConfig" :disabled="loading" class="px-4 py-2 bg-primary text-white rounded-lg text-sm hover:opacity-90 transition disabled:opacity-50">
        {{ loading ? '处理中...' : '保存配置' }}
      </button>
      <span v-if="message" class="text-sm" :class="success ? 'text-blue-600' : 'text-red-600'">{{ message }}</span>
    </div>

    <!-- 数据库工作区管理 -->
    <div class="p-4 border border-gray-200 rounded-lg">
      <div class="text-xs font-medium text-gray-500 mb-3">数据库工作区</div>
      <div class="flex items-center gap-2 flex-wrap">
        <select v-model="activeDatabase" class="px-2 py-1.5 border border-gray-300 rounded text-sm max-w-52">
          <option v-for="db in databases" :key="db" :value="db">库：{{ db }}</option>
        </select>
        <button @click="switchWorkspace" :disabled="!activeDatabase || activeDatabase === activeName" class="px-3 py-1.5 bg-primary text-white rounded text-sm hover:opacity-90 disabled:opacity-40">
          切换工作区
        </button>
        <input v-model="newDatabaseName" placeholder="新建数据库名（可选）" class="px-2 py-1.5 border border-gray-300 rounded text-sm" />
        <button @click="createWorkspace" class="px-3 py-1.5 border border-gray-300 rounded text-sm hover:bg-gray-50">新建并切换</button>
        <select v-model="databaseToDelete" class="px-2 py-1.5 border border-gray-300 rounded text-sm max-w-52 ml-4">
          <option value="" disabled>删除非当前数据库…</option>
          <option v-for="db in databases.filter(name => name !== activeName)" :key="db" :value="db">{{ db }}</option>
        </select>
        <button @click="deleteWorkspace" :disabled="!databaseToDelete" class="px-2.5 py-1.5 rounded text-sm text-red-600 border border-red-200 hover:bg-red-50 disabled:opacity-40 disabled:cursor-not-allowed">
          删除
        </button>
      </div>
      <div v-if="workspaceMessage" class="mt-3 text-sm" :class="workspaceSuccess ? 'text-blue-600' : 'text-red-600'">
        {{ workspaceMessage }}
      </div>
    </div>

    <!-- 多数据源管理（P1-1 对标 Spotter automatic model selection 渐进式落地） -->
    <div class="p-4 border border-gray-200 rounded-lg">
      <div class="flex items-center justify-between mb-3">
        <div>
          <div class="text-xs font-medium text-gray-500">多数据源</div>
          <div class="text-[10px] text-gray-400 mt-0.5">注册多个数据源，一键切换；跨源搜索可发现「表在哪个源」</div>
        </div>
      </div>
      <!-- 已注册源列表 -->
      <div v-if="sourcesLoading" class="text-xs text-gray-400 py-2">加载中…</div>
      <div v-else class="space-y-2">
        <div v-for="s in sources" :key="s.id" class="flex items-center gap-3 border border-gray-100 rounded-lg px-3 py-2">
          <span class="text-[10px] px-1.5 py-0.5 rounded shrink-0"
                :class="s.is_active ? 'bg-blue-50 text-blue-600' : 'bg-gray-100 text-gray-500'">
            {{ s.is_active ? '当前' : '备用' }}
          </span>
          <div class="min-w-0 flex-1">
            <div class="text-xs text-gray-700 font-medium truncate">{{ s.name }}</div>
            <div class="text-[10px] text-gray-400 truncate">{{ s.db_type }} · {{ s.host }}:{{ s.port }} / {{ s.database }}（{{ s.user }}）</div>
          </div>
          <button v-if="!s.is_active" @click="activateSource(s)" :disabled="savingSrc"
                  class="text-[11px] px-2.5 py-1 rounded border border-gray-200 text-gray-600 hover:bg-gray-50 disabled:opacity-40 shrink-0">
            设为当前
          </button>
          <button v-if="!s.is_active" @click="removeSource(s)" :disabled="savingSrc"
                  class="text-[11px] px-2 py-1 rounded border border-red-100 text-red-500 hover:bg-red-50 disabled:opacity-40 shrink-0">
            删除
          </button>
        </div>
      </div>
      <p v-if="srcMessage" class="mt-2 text-xs" :class="srcSuccess ? 'text-blue-600' : 'text-red-600'">{{ srcMessage }}</p>
      <!-- 把当前连接配置保存为命名源 -->
      <div class="flex items-center gap-2 mt-3 pt-3 border-t border-gray-100">
        <input v-model="newSourceName" type="text" placeholder="源名称，如：生产库 / 测试库"
               class="flex-1 px-2.5 py-1.5 border border-gray-300 rounded text-sm focus:outline-none focus:border-primary/50" />
        <button @click="addSource" :disabled="savingSrc || !newSourceName.trim()"
                class="px-3 py-1.5 bg-primary text-white rounded text-sm hover:opacity-90 disabled:opacity-40 shrink-0">
          保存当前连接为新数据源
        </button>
      </div>
      <p class="text-[10px] text-gray-400 mt-2">新源连接信息取上方「数据库软件类型 + 连接配置」表单；保存前会先测试连接。</p>
    </div>

    <!-- 反馈复核队列（仅管理员）：用户纠错反馈 → 复核/驳回，形成口径持续改进闭环 -->
    <div v-if="isAdmin()" class="p-4 border border-gray-200 rounded-lg">
      <div class="flex items-center justify-between mb-3">
        <div class="text-xs font-medium text-gray-500">反馈复核队列</div>
        <div class="flex items-center gap-2">
          <select v-model="fbFilter" @change="loadFeedbackQueue" class="px-2 py-1 border border-gray-300 rounded text-xs bg-white">
            <option value="">全部</option>
            <option value="pending">待复核</option>
            <option value="resolved">已修正</option>
            <option value="rejected">已驳回</option>
          </select>
          <button @click="loadFeedbackQueue" class="px-2 py-1 border border-gray-300 rounded text-xs hover:bg-gray-50">刷新</button>
        </div>
      </div>
      <div v-if="fbLoading" class="text-xs text-gray-400 py-3">加载中…</div>
      <div v-else-if="!fbItems.length" class="text-xs text-gray-400 py-3">暂无反馈，用户点击「<span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" /> <path d="M12 9v4" /> <path d="M12 17h.01" /> </svg></span> 纠错反馈」后进入这里复核。</div>
      <div v-else class="space-y-2">
        <div v-for="it in fbItems" :key="it.id" class="border border-gray-100 rounded-lg p-3">
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="text-xs font-medium text-gray-700">「{{ it.query }}」</span>
                <span class="text-[10px] px-1.5 py-0.5 rounded-full" :class="it.status === 'pending' ? 'bg-amber-50 text-amber-700' : it.status === 'resolved' ? 'bg-blue-50 text-blue-600' : 'bg-gray-100 text-gray-500'">
                  {{ it.status === 'pending' ? '待复核' : it.status === 'resolved' ? '已修正' : '已驳回' }}
                </span>
                <span class="text-[10px] px-1.5 py-0.5 rounded-full bg-red-50 text-red-600">{{ it.feedback_type }}</span>
              </div>
              <div class="text-xs text-gray-500 mt-1.5">反馈人：{{ it.user }} · {{ new Date(it.ts * 1000).toLocaleString('zh-CN') }}</div>
              <div v-if="it.note" class="text-xs text-gray-600 mt-1 bg-gray-50 rounded px-2 py-1">{{ it.note }}</div>
              <div v-if="it.sql" class="text-[10px] font-mono text-blue-700 mt-1 bg-gray-50 rounded px-2 py-1 break-all">{{ it.sql.slice(0, 180) }}</div>
            </div>
            <div v-if="it.status === 'pending'" class="flex gap-1.5 shrink-0">
              <button @click="resolveFeedback(it, 'resolved')" class="px-2 py-1 text-[11px] bg-blue-600 text-white rounded hover:bg-blue-700">标记已修正</button>
              <button @click="resolveFeedback(it, 'rejected')" class="px-2 py-1 text-[11px] border border-gray-300 text-gray-600 rounded hover:bg-gray-50">驳回</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { isAdmin } from '../auth'

const emit = defineEmits<{ (event: 'database-switched'): void }>()
const form = reactive({ db_type: 'postgresql', host: '', port: 5432, database: '', user: '', password: '' })
const loading = ref(false)
const success = ref(false)
const message = ref('')

// 统一请求：先判 res.ok 再取 json，失败抛错（否则 401/403/500 的 JSON 体被当成功，页面只显示空白红字）
async function fetchJson(url: string, init?: RequestInit): Promise<any> {
  const res = await fetch(url, init)
  let data: any = {}
  try { data = await res.json() } catch { /* 非 JSON 响应体 */ }
  if (!res.ok) {
    const detail = data?.detail || data?.message || `HTTP ${res.status}`
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return data
}

// 运行观测（编译覆盖率 / 成功率 / P95 耗时 / LLM 兜底率）
const opsView = ref({ total: 0, window: 500, compile_rate: 0, ok_rate: 0, llm_rate: 0, p95_ms: 0 })
const loadOpsView = async () => {
  try {
    const data = await fetchJson('/api/ops/overview')
    if (data && data.total > 0) {
      opsView.value = { ...opsView.value, ...data }
    }
  } catch { /* 观测不可用不阻塞页面 */ }
}
const fmtMs = (ms: number) => {
  if (!ms) return '0ms'
  return ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : Math.round(ms) + 'ms'
}

// 数据库工作区
const databases = ref<string[]>([])
const activeName = ref('')
const activeDatabase = ref('')
const newDatabaseName = ref('')
const databaseToDelete = ref('')
const workspaceMessage = ref('')
const workspaceSuccess = ref(false)

// 多数据源管理（P1-1）
const sources = ref<any[]>([])
const sourcesLoading = ref(false)
const newSourceName = ref('')
const savingSrc = ref(false)
const srcMessage = ref('')
const srcSuccess = ref(false)

const loadSources = async () => {
  sourcesLoading.value = true
  try {
    const data = await fetchJson('/api/database/sources')
    sources.value = data.sources || []
  } catch {
    sources.value = []
  } finally {
    sourcesLoading.value = false
  }
}

const addSource = async () => {
  const name = newSourceName.value.trim()
  if (!name) return
  // 掩码占位（后端不回显明文）不能作为新源密码——连接测试必失败，直接提示
  if (form.password === '********') {
    srcSuccess.value = false
    srcMessage.value = '新增数据源需要填写真实密码（上方表单当前为掩码占位）'
    return
  }
  savingSrc.value = true
  srcMessage.value = ''
  try {
    const res = await fetch('/api/database/sources', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, ...form }),
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data?.detail || '保存失败')
    srcSuccess.value = true
    srcMessage.value = `数据源「${name}」已保存（连接测试通过）`
    newSourceName.value = ''
    await loadSources()
  } catch (e: any) {
    srcSuccess.value = false
    srcMessage.value = e?.message || '保存失败'
  } finally {
    savingSrc.value = false
  }
}

const activateSource = async (s: any) => {
  if (!window.confirm(`确认切换到数据源「${s.name}」？切换后将从该源读取全部数据。`)) return
  savingSrc.value = true
  srcMessage.value = ''
  try {
    const res = await fetch(`/api/database/sources/${s.id}/activate`, { method: 'POST' })
    const data = await res.json()
    if (!res.ok) throw new Error(data?.detail || '切换失败')
    srcSuccess.value = true
    srcMessage.value = `已切换到「${s.name}」`
    await loadSources()
    emit('database-switched')
  } catch (e: any) {
    srcSuccess.value = false
    srcMessage.value = e?.message || '切换失败'
  } finally {
    savingSrc.value = false
  }
}

const removeSource = async (s: any) => {
  if (!window.confirm(`确认删除数据源「${s.name}」？`)) return
  savingSrc.value = true
  srcMessage.value = ''
  try {
    const res = await fetch(`/api/database/sources/${s.id}`, { method: 'DELETE' })
    const data = await res.json()
    if (!res.ok) throw new Error(data?.detail || '删除失败')
    srcSuccess.value = true
    srcMessage.value = `已删除「${s.name}」`
    await loadSources()
  } catch (e: any) {
    srcSuccess.value = false
    srcMessage.value = e?.message || '删除失败'
  } finally {
    savingSrc.value = false
  }
}

const testConnection = async () => {
  loading.value = true
  message.value = ''
  try {
    const res = await fetch('/api/config/db', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // 密码为掩码占位 → 不提交，后端保留当前密码
      body: JSON.stringify({ ...form, name: form.database, test_connection: true, ...(form.password === '********' ? { password: '' } : {}) }),
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || data.message || `测试连接失败（HTTP ${res.status}）`)
    success.value = data.success
    message.value = data.message
  } catch (error: any) {
    success.value = false
    message.value = error.message || '测试连接失败，请检查网络或后端服务'
  } finally {
    loading.value = false
  }
}

const saveDbConfig = async () => {
  loading.value = true
  message.value = ''
  try {
    const res = await fetch('/api/config/db', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...form, name: form.database, test_connection: false, ...(form.password === '********' ? { password: '' } : {}) }),
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || data.message || `保存失败（HTTP ${res.status}）`)
    success.value = data.success
    message.value = data.message
    if (data.success) {
      await loadWorkspaces()
      emit('database-switched')
    }
  } catch (error: any) {
    success.value = false
    message.value = error.message || '保存配置失败，请检查网络或后端服务'
  } finally {
    loading.value = false
  }
}

const onDbTypeChange = () => {
  const ports: Record<string, number> = { postgresql: 5432, mysql: 3306, mssql: 1433, snowflake: 443, clickhouse: 8123, bigquery: 0 }
  const p = ports[form.db_type]
  if (p !== undefined) form.port = p
}

// ===== 工作区管理 =====
const loadWorkspaces = async () => {
  try {
    const data = await fetchJson('/api/config/databases')
    databases.value = data.databases || []
    activeName.value = data.active || ''
    activeDatabase.value = activeName.value
    if (!databases.value.includes(databaseToDelete.value) || databaseToDelete.value === activeName.value) {
      databaseToDelete.value = ''
    }
  } catch (e) {
    console.error('加载数据库工作区失败', e)
  }
}

const switchWorkspace = async () => {
  if (!activeDatabase.value || activeDatabase.value === activeName.value) return
  try {
    const data = await fetchJson(`/api/config/databases/${encodeURIComponent(activeDatabase.value)}/activate`, { method: 'POST' })
    workspaceSuccess.value = data.success
    workspaceMessage.value = data.message
    if (data.success) {
      databases.value = data.profiles || databases.value
      activeName.value = data.active || activeName.value
      activeDatabase.value = activeName.value
      // 修复（P0）：切换/新建后必须清空"待删除库"选择。
      // 原实现不调用 loadWorkspaces，databaseToDelete 保留旧值；下拉框已把活动库过滤掉（显示空白），
      // 但删除按钮的 disabled 只看"是否非空" → 用户可在无提示的情况下删掉刚切换到的活动库。
      databaseToDelete.value = ''
      emit('database-switched')
    }
  } catch (error: any) {
    workspaceSuccess.value = false
    workspaceMessage.value = error.message || '切换失败'
  }
}

const createWorkspace = async () => {
  const name = newDatabaseName.value.trim()
  if (!name) return
  try {
    const data = await fetchJson(`/api/config/databases/${encodeURIComponent(name)}/activate`, { method: 'POST' })
    workspaceSuccess.value = data.success
    workspaceMessage.value = data.message
    if (data.success) {
      newDatabaseName.value = ''
      databases.value = data.profiles || databases.value
      activeName.value = data.active || activeName.value
      activeDatabase.value = activeName.value
      // 修复（P0）：切换/新建后必须清空"待删除库"选择。
      // 原实现不调用 loadWorkspaces，databaseToDelete 保留旧值；下拉框已把活动库过滤掉（显示空白），
      // 但删除按钮的 disabled 只看"是否非空" → 用户可在无提示的情况下删掉刚切换到的活动库。
      databaseToDelete.value = ''
      emit('database-switched')
    }
  } catch (error: any) {
    workspaceSuccess.value = false
    workspaceMessage.value = error.message || '新建失败'
  }
}

const deleteWorkspace = async () => {
  if (!databaseToDelete.value) return
  // 修复（P0）：纵深防御——即便上游状态异常，也绝不允许删除当前活动库。
  if (databaseToDelete.value === activeName.value) {
    workspaceSuccess.value = false
    workspaceMessage.value = '不能删除当前活动数据库，请先切换到其它数据库'
    return
  }
  if (!window.confirm(`确认永久删除数据库"${databaseToDelete.value}"及其中全部数据表吗？此操作不可恢复。`)) return
  try {
    const data = await fetchJson(`/api/config/databases/${encodeURIComponent(databaseToDelete.value)}`, { method: 'DELETE' })
    workspaceSuccess.value = data.success
    workspaceMessage.value = data.message
    if (data.success) {
      databases.value = data.profiles || databases.value
      databaseToDelete.value = ''
    }
  } catch (error: any) {
    workspaceSuccess.value = false
    workspaceMessage.value = error.message || '删除失败'
  }
}

// ===== 反馈复核队列（管理员）=====
const fbItems = ref<any[]>([])
const fbLoading = ref(false)
const fbFilter = ref('')

const loadFeedbackQueue = async () => {
  fbLoading.value = true
  try {
    const qs = fbFilter.value ? `?status=${fbFilter.value}` : ''
    const data = await fetchJson(`/api/feedback/queue${qs}`)
    fbItems.value = data.items || []
  } catch (e) {
    console.error('加载反馈队列失败', e)
  } finally {
    fbLoading.value = false
  }
}

const resolveFeedback = async (it: any, resolution: string) => {
  try {
    await fetchJson(`/api/feedback/queue/${it.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ resolution }),
    })
    await loadFeedbackQueue()
  } catch (e: any) {
    alert('操作失败：' + (e?.message || '网络错误'))
  }
}

onMounted(async () => {
  // 数据库配置加载失败不应中断工作区列表加载（后端不可达时 fetch 会抛错）
  try {
    const response = await fetch('/api/config/db')
    if (response.ok) {
      const data = await response.json()
      Object.assign(form, {
        db_type: data.db_type || 'postgresql',
        host: data.host,
        port: data.port,
        database: data.name,
        user: data.user,
        password: data.password || '',
      })
      // 密码已掩码（后端不回显明文）：填占位符，保存/测试时转成"不修改密码"
      if (data.password === '********') form.password = '********'
    }
  } catch {
    // 静默：后续 loadWorkspaces 仍会执行，避免配置页整体不可用
  }
  await loadWorkspaces()
  await loadSources()
  loadOpsView()
  if (isAdmin()) await loadFeedbackQueue()
})
</script>
