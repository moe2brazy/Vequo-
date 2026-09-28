<template>
  <div class="space-y-5">
    <!-- 头部（page-hero：统一页面 Hero 横幅） -->
    <div class="page-hero flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="text-lg font-bold text-gray-800">权限管理</h2>
        <p class="text-xs text-gray-500 mt-1">
          管理谁能看什么数据、谁能导出。系统固定两种身份：管理员 / 普通员工；给员工点选身份即可，还可对每位员工添加备注。
        </p>
      </div>
      <div class="flex items-center gap-2">
        <button
          class="px-3 py-1.5 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition"
          @click="openWizard"
        >快捷向导</button>
        <div class="flex items-center gap-2 text-[11px] text-gray-400">
          <span class="px-2 py-1 bg-gray-50 border border-gray-200 rounded-lg">模型 v{{ model.version || '-' }}</span>
          <span class="px-2 py-1 bg-gray-50 border border-gray-200 rounded-lg">更新 {{ model.updated_by || '-' }}</span>
        </div>
      </div>
    </div>

    <!-- Tab 导航（两级：分组 + 组内子 tab） -->
    <div class="flex gap-1 border-b border-gray-200 overflow-x-auto">
      <button
        v-for="g in visibleGroups" :key="g.key"
        class="px-4 py-2 text-sm whitespace-nowrap border-b-2 transition"
        :class="activeGroup === g.key ? 'text-blue-600 border-blue-600 font-semibold' : 'text-gray-500 border-transparent hover:text-gray-700'"
        @click="switchGroup(g.key)"
      >{{ g.label }}</button>
    </div>
    <div v-if="currentGroup.tabs.length > 1" class="flex flex-wrap gap-1.5">
      <button
        v-for="t in currentGroup.tabs" :key="t.key"
        class="px-3 py-1 text-xs whitespace-nowrap rounded-lg border transition"
        :class="activeTab === t.key ? 'bg-primary text-gray-900 border-transparent font-medium' : 'text-gray-500 border-gray-200 hover:border-gray-300'"
        @click="switchTab(t.key)"
      >{{ t.label }}</button>
    </div>

    <!-- ═══════════ 1. 角色管理 ═══════════ -->
    <div v-if="activeTab === 'roles'" class="space-y-4">
      <details class="bg-blue-50 border border-blue-200 rounded-xl p-3 text-xs text-blue-800 leading-relaxed">
        <summary class="cursor-pointer font-semibold select-none"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5" /> <path d="M9 18h6" /> <path d="M10 22h4" /> </svg></span> 推荐做法：用「角色 + 用户属性」管理权限</summary>
        <div class="mt-2 space-y-1">
          <p>1. <strong>新增看数方式 → 建角色</strong>：点「<span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M5 12h14" /> <path d="M12 5v14" /> </svg></span> 新增角色」选模板（如区域经理）一键生成初始策略。</p>
          <p>2. <strong>某人拥有该方式 → 加入角色 + 配用户属性</strong>：在「用户授权」里给员工分配角色，并填属性（如 region=华东），行级权限即按属性自动过滤。</p>
          <p>3. <strong>员工授权（例外）</strong>仅用于极少数无法用角色覆盖的场景，避免逐人维护表/字段白名单。</p>
        </div>
      </details>
      <div class="flex justify-end">
        <button class="px-3 py-1.5 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition" @click="openRole(null)"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M5 12h14" /> <path d="M12 5v14" /> </svg></span> 新增角色</button>
      </div>
      <div class="grid md:grid-cols-2 xl:grid-cols-3 gap-3">
        <div v-for="r in model.roles" :key="r.key" class="bg-white border border-gray-200 rounded-xl p-4 hover:shadow-md hover:-translate-y-0.5 hover:border-[#B9DCFF] transition">
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-2">
              <span class="font-semibold text-gray-800 text-sm">{{ r.name }}</span>
              <code class="text-[10px] text-gray-400 bg-gray-100 rounded px-1.5 py-0.5">{{ r.key }}</code>
            </div>
            <span class="text-[10px] px-2 py-0.5 rounded-full border" :class="r.system ? 'bg-gray-50 text-gray-500 border-gray-200' : 'bg-blue-50 text-blue-600 border-blue-200'">
              {{ r.system ? '系统' : '自定义' }}
            </span>
          </div>
          <p class="text-xs text-gray-500 mt-1.5 min-h-[2rem]">{{ r.description || '暂无描述' }}</p>
          <div class="flex flex-wrap gap-1.5 mt-2 text-[10px] text-gray-500">
            <span class="px-1.5 py-0.5 bg-gray-50 border border-gray-100 rounded">表 {{ policyCount(r.key).tables }}</span>
            <span class="px-1.5 py-0.5 bg-gray-50 border border-gray-100 rounded">用户 {{ roleUserCount(r.key) }}</span>
            <span v-if="policyCount(r.key).rows" class="px-1.5 py-0.5 bg-gray-50 border border-gray-100 rounded">行 {{ policyCount(r.key).rows }}</span>
            <span v-if="policyCount(r.key).cols" class="px-1.5 py-0.5 bg-gray-50 border border-gray-100 rounded">列 {{ policyCount(r.key).cols }}</span>
            <span v-if="policyCount(r.key).mets" class="px-1.5 py-0.5 bg-gray-50 border border-gray-100 rounded">指标 {{ policyCount(r.key).mets }}</span>
          </div>
          <div class="flex items-center gap-3 mt-3 pt-3 border-t border-gray-100 text-xs">
            <button class="text-blue-600 hover:underline font-medium" @click="manageRolePermissions(r)">权限</button>
            <button v-if="!r.system" class="text-blue-600 hover:underline" @click="openRole(r)">编辑</button>
            <button v-if="!r.system" class="text-red-500 hover:underline" @click="removeRole(r)">删除</button>
            <span v-if="r.system" class="text-gray-300">系统角色不可编辑</span>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══════════ 2. 用户授权 ═══════════ -->
    <div v-if="activeTab === 'users'" class="space-y-4">
      <div class="flex items-center gap-2 text-xs text-gray-500 bg-blue-50 border border-blue-100 rounded-lg px-3 py-2">
        <span class="text-blue-600">i</span>
        <span>给员工点选角色即完成授权（同一人可多角色，权限自动叠加）；改完点「保存」。</span>
      </div>

      <!-- 新建成员 -->
      <div class="flex items-center justify-between gap-3">
        <div class="text-xs text-gray-400">共 {{ users.length }} 名成员</div>
        <button
          class="px-3 py-1.5 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition"
          @click="showNewUser ? (showNewUser = false) : openNewUser()"
        >{{ showNewUser ? '收起' : '＋ 新建成员' }}</button>
      </div>
      <div v-if="showNewUser" class="bg-white border border-blue-200 rounded-xl p-4 space-y-3">
        <div class="grid grid-cols-1 md:grid-cols-4 gap-2.5">
          <input v-model="newUser.username" placeholder="登录名（必填）" class="px-2.5 py-1.5 border rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-blue-200" style="border-color:#c9cdd4" />
          <input v-model="newUser.display_name" placeholder="姓名（可空）" class="px-2.5 py-1.5 border rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-blue-200" style="border-color:#c9cdd4" />
          <input v-model="newUser.password" type="password" placeholder="初始密码（必填）" class="px-2.5 py-1.5 border rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-blue-200" style="border-color:#c9cdd4" />
          <select v-model="newUser.role" class="px-2.5 py-1.5 border rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-blue-200" style="border-color:#c9cdd4">
            <option v-for="r in allRoles" :key="r.key" :value="r.key">{{ r.name }}</option>
          </select>
        </div>
        <div class="flex items-center gap-2">
          <button
            class="px-3 py-1.5 text-xs rounded-lg font-medium text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-40"
            :disabled="newUser.saving"
            @click="createMember"
          >{{ newUser.saving ? '创建中…' : '创建' }}</button>
          <span v-if="newUser.msg" class="text-[11px]" :style="newUser.ok ? 'color:#1677ff' : 'color:#b34a3a'">{{ newUser.msg }}</span>
        </div>
        <div class="text-[11px] leading-relaxed" style="color:#8a6d1b;background:#fbf3d9;padding:6px 8px;border-radius:8px">
          密码至少 8 位且包含字母和数字。这里选的身份就是该成员的数据范围依据——选错了直接用下面的角色按钮改，不用重建账号。
        </div>
      </div>

      <div v-if="loading.users" class="text-xs text-gray-400 py-6 text-center">加载中…</div>
      <div v-else class="overflow-x-auto bg-white border border-gray-200 rounded-xl">
        <table class="w-full text-left text-xs">
          <thead class="bg-gray-50 text-gray-500">
            <tr>
              <th class="px-3 py-2 font-medium">成员</th>
              <th class="px-3 py-2 font-medium">状态</th>
              <th class="px-3 py-2 font-medium">角色</th>
              <th class="px-3 py-2 font-medium">备注</th>
              <th class="px-3 py-2 font-medium">能看什么</th>
              <th class="px-3 py-2 font-medium text-right">操作</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-100">
            <tr v-for="u in users" :key="u.username" class="align-top hover:bg-gray-50/60" :class="u.enabled === false ? 'opacity-60' : ''">
              <td class="px-3 py-2 min-w-[120px]">
                <span class="font-semibold text-gray-800">{{ u.display_name || u.username }}</span>
                <div class="text-gray-400 mt-0.5">{{ u.username }}</div>
                <div v-if="u.department || u.title" class="text-[10px] text-gray-400 mt-0.5">
                  {{ [u.department, u.title].filter(Boolean).join(' · ') }}
                </div>
              </td>
              <!-- 状态列：管理员要能一眼看出谁不能登录、谁还没授权 -->
              <td class="px-3 py-2 min-w-[92px]">
                <span v-if="u.enabled === false" class="text-[10px] px-1.5 py-0.5 rounded bg-red-50 text-red-600 border border-red-100">已禁用</span>
                <span v-else-if="u.unassigned" class="text-[10px] px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 border border-amber-100">待授权</span>
                <span v-else class="text-[10px] px-1.5 py-0.5 rounded bg-green-50 text-green-700 border border-green-100">正常</span>
                <div v-if="u.builtin" class="text-[10px] text-gray-400 mt-1">内置</div>
                <div class="text-[10px] text-gray-400 mt-1 leading-tight">
                  {{ u.last_login ? '上次登录 ' + String(u.last_login).slice(0, 10) : '从未登录' }}
                </div>
              </td>
              <td class="px-3 py-2 min-w-[260px]">
                <div class="flex flex-wrap gap-1">
                  <button
                    v-for="r in allRoles" :key="r.key"
                    class="text-[10px] px-2 py-0.5 rounded-full border transition"
                    :class="(u.roles || []).includes(r.key) ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-gray-400 border-gray-200 hover:border-blue-300'"
                    @click="toggleUserRole(u, r.key)"
                  >{{ r.name }}</button>
                </div>
                <details class="mt-1.5">
                  <summary class="cursor-pointer text-[10px] text-gray-400 hover:text-gray-600 select-none">行级属性（可选，按区域/部门过滤用）</summary>
                  <textarea
                    :value="JSON.stringify(u.attributes || {}, null, 0)"
                    rows="2"
                    class="mt-1 w-full font-mono text-[11px] border border-gray-200 rounded-lg px-2 py-1 focus:outline-none focus:ring-2 focus:ring-blue-200"
                    placeholder='{"region": "华东"}'
                    @change="onAttrChange(u.username, $event)"
                  />
                </details>
              </td>
              <td class="px-3 py-2 min-w-[160px]">
                <template v-if="noteEditing && noteEditing.username === u.username">
                  <input v-model="noteEditing.text" type="text" class="w-full text-[11px] border border-blue-300 rounded-lg px-2 py-1 focus:outline-none focus:ring-2 focus:ring-blue-200" placeholder="给员工加备注…（如负责区域/说明）" @keyup.enter="saveNote(u)" />
                  <div class="mt-1 flex gap-2">
                    <button class="text-[10px] px-2 py-0.5 rounded bg-blue-600 text-white" @click="saveNote(u)">保存</button>
                    <button class="text-[10px] px-2 py-0.5 rounded text-gray-400 hover:text-gray-600" @click="noteEditing = null">取消</button>
                  </div>
                </template>
                <template v-else>
                  <span v-if="u.note" class="text-gray-700 break-words leading-relaxed">{{ u.note }}</span>
                  <button v-if="!u.note" class="text-gray-300 hover:text-gray-500" @click="openNoteEdit(u)"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M5 12h14" /> <path d="M12 5v14" /> </svg></span> 备注</button>
                  <button v-if="u.note" class="text-gray-300 hover:text-blue-500 ml-1 align-middle" title="编辑备注" @click="openNoteEdit(u)"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" /> <path d="m15 5 4 4" /> </svg></span></button>
                </template>
              </td>
              <td class="px-3 py-2 text-gray-600">
                <div v-if="u.has_grants" class="text-[10px] px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 border border-amber-100 inline-block mb-1">例外</div>
                <div class="text-[11px] leading-relaxed">{{ userScope(u) }}</div>
              </td>
              <td class="px-3 py-2 text-right whitespace-nowrap">
                <button class="text-blue-600 hover:underline" @click="showEffective(u.username)">查看生效</button>
                <button class="ml-2 px-2.5 py-1 text-[11px] bg-blue-600 text-white rounded-lg hover:bg-blue-700" @click="saveUser(u)">保存</button>
                <!-- 账号维护：改资料 / 重置密码 / 启用禁用 / 删除 -->
                <details class="inline-block ml-2 relative align-middle">
                  <summary class="cursor-pointer list-none px-2.5 py-1 text-[11px] rounded-lg border text-gray-600 hover:bg-gray-50 select-none" style="border-color:#c9cdd4">
                    维护 ▾
                  </summary>
                  <div class="absolute right-0 mt-1 z-20 bg-white border rounded-lg shadow-lg py-1 min-w-[132px] text-left" style="border-color:#e5e6eb">
                    <button class="block w-full text-left px-3 py-1.5 text-[11px] text-gray-700 hover:bg-gray-50" @click="openMaintain(u)">编辑资料</button>
                    <button class="block w-full text-left px-3 py-1.5 text-[11px] text-gray-700 hover:bg-gray-50" @click="openPassword(u)">重置密码</button>
                    <button
                      class="block w-full text-left px-3 py-1.5 text-[11px] hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
                      :style="canTouch(u).ok ? (u.enabled === false ? 'color:#1677ff' : 'color:#8a6d1b') : 'color:#a9aeb8'"
                      :disabled="!canTouch(u).ok"
                      :title="canTouch(u).why"
                      @click="toggleEnabled(u)"
                    >{{ u.enabled === false ? '启用账号' : '禁用账号' }}</button>
                    <div class="my-1 border-t" style="border-color:#f2f3f5"></div>
                    <button
                      class="block w-full text-left px-3 py-1.5 text-[11px] hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
                      :style="canTouch(u).ok ? 'color:#b34a3a' : 'color:#a9aeb8'"
                      :disabled="!canTouch(u).ok"
                      :title="canTouch(u).why"
                      @click="removeUser(u)"
                    >删除账号</button>
                  </div>
                </details>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- ═══════════ 3. 数据集 ═══════════ -->
    <div v-if="activeTab === 'datasets'" class="space-y-4">
      <div class="flex justify-end">
        <button class="px-3 py-1.5 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition" @click="openDataset(null)"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M5 12h14" /> <path d="M12 5v14" /> </svg></span> 新增数据集</button>
      </div>
      <div class="overflow-x-auto bg-white border border-gray-200 rounded-xl">
        <table class="w-full text-left text-xs">
          <thead class="bg-gray-50 text-gray-500">
            <tr>
              <th class="px-3 py-2 font-medium">数据集</th>
              <th class="px-3 py-2 font-medium">包含表</th>
              <th class="px-3 py-2 font-medium">敏感</th>
              <th class="px-3 py-2 font-medium">操作</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-100">
            <tr v-for="d in model.datasets" :key="d.key" class="hover:bg-gray-50/60">
              <td class="px-3 py-2">
                <span class="font-semibold text-gray-800">{{ d.name }}</span>
                <code class="ml-1 text-[10px] text-gray-400 bg-gray-100 rounded px-1">{{ d.key }}</code>
                <div class="text-gray-400 mt-0.5">{{ d.description }}</div>
              </td>
              <td class="px-3 py-2">
                <span v-for="t in d.tables" :key="t" class="inline-block mr-1 mb-0.5 font-mono text-[10px] bg-gray-50 border border-gray-200 rounded px-1.5 py-0.5">{{ t }}</span>
              </td>
              <td class="px-3 py-2">
                <span class="text-[10px] px-2 py-0.5 rounded-full" :class="d.sensitive ? 'bg-red-50 text-red-600 border border-red-200' : 'bg-gray-50 text-gray-400 border border-gray-200'">
                  {{ d.sensitive ? '敏感' : '普通' }}
                </span>
              </td>
              <td class="px-3 py-2">
                <button class="text-blue-600 hover:underline mr-2" @click="openDataset(d)">编辑</button>
                <button class="text-red-500 hover:underline" @click="removeDataset(d)">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- ═══════════ 高级权限（列脱敏 / 行规则 / 指标口径 精细配置）═══════════ -->
    <div v-if="showAdv" id="advanced-permissions" class="space-y-4">
      <div class="flex items-center justify-between">
        <div class="text-sm font-semibold text-gray-700">高级权限 · 精细配置</div>
        <button class="text-xs text-gray-500 hover:text-gray-700" @click="showAdv = false">收起 ▲</button>
      </div>
      <div class="flex gap-1 text-[11px]">
        <button class="px-3 py-1.5 rounded-lg border transition" :class="advTab === 'columns' ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-gray-500 border-gray-200 hover:border-blue-300'" @click="setAdvTab('columns')">列脱敏</button>
        <button class="px-3 py-1.5 rounded-lg border transition" :class="advTab === 'rows' ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-gray-500 border-gray-200 hover:border-blue-300'" @click="setAdvTab('rows')">行规则</button>
        <button class="px-3 py-1.5 rounded-lg border transition" :class="advTab === 'metrics' ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-gray-500 border-gray-200 hover:border-blue-300'" @click="setAdvTab('metrics')">指标口径</button>
        <button class="px-3 py-1.5 rounded-lg border transition" :class="advTab === 'actions' ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-gray-500 border-gray-200 hover:border-blue-300'" @click="setAdvTab('actions')">操作权限</button>
      </div>

    <!-- ═══════════ 4. 列级权限 ═══════════ -->
    <div v-if="advTab === 'columns'" class="space-y-4">
      <div class="flex items-center gap-2">
        <span class="text-xs text-gray-500">角色：</span>
        <select :value="colRole" @change="onRoleChange('col', $event)" class="px-2 py-1.5 text-xs border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-200">
          <option v-for="r in model.roles" :key="r.key" :value="r.key">{{ r.name }}</option>
        </select>
      </div>
      <div v-if="!colRole" class="text-xs text-gray-400 py-6 text-center bg-white border border-dashed border-gray-200 rounded-xl">请先选择角色</div>
      <template v-else>
        <div class="flex gap-2">
          <select v-model="colTable" @change="loadColRules" class="px-2 py-1.5 text-xs border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-200">
            <option v-for="t in roleTables(colRole)" :key="t" :value="t">{{ t }}</option>
          </select>
          <span class="text-[11px] text-gray-400 self-center">列策略：visible 明文可见 · mask 动态脱敏 · deny 拒绝访问</span>
        </div>
        <div v-if="colTable && colColumns.length" class="overflow-x-auto bg-white border border-gray-200 rounded-xl">
          <table class="w-full text-left text-xs">
            <thead class="bg-gray-50 text-gray-500">
              <tr>
                <th class="px-3 py-2 font-medium">字段</th>
                <th class="px-3 py-2 font-medium">敏感</th>
                <th class="px-3 py-2 font-medium w-32">模式</th>
                <th class="px-3 py-2 font-medium w-40">脱敏方式</th>
                <th class="px-3 py-2 font-medium">说明</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-100">
              <tr v-for="c in colColumns" :key="c.name" class="hover:bg-gray-50/60">
                <td class="px-3 py-2 font-mono text-gray-700">{{ c.name }}</td>
                <td class="px-3 py-2">
                  <span v-if="isSensitiveColumn(colTable, c.name)" class="text-[10px] px-1.5 py-0.5 bg-red-50 text-red-500 border border-red-200 rounded">敏感</span>
                  <span v-else class="text-gray-300">—</span>
                </td>
                <td class="px-3 py-2">
                  <select v-model="colRules[c.name].mode" class="px-2 py-1 text-[11px] border border-gray-200 rounded-lg" @change="onColModeChange(c.name)">
                    <option value="visible">可见</option>
                    <option value="mask">脱敏</option>
                    <option value="deny">拒绝</option>
                  </select>
                </td>
                <td class="px-3 py-2">
                  <select v-if="colRules[c.name].mode === 'mask'" v-model="colRules[c.name].mask" class="px-2 py-1 text-[11px] border border-gray-200 rounded-lg w-full">
                    <option v-for="m in maskTypes" :key="m.key" :value="m.key">{{ m.label }}</option>
                  </select>
                  <span v-else class="text-gray-300 text-[11px]">—</span>
                </td>
                <td class="px-3 py-2">
                  <input v-model="colRules[c.name].note" class="w-full px-2 py-1 text-[11px] border border-gray-200 rounded-lg" placeholder="说明（可选）" />
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="colTable" class="flex justify-end">
          <button class="px-4 py-2 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition" @click="saveColumns">保存列策略</button>
        </div>
      </template>
    </div>

    <!-- ═══════════ 5. 行级权限 ═══════════ -->
    <div v-if="advTab === 'rows'" class="space-y-4">
      <div class="flex items-center gap-2">
        <span class="text-xs text-gray-500">角色：</span>
        <select :value="rowRole" @change="onRoleChange('row', $event)" class="px-2 py-1.5 text-xs border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-200">
          <option v-for="r in model.roles" :key="r.key" :value="r.key">{{ r.name }}</option>
        </select>
      </div>
      <div v-if="!rowRole" class="text-xs text-gray-400 py-6 text-center bg-white border border-dashed border-gray-200 rounded-xl">请先选择角色</div>
      <template v-else>
        <div class="flex items-center gap-2 text-xs text-gray-500 bg-amber-50 border border-amber-100 rounded-lg px-3 py-2">
          <span class="text-amber-600">i</span>
          行级条件按角色动态注入 WHERE；可用变量
          <code class="font-mono bg-white px-1 rounded">${user.region}</code>
          <code class="font-mono bg-white px-1 rounded">${user.dept}</code>
          等（取自用户属性）。变量缺失时该策略按「拒绝访问」处理（fail-close）。
        </div>
        <div class="space-y-3">
          <div v-for="t in roleTables(rowRole)" :key="t" class="bg-white border rounded-xl p-4"
               :class="isTableAuthorized(rowRole, t) ? 'border-gray-200' : 'border-amber-300 bg-amber-50/40'">
            <div class="flex items-center justify-between mb-2">
              <div class="flex items-center gap-2">
                <code class="text-xs font-semibold text-gray-700">{{ t }}</code>
                <span v-if="!isTableAuthorized(rowRole, t)"
                      class="text-[10px] text-amber-700 bg-amber-100 border border-amber-200 rounded px-1.5 py-0.5">
                  未授权给该角色 · 规则不会生效
                </span>
              </div>
              <label class="flex items-center gap-1.5 text-[11px] text-gray-500">
                <input type="checkbox" v-model="rowRules[t].enabled" class="accent-blue-600" />
                启用
              </label>
            </div>
            <div class="flex items-center gap-2 mb-2">
              <div class="flex rounded-lg overflow-hidden border border-gray-200 text-[11px]">
                <button
                  @click="rowRules[t].mode = 'form'"
                  class="px-2 py-1 transition"
                  :class="rowRules[t].mode === 'form' ? 'bg-blue-600 text-white' : 'text-gray-500 hover:bg-gray-50'"
                ><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <line x1="21" x2="14" y1="4" y2="4" /> <line x1="10" x2="3" y1="4" y2="4" /> <line x1="21" x2="12" y1="12" y2="12" /> <line x1="8" x2="3" y1="12" y2="12" /> <line x1="21" x2="16" y1="20" y2="20" /> <line x1="12" x2="3" y1="20" y2="20" /> <line x1="14" x2="14" y1="2" y2="6" /> <line x1="8" x2="8" y1="10" y2="14" /> <line x1="16" x2="16" y1="18" y2="22" /> </svg></span> 可视化配置</button>
                <button
                  @click="rowRules[t].mode = 'expr'"
                  class="px-2 py-1 transition"
                  :class="rowRules[t].mode === 'expr' ? 'bg-blue-600 text-white' : 'text-gray-500 hover:bg-gray-50'"
                >SQL 表达式</button>
              </div>
              <span class="text-[10px] text-gray-400">可视化配置无需手写 SQL，自动生成生效条件</span>
            </div>
            <div v-if="rowRules[t].mode === 'expr'">
              <textarea
                v-model="rowRules[t].expr"
                rows="2"
                class="w-full font-mono text-[11px] border border-gray-200 rounded-lg px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-blue-200"
                placeholder="例：factory_id IN (SELECT factory_id FROM test_factories WHERE city = ${user.region})"
              />
            </div>
            <div v-else class="space-y-2">
              <div class="flex flex-wrap items-center gap-2 text-[11px]">
                <span class="text-gray-400">当</span>
                <select v-model="rowRules[t].rule.op" class="px-1.5 py-1 border border-gray-200 rounded text-[11px] bg-white">
                  <option value="eq">等于</option>
                  <option value="neq">不等于</option>
                  <option value="in">属于（用户属性列表）</option>
                  <option value="contains">包含</option>
                  <option value="in_subquery">属于另一张表的记录</option>
                </select>
              </div>
              <template v-if="rowRules[t].rule.op === 'in_subquery'">
                <div class="flex flex-wrap items-center gap-1.5 text-[11px]">
                  <span class="text-gray-400">本表</span>
                  <select v-model="rowRules[t].rule.on.left" class="px-1.5 py-1 border border-gray-200 rounded bg-white">
                    <option v-for="c in colsOf(t)" :key="c" :value="c">{{ c }}</option>
                  </select>
                  <span class="text-gray-400">字段 ∈</span>
                  <select v-model="rowRules[t].rule.ref_table" class="px-1.5 py-1 border border-gray-200 rounded bg-white" @change="onRefTableChange(t)">
                    <option v-for="rt in schemaTables" :key="rt" :value="rt">{{ rt }}</option>
                  </select>
                  <span class="text-gray-400">.字段</span>
                  <select v-model="rowRules[t].rule.on.right" class="px-1.5 py-1 border border-gray-200 rounded bg-white">
                    <option v-for="c in colsOf(rowRules[t].rule.ref_table)" :key="c" :value="c">{{ c }}</option>
                  </select>
                </div>
                <div class="flex flex-wrap items-center gap-1.5 text-[11px]">
                  <span class="text-gray-400">且该表的</span>
                  <select v-model="rowRules[t].rule.where.field" class="px-1.5 py-1 border border-gray-200 rounded bg-white">
                    <option v-for="c in colsOf(rowRules[t].rule.ref_table)" :key="c" :value="c">{{ c }}</option>
                  </select>
                  <select v-model="rowRules[t].rule.where.op" class="px-1.5 py-1 border border-gray-200 rounded bg-white">
                    <option value="eq">=</option>
                    <option value="neq">≠</option>
                    <option value="in">∈</option>
                    <option value="contains">包含</option>
                  </select>
                  <input v-model="rowRules[t].rule.where.source" list="user-attr-opts" class="px-1.5 py-1 border border-gray-200 rounded w-40" placeholder="user.region 或固定值" />
                </div>
              </template>
              <template v-else>
                <div class="flex flex-wrap items-center gap-1.5 text-[11px]">
                  <select v-model="rowRules[t].rule.field" class="px-1.5 py-1 border border-gray-200 rounded bg-white">
                    <option v-for="c in colsOf(t)" :key="c" :value="c">{{ c }}</option>
                  </select>
                  <input v-model="rowRules[t].rule.source" list="user-attr-opts" class="px-1.5 py-1 border border-gray-200 rounded w-44" placeholder="user.region 或固定值" />
                  <span class="text-gray-400">（user.前缀=用户属性，否则为固定值）</span>
                </div>
              </template>
              <div class="text-[10px] font-mono text-gray-400 bg-gray-50 border border-gray-100 rounded px-2 py-1.5">
                生效条件：{{ rowRulePreview(t) || '（信息不完整，暂不生效）' }}
              </div>
            </div>
            <input v-model="rowRules[t].note" class="mt-2 w-full px-2 py-1 text-[11px] border border-gray-200 rounded-lg" placeholder="策略说明（可选）" />
          </div>
        </div>
        <div class="flex justify-end">
          <button class="px-4 py-2 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition" @click="saveRows">保存行策略</button>
        </div>
      </template>
      <datalist id="user-attr-opts">
        <option v-for="k in userAttributeKeys" :key="k" :value="'user.' + k">{{ k }}</option>
      </datalist>
    </div>

    <!-- ═══════════ 6. 指标口径 ═══════════ -->
    <div v-if="advTab === 'metrics'" class="space-y-4">
      <div class="flex items-center gap-2">
        <span class="text-xs text-gray-500">角色：</span>
        <select :value="metricRole" @change="onRoleChange('metric', $event)" class="px-2 py-1.5 text-xs border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-200">
          <option v-for="r in model.roles" :key="r.key" :value="r.key">{{ r.name }}</option>
        </select>
      </div>
      <div v-if="!metricRole" class="text-xs text-gray-400 py-6 text-center bg-white border border-dashed border-gray-200 rounded-xl">请先选择角色</div>
      <template v-else>
        <div class="flex items-center gap-2 text-xs text-gray-500 bg-blue-50 border border-blue-100 rounded-lg px-3 py-2">
          <span class="text-blue-600">i</span>
          同一指标可按角色覆盖不同计算口径（override 注入 SQL 表达式）；deny 禁止使用。优先级高的角色覆盖低优先级角色。
        </div>
        <div class="overflow-x-auto bg-white border border-gray-200 rounded-xl">
          <table class="w-full text-left text-xs">
            <thead class="bg-gray-50 text-gray-500">
              <tr>
                <th class="px-3 py-2 font-medium">指标</th>
                <th class="px-3 py-2 font-medium w-32">策略</th>
                <th class="px-3 py-2 font-medium">覆盖口径 SQL</th>
                <th class="px-3 py-2 font-medium">业务公式</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-100">
              <tr v-for="m in metrics" :key="m.name" class="hover:bg-gray-50/60">
                <td class="px-3 py-2">
                  <span class="font-semibold text-gray-800">{{ m.name }}</span>
                  <span v-if="isSensitiveMetric(m.name)" class="ml-1 text-[10px] px-1.5 py-0.5 bg-red-50 text-red-500 border border-red-200 rounded">敏感</span>
                  <div class="text-gray-400 mt-0.5">{{ m.description || m.formula }}</div>
                </td>
                <td class="px-3 py-2">
                  <select v-model="metricRules[m.name].mode" class="px-2 py-1 text-[11px] border border-gray-200 rounded-lg w-full" @change="onMetricModeChange(m.name)">
                    <option value="allow">允许（默认口径）</option>
                    <option value="override">覆盖口径</option>
                    <option value="deny">禁止</option>
                  </select>
                </td>
                <td class="px-3 py-2">
                  <input
                    v-if="metricRules[m.name].mode === 'override'"
                    v-model="metricRules[m.name].sql_expression"
                    class="w-full px-2 py-1 text-[11px] font-mono border border-gray-200 rounded-lg"
                    :placeholder="m.sql_expression"
                  />
                  <span v-else class="text-gray-300 font-mono text-[11px]">{{ m.sql_expression }}</span>
                </td>
                <td class="px-3 py-2">
                  <input v-if="metricRules[m.name].mode === 'override'" v-model="metricRules[m.name].formula" class="w-full px-2 py-1 text-[11px] border border-gray-200 rounded-lg" placeholder="业务公式说明" />
                  <span v-else class="text-gray-500">{{ m.formula }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="flex justify-end">
          <button class="px-4 py-2 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition" @click="saveMetrics">保存指标策略</button>
        </div>
      </template>
    </div>

    <!-- ═══════════ 6b. 操作权限（export / share / download）═══════════ -->
    <div v-if="advTab === 'actions'" class="space-y-4">
      <div class="flex items-center gap-2">
        <span class="text-xs text-gray-500">角色：</span>
        <select :value="colRole" @change="onRoleChange('col', $event); loadActionRules()" class="px-2 py-1.5 text-xs border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-200">
          <option v-for="r in model.roles" :key="r.key" :value="r.key">{{ r.name }}</option>
        </select>
      </div>
      <div v-if="!colRole" class="text-xs text-gray-400 py-6 text-center bg-white border border-dashed border-gray-200 rounded-xl">请先选择角色</div>
      <template v-else>
        <div class="flex items-center gap-2 text-xs text-gray-500 bg-blue-50 border border-blue-100 rounded-lg px-3 py-2">
          <span class="text-blue-600">i</span>
          控制角色「能对数据做什么」：默认全部关闭（只能看，不能带走），需显式开通。
        </div>
        <div class="space-y-2 bg-white border border-gray-200 rounded-xl p-4">
          <label class="flex items-center justify-between py-2 border-b border-gray-100">
            <div>
              <div class="text-sm font-medium text-gray-800">导出数据</div>
              <div class="text-[11px] text-gray-400">导出查询结果 / 数据总览报告 / HTML 报告</div>
            </div>
            <input type="checkbox" v-model="actionRules.export" class="accent-blue-600 w-4 h-4" />
          </label>
          <label class="flex items-center justify-between py-2 border-b border-gray-100">
            <div>
              <div class="text-sm font-medium text-gray-800">分享</div>
              <div class="text-[11px] text-gray-400">分享图表 / 分析结果给他人</div>
            </div>
            <input type="checkbox" v-model="actionRules.share" class="accent-blue-600 w-4 h-4" />
          </label>
          <label class="flex items-center justify-between py-2">
            <div>
              <div class="text-sm font-medium text-gray-800">下载原始数据</div>
              <div class="text-[11px] text-gray-400">下载数据文件的原始内容</div>
            </div>
            <input type="checkbox" v-model="actionRules.download" class="accent-blue-600 w-4 h-4" />
          </label>
        </div>
        <div class="flex justify-end">
          <button class="px-4 py-2 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition" @click="saveActions">保存操作权限</button>
        </div>
      </template>
    </div>
    </div>

    <!-- ═══════════ 7. 审批中心 ═══════════ -->
    <div v-if="activeTab === 'approvals'" class="space-y-4">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div class="flex gap-1 text-[11px]">
          <button v-for="s in ['', 'pending', 'approved', 'rejected', 'auto_approved']" :key="s"
            class="px-2.5 py-1 rounded-lg border transition"
            :class="approvalFilter === s ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-gray-500 border-gray-200 hover:border-blue-300'"
            @click="approvalFilter = s; loadRequests()"
          >{{ s === '' ? '全部' : statusLabel(s) }}</button>
        </div>
        <button class="px-3 py-1.5 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition" @click="showRequestForm = !showRequestForm"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M5 12h14" /> <path d="M12 5v14" /> </svg></span> 发起申请</button>
      </div>

      <!-- 发起申请表单 -->
      <div v-if="showRequestForm" class="bg-white border border-gray-200 rounded-xl p-4 space-y-3">
        <h3 class="text-sm font-bold text-gray-800">发起权限申请</h3>
        <div class="grid md:grid-cols-2 gap-3 text-xs">
          <label class="block">
            <span class="text-gray-500">变更类型</span>
            <select v-model="reqForm.kind" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg">
              <option v-for="(k, key) in kindOptions" :key="key" :value="key">{{ k }}</option>
            </select>
          </label>
          <label class="block">
            <span class="text-gray-500">申请原因</span>
            <input v-model="reqForm.reason" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg" placeholder="如：业务需要查看本区域客户明细" />
          </label>
        </div>
        <label class="block text-xs">
          <span class="text-gray-500">变更指令 JSON（action + 参数，与统一变更入口一致）</span>
          <textarea v-model="reqForm.payloadText" rows="4" class="mt-1 w-full font-mono text-[11px] px-2 py-1.5 border border-gray-300 rounded-lg" />
        </label>
        <div class="flex justify-end gap-2">
          <button class="px-3 py-1.5 text-xs bg-gray-100 hover:bg-gray-200 rounded-lg" @click="showRequestForm = false">取消</button>
          <button class="px-3 py-1.5 text-xs bg-blue-600 text-white hover:bg-blue-700 rounded-lg" @click="submitRequest">提交申请</button>
        </div>
      </div>

      <div v-if="loading.approvals" class="text-xs text-gray-400 py-6 text-center">加载中…</div>
      <div v-else class="overflow-x-auto bg-white border border-gray-200 rounded-xl">
        <table class="w-full text-left text-xs">
          <thead class="bg-gray-50 text-gray-500">
            <tr>
              <th class="px-3 py-2 font-medium">申请</th>
              <th class="px-3 py-2 font-medium">类型</th>
              <th class="px-3 py-2 font-medium">目标</th>
              <th class="px-3 py-2 font-medium">原因</th>
              <th class="px-3 py-2 font-medium">状态</th>
              <th class="px-3 py-2 font-medium">提交人/时间</th>
              <th class="px-3 py-2 font-medium">审批</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-100">
            <tr v-for="q in requests" :key="q.id" class="align-top hover:bg-gray-50/60">
              <td class="px-3 py-2">
                <button class="text-blue-600 hover:underline font-mono" @click="q.expanded = !q.expanded">{{ q.id }}</button>
                <pre v-if="q.expanded" class="mt-1 max-h-40 overflow-auto text-[10px] bg-gray-50 border border-gray-100 rounded p-2 text-gray-600">{{ JSON.stringify(q.payload, null, 2) }}</pre>
              </td>
              <td class="px-3 py-2 text-gray-600">{{ q.kind_label || q.kind }}</td>
              <td class="px-3 py-2 font-mono text-gray-600">{{ q.target }}</td>
              <td class="px-3 py-2 text-gray-500 max-w-[180px] truncate" :title="q.reason">{{ q.reason || '-' }}</td>
              <td class="px-3 py-2">
                <span class="text-[10px] px-2 py-0.5 rounded-full"
                  :class="statusClass(q.status)">{{ statusLabel(q.status) }}</span>
              </td>
              <td class="px-3 py-2 text-gray-500">{{ q.requester }}<div class="text-[10px] text-gray-400">{{ fmtTime(q.created_at) }}</div></td>
              <td class="px-3 py-2">
                <template v-if="q.status === 'pending' && isAdminUser">
                  <button class="px-2 py-1 text-[11px] bg-blue-600 text-white rounded-lg hover:bg-blue-700 mr-1" @click="reviewRequest(q, true)">通过</button>
                  <button class="px-2 py-1 text-[11px] bg-red-500 text-white rounded-lg hover:bg-red-600" @click="reviewRequest(q, false)">驳回</button>
                </template>
                <span v-else class="text-gray-300 text-[11px]">{{ q.reviewer ? '已处理' : '—' }}</span>
              </td>
            </tr>
            <tr v-if="!requests.length"><td colspan="7" class="px-3 py-6 text-center text-gray-400">暂无申请单</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- ═══════════ 8. 审计日志 ═══════════ -->
    <div v-if="activeTab === 'audit'" class="space-y-5">
      <div>
        <h3 class="text-sm font-bold text-gray-800 mb-2">权限变更历史</h3>
        <div class="overflow-x-auto bg-white border border-gray-200 rounded-xl">
          <table class="w-full text-left text-xs">
            <thead class="bg-gray-50 text-gray-500">
              <tr>
                <th class="px-3 py-2 font-medium">时间</th>
                <th class="px-3 py-2 font-medium">操作人</th>
                <th class="px-3 py-2 font-medium">动作</th>
                <th class="px-3 py-2 font-medium">目标</th>
                <th class="px-3 py-2 font-medium">状态</th>
                <th class="px-3 py-2 font-medium">明细</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-100">
              <tr v-for="c in changes" :key="c.ts" class="align-top hover:bg-gray-50/60">
                <td class="px-3 py-2 text-gray-500 whitespace-nowrap">{{ fmtTime(c.ts) }}</td>
                <td class="px-3 py-2 text-gray-700">{{ c.actor }}</td>
                <td class="px-3 py-2 font-mono text-blue-700">{{ c.action }}</td>
                <td class="px-3 py-2 font-mono text-gray-600">{{ c.target }}</td>
                <td class="px-3 py-2">
                  <span class="text-[10px] px-1.5 py-0.5 rounded-full" :class="c.status === 'rejected' ? 'bg-red-50 text-red-500 border border-red-200' : 'bg-blue-50 text-blue-600 border border-blue-100'">{{ c.status }}</span>
                </td>
                <td class="px-3 py-2">
                  <button class="text-blue-600 hover:underline" @click="c.expanded = !c.expanded">{{ c.expanded ? '收起' : '展开' }}</button>
                  <pre v-if="c.expanded" class="mt-1 max-h-32 overflow-auto text-[10px] bg-gray-50 border border-gray-100 rounded p-2 text-gray-600">{{ JSON.stringify(c.detail || {}, null, 2) }}</pre>
                </td>
              </tr>
              <tr v-if="!changes.length"><td colspan="6" class="px-3 py-6 text-center text-gray-400">暂无变更记录</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <div>
        <h3 class="text-sm font-bold text-gray-800 mb-2">操作审计（查询行为）</h3>
        <div class="overflow-x-auto bg-white border border-gray-200 rounded-xl">
          <table class="w-full text-left text-xs">
            <thead class="bg-gray-50 text-gray-500">
              <tr>
                <th class="px-3 py-2 font-medium">时间</th>
                <th class="px-3 py-2 font-medium">用户</th>
                <th class="px-3 py-2 font-medium">角色</th>
                <th class="px-3 py-2 font-medium">事件</th>
                <th class="px-3 py-2 font-medium">查询 / 明细</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-100">
              <tr v-for="(l, i) in auditLogs" :key="i" class="align-top hover:bg-gray-50/60">
                <td class="px-3 py-2 text-gray-500 whitespace-nowrap">{{ fmtTime(l.ts) }}</td>
                <td class="px-3 py-2 text-gray-700">{{ l.user }}</td>
                <td class="px-3 py-2 text-gray-500">{{ l.role }}</td>
                <td class="px-3 py-2 font-mono" :class="l.event === 'access_denied' ? 'text-red-600 font-semibold' : 'text-blue-700'">
                  <span v-if="l.event === 'access_denied'"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <circle cx="12" cy="12" r="10" /> <path d="m4.9 4.9 14.2 14.2" /> </svg></span></span>{{ l.event }}
                </td>
                <td class="px-3 py-2 max-w-[320px] truncate" :class="l.event === 'access_denied' ? 'text-red-600' : 'text-gray-500'" :title="auditDetail(l)">
                  {{ l.event === 'access_denied' ? (l.reason || '越权尝试') : (l.query || l.detail || '-') }}
                </td>
              </tr>
              <tr v-if="!auditLogs.length"><td colspan="5" class="px-3 py-6 text-center text-gray-400">暂无审计记录</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ═══════════ 9. 权限模拟器 ═══════════ -->
    <div v-if="activeTab === 'simulate'" class="space-y-4">
      <div class="flex items-center gap-2 text-xs text-gray-500 bg-gray-50 border border-gray-200 rounded-lg px-3 py-2">
        <span class="text-gray-600">i</span>
        选择用户与角色后输入 SQL，预览引擎层改写结果（行过滤注入 / 列脱敏 / 无权列拒绝），可选项执行验证。
      </div>
      <div class="grid md:grid-cols-2 gap-3">
        <label class="block text-xs">
          <span class="text-gray-500">目标用户</span>
          <select v-model="simUser" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg">
            <option v-for="u in users" :key="u.username" :value="u.username">{{ u.username }}（{{ (u.roles || []).join('、') }}）</option>
          </select>
        </label>
        <label class="block text-xs">
          <span class="text-gray-500">数据库方言</span>
          <select v-model="simDialect" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg">
            <option value="postgres">PostgreSQL</option>
            <option value="mysql">MySQL</option>
          </select>
        </label>
      </div>
      <label class="block text-xs">
        <span class="text-gray-500">SQL</span>
        <textarea v-model="simSql" rows="4" class="mt-1 w-full font-mono text-[11px] px-2 py-1.5 border border-gray-300 rounded-lg" placeholder="SELECT customer_name, unit_price FROM test_orders LIMIT 10" />
      </label>
      <div class="flex items-center gap-4">
        <label class="flex items-center gap-1.5 text-xs text-gray-600">
          <input type="checkbox" v-model="simExecute" class="accent-blue-600" /> 模拟执行（连接真实库）
        </label>
        <button class="px-4 py-2 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition" :disabled="!simUser || !simSql" @click="runSimulate">模拟</button>
        <span v-if="loading.simulate" class="text-xs text-gray-400">运行中…</span>
      </div>

      <div v-if="simResult" class="grid gap-4">
        <div class="bg-white border border-gray-200 rounded-xl p-4">
          <div class="flex items-center justify-between mb-2">
            <h3 class="text-sm font-bold text-gray-800">改写结果</h3>
            <span v-if="simResult.error" class="text-[10px] px-2 py-0.5 bg-red-50 text-red-600 border border-red-200 rounded-full">已拒绝</span>
            <span v-else class="text-[10px] px-2 py-0.5 bg-blue-50 text-blue-600 border border-blue-100 rounded-full">已改写</span>
          </div>
          <pre class="text-[11px] font-mono bg-gray-50 border border-gray-100 rounded-lg p-3 overflow-x-auto text-gray-700">{{ simResult.error || simResult.rewritten_sql }}</pre>
        </div>
        <!-- 字段名必须跟后端一致：enforcer.rewrite_sql 返回的 applied 只有
             masked / hidden / row_filters / denied（外加 agg_ok）。
             原来这里读的是 masks / denied_columns / metric_overrides，后端根本没这几个键，
             于是整张卡片的 v-if 永远为假 —— 配了脱敏也看不到"到底改了哪几列"。 -->
        <div v-if="appliedItems.length" class="bg-white border border-gray-200 rounded-xl p-4">
          <h3 class="text-sm font-bold text-gray-800 mb-2">应用明细</h3>
          <div class="space-y-1.5 text-xs">
            <div v-for="(m, i) in appliedItems" :key="'a' + i" :class="m.tone">
              {{ m.label }}<span v-if="m.detail" class="text-gray-400"> · {{ m.detail }}</span>
              <span v-if="m.code" class="font-mono text-[10px] text-gray-400 ml-1">{{ m.code }}</span>
            </div>
          </div>
        </div>
        <div v-if="simResult.execution" class="bg-white border border-gray-200 rounded-xl p-4">
          <h3 class="text-sm font-bold text-gray-800 mb-2">执行结果</h3>
          <div v-if="simResult.execution.success" class="text-xs text-gray-500 mb-1">共 {{ simResult.execution.row_count }} 行</div>
          <div v-else class="text-xs text-red-500 mb-1">{{ simResult.execution.error }}</div>
          <pre class="text-[10px] font-mono bg-gray-50 border border-gray-100 rounded-lg p-3 overflow-x-auto text-gray-600">{{ JSON.stringify(simResult.execution.rows || [], null, 2) }}</pre>
        </div>
      </div>
    </div>

    <!-- ═══════════ 弹窗：角色 ═══════════ -->
    <div v-if="roleEditing !== null" class="fixed inset-0 bg-black/30 flex items-center justify-center z-50" @click.self="roleEditing = null">
      <div class="bg-white rounded-xl p-5 w-[440px] shadow-xl">
        <h3 class="text-sm font-bold text-gray-800 mb-4">{{ roleEditing.key ? '编辑角色' : '新增角色' }}</h3>
        <div class="space-y-3 text-xs">
          <label class="block">
            <span class="text-gray-500">角色 key（唯一标识，字母数字下划线）*</span>
            <input v-model="roleEditing.key" :disabled="!!roleEditing.origKey" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg disabled:bg-gray-50 disabled:text-gray-400" />
          </label>
          <label class="block">
            <span class="text-gray-500">角色名称 *</span>
            <input v-model="roleEditing.name" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg" placeholder="如：区域销售经理" />
          </label>
          <label class="block">
            <span class="text-gray-500">优先级（数字越大越优先；行/列/指标策略冲突时生效）</span>
            <input v-model.number="roleEditing.priority" type="number" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg" />
          </label>
          <label class="block">
            <span class="text-gray-500">描述</span>
            <textarea v-model="roleEditing.description" rows="2" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg" />
          </label>
          <label v-if="!roleEditing.origKey" class="block">
            <span class="text-gray-500">初始策略模板（可选，创建后自动套用）</span>
            <select v-model="roleEditing.template" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg">
              <option value="">不使用模板（空策略）</option>
              <option v-for="tpl in roleTemplates" :key="tpl.key" :value="tpl.key">{{ tpl.name }} — {{ tpl.description }}</option>
            </select>
          </label>
        </div>
        <div class="flex justify-end gap-2 mt-5">
          <button class="px-3 py-1.5 text-xs bg-gray-100 hover:bg-gray-200 rounded-lg" @click="roleEditing = null">取消</button>
          <button class="px-3 py-1.5 text-xs bg-blue-600 text-white hover:bg-blue-700 rounded-lg" @click="saveRole">保存</button>
        </div>
      </div>
    </div>

    <!-- ═══════════ 弹窗：数据集 ═══════════ -->
    <div v-if="dsEditing !== null" class="fixed inset-0 bg-black/30 flex items-center justify-center z-50" @click.self="dsEditing = null">
      <div class="bg-white rounded-xl p-5 w-[520px] shadow-xl">
        <h3 class="text-sm font-bold text-gray-800 mb-4">{{ dsEditing.origKey ? '编辑数据集' : '新增数据集' }}</h3>
        <div class="space-y-3 text-xs">
          <div class="grid grid-cols-2 gap-3">
            <label class="block">
              <span class="text-gray-500">数据集 key *</span>
              <input v-model="dsEditing.key" :disabled="!!dsEditing.origKey" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg disabled:bg-gray-50 disabled:text-gray-400" />
            </label>
            <label class="block">
              <span class="text-gray-500">名称 *</span>
              <input v-model="dsEditing.name" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg" />
            </label>
          </div>
          <label class="block">
            <span class="text-gray-500">包含表（逗号分隔，可带 schema）</span>
            <input v-model="dsTablesText" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg font-mono" placeholder="mes_process_output, mes_work_order" />
          </label>
          <label class="block">
            <span class="text-gray-500">描述</span>
            <input v-model="dsEditing.description" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg" />
          </label>
          <label class="flex items-center gap-2 text-gray-600">
            <input type="checkbox" v-model="dsEditing.sensitive" class="accent-red-600" />
            敏感数据集（变更授权需审批）
          </label>
        </div>
        <div class="flex justify-end gap-2 mt-5">
          <button class="px-3 py-1.5 text-xs bg-gray-100 hover:bg-gray-200 rounded-lg" @click="dsEditing = null">取消</button>
          <button class="px-3 py-1.5 text-xs bg-blue-600 text-white hover:bg-blue-700 rounded-lg" @click="saveDataset">保存</button>
        </div>
      </div>
    </div>

    <!-- ═══════════ 弹窗：生效权限白盒 ═══════════ -->
    <div v-if="effectiveUser" class="fixed inset-0 bg-black/30 flex items-center justify-center z-50" @click.self="effectiveUser = ''">
      <div class="bg-white rounded-xl p-5 w-[640px] max-h-[80vh] overflow-y-auto shadow-xl">
        <h3 class="text-sm font-bold text-gray-800 mb-1">生效权限 · {{ effectiveUser }}</h3>
        <p class="text-[11px] text-gray-400 mb-3">多角色合并后的引擎层拦截结果（与查询链路完全一致）</p>
        <pre v-if="effectiveAcl" class="text-[11px] font-mono bg-gray-50 border border-gray-100 rounded-lg p-3 overflow-x-auto text-gray-700">{{ JSON.stringify(effectiveAcl, null, 2) }}</pre>
        <div class="flex justify-end mt-4">
          <button class="px-3 py-1.5 text-xs bg-gray-100 hover:bg-gray-200 rounded-lg" @click="effectiveUser = ''">关闭</button>
        </div>
      </div>
    </div>

    <!-- ═══════════ 弹窗：账号维护（编辑资料 / 重置密码）═══════════ -->
    <div v-if="showMaintain" class="fixed inset-0 bg-black/40 flex items-center justify-center z-50" @click.self="showMaintain = false">
      <div class="rounded-2xl shadow-2xl w-[520px] max-h-[86vh] overflow-hidden flex flex-col" style="background:#ffffff;color:#1d2129">
        <div class="px-5 pt-4 pb-3 border-b" style="border-color:#e5e6eb">
          <h3 class="text-sm font-bold" style="color:#1d2129">
            {{ maintainMode === 'password' ? '重置密码' : '编辑资料' }} · {{ maintain.username }}
          </h3>
          <p class="text-[11px] mt-1" style="color:#86909c">
            <template v-if="maintainMode === 'password'">改成新密码后，这个人用旧密码就登不进来了。</template>
            <template v-else>这些信息只用于辨认和联系，不影响他能看什么数据。</template>
          </p>
        </div>

        <div class="px-5 py-4 space-y-3 overflow-y-auto text-xs">
          <!-- 重置密码 -->
          <template v-if="maintainMode === 'password'">
            <label class="block">
              <span style="color:#4e5969">新密码</span>
              <input v-model="maintain.password" type="password" class="mt-1 w-full px-2.5 py-2 border rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-blue-200" style="border-color:#c9cdd4" placeholder="至少 8 位，含字母和数字" />
            </label>
            <label class="block">
              <span style="color:#4e5969">再输一遍</span>
              <input v-model="maintain.confirm" type="password" class="mt-1 w-full px-2.5 py-2 border rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-blue-200" style="border-color:#c9cdd4" placeholder="确认新密码" @keyup.enter="submitMaintain" />
            </label>
            <div class="text-[11px] leading-relaxed" style="color:#8a6d1b;background:#fbf3d9;padding:6px 8px;border-radius:8px">
              密码由后端强校验：至少 8 位，必须同时包含字母和数字。改完请当面或通过可靠渠道告诉本人。
            </div>
          </template>

          <!-- 编辑资料 -->
          <template v-else>
            <div class="grid grid-cols-2 gap-3">
              <label class="block">
                <span style="color:#4e5969">姓名</span>
                <input v-model="maintain.display_name" class="mt-1 w-full px-2.5 py-2 border rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-blue-200" style="border-color:#c9cdd4" />
              </label>
              <label class="block">
                <span style="color:#4e5969">登录名</span>
                <input :value="maintain.username" disabled class="mt-1 w-full px-2.5 py-2 border rounded-lg text-xs bg-gray-50 text-gray-400" style="border-color:#e5e6eb" />
              </label>
              <label class="block">
                <span style="color:#4e5969">部门</span>
                <input v-model="maintain.department" class="mt-1 w-full px-2.5 py-2 border rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-blue-200" style="border-color:#c9cdd4" placeholder="如：生产部" />
              </label>
              <label class="block">
                <span style="color:#4e5969">职位</span>
                <input v-model="maintain.title" class="mt-1 w-full px-2.5 py-2 border rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-blue-200" style="border-color:#c9cdd4" placeholder="如：设备维护" />
              </label>
              <label class="block">
                <span style="color:#4e5969">邮箱</span>
                <input v-model="maintain.email" class="mt-1 w-full px-2.5 py-2 border rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-blue-200" style="border-color:#c9cdd4" />
              </label>
              <label class="block">
                <span style="color:#4e5969">手机</span>
                <input v-model="maintain.phone" class="mt-1 w-full px-2.5 py-2 border rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-blue-200" style="border-color:#c9cdd4" />
              </label>
            </div>
            <label class="block">
              <span style="color:#4e5969">备注</span>
              <input v-model="maintain.note" class="mt-1 w-full px-2.5 py-2 border rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-blue-200" style="border-color:#c9cdd4" placeholder="如：负责哪条产线" />
            </label>
          </template>

          <div v-if="maintain.msg" class="text-[11px] px-2 py-1.5 rounded-lg"
            :style="maintain.ok ? 'color:#1677ff;background:#e8f3ff' : 'color:#b34a3a;background:#fdf0ed'">{{ maintain.msg }}</div>
        </div>

        <div class="px-5 py-3 border-t flex items-center justify-end gap-2" style="border-color:#e5e6eb;background:#fafbfc">
          <button class="px-3 py-1.5 text-xs rounded-lg border" style="border-color:#c9cdd4;color:#4e5969" @click="showMaintain = false">取消</button>
          <button
            class="px-4 py-1.5 text-xs rounded-lg font-medium text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-40"
            :disabled="maintain.saving"
            @click="submitMaintain"
          >{{ maintain.saving ? '提交中…' : (maintainMode === 'password' ? '重置密码' : '保存') }}</button>
        </div>
      </div>
    </div>

    <!-- ═══════════ 弹窗：危险操作二次确认 ═══════════ -->
    <div v-if="confirmBox.show" class="fixed inset-0 bg-black/40 flex items-center justify-center z-[60]" @click.self="confirmBox.show = false">
      <div class="rounded-2xl shadow-2xl w-[420px] overflow-hidden" style="background:#ffffff;color:#1d2129">
        <div class="px-5 pt-4 pb-3">
          <h3 class="text-sm font-bold" :style="confirmBox.danger ? 'color:#b34a3a' : 'color:#1d2129'">{{ confirmBox.title }}</h3>
          <p class="text-xs mt-2 leading-relaxed" style="color:#4e5969">{{ confirmBox.body }}</p>
        </div>
        <div class="px-5 py-3 border-t flex items-center justify-end gap-2" style="border-color:#e5e6eb;background:#fafbfc">
          <button class="px-3 py-1.5 text-xs rounded-lg border" style="border-color:#c9cdd4;color:#4e5969" @click="confirmBox.show = false">取消</button>
          <button
            class="px-4 py-1.5 text-xs rounded-lg font-medium text-white disabled:opacity-40"
            :style="confirmBox.danger ? 'background:#b34a3a' : 'background:#1677ff'"
            :disabled="confirmBox.running"
            @click="runConfirm"
          >{{ confirmBox.running ? '处理中…' : '确认' }}</button>
        </div>
      </div>
    </div>

    <!-- ═══════════ 弹窗：快捷向导 ═══════════ -->
    <div v-if="showWizard" class="fixed inset-0 bg-black/40 flex items-center justify-center z-50" @click.self="showWizard = false">
      <div class="w-[760px] max-h-[88vh] overflow-hidden shadow-2xl rounded-2xl flex flex-col" style="background:#ffffff;color:#1d2129">
        <!-- 头部 -->
        <div class="px-6 pt-5 pb-4 border-b" style="border-color:#e5e6eb">
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-2">
              <h3 class="text-base font-bold" style="color:#1d2129">快速配置权限</h3>
              <span class="text-[11px] px-1.5 py-0.5 rounded" style="color:#1677ff;background:#f2f3f5">第 {{ wizardStep }} / {{ wizardSteps.length }} 步</span>
            </div>
            <button class="text-lg leading-none px-1 hover:opacity-70" style="color:#a9aeb8" @click="showWizard = false" aria-label="关闭"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M18 6 6 18" /> <path d="m6 6 12 12" /> </svg></span></button>
          </div>
          <p class="text-xs mt-1.5" style="color:#4e5969">配「能看什么数据、数据怎么保护」。可以配给整个角色，也可以只挑几位员工（没账号可当场建）。敏感变更自动走审批留痕。</p>
        </div>

        <!-- 步骤条 -->
        <div class="px-6 pt-4">
          <div class="flex items-center">
            <template v-for="(s, i) in wizardSteps" :key="s">
              <div class="flex items-center gap-1.5">
                <div class="w-6 h-6 rounded-full grid place-items-center text-xs font-semibold transition"
                  :style="i + 1 === wizardStep ? 'background:#4D9EFF;color:#ffffff;border:1px solid #4D9EFF' : (i + 1 < wizardStep ? 'background:#2E7CF0;color:#ffffff' : 'background:#fff;color:#a9aeb8;border:1px solid #e5e6eb')"
                >{{ i + 1 }}</div>
                <span class="text-xs whitespace-nowrap" :style="i + 1 === wizardStep ? 'color:#1d2129;font-weight:600' : 'color:#a9aeb8'">{{ s }}</span>
              </div>
              <div v-if="i < wizardSteps.length - 1" class="flex-1 h-px mx-2" :style="i + 1 < wizardStep ? 'background:#4D9EFF' : 'background:#e5e6eb'"></div>
            </template>
          </div>
        </div>

        <!-- 内容区 -->
        <div class="px-6 py-5 flex-1 overflow-y-auto">
          <!-- Step 1: 给谁用 -->
          <div v-if="wizardStep === 1" class="space-y-4">
            <div>
              <div class="text-sm font-semibold" style="color:#1d2129">配给谁？</div>
              <p class="text-xs mt-1 leading-relaxed" style="color:#4e5969">
                两种配法二选一：给<strong>整个角色</strong>配默认权限，或者只给<strong>几位指定员工</strong>开数据范围（员工还没账号可以当场建）。<br />
                超级管理员（{{ roleName('admin') }}）是系统内置的唯一管理员，拥有全部权限，不需要也不能在这里创建。
              </p>
            </div>

            <!-- 配置对象：整个角色 / 指定员工 -->
            <div class="grid grid-cols-2 gap-2.5">
              <button
                class="text-left border rounded-xl p-3 transition"
                :style="wizardTargetMode === 'role' ? 'background:#4D9EFF;border-color:#4D9EFF' : 'background:#fff;border-color:#e5e6eb'"
                @click="wizardTargetMode = 'role'"
              >
                <div class="text-xs font-semibold" style="color:#1d2129">整个角色</div>
                <div class="text-[11px] mt-1 leading-relaxed" :style="wizardTargetMode === 'role' ? 'color:#eaf3ff' : 'color:#4e5969'">
                  该角色下所有员工统一生效，改动一次全体同步
                </div>
              </button>
              <button
                class="text-left border rounded-xl p-3 transition"
                :style="wizardTargetMode === 'users' ? 'background:#4D9EFF;border-color:#4D9EFF' : 'background:#fff;border-color:#e5e6eb'"
                @click="wizardTargetMode = 'users'"
              >
                <div class="text-xs font-semibold" style="color:#1d2129">指定员工</div>
                <div class="text-[11px] mt-1 leading-relaxed" :style="wizardTargetMode === 'users' ? 'color:#eaf3ff' : 'color:#4e5969'">
                  只挑几位员工开权限，可当场新建员工账号
                </div>
              </button>
            </div>

            <!-- 配置哪个角色 —— 两种模式共用。
                 原先它嵌在「整个角色」分支里，导致在「指定员工」模式下想建人时
                 看不到自己选的是哪个身份，只能靠默认值（viewer）兜底，建出来的人
                 就挂到了错误身份上。提到外面，让「建号」和「身份」始终绑在一起。 -->
            <div class="border rounded-xl p-4" style="border-color:#e5e6eb;background:#f7f8fa">
              <div class="text-xs" style="color:#a9aeb8">配置哪个角色</div>
              <select
                v-model="wizardRole"
                class="mt-1.5 w-full px-2.5 py-1.5 border rounded-lg text-xs"
                style="border-color:#c9cdd4;background:#fff"
              >
                <option v-for="r in wizardTargets" :key="r.key" :value="r.key">{{ r.name }}</option>
              </select>
              <div class="text-[11px] mt-2" style="color:#4e5969">
                <template v-if="wizardTargetMode === 'role'">
                  当前该角色下有 <strong>{{ roleUserCount(wizardRole) }}</strong> 名员工，配好后他们统一生效。
                </template>
                <template v-else>
                  在这里新建员工，会直接以「<strong>{{ roleName(wizardRole) }}</strong>」身份创建，不会再挂到别的身份上。
                </template>
              </div>
            </div>

            <!-- ① 整个角色：角色选择已提到上方共用，这里不再重复 -->
            <div v-if="wizardTargetMode === 'role'" class="space-y-3"></div>

            <!-- ② 指定员工 -->
            <div v-else class="border rounded-xl overflow-hidden" style="border-color:#e5e6eb">
              <div class="flex items-center justify-between px-3 py-2.5" style="border-bottom:1px solid #f2f3f5;background:#f7f8fa">
                <div class="text-xs" style="color:#4e5969">
                  勾选员工 <strong style="color:#1677ff">{{ wizardUsers.length }}</strong> 人已选
                </div>
                <button
                  class="px-2.5 py-1 text-[11px] rounded-lg transition"
                  :style="newEmp.open ? 'background:#e8f3ff;color:#1677ff;border:1px solid #4D9EFF' : 'background:#1d2129;color:#fff'"
                  @click="toggleNewEmpForm"
                >{{ newEmp.open ? '收起' : '＋ 新建员工' }}</button>
              </div>

              <!-- 新建员工表单 -->
              <div v-if="newEmp.open" class="px-3 py-3 space-y-2" style="border-bottom:1px solid #f2f3f5;background:#fbfcfe">
                <div class="grid grid-cols-3 gap-2">
                  <input v-model="newEmp.username" placeholder="登录名（必填）" class="px-2.5 py-1.5 border rounded-lg text-xs" style="border-color:#c9cdd4;background:#fff" />
                  <input v-model="newEmp.display_name" placeholder="姓名（可空，默认同登录名）" class="px-2.5 py-1.5 border rounded-lg text-xs" style="border-color:#c9cdd4;background:#fff" />
                  <input v-model="newEmp.password" type="password" placeholder="初始密码（必填）" class="px-2.5 py-1.5 border rounded-lg text-xs" style="border-color:#c9cdd4;background:#fff" />
                </div>
                <div class="flex items-center gap-2">
                  <button
                    class="px-3 py-1.5 text-[11px] rounded-lg font-medium disabled:opacity-40"
                    style="background:#4D9EFF;color:#fff"
                    :disabled="newEmp.saving"
                    @click="createWizardEmp"
                  >{{ newEmp.saving ? '创建中…' : '创建并勾选' }}</button>
                  <span v-if="newEmp.msg" class="text-[11px]" :style="newEmp.ok ? 'color:#1677ff' : 'color:#b34a3a'">{{ newEmp.msg }}</span>
                </div>
                <div class="text-[11px] leading-relaxed" style="color:#8a6d1b;background:#fbf3d9;padding:6px 8px;border-radius:8px">
                  密码至少 8 位且包含字母和数字（这是后端强校验）。新账号直接以「{{ roleName(wizardRole) }}」身份创建，建完即可用。
                </div>
              </div>

              <!-- 员工勾选列表 -->
              <div class="max-h-[220px] overflow-y-auto p-2 space-y-1">
                <button
                  v-for="u in wizardEmployeeChoices" :key="u.username"
                  class="w-full text-left px-2.5 py-2 rounded-lg flex items-center justify-between gap-2 transition"
                  :style="wizardUsers.includes(u.username) ? 'background:#e8f3ff;border:1px solid #4D9EFF' : 'background:#fff;border:1px solid #f2f3f5'"
                  @click="toggleWizardUser(u.username)"
                >
                  <span class="min-w-0 truncate">
                    <span class="text-xs font-medium" style="color:#1d2129">{{ u.display_name || u.username }}</span>
                    <span class="text-[11px] ml-1.5" style="color:#a9aeb8">@{{ u.username }}</span>
                    <span v-if="u.has_grants" class="text-[10px] ml-1.5 px-1.5 py-px rounded" style="color:#8a6d1b;background:#fbf3d9">已有例外授权</span>
                  </span>
                  <span class="shrink-0 text-[11px]" style="color:#4e5969">{{ (u.roles && u.roles.length ? u.roles : [u.role]).map(roleName).join(' / ') }}</span>
                </button>
                <div v-if="!wizardEmployeeChoices.length" class="text-xs py-6 text-center" style="color:#a9aeb8">
                  还没有员工账号，点右上角「<span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M5 12h14" /> <path d="M12 5v14" /> </svg></span> 新建员工」建一个。
                </div>
              </div>
            </div>
          </div>

          <!-- Step 2: 能看什么 -->
          <div v-else-if="wizardStep === 2" class="space-y-4">
            <div>
              <div class="text-sm font-semibold" style="color:#1d2129">这个角色能看哪些数据？</div>
              <p class="text-xs mt-1" style="color:#4e5969">勾选数据域（可多选）。不勾选任何一项 = 该角色看不到任何数据。</p>
            </div>
            <div class="grid grid-cols-2 gap-2.5">
              <button
                v-for="d in model.datasets" :key="d.key"
                class="text-left border rounded-xl p-3 transition flex items-start gap-2.5"
                :style="wizardDatasets.includes(d.key) ? 'background:#4D9EFF;border-color:#4D9EFF' : 'background:#fff;border-color:#e5e6eb'"
                @click="toggleDataset(d.key)"
              >
                <span class="mt-0.5 w-4 h-4 rounded border grid place-items-center text-[11px] leading-none shrink-0"
                  :style="wizardDatasets.includes(d.key) ? 'background:#1d2129;color:#1677ff;border-color:#1d2129' : 'border-color:#c9cdd4;color:transparent'"
                ><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <polyline points="20 6 9 17 4 12" /> </svg></span></span>
                <span class="min-w-0">
                  <span class="flex items-center gap-1.5">
                    <span class="text-xs font-semibold" style="color:#1d2129">{{ d.name }}</span>
                    <span v-if="d.sensitive" class="text-[10px] px-1 py-px rounded" style="color:#b34a3a;background:#f7e7e4">敏感</span>
                  </span>
                  <span class="block text-[11px] mt-0.5 leading-relaxed" style="color:#4e5969">{{ d.description || '（无描述）' }}</span>
                  <span class="block text-[10px] mt-1" style="color:#a9aeb8">{{ (d.tables || []).length }} 张表<span v-if="datasetMissingCount(d)" style="color:#b34a3a"> · 当前库缺 {{ datasetMissingCount(d) }} 张</span></span>
                </span>
              </button>
              <div v-if="!model.datasets.length" class="col-span-2 text-xs py-6 text-center" style="color:#a9aeb8">暂无数据域，请先到「数据集」页创建</div>
            </div>
          </div>

          <!-- Step 3: 数据保护 -->
          <div v-else-if="wizardStep === 3" class="space-y-4">
            <!-- 指定员工模式：规则按角色下发，这里不做 -->
            <template v-if="wizardTargetMode === 'users'">
              <div>
                <div class="text-sm font-semibold" style="color:#1d2129">数据保护（这一步不适用）</div>
                <p class="text-xs mt-1 leading-relaxed" style="color:#4e5969">
                  脱敏、行过滤、指标口径这几类规则，引擎是<strong>按角色下发</strong>的 —— 配一次，该角色下所有员工都会生效，做不到"只对某一个人"。
                  所以「指定员工」模式只配数据范围，不配规则。
                </p>
              </div>
              <div class="text-xs rounded-xl px-3 py-2.5 leading-relaxed" style="color:#8a6d1b;background:#fbf3d9">
                要给这批人单独加脱敏或行过滤，两条路：① 先给他们建一个专用角色（「角色管理」→「<span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M5 12h14" /> <path d="M12 5v14" /> </svg></span> 新增角色」），再用本轮向导的「整个角色」模式配规则；
                ② 切回上一步把对象改成「整个角色」。
              </div>
            </template>
            <template v-else>
            <div>
              <div class="text-sm font-semibold" style="color:#1d2129">需要额外的数据保护吗？</div>
              <p class="text-xs mt-1" style="color:#4e5969">可选。给敏感字段加保护规则，不选也可以直接进入下一步。</p>
            </div>

            <!-- 常用规则 -->
            <div class="grid grid-cols-2 gap-2.5">
              <button
                v-for="t in templates.filter(x => !x.group || x.group === 'common')" :key="t.key"
                class="text-left border rounded-xl p-3 transition"
                :style="wizardTemplates.includes(t.key) ? 'background:#4D9EFF;border-color:#4D9EFF' : 'background:#fff;border-color:#e5e6eb'"
                @click="toggleTemplate(t.key)"
              >
                <div class="flex items-center justify-between">
                  <span class="text-xs font-semibold" style="color:#1d2129">{{ t.icon }} {{ t.name }}</span>
                  <span class="w-4 h-4 rounded border grid place-items-center text-[11px] leading-none"
                    :style="wizardTemplates.includes(t.key) ? 'background:#1d2129;color:#1677ff;border-color:#1d2129' : 'border-color:#c9cdd4;color:transparent'"
                  ><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <polyline points="20 6 9 17 4 12" /> </svg></span></span>
                </div>
                <div class="text-[11px] mt-1 leading-relaxed" style="color:#4e5969">{{ t.desc }}</div>
                <div class="text-[10px] mt-1.5 inline-block px-1.5 py-px rounded" style="color:#1677ff;background:#f2f3f5">{{ t.effect }}</div>
              </button>
            </div>

            <!-- 高级选项 -->
            <details class="border rounded-xl" style="border-color:#e5e6eb;background:#fff">
              <summary class="px-3 py-2.5 text-xs cursor-pointer select-none" style="color:#4e5969">高级选项（指标口径等）</summary>
              <div class="px-3 pb-3 grid grid-cols-2 gap-2.5">
                <button
                  v-for="t in templates.filter(x => x.group === 'advanced')" :key="t.key"
                  class="text-left border rounded-xl p-3 transition"
                  :style="wizardTemplates.includes(t.key) ? 'background:#4D9EFF;border-color:#4D9EFF' : 'background:#ffffff;border-color:#e5e6eb'"
                  @click="toggleTemplate(t.key)"
                >
                  <div class="flex items-center justify-between">
                    <span class="text-xs font-semibold" style="color:#1d2129">{{ t.icon }} {{ t.name }}</span>
                    <span class="w-4 h-4 rounded border grid place-items-center text-[11px] leading-none"
                      :style="wizardTemplates.includes(t.key) ? 'background:#1d2129;color:#1677ff;border-color:#1d2129' : 'border-color:#c9cdd4;color:transparent'"
                    ><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <polyline points="20 6 9 17 4 12" /> </svg></span></span>
                  </div>
                  <div class="text-[11px] mt-1 leading-relaxed" style="color:#4e5969">{{ t.desc }}</div>
                </button>
              </div>
            </details>

            <!-- 已选规则的参数 -->
            <div v-for="t in selectedRules.filter(x => x.params?.length)" :key="'p' + t.key" class="border rounded-xl p-3" style="border-color:#e5e6eb;background:#fff">
              <div class="flex items-center justify-between mb-2">
                <span class="text-xs font-semibold" style="color:#1d2129">{{ t.icon }} {{ t.name }} · 细化设置</span>
                <button class="text-[11px]" style="color:#a9aeb8" @click="toggleTemplate(t.key)">移除</button>
              </div>
              <div class="grid grid-cols-2 gap-2">
                <label v-for="p in t.params!" :key="p.key" class="block text-xs">
                  <span style="color:#4e5969">{{ p.label }}</span>
                  <select v-if="p.type === 'select'" v-model="wizardParams[p.key]" class="mt-1 w-full px-2.5 py-1.5 border rounded-lg text-xs" style="border-color:#c9cdd4;background:#fff">
                    <option v-for="o in p.options" :key="o.value" :value="o.value">{{ o.label }}</option>
                  </select>
                </label>
              </div>
              <div v-if="t.hint" class="text-[11px] mt-2 leading-relaxed" style="color:#8a6d1a;background:#fbf3d9;padding:6px 8px;border-radius:8px">{{ t.hint }}</div>
            </div>
            </template>
          </div>

          <!-- Step 4: 确认 -->
          <div v-else class="space-y-4">
            <div>
              <div class="text-sm font-semibold" style="color:#1d2129">确认配置</div>
              <p class="text-xs mt-1" style="color:#4e5969">
                {{ wizardTargetMode === 'users'
                  ? '检查无误后点「应用配置」。会给这些员工补开你勾的数据表，员工原有的角色权限保留不动。'
                  : '检查无误后点「应用配置」。会合并进该角色已有的默认配置，不覆盖你之前设置的内容。' }}
              </p>
            </div>
            <div class="border rounded-xl overflow-hidden" style="border-color:#e5e6eb">
              <div class="grid grid-cols-[90px_1fr] text-xs" style="border-bottom:1px solid #f2f3f5">
                <div class="px-3 py-2.5 font-medium" style="color:#4e5969;background:#f7f8fa">配给谁</div>
                <div class="px-3 py-2.5" style="color:#1d2129">
                  <span class="text-[10px] px-1.5 py-px rounded mr-1.5" style="color:#1677ff;background:#e8f3ff">{{ wizardSummary.whoLabel }}</span>{{ wizardSummary.role }}
                </div>
              </div>
              <div class="grid grid-cols-[90px_1fr] text-xs" style="border-bottom:1px solid #f2f3f5">
                <div class="px-3 py-2.5 font-medium" style="color:#4e5969;background:#f7f8fa">能看什么</div>
                <div class="px-3 py-2.5" style="color:#1d2129">{{ wizardSummary.datasets }}</div>
              </div>
              <div v-if="wizardTargetMode === 'role'" class="grid grid-cols-[90px_1fr] text-xs" style="border-bottom:1px solid #f2f3f5">
                <div class="px-3 py-2.5 font-medium" style="color:#4e5969;background:#f7f8fa">数据保护</div>
                <div class="px-3 py-2.5" style="color:#1d2129">{{ wizardSummary.rules }}</div>
              </div>
              <div v-if="wizardTargetMode === 'users'" class="grid grid-cols-[90px_1fr] text-xs" style="border-bottom:1px solid #f2f3f5">
                <div class="px-3 py-2.5 font-medium" style="color:#4e5969;background:#f7f8fa">附带</div>
                <div class="px-3 py-2.5" style="color:#1d2129">把这 {{ wizardUsers.length }} 人归入「{{ roleName(wizardRole) }}」角色</div>
              </div>
              <div v-if="wizardTargetMode === 'users'" class="grid grid-cols-[90px_1fr] text-xs">
                <div class="px-3 py-2.5 font-medium" style="color:#4e5969;background:#f7f8fa">实际开表</div>
                <div class="px-3 py-2.5" style="color:#1d2129">
                  <span v-if="datasetTablePlan.ok.length">共 {{ datasetTablePlan.ok.length }} 张：{{ datasetTablePlan.ok.join('、') }}</span>
                  <span v-else style="color:#b34a3a">0 张 —— 所选数据域的表在当前连接的库里都不存在，先去「系统设置」确认连的是哪个库</span>
                </div>
              </div>
            </div>
            <div v-if="wizardTargetMode === 'users' && datasetTablePlan.missing.length" class="text-xs rounded-lg px-3 py-2.5 leading-relaxed" style="color:#8a6d1b;background:#fbf3d9">
              有 <strong>{{ datasetTablePlan.missing.length }}</strong> 张表在当前连接的库里找不到（{{ datasetTablePlan.missing.join('、') }}），应用时会自动跳过。
              这通常是「系统设置」里换过数据库、而数据域还指向老库的表。要么切回原来的库，要么到「数据集」把这几张表改成新库的表名。
            </div>
            <div v-if="!wizardSummary.ready" class="text-xs rounded-lg px-3 py-2.5" style="color:#b34a3a;background:#f7e7e4">
              {{ wizardSummary.hint }}
            </div>
          </div>
        </div>

        <!-- 底部按钮 -->
        <div class="px-6 py-4 border-t flex items-center justify-between" style="border-color:#e5e6eb;background:#ffffff">
          <button v-if="wizardStep > 1" class="px-3.5 py-2 text-xs rounded-lg" style="color:#4e5969;background:#fff;border:1px solid #c9cdd4" @click="wizardStep--">上一步</button>
          <span v-else />
          <div class="flex gap-2">
            <button class="px-3.5 py-2 text-xs rounded-lg" style="color:#4e5969;background:#fff;border:1px solid #c9cdd4" @click="showWizard = false">取消</button>
            <button
              v-if="wizardStep < 4"
              class="px-4 py-2 text-xs rounded-lg font-medium transition disabled:opacity-40 disabled:cursor-not-allowed"
              style="background:#1d2129;color:#ffffff"
              :disabled="wizardStep === 1 && !wizardRole"
              @click="wizardStep++"
            >下一步</button>
            <button
              v-else
              class="px-4 py-2 text-xs rounded-lg font-medium transition disabled:opacity-40 disabled:cursor-not-allowed"
              style="background:#1d2129;color:#ffffff"
              :disabled="applying || !wizardSummary.ready"
              @click="applyWizard"
            >{{ applying ? '应用中…' : '应用配置' }}</button>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- ═══════════ 员工数据授权（per-user 表/字段白名单）═══════════ -->
  <div v-if="activeTab === 'emp_grants'" class="space-y-4">
    <div class="bg-amber-50 border border-amber-200 rounded-xl p-3 text-xs text-amber-800 leading-relaxed">
      <strong><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" /> <path d="M12 9v4" /> <path d="M12 17h.01" /> </svg></span> 例外场景，常规请用「角色 + 用户属性」。</strong>
      本页为某位员工单独指定可查看的<strong>表</strong>与<strong>字段</strong>，适用于无法用角色覆盖的少数例外。
      常规做法：先建角色（可在「角色管理」套用模板）→ 把员工加入角色 → 配用户属性（如 region=华东），行级权限按属性自动过滤。
      提交后进入审计留痕（敏感变更走审批流）。
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-[260px_1fr] gap-4">
      <!-- 左：员工列表 -->
      <div class="border border-gray-200 rounded-xl p-3">
        <div class="text-xs font-semibold text-gray-600 mb-2">选择员工</div>
        <div class="space-y-1 max-h-[60vh] overflow-auto">
          <button v-for="u in empUsers" :key="u.username"
            class="w-full text-left px-2.5 py-2 rounded-lg text-sm flex items-center justify-between gap-2"
            :class="empSelected === u.username ? 'bg-blue-600 text-white' : 'hover:bg-gray-100 text-gray-700'"
            @click="selectEmp(u.username)">
            <span class="truncate">
              <span class="font-medium">{{ u.display_name || u.username }}</span>
              <span class="text-[11px] opacity-70">@{{ u.username }}</span>
            </span>
            <span v-if="empGrantState[u.username]" class="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-blue-100 text-blue-700">已授权</span>
          </button>
          <div v-if="!empUsers.length" class="text-xs text-gray-400 p-2">暂无员工账号</div>
        </div>
      </div>

      <!-- 右：授权编辑 -->
      <div class="border border-gray-200 rounded-xl p-3" v-if="empSelected">
        <div class="flex items-center justify-between mb-3 gap-2 flex-wrap">
          <div>
            <div class="text-sm font-semibold text-gray-800">为 {{ empSelectedName }} 配置数据访问</div>
            <div class="text-[11px] text-gray-400">勾选表 → 勾选字段；支持按表快速全选</div>
          </div>
          <div class="flex gap-2">
            <button class="px-3 py-1.5 text-xs bg-gray-100 hover:bg-gray-200 rounded-lg" @click="empSelectAllTables">全选表</button>
            <button class="px-3 py-1.5 text-xs bg-blue-600 text-white hover:bg-blue-700 rounded-lg disabled:opacity-40"
              :disabled="empSaving" @click="saveEmpGrants">{{ empSaving ? '保存中…' : '保存授权' }}</button>
          </div>
        </div>

        <div v-if="empLoading" class="text-xs text-gray-400 py-6 text-center">加载中…</div>
        <div v-else class="space-y-2 max-h-[58vh] overflow-auto pr-1">
          <div v-for="t in empSchema" :key="t.table_name"
            class="border border-gray-100 rounded-lg p-2.5 transition"
            :class="empGrants[t.table_name] ? 'bg-blue-50/50 border-blue-200' : ''">
            <label class="flex items-center gap-2 cursor-pointer">
              <input type="checkbox" :checked="!!empGrants[t.table_name]" @change="toggleEmpTable(t)" class="accent-blue-600" />
              <span class="font-medium text-sm text-gray-800">{{ t.table_name }}</span>
              <span v-if="t.chinese_name" class="text-[11px] text-gray-400">（{{ t.chinese_name }}）</span>
              <span class="text-[11px] text-gray-400">· {{ (t.columns || []).length }} 字段</span>
            </label>
            <div v-if="empGrants[t.table_name]" class="mt-2 pl-6 flex flex-wrap gap-1.5">
              <button v-for="c in t.columns" :key="c.name"
                class="text-[11px] px-2 py-1 rounded-full border transition"
                :class="empGrants[t.table_name].includes(c.name) ? 'bg-blue-600 border-blue-600 text-white' : 'bg-white border-gray-200 text-gray-500 hover:border-blue-300'"
                @click="toggleEmpCol(t.table_name, c.name)">{{ c.name }}</button>
            </div>
          </div>
          <div v-if="!empSchema.length" class="text-xs text-gray-400 py-6 text-center">数据库暂无表</div>
        </div>
      </div>
      <div v-else class="border border-dashed border-gray-200 rounded-xl p-10 text-center text-sm text-gray-400">
        请从左侧选择一名员工进行授权
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, nextTick } from 'vue'
import { isAdmin } from '../auth'

// ── 常量 ──────────────────────────────────────────────
interface WizardParam {
  key: string
  label: string
  type: 'select'
  options: { value: string; label: string }[]
}
interface WizardTemplate {
  key: string
  name: string
  desc: string
  effect: string
  icon: string
  group?: 'common' | 'advanced'
  params?: WizardParam[]
  hint?: string
  build: (p: Record<string, string>) => { section: string; value: any }
}

// 快捷向导「数据保护规则」模板（build 产出「策略节 + 值」供合并应用）。
// 数据域（数据集）不再做成模板，改在第 2 步直接用真实数据集勾选。
const templates: WizardTemplate[] = [
  {
    key: 'region_isolation', name: '只看本区域', icon: '◈', group: 'common',
    desc: '每个人只能看到自己负责区域的数据',
    effect: '行级过滤',
    hint: '提示：还需在「用户」页给相关用户填 region 属性（如 region=华东），否则该角色按「拒绝查看」处理。',
    build: () => ({
      section: 'rows',
      value: {
        test_factories: {
          expr: 'city = ${user.region}',
          rule: { field: 'city', op: 'eq', source: 'user.region' },
          enabled: true, note: '快捷向导-只看本区域工厂',
        },
        test_orders: {
          expr: 'factory_id IN (SELECT factory_id FROM test_factories WHERE city = ${user.region})',
          rule: { op: 'in_subquery', ref_table: 'test_factories', on: { left: 'factory_id', right: 'factory_id' }, where: { field: 'city', op: 'eq', source: 'user.region' } },
          enabled: true, note: '快捷向导-只看本区域订单',
        },
      },
    }),
  },
  {
    key: 'customer_mask', name: '客户姓名打码', icon: '◧', group: 'common',
    desc: '订单里的客户姓名用 * 遮挡（如 张伟 → 张**伟）',
    effect: '列级脱敏',
    params: [{ key: 'mask', label: '遮挡方式', type: 'select', options: [
      { value: 'partial_1_1', label: '保留首尾各 1 字（张**伟）' },
      { value: 'partial_3_4', label: '保留前 3 后 4（138****5678）' },
      { value: 'hash', label: '彻底打乱（不可逆）' },
    ] }],
    build: (p) => ({
      section: 'columns',
      value: { test_orders: { customer_name: { mode: 'mask', mask: p.mask || 'partial_1_1', note: '快捷向导-客户名脱敏' } } },
    }),
  },
  {
    key: 'cost_protect', name: '隐藏成本价', icon: '◫', group: 'common',
    desc: '成本价不让看，或取整模糊处理',
    effect: '列级拒绝 / 脱敏',
    params: [{ key: 'mode', label: '保护方式', type: 'select', options: [
      { value: 'deny', label: '完全不可见（查成本价会报错）' },
      { value: 'round_100', label: '百元取整（1234 → 1200）' },
    ] }],
    build: (p) => ({
      section: 'columns',
      value: {
        test_materials: p.mode === 'round_100'
          ? { unit_cost: { mode: 'mask', mask: 'round_100', note: '快捷向导-成本价模糊化' } }
          : { unit_cost: { mode: 'deny', note: '快捷向导-成本价拒绝' } },
      },
    }),
  },
  {
    key: 'supervisor_mask', name: '负责人姓名打码', icon: '◩', group: 'common',
    desc: '产线负责人的姓名用 * 遮挡',
    effect: '列级脱敏',
    params: [{ key: 'mask', label: '遮挡方式', type: 'select', options: [
      { value: 'partial_1_1', label: '保留首尾各 1 字（王芳 → 王**芳）' },
      { value: 'partial_3_4', label: '保留前 3 后 4' },
      { value: 'hash', label: '彻底打乱（不可逆）' },
    ] }],
    build: (p) => ({
      section: 'columns',
      value: { dim_production_line: { supervisor: { mode: 'mask', mask: p.mask || 'partial_1_1', note: '快捷向导-负责人脱敏' } } },
    }),
  },
  {
    key: 'yield_override', name: '产量按总产出算', icon: '∑', group: 'advanced',
    desc: '该角色问「产量」时，按 合格 + 不良 的总产出计算',
    effect: '指标口径',
    hint: '说明：这是高级选项，影响指标「产量」的计算口径，与上面「能看什么」无关。',
    build: () => ({
      section: 'metrics',
      value: {
        产量: {
          mode: 'override', sql_expression: 'SUM(good_qty + defect_qty)',
          formula: '合格产出 + 不良产出（总产出口径）',
          description: '快捷向导-总产出口径', note: '快捷向导',
        },
      },
    }),
  },
]

const tabGroups = [
  // 「角色」必须是一级入口：角色策略（数据集/表/行/列/指标/操作权限）都从这里进入。
  // 此前它不在 tabGroups 里，而 roles 区块只能由它内部的「权限」按钮触发 ——
  // 形成闭环，导致「角色管理 + 操作权限（导出/分享/下载）」在界面上完全不可达。
  { key: 'roleGroup', label: '角色', tabs: [
    { key: 'roles', label: '角色' },
  ] },
  { key: 'members', label: '成员', tabs: [
    { key: 'users', label: '成员与角色' },
    { key: 'emp_grants', label: '例外授权' },
  ] },
  { key: 'datasets', label: '数据集', tabs: [
    { key: 'datasets', label: '数据集' },
  ] },
  { key: 'govern', label: '高级', tabs: [
    { key: 'approvals', label: '审批中心' },
    { key: 'audit', label: '审计日志' },
    { key: 'simulate', label: '模拟器' },
  ] },
]

const activeTab = ref('users')
const activeGroup = ref('members')
const authFullMode = ref(true)   // P4：轻量模式（AUTH_FULL_MODE=0）隐藏审批中心
const visibleGroups = computed(() => tabGroups.map(g => ({
  ...g,
  tabs: g.tabs.filter(t => t.key !== 'approvals' || authFullMode.value),
})))
const currentGroup = computed(() => visibleGroups.value.find(g => g.key === activeGroup.value) || visibleGroups.value[0])
const maskTypes = ref<Array<{ key: string; label: string; example?: string }>>([])
const kindOptions = ref<Record<string, string>>({})
const model = reactive<any>({ version: '', updated_by: '', roles: [], datasets: [], policies: {}, sensitive: {} })
const users = ref<any[]>([])
/**
 * 「新建成员」表单。
 * 原先权限页只能给**已有**成员点选角色，想建新业务人员得跑去账户页的「管理员模式」，
 * 而那儿的角色下拉只列了 viewer / admin，岗位角色根本选不到 —— 于是只能建成 viewer。
 * 这里补上建号入口，角色直接用 model.roles，一次建对。
 */
const showNewUser = ref(false)
const newUser = reactive({ username: '', password: '', display_name: '', role: '', saving: false, msg: '', ok: false })

/**
 * 账号维护弹窗（编辑资料 / 重置密码）。
 *
 * 原先权限页只能改角色，账号本身的事（改名、换部门、重置密码、禁用）都得去
 * 账户页的「管理员模式」——那儿已经删了。管理员要维护账号，这里就得给全。
 * 一个弹窗同时管资料和密码，因为它们对使用者是同一件事："把这个人改一下"。
 *
 * mode: 'profile' 编辑姓名/部门/职位/联系方式；'password' 重置密码（只显示密码输入框）
 */
const showMaintain = ref(false)
const maintainMode = ref<'profile' | 'password'>('profile')
const maintain = reactive({
  username: '', display_name: '', department: '', title: '', email: '', phone: '', note: '',
  password: '', confirm: '',
  saving: false, msg: '', ok: false,
})

/** 危险操作确认弹窗（禁用 / 删除）——不可逆动作必须停下来问一句 */
const confirmBox = reactive({
  show: false, title: '', body: '', danger: false,
  action: null as null | (() => Promise<void>),
  running: false,
})
const metrics = ref<any[]>([])
const requests = ref<any[]>([])
const changes = ref<any[]>([])
const auditLogs = ref<any[]>([])
const loading = reactive<any>({ users: false, approvals: false, simulate: false })
const attrsDraft = reactive<Record<string, string>>({})
const approvalFilter = ref('')
const showRequestForm = ref(false)
const reqForm = reactive({ kind: 'role_grant', reason: '', payloadText: '' })
const isAdminUser = isAdmin()

// 列 / 行 / 指标 高级权限上下文（收进「高级权限」折叠区）
const showAdv = ref(false)          // 高级权限折叠区是否展开
const advTab = ref('columns')       // 折叠区内子切换：columns / rows / metrics
const colRole = ref('')
const colTable = ref('')
const colRules = reactive<Record<string, any>>({})
const rowRole = ref('')
const rowRules = reactive<Record<string, any>>({})
const metricRole = ref('')
const metricRules = reactive<Record<string, any>>({})
const actionRules = reactive<Record<string, boolean>>({ export: false, share: false, download: false })

/** 角色下拉切换（列/行/指标 Tab 共用） */
function onRoleChange(tab: 'col' | 'row' | 'metric', ev: Event) {
  const v = (ev.target as HTMLSelectElement).value
  if (tab === 'col') { colRole.value = v; loadColRules() }
  else if (tab === 'row') { rowRole.value = v; loadRowRules() }
  else { metricRole.value = v; loadMetricRules() }
}

/** 切换「高级权限」二级 tab。
 *  必须先加载该 tab 的数据源再渲染：行规则/指标口径的卡片直接读写 rowRules[t].xxx，
 *  未加载时对象为空 → 模板取值抛错 → 整个列表渲染不出来（表现为空白页）。 */
function setAdvTab(key: 'columns' | 'rows' | 'metrics' | 'actions') {
  advTab.value = key
  if (key === 'columns' && colRole.value) loadColRules()
  else if (key === 'rows' && rowRole.value) loadRowRules()
  else if (key === 'metrics' && metricRole.value) loadMetricRules()
  else if (key === 'actions') loadActionRules()
}

// 弹窗状态
const roleEditing = ref<any>(null)

// ── 员工备注（管理员对每位员工添加说明，存 auth_users.note）──
const noteEditing = ref<{ username: string; text: string } | null>(null)
function openNoteEdit(u: any) {
  noteEditing.value = { username: u.username, text: u.note || '' }
}
async function saveNote(u: any) {
  if (!noteEditing.value) return
  const text = (noteEditing.value.text || '').trim()
  try {
    await api(`/api/auth/users/${encodeURIComponent(u.username)}`, {
      method: 'PUT',
      body: JSON.stringify({ note: text }),
    })
    u.note = text
    noteEditing.value = null
    toast('备注已保存')
  } catch (e: any) {
    toast(e.message || '保存失败', false)
  }
}const dsEditing = ref<any>(null)
const dsTablesText = ref('')
const effectiveUser = ref('')
const effectiveAcl = ref<any>(null)
// 角色模板列表（后端 /api/permission/templates 拉取；失败时回退本地常量）
const roleTemplates = ref<{ key: string; name: string; description: string }[]>([])
// 用户属性键（行级规则「取值来源」下拉：来自 /api/permission/meta）
const userAttributeKeys = ref<string[]>([])

// ── 快捷向导状态 ──────────────────────────────────────
const showWizard = ref(false)
const wizardStep = ref(1)
const wizardSteps = ['给谁用', '能看什么', '数据保护', '确认']
const wizardRole = ref('')
const wizardTargetMode = ref<'role' | 'users'>('role')  // 第 1 步：配给整个角色 / 指定员工
const wizardUsers = ref<string[]>([])              // 第 1 步勾选的员工（指定员工模式）
const wizardDatasets = ref<string[]>([])          // 第 2 步选中的数据域 key
const wizardTemplates = ref<string[]>([])         // 第 3 步选中的保护规则 key
const wizardParams = reactive<Record<string, string>>({})
const applying = ref(false)
// 向导内「新建员工」表单（建完自动勾选，不用另开页面）
const newEmp = reactive({ open: false, username: '', password: '', display_name: '', saving: false, msg: '', ok: true })

const roleName = (key: string) => (model.roles || []).find((r: any) => r.key === key)?.name || key
// 向导只面向非超管身份（超级管理员唯一内置，不参与配置）
const wizardTargets = computed(() => (model.roles || []).filter((r: any) => r.key !== 'admin'))
// 可被单独授权的员工（超管不在列表里可配）
const wizardEmployeeChoices = computed(() =>
  (users.value || []).filter((u: any) => ![...(u.roles || []), u.role].includes('admin')),
)
const userDisplay = (username: string) => {
  const u = (users.value || []).find((x: any) => x.username === username)
  return u ? (u.display_name || u.username) : username
}
const selectedDatasets = computed(() => (model.datasets || []).filter((d: any) => wizardDatasets.value.includes(d.key)))
const selectedRules = computed(() => templates.filter((t) => wizardTemplates.value.includes(t.key)))

/** 该数据域里有几张表在当前连接的库里不存在（切换数据库后数据域会「悬空」）。
 *  返回 0 表示内省结果未加载或全部命中，界面不提示，避免误报。 */
function datasetMissingCount(d: any): number {
  const full = schema.value || {}
  if (!Object.keys(full).length) return 0
  return (d.tables || []).filter((raw: string) => !(full as any)[String(raw).split('.').pop() || String(raw)]).length
}

/** 选中数据域 → 当前库真实存在的表（缺失的表后端会拒绝写入，先在界面上分拣出来） */
const datasetTablePlan = computed(() => {
  const full = (schema.value || {}) as Record<string, any>
  const ok: string[] = []
  const missing: string[] = []
  const seen = new Set<string>()
  for (const d of selectedDatasets.value as Array<{ tables?: string[] }>) {
    for (const raw of (d.tables || [])) {
      const t = String(raw).split('.').pop() || String(raw)
      if (seen.has(t)) continue
      seen.add(t)
      if (full[t]) ok.push(t)
      else missing.push(t)
    }
  }
  return { ok, missing }
})

// 第 1 步：目标身份 = 普通员工（自动选中第一个非超管角色）
const wizardRoleLabel = computed(() => {
  if (wizardRole.value) return roleName(wizardRole.value)
  return ''
})

// 人话摘要（第 4 步展示，替代原来的 JSON 预览）
const wizardSummary = computed(() => {
  const dsNames = selectedDatasets.value.map((d: any) => d.name)
  const ruleNames = selectedRules.value.map((t) => t.name)
  const userMode = wizardTargetMode.value === 'users'
  if (userMode) {
    return {
      whoLabel: `指定员工 ${wizardUsers.value.length} 人`,
      role: wizardUsers.value.length ? wizardUsers.value.map(userDisplay).join('、') : '未选择员工',
      datasets: dsNames.length ? dsNames.join('、') : '未选择（看不到任何数据）',
      rules: '',
      ready: wizardUsers.value.length > 0 && dsNames.length > 0,
      hint: wizardUsers.value.length
        ? '请到上一步至少勾一个数据域，否则没有可开的数据。'
        : '请到上一步勾选员工，或点「＋ 新建员工」建一个账号。',
    }
  }
  return {
    whoLabel: '整个角色',
    role: wizardRoleLabel.value || '未选择',
    datasets: dsNames.length ? dsNames.join('、') : '未选择（看不到任何数据）',
    rules: ruleNames.length ? ruleNames.join('、') : '无',
    ready: !!wizardRoleLabel.value && (dsNames.length > 0 || ruleNames.length > 0),
    hint: '请至少选择「能看的数据」或一条「数据保护」规则，否则无法应用。',
  }
})

function openWizard() {
  wizardStep.value = 1
  wizardTargetMode.value = 'role'
  wizardUsers.value = []
  wizardRole.value = (wizardTargets.value[0] as any)?.key || ''
  wizardDatasets.value = []
  wizardTemplates.value = []
  newEmp.open = false
  newEmp.username = ''
  newEmp.password = ''
  newEmp.display_name = ''
  newEmp.msg = ''
  for (const k of Object.keys(wizardParams)) delete wizardParams[k]
  showWizard.value = true
}

function toggleWizardUser(username: string) {
  const i = wizardUsers.value.indexOf(username)
  if (i >= 0) wizardUsers.value.splice(i, 1)
  else wizardUsers.value.push(username)
}

function toggleNewEmpForm() {
  newEmp.open = !newEmp.open
  newEmp.msg = ''
  if (newEmp.open) {
    newEmp.username = ''
    newEmp.password = ''
    newEmp.display_name = ''
  }
}

/** 向导内建号：走系统同一条建号接口（密码强度由后端校验），建完自动勾选 */
async function createWizardEmp() {
  newEmp.msg = ''
  const username = newEmp.username.trim()
  if (!username || !newEmp.password) {
    newEmp.ok = false
    newEmp.msg = '登录名和初始密码都要填'
    return
  }
  // 建号就必须定死角色，不能「先建 viewer 再补」。
  //
  // 原先的实现有两处会让新账号权限越界：
  //   ① role 写死 'viewer'，账号从出生起就带 viewer；
  //   ② 补角色用 [...curRoles, role] 追加，于是变成 ['viewer', 'prod_staff']。
  // build_acl_context 对多角色取**并集**（任一角色不限制就不限制），
  // 而 viewer 挂的 4 个数据集正好覆盖全库 —— 结果「生产人员」拿到全库 11 张表，
  // 比该看的 6 张还多。建号即定角色，从源头消除叠加。
  const role = wizardRole.value
  if (!role) {
    newEmp.ok = false
    newEmp.msg = '未确定身份，请先在上一步选好要配置的角色'
    newEmp.saving = false
    return
  }
  newEmp.saving = true
  try {
    await api('/api/auth/users', {
      method: 'POST',
      body: JSON.stringify({
        username,
        password: newEmp.password,
        display_name: newEmp.display_name.trim() || username,
        role,
      }),
    })
    if (!wizardUsers.value.includes(username)) wizardUsers.value.push(username)
    newEmp.ok = true
    newEmp.msg = `已创建并勾选「${username}」，身份为「${roleName(role)}」`
    newEmp.username = ''
    newEmp.password = ''
    newEmp.display_name = ''
    await refreshUsers()
  } catch (e: any) {
    newEmp.ok = false
    newEmp.msg = e.message || '创建失败'
  }
  newEmp.saving = false
}

/** 只刷新员工列表（不整页重载），用于建号后立刻把新员工显示出来 */
async function refreshUsers() {
  try {
    const r = await api('/api/permission/users')
    users.value = r.users || []
  } catch {
    // 列表刷新失败不影响建号结果，忽略
  }
}

function toggleDataset(key: string) {
  const i = wizardDatasets.value.indexOf(key)
  if (i >= 0) wizardDatasets.value.splice(i, 1)
  else wizardDatasets.value.push(key)
}

function toggleTemplate(key: string) {
  const i = wizardTemplates.value.indexOf(key)
  if (i >= 0) wizardTemplates.value.splice(i, 1)
  else wizardTemplates.value.push(key)
}

/** 合并策略节：对象（rows/columns/metrics）按 key 覆盖；数组（datasets/tables）取并集 */
function mergeSection(cur: any, section: string, value: any): any {
  if (section === 'datasets' || section === 'tables') {
    return Array.from(new Set([...(Array.isArray(cur) ? cur : []), ...(Array.isArray(value) ? value : [])]))
  }
  const merged = JSON.parse(JSON.stringify(cur && typeof cur === 'object' ? cur : {}))
  for (const [k, v] of Object.entries(value || {})) merged[k] = v
  return merged
}

async function applyWizard() {
  applying.value = true
  try {
    const role = wizardRole.value
    if (!role) throw new Error('未找到可配置的身份（普通员工），请先确认权限数据已加载')

    // ① 指定员工：给这几位补开数据表（并集，不动角色已有范围），并确保他们在所选角色里
    if (wizardTargetMode.value === 'users') {
      if (!wizardUsers.value.length) throw new Error('请先勾选员工，或点「＋ 新建员工」建一个账号')
      if (!wizardDatasets.value.length) throw new Error('请先选择要给这些员工开的数据')
      // 只写当前库里真实存在的表：后端会拒绝悬空表（换库后数据域常指向老库的表）
      const tables = datasetTablePlan.value.ok
      const skipped = datasetTablePlan.value.missing
      if (!tables.length) {
        throw new Error('所选数据域的表在当前连接的库里都不存在（多半是「系统设置」里换过数据库）。请切回原库，或到「数据集」更新表清单')
      }
      let addedRole = 0
      for (const un of wizardUsers.value) {
        // 角色：写成「就该是这个角色」，不是往里加。
        // 多角色是并集，追加 old 会把人越配越宽（viewer 覆盖全库，加了它等于没配额）。
        // 所以这里刻意丢掉历史角色：本向导的语义就是「把他归到这个身份下」。
        const u = (users.value || []).find((x: any) => x.username === un)
        const curRoles: string[] = [...((u?.roles) || [])]
        if (!(curRoles.length === 1 && curRoles[0] === role)) {
          await api('/api/permission/users/roles', {
            method: 'POST', body: JSON.stringify({ username: un, roles: [role] }),
          })
          if (u) u.roles = [role]
          addedRole++
        }
        // 数据表：读现有例外授权 → 并集合并 → 写回（避免覆盖之前手工配的表）
        const cur = (await api(`/api/permission/users/${encodeURIComponent(un)}/grants`)).grants || {}
        const merged: Record<string, string[]> = { ...cur }
        for (const t of tables) {
          if (merged[t]) continue
          const cols = ((schema.value as any)?.[t]?.columns || []).map((c: any) => c.name)
          merged[t] = cols
        }
        await api(`/api/permission/users/${encodeURIComponent(un)}/grants`, {
          method: 'POST', body: JSON.stringify({ grants: merged }),
        })
      }
      toast(`已给 ${wizardUsers.value.length} 名员工开好 ${tables.length} 张表${addedRole ? `，其中 ${addedRole} 人补了「${roleName(role)}」角色` : ''}${skipped.length ? `；有 ${skipped.length} 张表当前库里没有，已跳过` : ''}`)
      showWizard.value = false
      await loadAll()
      return
    }

    if (!wizardDatasets.value.length && !wizardTemplates.value.length) {
      throw new Error('请至少选择「能看的数据」或一条「数据保护」规则')
    }
    // 2) 数据域授权（并集合并进现有配置）
    if (wizardDatasets.value.length) {
      const cur = model.policies?.[role]?.['datasets']
      const merged = mergeSection(cur, 'datasets', wizardDatasets.value)
      await api('/api/permission/policies', {
        method: 'POST', body: JSON.stringify({ role, section: 'datasets', value: merged }),
      })
    }
    // 3) 保护规则按策略节合并后逐节提交（统一走审批 + 审计）
    const bySection: Record<string, any> = {}
    const params = { ...wizardParams }
    for (const t of selectedRules.value) {
      const { section, value } = t.build(params)
      bySection[section] = bySection[section] || {}
      Object.assign(bySection[section], value)
    }
    for (const [section, value] of Object.entries(bySection)) {
      const cur = model.policies?.[role]?.[section]
      const merged = mergeSection(cur, section, value)
      await api('/api/permission/policies', {
        method: 'POST', body: JSON.stringify({ role, section, value: merged }),
      })
    }
    toast(`已为「${wizardRoleLabel.value}」配置好权限`)
    showWizard.value = false
    await loadAll()
  } catch (e: any) {
    toast(e.message, false)
  }
  applying.value = false
}

// 模拟器
const simUser = ref('')
const simDialect = ref('postgres')
const simSql = ref('')
const simExecute = ref(false)
const simResult = ref<any>(null)

// ── API 封装 ──────────────────────────────────────────
async function api(path: string, opts: RequestInit = {}) {
  const resp = await fetch(path, {
    ...opts,
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error((data as any).detail || `HTTP ${resp.status}`)
  return data
}

function toast(msg: string, ok = true) {
  const el = document.createElement('div')
  el.className = `fixed top-6 left-1/2 -translate-x-1/2 z-[1000] px-4 py-2.5 rounded-xl shadow-lg text-sm ${ok ? 'bg-blue-50 border border-blue-200 text-blue-700' : 'bg-red-50 border border-red-200 text-red-600'}`
  el.textContent = msg
  document.body.appendChild(el)
  setTimeout(() => el.remove(), 2600)
}

// ── 数据加载 ──────────────────────────────────────────
async function loadAll() {
  try {
    const [meta, m, u, s] = await Promise.all([
      api('/api/permission/meta'),
      api('/api/permission/model'),
      api('/api/permission/users'),
      api('/api/permission/schema').catch(() => ({ schema: {} })),
    ])
    maskTypes.value = meta.mask_types || []
    kindOptions.value = Object.fromEntries(Object.entries(meta.kinds || {}).map(([k, v]: any) => [k, v.label]))
    authFullMode.value = meta.auth_full_mode !== false
    userAttributeKeys.value = meta.user_attribute_keys || ['username']
    Object.assign(model, {
      version: m.version, updated_by: m.updated_by,
      roles: m.roles || [], datasets: m.datasets || [],
      policies: m.policies || {}, sensitive: m.sensitive || {},
    })
    schema.value = s.schema || {}
    users.value = u.users || []
    if (!simUser.value && users.value.length) simUser.value = users.value[0].username
    loadMetrics()
    loadTemplates()
  } catch (e: any) {
    toast('加载权限模型失败：' + e.message, false)
  }
}

/**
 * 新建成员：建号即定角色，不经过「先建 viewer 再补」两段式。
 *
 * 两段式的问题在多角色并集下会被放大——先建成 viewer，再追加岗位角色，
 * 两个角色取并集，而 viewer 的数据域覆盖全库，结果新员工看到的东西比岗位该看的还多。
 * 后端 /api/auth/users 现在直接接受任意已注册角色，所以这里一步到位。
 */
async function createMember() {
  newUser.msg = ''
  const username = newUser.username.trim()
  if (!username || !newUser.password) {
    newUser.ok = false; newUser.msg = '登录名和初始密码都要填'; return
  }
  if (!newUser.role) {
    newUser.ok = false; newUser.msg = '请选择身份'; return
  }
  newUser.saving = true
  try {
    await api('/api/auth/users', {
      method: 'POST',
      body: JSON.stringify({
        username,
        password: newUser.password,
        display_name: (newUser.display_name || '').trim() || username,
        role: newUser.role,
      }),
    })
    newUser.ok = true
    newUser.msg = `已创建「${username}」，身份为「${roleName(newUser.role)}」`
    newUser.username = ''; newUser.password = ''; newUser.display_name = ''
    await loadAll()
    setTimeout(() => { showNewUser.value = false; newUser.msg = '' }, 1500)
  } catch (e: any) {
    newUser.ok = false
    newUser.msg = e.message || '创建失败'
  }
  newUser.saving = false
}

/** 打开新建成员表单：默认身份取第一个非管理员角色，避免手滑建成管理员 */
function openNewUser() {
  newUser.username = ''; newUser.password = ''; newUser.display_name = ''
  newUser.msg = ''; newUser.ok = false
  const first = (model.roles || []).find((r: any) => r.key !== 'admin')
  newUser.role = first?.key || ''
  showNewUser.value = true
}

async function loadTemplates() {
  try {
    const r = await api('/api/permission/templates')
    roleTemplates.value = r.templates || []
  } catch {
    // 后端未返回时回退本地常量（label 映射到 name）
    roleTemplates.value = Object.entries(ROLE_TEMPLATES).map(([k, v]) => ({
      key: k, name: v.label, description: v.desc,
    }))
  }
}

async function loadMetrics() {
  try {
    const r = await api('/api/metrics')
    metrics.value = r.metrics || []
  } catch { /* 指标加载失败不阻塞页面 */ }
}

async function loadRequests() {
  loading.approvals = true
  try {
    const r = await api(`/api/permission/requests?status=${approvalFilter.value}`)
    requests.value = r.requests || []
  } catch (e: any) { toast('加载申请单失败：' + e.message, false) }
  loading.approvals = false
}

async function loadAudit() {
  try {
    const [c, a] = await Promise.all([
      api('/api/permission/changes'),
      api('/api/permission/audit?limit=100'),
    ])
    changes.value = c.changes || []
    auditLogs.value = a.logs || []
  } catch (e: any) { toast('加载审计失败：' + e.message, false) }
}

function switchTab(key: string) {
  // 轻量模式下审批中心不可达：若目标/当前是 approvals 则回退到审计
  if (key === 'approvals' && !authFullMode.value) key = 'audit'
  activeTab.value = key
  const g = tabGroups.find(x => x.tabs.some(t => t.key === key))
  if (g) activeGroup.value = g.key
  if (key !== 'roles') showAdv.value = false  // 离开角色页时收起高级权限
  if (key === 'users') { loading.users = true; api('/api/permission/users').then(r => { users.value = r.users || [] }).catch(() => {}).finally(() => { loading.users = false }) }
  if (key === 'approvals') loadRequests()
  if (key === 'audit') loadAudit()
  if (key === 'columns' && colRole.value) loadColRules()
  if (key === 'rows' && rowRole.value) loadRowRules()
  if (key === 'metrics' && metricRole.value) loadMetricRules()
  if (key === 'emp_grants' && !empSelected && empUsers.value.length) selectEmp(empUsers.value[0].username)
}

function switchGroup(key: string) {
  const g = tabGroups.find(x => x.key === key)
  if (!g) return
  switchTab(g.tabs[0].key)
}

// ── 员工级数据授权（per-user 表/字段白名单）────────────
const empUsers = computed(() => (users.value || []).filter((u: any) => !(u.roles || [u.role]).includes('admin')))
const empSelected = ref('')
const empSelectedName = computed(() => {
  const u = (users.value || []).find((x: any) => x.username === empSelected.value)
  return u ? (u.display_name || u.username) : empSelected.value
})
const empSchema = ref<any[]>([])
const empGrants = ref<Record<string, string[]>>({})
const empLoading = ref(false)
const empSaving = ref(false)
const empGrantState = reactive<Record<string, boolean>>({})

async function selectEmp(username: string) {
  empSelected.value = username
  empLoading.value = true
  empGrants.value = {}
  try {
    const r = await api(`/api/permission/users/${encodeURIComponent(username)}/grants`)
    empSchema.value = Object.entries(r.schema || {}).map(([table_name, info]: any) => ({
      table_name, chinese_name: info.chinese_name || '', columns: info.columns || [],
    }))
    empGrants.value = JSON.parse(JSON.stringify(r.grants || {}))
    empGrantState[username] = !!r.has_grants
  } catch (e: any) {
    toast('加载员工授权失败：' + e.message, false)
  } finally {
    empLoading.value = false
  }
}

function toggleEmpTable(t: any) {
  if (empGrants.value[t.table_name]) {
    delete empGrants.value[t.table_name]
    empGrants.value = { ...empGrants.value }
  } else {
    empGrants.value = { ...empGrants.value, [t.table_name]: (t.columns || []).map((c: any) => c.name) }
  }
}

function toggleEmpCol(table: string, col: string) {
  const cur = empGrants.value[table] || []
  const next = cur.includes(col) ? cur.filter((c: string) => c !== col) : [...cur, col]
  empGrants.value = { ...empGrants.value, [table]: next }
}

function empSelectAllTables() {
  const all: Record<string, string[]> = {}
  for (const t of empSchema.value) all[t.table_name] = (t.columns || []).map((c: any) => c.name)
  empGrants.value = all
}

async function saveEmpGrants() {
  if (!empSelected.value) return
  empSaving.value = true
  try {
    await api(`/api/permission/users/${encodeURIComponent(empSelected.value)}/grants`, {
      method: 'POST', body: JSON.stringify({ grants: empGrants.value }),
    })
    empGrantState[empSelected.value] = Object.keys(empGrants.value).length > 0
    toast('员工数据授权已保存（已留痕）')
  } catch (e: any) {
    toast('保存失败：' + e.message, false)
  } finally {
    empSaving.value = false
  }
}

// ── 辅助 ──────────────────────────────────────────────
const allRoles = computed(() => model.roles || [])
const policyCount = (roleKey: string) => {
  const p = model.policies?.[roleKey] || {}
  return {
    tables: (p.datasets || []).length + (p.tables || []).length,
    rows: Object.keys(p.rows || {}).length,
    cols: Object.keys(p.columns || {}).length,
    mets: Object.keys(p.metrics || {}).length,
  }
}
const roleUserCount = (roleKey: string) =>
  (users.value || []).filter((u: any) => (u.roles || [u.role]).includes(roleKey)).length
// 成员「能看什么」摘要：合并其所有角色的数据域，用大白话呈现；有例外授权则例外优先
const userScope = (u: any) => {
  if (u.has_grants && (u.grant_tables || []).length) {
    const ts = u.grant_tables
    const shown = ts.slice(0, 3).join('、') + (ts.length > 3 ? ' 等' : '')
    return `例外授权 ${ts.length} 张表：${shown}`
  }
  const roles = (u.roles && u.roles.length) ? u.roles : [u.role]
  if (!roles.length) return '未分配角色'
  if (roles.includes('admin')) return '全部数据'
  const dsKeys = new Set<string>()
  let directTables = 0
  for (const r of roles) {
    const p = model.policies?.[r] || {}
    for (const d of (p.datasets || [])) dsKeys.add(d)
    directTables += (p.tables || []).length
  }
  if (!dsKeys.size && !directTables) return '未分配数据'
  const names = (model.datasets || [])
    .filter((d: any) => dsKeys.has(d.key))
    .map((d: any) => d.name)
  const shown = names.slice(0, 3).join('、') + (names.length > 3 ? ' 等' : '')
  if (names.length) return `${names.length} 个数据域：${shown}`
  return `可直接访问 ${directTables} 张表`
}
function manageRolePermissions(r: any) {
  colRole.value = r.key
  rowRole.value = r.key
  metricRole.value = r.key
  showAdv.value = true
  advTab.value = 'columns'
  if (activeTab.value !== 'roles') switchTab('roles')
  loadColRules()
  // 滚动到高级权限折叠区
  nextTick(() => {
    const el = document.getElementById('advanced-permissions')
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  })
}
const roleTables = (roleKey: string): string[] => {
  const p = model.policies?.[roleKey] || {}
  const dsKeys: string[] = (p.datasets || [])
  const tables = new Set<string>((p.tables || []).map((t: string) => String(t).split('.').pop() || String(t)))
  for (const d of (model.datasets || []) as Array<{ key: string; tables?: string[] }>) {
    if (dsKeys.includes(d.key)) for (const t of d.tables || []) tables.add(String(t).split('.').pop() || String(t))
  }
  // 行策略涉及的表也展示（可能未在数据集授权中显式列出）
  for (const t of Object.keys(p.rows || {})) tables.add(t)
  for (const t of Object.keys(p.columns || {})) tables.add(t)
  return Array.from(tables).sort()
}

/** 该表是否真的授权给了角色（数据集授权 ∪ 单表授权）。
 *  引擎只对「角色可见」的表注入行条件（enforcer._table_visible），
 *  表未授权时行规则配了也不会生效，所以界面要提前把话说清楚。
 *  判据与引擎保持一致：admin 不受数据权限约束；未配置授权 = 不限制。 */
const isTableAuthorized = (roleKey: string, table: string): boolean => {
  if (roleKey === 'admin') return true
  const p = model.policies?.[roleKey] || {}
  const dsKeys: string[] = (p as any).datasets || []
  const tbKeys: string[] = (p as any).tables || []
  if (!dsKeys.length && !tbKeys.length) return true
  const bareOf = (x: string) => String(x).split('.').pop() || String(x)
  const bare = bareOf(table)
  if (tbKeys.map(bareOf).includes(bare)) return true
  for (const d of (model.datasets || []) as Array<{ key: string; tables?: string[] }>) {
    if (!dsKeys.includes(d.key)) continue
    if ((d.tables || []).map(bareOf).includes(bare)) return true
  }
  return false
}
const isSensitiveColumn = (table: string, col: string) =>
  (model.sensitive?.columns || []).includes(`${table}.${col}`.toLowerCase()) ||
  (model.sensitive?.columns || []).includes(`${table}.${col}`)
const isSensitiveMetric = (name: string) => (model.sensitive?.metrics || []).includes(name)
/** 把脱敏取值统一成算法 key。
 *  历史上有下拉选项整个对象被写入的情况（含被序列化成 "{'key': 'partial_1_1', ...}"），
 *  这里三种形态都能还原，避免脏数据继续在界面与后端之间来回传播。 */
const maskKey = (m: any): string => {
  if (m && typeof m === 'object') return String(m.key || '')
  const s = String(m ?? '').trim()
  if (s.startsWith('{') && s.endsWith('}')) {
    const hit = /['"]key['"]\s*:\s*['"]([^'"]+)['"]/.exec(s)
    if (hit) return hit[1]
  }
  return s
}
const maskLabel = (m: any) => {
  const key = maskKey(m)
  const map: Record<string, string> = {
    partial_3_4: '前3后4', partial_6_4: '前6后4', partial_1_1: '首尾各1',
    hash: '哈希', fixed: '固定值', null: '置空', round_100: '百元取整', year_only: '仅年份',
  }
  return map[key] || key
}
/** 「应用明细」的行：把后端 applied 的四类条目翻译成一句人话。
 *  后端结构见 security/enforcer.py::rewrite_sql —— 只有这几个键：
 *    masked      [{table, column, mask}]       列脱敏
 *    hidden      [{table, column}]             整列隐藏
 *    row_filters [{table, condition, alias}]   追加的行过滤条件
 *    denied      [{table} / {table, column}]   无权被拒
 *  另外 mask 值可能是脏数据（整个下拉对象被序列化写过），统一过 maskKey 还原。 */
type AppliedItem = { label: string; detail: string; code: string; tone: string }
const appliedItems = computed<AppliedItem[]>(() => {
  const a = simResult.value?.applied
  if (!a) return []
  const out: AppliedItem[] = []
  for (const m of a.masked || []) {
    out.push({ label: `脱敏 ${m.table}.${m.column}`, detail: maskLabel(m.mask), code: maskKey(m.mask), tone: 'text-gray-600' })
  }
  for (const h of a.hidden || []) {
    out.push({ label: `隐藏列 ${h.table}.${h.column}`, detail: '整列不返回', code: '', tone: 'text-gray-500' })
  }
  for (const r of a.row_filters || []) {
    out.push({ label: `行过滤 ${r.table}`, detail: '查询里追加了条件', code: String(r.condition || ''), tone: 'text-gray-600' })
  }
  for (const d of a.denied || []) {
    out.push({ label: `拒绝 ${d.table}${d.column ? '.' + d.column : ''}`, detail: '无权访问', code: '', tone: 'text-red-600' })
  }
  return out
})
const statusLabel = (s: string) => ({ pending: '待审批', approved: '已通过', rejected: '已驳回', auto_approved: '自动通过' } as any)[s] || s
const statusClass = (s: string) => ({
  pending: 'bg-amber-50 text-amber-600 border border-amber-200',
  approved: 'bg-blue-50 text-blue-600 border border-blue-100',
  rejected: 'bg-red-50 text-red-500 border border-red-200',
  auto_approved: 'bg-blue-50 text-blue-600 border border-blue-100',
} as any)[s] || 'bg-gray-50 text-gray-400 border border-gray-200'
const fmtTime = (t: string) => (t ? t.replace('T', ' ').slice(0, 19) : '-')
const auditDetail = (l: any) => l.query || (typeof l.detail === 'string' ? l.detail : JSON.stringify(l.detail || ''))

// ── 角色模板（P3：新建角色一键套用初始策略）─────────────
const ROLE_TEMPLATES: Record<string, { label: string; desc: string; policies: Record<string, any> }> = {
  readonly: {
    label: '🔒 只读访客',
    desc: '无默认表授权（员工默认空，需逐人授权）；敏感字段默认拒绝',
    policies: {
      datasets: [], tables: [],
      rows: {},
      columns: {
        dim_production_line: { supervisor: { mode: 'mask', mask: 'partial_1_1' } },
        test_materials: { unit_cost: { mode: 'deny' } },
      },
      metrics: {},
    },
  },
  region_manager: {
    label: '🗺️ 区域经理',
    desc: '只看本区域工厂/订单（行规则可视化），客户名脱敏，产量按总口径',
    policies: {
      datasets: ['sales', 'prod_core'], tables: [],
      rows: {
        test_factories: {
          expr: 'city = ${user.region}',
          rule: { field: 'city', op: 'eq', source: 'user.region' },
          enabled: true, note: '只看本区域工厂',
        },
        test_orders: {
          expr: 'factory_id IN (SELECT factory_id FROM test_factories WHERE city = ${user.region})',
          rule: { op: 'in_subquery', ref_table: 'test_factories', on: { left: 'factory_id', right: 'factory_id' }, where: { field: 'city', op: 'eq', source: 'user.region' } },
          enabled: true, note: '只看本区域订单',
        },
      },
      columns: { test_orders: { customer_name: { mode: 'mask', mask: 'partial_1_1' } } },
      metrics: { 产量: { mode: 'override', sql_expression: 'SUM(good_qty + defect_qty)', formula: '合格产出 + 不良产出（总产出口径）', description: '总产出口径' } },
    },
  },
}

// ── 角色 CRUD ─────────────────────────────────────────
function openRole(r: any) {
  roleEditing.value = r
    ? { ...r, origKey: r.key }
    : { key: '', name: '', description: '', priority: 30, origKey: null, template: '' }
}
async function saveRole() {
  const f = roleEditing.value
  if (!f.key) return toast('角色 key 必填', false)
  try {
    if (!f.origKey && f.template) {
      // 有模板：后端一次落地（建角色 + 套模板策略，走审批），name 由模板提供
      await api('/api/permission/templates/apply', {
        method: 'POST', body: JSON.stringify({ role: f.key, template: f.template }),
      })
    } else {
      if (!f.name) return toast('角色名称必填', false)
      await api('/api/permission/roles', {
        method: 'POST', body: JSON.stringify({ key: f.key, name: f.name, description: f.description, priority: f.priority }),
      })
    }
    toast(`角色「${f.name || f.key}」已保存${f.template && !f.origKey ? '（已套用模板）' : ''}`)
    roleEditing.value = null
    loadAll()
  } catch (e: any) { toast(e.message, false) }
}
async function removeRole(r: any) {
  if (!window.confirm(`确定删除角色「${r.name}」？其策略将一并移除。`)) return
  try {
    await api(`/api/permission/roles/${r.key}`, { method: 'DELETE' })
    toast(`角色「${r.name}」已删除`)
    loadAll()
  } catch (e: any) { toast(e.message, false) }
}

// ── 数据集 CRUD ───────────────────────────────────────
function openDataset(d: any) {
  dsEditing.value = d ? { ...d, origKey: d.key } : { key: '', name: '', description: '', sensitive: false, tables: [], origKey: null }
  dsTablesText.value = (d?.tables || []).join(', ')
}
async function saveDataset() {
  const f = dsEditing.value
  if (!f.key || !f.name) return toast('数据集 key 与名称必填', false)
  try {
    await api('/api/permission/datasets', { method: 'POST', body: JSON.stringify({
      key: f.key, name: f.name, description: f.description, sensitive: !!f.sensitive,
      tables: dsTablesText.value.split(',').map((t: string) => t.trim()).filter(Boolean),
    }) })
    toast(`数据集「${f.name}」已保存`)
    dsEditing.value = null
    loadAll()
  } catch (e: any) { toast(e.message, false) }
}
async function removeDataset(d: any) {
  if (!window.confirm(`确定删除数据集「${d.name}」？所有角色的该数据集授权将被移除。`)) return
  try {
    await api(`/api/permission/datasets/${d.key}`, { method: 'DELETE' })
    toast(`数据集「${d.name}」已删除`)
    loadAll()
  } catch (e: any) { toast(e.message, false) }
}

// ── 用户授权 ──────────────────────────────────────────
function onAttrChange(username: string, ev: Event) {
  attrsDraft[username] = (ev.target as HTMLTextAreaElement).value
}
async function toggleUserRole(u: any, roleKey: string) {
  const roles = [...(u.roles || [])]
  const i = roles.indexOf(roleKey)
  if (i >= 0) roles.splice(i, 1)
  else roles.push(roleKey)
  u.roles = roles
}
async function saveUser(u: any) {
  try {
    const attrsRaw = attrsDraft[u.username]
    let attrs = u.attributes || {}
    if (attrsRaw !== undefined) {
      try { attrs = JSON.parse(attrsRaw || '{}') } catch { return toast('属性 JSON 格式错误', false) }
    }
    await api('/api/permission/users/roles', { method: 'POST', body: JSON.stringify({ username: u.username, roles: u.roles || [] }) })
    await api('/api/permission/users/attributes', { method: 'POST', body: JSON.stringify({ username: u.username, attributes: attrs }) })
    u.attributes = attrs
    toast(`用户「${u.username}」已保存`)
  } catch (e: any) { toast(e.message, false) }
}
async function showEffective(username: string) {
  try {
    const r = await api(`/api/permission/effective/${username}`)
    effectiveUser.value = username
    effectiveAcl.value = r.acl
  } catch (e: any) { toast(e.message, false) }
}

// ── 账号维护 ──────────────────────────────────────────

/** 当前登录的管理员是谁——用于"不能禁用/删除自己"的前端预判 */
const meName = computed(() => {
  try { return JSON.parse(localStorage.getItem('sqlbot_auth_user') || '{}').username || '' } catch { return '' }
})

/** 这个账号能不能动？返回 {ok, why}；why 用来做按钮的 title 提示 */
function canTouch(u: any): { ok: boolean; why: string } {
  if (u.builtin) return { ok: false, why: '内置演示账号，后端不允许改角色 / 禁用 / 删除' }
  if (u.username === meName.value) return { ok: false, why: '这是你自己的账号，不能禁用或删除' }
  return { ok: true, why: '' }
}

/** 打开「编辑资料」弹窗 */
function openMaintain(u: any) {
  maintainMode.value = 'profile'
  maintain.username = u.username
  maintain.display_name = u.display_name || ''
  maintain.department = u.department || ''
  maintain.title = u.title || ''
  maintain.email = u.email || ''
  maintain.phone = u.phone || ''
  maintain.note = u.note || ''
  maintain.msg = ''; maintain.ok = false; maintain.saving = false
  showMaintain.value = true
}

/** 打开「重置密码」弹窗 */
function openPassword(u: any) {
  maintainMode.value = 'password'
  maintain.username = u.username
  maintain.password = ''; maintain.confirm = ''
  maintain.msg = ''; maintain.ok = false; maintain.saving = false
  showMaintain.value = true
}

/** 提交维护表单：资料或密码，二选一 */
async function submitMaintain() {
  const un = maintain.username
  maintain.msg = ''; maintain.ok = false

  const body: any = {}
  if (maintainMode.value === 'password') {
    if (!maintain.password) { maintain.msg = '请填写新密码'; return }
    if (maintain.password !== maintain.confirm) { maintain.msg = '两次输入的密码不一致'; return }
    body.password = maintain.password
  } else {
    body.display_name = maintain.display_name.trim()
    body.department = maintain.department.trim()
    body.title = maintain.title.trim()
    body.email = maintain.email.trim()
    body.phone = maintain.phone.trim()
    body.note = maintain.note.trim()
  }

  maintain.saving = true
  try {
    await api(`/api/permission/users/${encodeURIComponent(un)}/maintain`, {
      method: 'POST', body: JSON.stringify(body),
    })
    maintain.ok = true
    maintain.msg = maintainMode.value === 'password' ? `「${un}」的密码已重置` : `「${un}」的资料已保存`
    await loadAll()
    setTimeout(() => {
      if (maintain.ok) { showMaintain.value = false; maintain.msg = '' }
    }, 1200)
  } catch (e: any) {
    maintain.ok = false
    maintain.msg = e.message || '保存失败'
  }
  maintain.saving = false
}

/** 通用确认框：把不可逆动作包进一次显式确认里 */
function askConfirm(title: string, body: string, fn: () => Promise<void>, danger = true) {
  confirmBox.title = title
  confirmBox.body = body
  confirmBox.danger = danger
  confirmBox.action = fn
  confirmBox.running = false
  confirmBox.show = true
}

/** 执行确认框里的动作 */
async function runConfirm() {
  if (!confirmBox.action) return
  confirmBox.running = true
  try {
    await confirmBox.action()
    confirmBox.show = false
  } catch (e: any) {
    toast(e.message || '操作失败', false)
  }
  confirmBox.running = false
}

/** 禁用 / 启用账号 */
async function toggleEnabled(u: any) {
  const chk = canTouch(u)
  if (!chk.ok) return toast(chk.why, false)
  const toDisable = u.enabled !== false

  const doIt = async () => {
    await api(`/api/permission/users/${encodeURIComponent(u.username)}/maintain`, {
      method: 'POST', body: JSON.stringify({ enabled: !toDisable }),
    })
    toast(`「${u.username}」已${toDisable ? '禁用' : '启用'}`)
    await loadAll()
  }

  if (toDisable) {
    askConfirm(
      `禁用「${u.display_name || u.username}」？`,
      '禁用后这个账号立即无法登录，已有的登录状态也会失效。数据权限配置不会丢，以后启用还能接着用。',
      doIt,
    )
  } else {
    await doIt().catch((e: any) => toast(e.message, false))
  }
}

/** 删除账号 */
function removeUser(u: any) {
  const chk = canTouch(u)
  if (!chk.ok) return toast(chk.why, false)
  askConfirm(
    `删除「${u.display_name || u.username}」？`,
    '这个动作不可撤销。账号会被从系统里移除，它名下的角色配置和例外授权也一并消失。如果只是暂时不想让人登录，用「禁用」更稳妥。',
    async () => {
      await api(`/api/permission/users/${encodeURIComponent(u.username)}`, { method: 'DELETE' })
      toast(`「${u.username}」已删除`)
      await loadAll()
    },
  )
}

// ── 列级权限 ──────────────────────────────────────────
const realColumns = ref<string[]>([])

async function loadColRules() {
  const p = model.policies?.[colRole.value] || {}
  const cfg = p.columns || {}
  if (!colTable.value) colTable.value = roleTables(colRole.value)[0] || ''
  const t = colTable.value
  if (!t) return
  // 拉取真实列（复用 /api/tables/{table}），失败则回退策略已配置的列
  realColumns.value = []
  try {
    const r = await api(`/api/tables/${t}`)
    realColumns.value = (r.columns || []).map((c: any) => c.name)
  } catch { /* 表可能不存在，使用策略列 */ }
  const names = realColumns.value.length ? realColumns.value : Object.keys(cfg[t] || {})
  for (const k of Object.keys(colRules)) delete colRules[k]
  const tblCfg = cfg[t] || {}
  for (const col of names) {
    const rule = tblCfg[col] || {}
    colRules[col] = { mode: rule.mode || 'visible', mask: maskKey(rule.mask) || 'partial_3_4', note: rule.note || '' }
  }
}
const colColumns = computed(() => {
  const t = colTable.value
  if (!t) return []
  const p = model.policies?.[colRole.value] || {}
  const cfg = p.columns?.[t] || {}
  const names = realColumns.value.length ? realColumns.value : Object.keys(cfg)
  return names.map((n) => ({ name: n }))
})
function onColModeChange(col: string) {
  if (colRules[col].mode === 'mask' && !colRules[col].mask) colRules[col].mask = 'partial_3_4'
}
async function saveColumns() {
  const t = colTable.value
  const value: Record<string, any> = {}
  for (const c of colColumns.value) {
    const r = colRules[c.name]
    if (!r || r.mode === 'visible') continue
    const item: any = { mode: r.mode, note: r.note || '' }
    if (r.mode === 'mask') item.mask = maskKey(r.mask) || 'partial_3_4'
    value[c.name] = item
  }
  // 合并保存：仅覆盖当前表，避免丢失其他表的列策略
  const cur = JSON.parse(JSON.stringify(model.policies?.[colRole.value]?.columns || {}))
  if (Object.keys(value).length) cur[t] = value
  else delete cur[t]
  try {
    await api('/api/permission/policies', { method: 'POST', body: JSON.stringify({ role: colRole.value, section: 'columns', value: cur }) })
    toast(`列策略已保存（表 ${t}）`)
    loadAll()
  } catch (e: any) { toast(e.message, false) }
}

// ── 行级权限（P2：支持声明式 rule 可视化配置，expr 兼容保留）──
function loadRowRules() {
  const p = model.policies?.[rowRole.value] || {}
  const cfg = p.rows || {}
  for (const k of Object.keys(rowRules)) delete rowRules[k]
  for (const t of roleTables(rowRole.value)) {
    const rule = cfg[t] || {}
    rowRules[t] = {
      expr: rule.expr || '',
      enabled: rule.enabled !== false,
      note: rule.note || '',
      rule: rule.rule
        ? JSON.parse(JSON.stringify(rule.rule))
        : { op: 'eq', field: '', source: 'user.', ref_table: '', on: { left: '', right: '' }, where: { field: '', op: 'eq', source: 'user.' } },
      mode: rule.rule ? 'form' : 'expr',
    }
  }
}
const schema = ref<Record<string, any>>({})          // /api/permission/schema：表 → {columns}
const schemaTables = computed(() => Object.keys(schema.value).sort())
const colsOf = (t: string): string[] => {
  const info = schema.value[t] || schema.value[String(t).split('.').pop() || t]
  return (info?.columns || []).map((c: any) => c.name)
}
function onRefTableChange(t: string) {
  // 切换参照表后，把 on.right / where.field 重置为该表第一个字段（避免残留非法列）
  const cols = colsOf(rowRules[t].rule.ref_table)
  if (cols.length) {
    if (!cols.includes(rowRules[t].rule.on.right)) rowRules[t].rule.on.right = cols[0]
    if (!cols.includes(rowRules[t].rule.where.field)) rowRules[t].rule.where.field = cols[0]
  }
}
const _CMP_OPS: Record<string, string> = { eq: '=', neq: '<>', lt: '<', lte: '<=', gt: '>', gte: '>=' }
function compileCmp(field: string, op: string, source: any): string {
  const o = op || 'eq'
  const src = String(source ?? '').trim()
  if (src.startsWith('user.')) {
    const v = src.slice(5)
    if (o === 'eq') return `${field} = ${'${user.' + v + '}'}`
    if (o === 'neq') return `${field} <> ${'${user.' + v + '}'}`
    if (o === 'in') return `${field} IN ${'${user.' + v + '}'}`
    if (o === 'contains') return `${field} LIKE CONCAT('%', ${'${user.' + v + '}'}, '%')`
    return `${field} ${_CMP_OPS[o] || '='} ${'${user.' + v + '}'}`
  }
  if (o === 'in') return `${field} IN (${String(source)})`
  if (o === 'contains') return `${field} LIKE CONCAT('%', '${String(source)}', '%')`
  return `${field} ${_CMP_OPS[o] || '='} '${String(source)}'`
}
function compileRowRule(rule: any): string {
  const op = rule?.op || 'eq'
  if (op === 'in_subquery') {
    const rt = String(rule?.ref_table || '').split('.').pop() || ''
    const left = rule?.on?.left || ''
    const right = rule?.on?.right || ''
    const wf = rule?.where?.field || ''
    if (!rt || !left || !right || !wf) return ''
    return `${left} IN (SELECT ${right} FROM ${rt} WHERE ${compileCmp(wf, rule?.where?.op, rule?.where?.source)})`
  }
  const field = rule?.field || ''
  if (!field) return ''
  return compileCmp(field, op, rule?.source)
}
const rowRulePreview = (t: string): string => {
  try { return compileRowRule(rowRules[t]?.rule) } catch { return '' }
}
async function saveRows() {
  const value: Record<string, any> = {}
  for (const [t, r] of Object.entries(rowRules) as any) {
    if (!r) continue
    if (r.mode === 'form' && r.rule) {
      const expr = compileRowRule(r.rule)
      if (!expr) continue  // 表单信息不完整 → 该表不写入
      value[t] = { rule: JSON.parse(JSON.stringify(r.rule)), expr, enabled: !!r.enabled, note: r.note || '' }
    } else if (r.expr?.trim()) {
      value[t] = { expr: r.expr.trim(), enabled: !!r.enabled, note: r.note || '' }
    }
  }
  // 合并保存：先移除本角色所有相关表的旧行策略，再写入新配置
  const cur = JSON.parse(JSON.stringify(model.policies?.[rowRole.value]?.rows || {}))
  for (const t of roleTables(rowRole.value)) delete cur[t]
  Object.assign(cur, value)
  try {
    const res: any = await api('/api/permission/policies', { method: 'POST', body: JSON.stringify({ role: rowRole.value, section: 'rows', value: cur }) })
    const warns: string[] = res?.warnings || []
    if (warns.length) toast('已保存，但需要注意：' + warns.join('；'), false)
    else toast('行策略已保存')
    loadAll()
  } catch (e: any) { toast(e.message, false) }
}

// ── 指标口径 ──────────────────────────────────────────
function loadMetricRules() {
  const p = model.policies?.[metricRole.value] || {}
  const cfg = p.metrics || {}
  for (const k of Object.keys(metricRules)) delete metricRules[k]
  for (const m of metrics.value) {
    const rule = cfg[m.name] || {}
    metricRules[m.name] = {
      mode: rule.mode || 'allow',
      sql_expression: rule.sql_expression || '',
      formula: rule.formula || '',
      description: rule.description || '',
      note: rule.note || '',
    }
  }
}
function onMetricModeChange(name: string) {
  const r = metricRules[name]
  const m = metrics.value.find((x) => x.name === name)
  if (r.mode === 'override' && !r.sql_expression) r.sql_expression = m?.sql_expression || ''
}
async function saveMetrics() {
  const value: Record<string, any> = {}
  for (const m of metrics.value) {
    const r = metricRules[m.name]
    if (!r) continue
    if (r.mode === 'override') {
      value[m.name] = { mode: 'override', sql_expression: r.sql_expression, formula: r.formula, description: r.description || m.formula, note: r.note }
    } else if (r.mode === 'deny') {
      value[m.name] = { mode: 'deny', note: r.note }
    }
  }
  // 合并保存：先移除本角色已配置的全部指标策略，再写入新配置
  const cur = JSON.parse(JSON.stringify(model.policies?.[metricRole.value]?.metrics || {}))
  for (const m of metrics.value) delete cur[m.name]
  Object.assign(cur, value)
  try {
    await api('/api/permission/policies', { method: 'POST', body: JSON.stringify({ role: metricRole.value, section: 'metrics', value: cur }) })
    toast('指标策略已保存')
    loadAll()
  } catch (e: any) { toast(e.message, false) }
}

// ── 操作级权限（export / share / download）──
function loadActionRules() {
  const p = model.policies?.[colRole.value] || {}
  const cfg = p.actions || {}
  actionRules.export = !!cfg.export
  actionRules.share = !!cfg.share
  actionRules.download = !!cfg.download
}
async function saveActions() {
  const value: Record<string, boolean> = {}
  if (actionRules.export) value.export = true
  if (actionRules.share) value.share = true
  if (actionRules.download) value.download = true
  try {
    await api('/api/permission/policies', { method: 'POST', body: JSON.stringify({ role: colRole.value, section: 'actions', value }) })
    toast('操作权限已保存')
    loadAll()
  } catch (e: any) { toast(e.message, false) }
}

// ── 审批 ──────────────────────────────────────────────
async function submitRequest() {
  let payload: any
  try { payload = JSON.parse(reqForm.payloadText) } catch { return toast('变更指令 JSON 格式错误', false) }
  if (!payload.action) return toast('指令缺少 action 字段', false)
  try {
    const r = await api('/api/permission/requests', { method: 'POST', body: JSON.stringify({ kind: reqForm.kind, payload, reason: reqForm.reason }) })
    toast(r.message)
    showRequestForm.value = false
    loadRequests()
  } catch (e: any) { toast(e.message, false) }
}
async function reviewRequest(q: any, approve: boolean) {
  const comment = approve ? '' : window.prompt('驳回原因（可选）', '') || ''
  try {
    const r = await api(`/api/permission/requests/${q.id}/review`, { method: 'POST', body: JSON.stringify({ approve, comment }) })
    toast(r.message)
    loadRequests()
  } catch (e: any) { toast(e.message, false) }
}

// ── 模拟器 ────────────────────────────────────────────
async function runSimulate() {
  loading.simulate = true
  simResult.value = null
  try {
    simResult.value = await api('/api/permission/simulate', { method: 'POST', body: JSON.stringify({
      username: simUser.value, sql: simSql.value, execute: simExecute.value, dialect: simDialect.value,
    }) })
  } catch (e: any) { toast(e.message, false) }
  loading.simulate = false
}

// ── 初始化 ────────────────────────────────────────────
onMounted(() => {
  loadAll()
})
</script>
