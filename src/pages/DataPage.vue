<template>


  <!-- 根元素手动接管 attrs：本页顶层还有两个 <Teleport>（弹窗挂 body），组件是多根，
       Vue 无法自动继承父级传入的 class（如 h-full）→ 页面内容不足一屏时底部会空一截，
       且每次渲染刷两条 Extraneous non-props attributes 警告。 -->
  <div class="space-y-3" v-bind="$attrs">



    <!-- ====== 页面定位（管理专区）======

      这一页 2026-09-21 起从通用功能区挪进管理专区：只给管理员维护数据源用。
      业务人员在侧边栏看不到它 —— 他们不需要认表名和字段，直接在「智能问析」里提问即可。
      所以这里不放任何"教业务人员看懂数据库"的内容，默认进来的人就是来干活的。 -->

    <div class="flex items-start gap-2.5 border border-amber-200 bg-amber-50/60 rounded-lg px-3 py-2.5">

      <span class="text-[11px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-700 shrink-0 mt-0.5">管理专区</span>

      <p class="text-xs leading-relaxed text-amber-900">

        这一页用来管数据源：切换数据库、导入表、看表结构和字段说明。业务人员看不到这一页 ——
        他们要数据，去「智能问析」提问就行。下面表格里的每一列都可以直接搜。

      </p>

    </div>



    <!-- ====== 数据库切换器 ====== -->



    <div class="flex items-center justify-between border border-gray-200 rounded-lg px-3 py-2 bg-gray-50/70">



      <div class="flex items-center gap-2 text-sm text-gray-600">



        <span class="text-xs font-medium text-gray-500">当前数据库：</span>



        <select v-model="activeDatabase" @change="switchDatabase" :disabled="!canSwitchDb" title="仅管理员可切换数据库"



          class="px-2 py-1.5 border border-gray-300 rounded text-sm max-w-48 disabled:bg-gray-100 disabled:text-gray-400 disabled:cursor-not-allowed">



          <option v-for="db in databases" :key="db" :value="db">{{ db }}</option>



        </select>



        <span v-if="!canSwitchDb" class="text-[11px] text-amber-500">仅管理员可切换数据库</span>



        <span v-else class="text-xs text-gray-400" v-if="activeDatabase">（切换后自动刷新表数据）</span>



      </div>



      <div v-if="isEditor()" class="flex items-center gap-2 text-xs text-gray-500">



        <span>删除数据库：</span>



        <select v-model="databaseToDelete" class="px-2 py-1.5 border border-gray-300 rounded text-sm">



          <option value="" disabled>请选择非当前数据库</option>



          <option v-for="db in databases.filter(name => name !== activeDatabase)" :key="db" :value="db">{{ db }}</option>



        </select>



        <button @click="deleteDatabase" :disabled="!databaseToDelete" class="px-2.5 py-1.5 rounded text-sm text-red-600 border border-red-200 hover:bg-red-50 disabled:opacity-40 disabled:cursor-not-allowed">删除</button>



      </div>



    </div>







    <!-- ====== 数据源导入（仅管理员） ====== -->



    <div v-if="isEditor()" class="border border-gray-200 rounded-lg p-3 bg-gray-50/70">



      <div class="flex items-center justify-between gap-3 flex-wrap">



        <div>



          <h3 class="text-sm font-semibold text-gray-700">数据源导入</h3>



          <p class="text-xs text-gray-400 mt-0.5">上传 CSV、SQLite/DB、ZIP 压缩包，或通过远程地址导入；主键会自动识别。</p>



        </div>



        <div class="flex items-center gap-2 flex-wrap">



          <select v-model="importMode" class="px-2 py-1.5 border border-gray-300 rounded text-sm">



            <option value="file">上传文件</option>



            <option value="url">远程地址</option>



          </select>



          <input v-if="importMode === 'file'" ref="csvFileInput" type="file" class="text-sm text-gray-500" accept=".csv,.sqlite,.sqlite3,.db,.zip" multiple />



          <input v-else v-model="importUrl" placeholder="https://.../database.sqlite" class="px-2 py-1.5 border border-gray-300 rounded text-sm min-w-[280px]" />



          <input v-model="importTableName" placeholder="表名（可选）" class="px-2 py-1.5 border border-gray-300 rounded text-sm" />



          <button @click="handleImportCsv" :disabled="importing" class="px-3 py-1.5 bg-primary text-white rounded text-sm hover:opacity-90 disabled:opacity-50">



            {{ importing ? '导入中...' : '导入' }}



          </button>



        </div>



      </div>



      <div v-if="importMessage" class="mt-3 text-sm" :class="importMessageType === 'success' ? 'text-success' : 'text-red-600'">



        {{ importMessage }}



      </div>



    </div>



    <!-- 非管理员（普通员工）：只读提示 + 反馈/申请授权入口 -->



    <div v-else class="border border-gray-200 rounded-lg p-3 bg-gray-50/70 flex items-center justify-between gap-3">



      <span class="text-xs text-gray-400">当前为只读浏览模式，仅可查看管理员已授权的数据表与字段</span>



      <button



        @click="openRequestModal"



        class="flex-shrink-0 px-3 py-1.5 bg-primary text-white rounded text-sm hover:opacity-90 transition flex items-center gap-1.5"



      >



        <span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="m3 21 1.9-5.7a8.5 8.5 0 1 1 3.8 3.8z" /> </svg></span> 反馈 / 申请授权



      </button>



    </div>







    <!-- 普通员工：授权目录加载中 -->



    <div



      v-if="authMode && !authCatalogError && authCatalog === null"



      class="border border-gray-200 rounded-lg p-10 bg-gray-50/50 flex flex-col items-center justify-center text-center h-[calc(100vh-260px)]"



    >



      <span class="w-6 h-6 border-2 border-gray-300 border-t-transparent rounded-full animate-spin mb-3"></span>



      <p class="text-sm text-gray-400">正在加载您的数据访问授权…</p>



    </div>







    <!-- 普通员工：授权目录加载失败（错误态 + 重试，避免永久加载死屏） -->



    <div



      v-else-if="authMode && authCatalogError"



      class="border border-gray-200 rounded-lg p-10 bg-red-50/40 flex flex-col items-center justify-center text-center h-[calc(100vh-260px)]"



    >



      <div class="text-4xl mb-3"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" /> <path d="M12 9v4" /> <path d="M12 17h.01" /> </svg></span></div>



      <h3 class="text-base font-semibold text-gray-700">加载数据访问授权失败</h3>



      <p class="text-sm text-gray-400 mt-2 max-w-md leading-relaxed">



        无法获取您的授权信息，可能是网络异常或登录已过期。请稍后重试，或重新登录后再进入本页。



      </p>



      <button



        @click="loadCatalog"



        class="mt-5 px-4 py-2 bg-primary text-white rounded text-sm hover:opacity-90 transition"



      >



        重新加载



      </button>



    </div>







    <!-- 普通员工未授权：友好空状态 -->



    <div



      v-else-if="authMode && authCatalog?.empty"



      class="border border-dashed border-gray-300 rounded-lg p-10 bg-gray-50/50 flex flex-col items-center justify-center text-center h-[calc(100vh-260px)]"



    >



      <div class="text-4xl mb-3"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <rect width="18" height="11" x="3" y="11" rx="2" ry="2" /> <path d="M7 11V7a5 5 0 0 1 10 0v4" /> </svg></span></div>



      <h3 class="text-base font-semibold text-gray-700">您当前暂无任何数据访问授权</h3>



      <p class="text-sm text-gray-400 mt-2 max-w-md leading-relaxed">



        管理员尚未为您分配可查看的数据表与字段，因此当前没有可展示的内容。



        如需访问特定数据，请点击「反馈 / 申请授权」向管理员提交申请，审批通过后即可在此查看。



      </p>



      <button



        @click="openRequestModal"



        class="mt-5 px-4 py-2 bg-primary text-white rounded text-sm hover:opacity-90 transition flex items-center gap-1.5"



      >



        <span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="m3 21 1.9-5.7a8.5 8.5 0 1 1 3.8 3.8z" /> </svg></span> 反馈 / 申请授权



      </button>



    </div>







    <div v-else class="flex gap-4 h-[calc(100vh-220px)]">



    <!-- ====== 左侧表列表 ====== -->



    <div class="w-56 flex-shrink-0 border border-gray-200 rounded-lg overflow-hidden flex flex-col">



      <div class="bg-gray-50 px-3 py-2 border-b border-gray-200 flex items-center justify-between">



        <span class="text-sm font-medium text-gray-600">数据表</span>



        <span class="text-sm text-gray-400">{{ filteredTableTabs.length }}</span>



      </div>



      <!-- 表搜索框 -->



      <div class="px-2 py-1.5 border-b border-gray-100">



        <input



          v-model="tableFilterKeyword"



          type="text"



          placeholder="搜索表名..."



          class="w-full px-2 py-1 text-xs border border-gray-300 rounded outline-none focus:border-primary focus:ring-1 focus:ring-primary"



        />



      </div>



      <div class="overflow-y-auto flex-1">



        <template v-for="group in groupedTableTabs" :key="group.scene">



          <div class="sticky top-0 z-[1] px-3 py-1.5 bg-gray-100/95 border-b border-gray-200 text-[10px] font-semibold text-gray-500">



            {{ group.scene }} <span class="font-normal text-gray-400">({{ group.names.length }})</span>



          </div>



          <div



            v-for="name in group.names"



            :key="name"



            @click="activeTab = name"



            class="px-3 py-2 text-sm cursor-pointer transition flex items-center justify-between border-b border-gray-50 hover:bg-gray-50"



            :class="activeTab === name ? 'bg-primary/10 text-primary border-l-2 border-primary' : 'text-gray-600'"



          >



            <span class="font-mono text-sm truncate">{{ name }}</span>



            <span v-if="getTableData(name)?.row_count !== undefined" class="text-[10px] text-gray-400 flex-shrink-0 ml-2">



              {{ getTableData(name)?.row_count }}行



            </span>



          </div>



        </template>



        <div v-if="loading" class="px-3 py-4 text-center text-sm text-gray-400">加载中...</div>



        <div v-if="!loading && filteredTableTabs.length === 0" class="px-3 py-4 text-center text-sm text-gray-400">



          {{ tableFilterKeyword ? '未找到匹配的表' : '暂无数据表' }}



        </div>



      </div>



    </div>







    <!-- ====== 右侧详情 ====== -->



    <div class="flex-1 min-w-0 border border-gray-200 rounded-lg overflow-hidden flex flex-col">



      <!-- 表头信息 -->



      <div v-if="currentTableData" class="bg-gray-50 px-4 py-2 border-b border-gray-200 flex items-center justify-between flex-shrink-0">



        <div class="flex items-center gap-3">



          <span class="font-mono text-sm font-semibold text-gray-700">{{ currentTableData.table_name }}</span>



          <span class="text-sm text-gray-400">共 {{ visibleColumns.length }} 个字段</span>



        </div>



        <div class="flex items-center gap-3 text-sm text-gray-400">



          <span>{{ currentTableData.row_count ?? 0 }} 行</span>



        </div>



      </div>







      <!-- 内容区 -->



      <div v-if="currentTableData" class="flex-1 flex flex-col overflow-hidden">



        <!-- Tab 切换栏 -->



        <div class="flex items-center justify-between px-4 py-2 border-b border-gray-200 flex-shrink-0 bg-gray-50/80">



          <div class="flex gap-1 p-0.5 bg-gray-100 rounded-lg">



            <button



              @click="activeTabView = 'fields'"



              class="px-4 py-1.5 text-xs font-medium rounded-md transition-all duration-200"



              :class="activeTabView === 'fields' 



                ? 'bg-white text-primary shadow-sm' 



                : 'text-gray-500 hover:text-gray-700'"



            >



              <span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <line x1="8" x2="21" y1="6" y2="6" /> <line x1="8" x2="21" y1="12" y2="12" /> <line x1="8" x2="21" y1="18" y2="18" /> <line x1="3" x2="3.01" y1="6" y2="6" /> <line x1="3" x2="3.01" y1="12" y2="12" /> <line x1="3" x2="3.01" y1="18" y2="18" /> </svg></span> 字段



            </button>



            <button



              @click="activeTabView = 'sample'"



              class="px-4 py-1.5 text-xs font-medium rounded-md transition-all duration-200"



              :class="activeTabView === 'sample' 



                ? 'bg-white text-primary shadow-sm' 



                : 'text-gray-500 hover:text-gray-700'"



            >



              <span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <line x1="18" x2="18" y1="20" y2="10" /> <line x1="12" x2="12" y1="20" y2="4" /> <line x1="6" x2="6" y1="20" y2="14" /> </svg></span> 全部数据



            </button>



            <button



              @click="activeTabView = 'relationships'"



              class="px-4 py-1.5 text-xs font-medium rounded-md transition-all duration-200"



              :class="activeTabView === 'relationships' 



                ? 'bg-white text-primary shadow-sm' 



                : 'text-gray-500 hover:text-gray-700'"



            >



              <span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M9 17H7A5 5 0 0 1 7 7h2" /> <path d="M15 7h2a5 5 0 1 1 0 10h-2" /> <line x1="8" x2="16" y1="12" y2="12" /> </svg></span> 表关系



            </button>



          </div>



          <!-- 搜索（只在字段 Tab 显示） -->



          <div v-if="activeTabView === 'fields'" class="flex items-center gap-2">



            <input



              v-model="filterKeyword"



              type="text"



              placeholder="搜索字段名或备注..."



              class="px-2 py-1 text-xs border border-gray-300 rounded outline-none focus:border-primary focus:ring-1 focus:ring-primary w-48"



            />



            <button



              v-if="filterKeyword"



              @click="filterKeyword = ''"



              class="text-xs text-gray-400 hover:text-gray-600"



            >



              <span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M18 6 6 18" /> <path d="m6 6 12 12" /> </svg></span>



            </button>



          </div>



          <!-- 关系数量提示 -->



          <div v-else-if="activeTabView === 'relationships'" class="text-xs text-gray-400">



            共 {{ relationships.length }} 条关系



          </div>



        </div>







        <!-- ====== 字段列表 Tab ====== -->



        <div v-if="activeTabView === 'fields'" class="flex-1 overflow-y-auto px-4 py-2">



          <table class="w-full text-sm">



            <thead class="bg-gray-50 sticky top-0 z-10">



              <tr>



                <th class="px-3 py-1.5 text-left font-medium text-gray-500 text-base">字段名</th>



                <th class="px-3 py-1.5 text-left font-medium text-gray-500 text-base">类型</th>



                <th class="px-3 py-1.5 text-left font-medium text-gray-500 text-base">可空</th>



                <th class="px-3 py-1.5 text-left font-medium text-gray-500 text-base">主键</th>



                <th class="px-3 py-1.5 text-left font-medium text-gray-500 text-base">备注</th>



              </tr>



            </thead>



            <tbody>



              <tr v-for="col in filteredColumns" :key="col.name" class="border-t border-gray-100 hover:bg-gray-50/50 transition">



                <td class="px-3 py-1.5 font-mono text-primary text-sm">{{ col.name }}</td>



                <td class="px-3 py-1.5 text-gray-500 text-sm">{{ col.type }}</td>



                <td class="px-3 py-1.5 text-gray-400 text-sm"><span v-if="col.nullable" class="eico text-emerald-500"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <polyline points="20 6 9 17 4 12" /> </svg></span></span><span v-else class="eico text-gray-300"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M18 6 6 18" /> <path d="m6 6 12 12" /> </svg></span></span></td>



                <td class="px-3 py-1.5 text-gray-400 text-sm"><span v-if="col.primary_key" class="eico text-amber-500"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <circle cx="7.5" cy="15.5" r="5.5" /> <path d="m21 2-9.6 9.6" /> <path d="m15.5 7.5 3 3L22 7l-3-3" /> </svg></span></span></td>



                <!-- 备注：单击查看完整内容（编辑在弹框内进行） -->



                <td class="px-3 py-1 text-sm">



                  <div



                    @click="openCommentDetail(col)"



                    class="cursor-pointer text-gray-400 hover:text-primary hover:bg-gray-100 px-2 py-0.5 rounded min-h-[24px] transition group flex items-center"



                  >



                    <span class="truncate max-w-[160px]" :title="fieldCommentCn(currentTableData.table_name, col)">{{ fieldCommentCn(currentTableData.table_name, col) }}</span>



                    <span class="ml-1 opacity-0 group-hover:opacity-100 text-[10px] text-gray-300"><span v-if="isEditor()"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" /> <path d="m15 5 4 4" /> </svg></span></span><span v-else><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z" /> <circle cx="12" cy="12" r="3" /> </svg></span></span></span>



                  </div>



                </td>



              </tr>



            </tbody>



          </table>







          <!-- 备注详情弹框（单击备注查看完整内容，支持弹框内编辑）+ 保存成功提示 -->



          <Teleport to="body">



            <div v-if="commentDetail" class="fixed inset-0 z-[60] flex items-center justify-center">



              <div class="absolute inset-0 bg-slate-900/40" @click="commentDetail = null"></div>



              <div class="relative w-[520px] max-w-[92vw] bg-white rounded-2xl shadow-2xl border border-gray-200 overflow-hidden">



                <div class="flex items-center justify-between px-4 py-3 bg-primary text-white">



                  <span class="text-sm font-semibold truncate">字段备注 · {{ commentDetail.name }}</span>



                  <button @click="commentDetail = null" class="text-white/80 hover:text-white text-sm px-1.5"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M18 6 6 18" /> <path d="m6 6 12 12" /> </svg></span></button>



                </div>







                <!-- 查看模式 -->



                <div v-if="!commentEditing" class="px-4 py-4 max-h-[60vh] overflow-y-auto">



                  <p class="text-sm text-gray-700 leading-relaxed whitespace-pre-wrap break-words">{{ commentDetail.text || '（暂无备注）' }}</p>



                </div>



                <!-- 编辑模式（弹框内直接编辑） -->



                <div v-else class="px-4 py-4">



                  <textarea



                    v-model="commentEditValue"



                    rows="5"



                    class="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg outline-none focus:border-primary focus:ring-1 focus:ring-primary/20 resize-y"



                    placeholder="输入备注..."



                    maxlength="255"



                  ></textarea>



                  <div class="text-right text-[10px] text-gray-300 mt-1">{{ commentEditValue.length }}/255</div>



                </div>







                <div class="flex justify-end gap-2 px-4 py-3 bg-gray-50 border-t border-gray-100">



                  <template v-if="!commentEditing">



                    <button v-if="isEditor()" @click="editFromDetail" class="px-4 py-1.5 text-xs rounded bg-primary text-white font-medium hover:bg-primary-dark transition">编辑</button>



                    <button @click="commentDetail = null" class="px-4 py-1.5 text-xs rounded bg-gray-100 text-gray-600 hover:bg-gray-200 transition">关闭</button>



                  </template>



                  <template v-else>



                    <button @click="cancelFromDetail" class="px-4 py-1.5 text-xs rounded bg-gray-100 text-gray-600 hover:bg-gray-200 transition">取消</button>



                    <button @click="saveFromDetail" class="px-4 py-1.5 text-xs rounded bg-primary text-white font-medium hover:bg-primary-dark transition">保存</button>



                  </template>



                </div>



              </div>



            </div>







            <transition name="fade">



              <div v-if="commentToast" class="fixed top-20 left-1/2 -translate-x-1/2 z-[70] px-4 py-2 rounded-lg bg-primary text-white text-sm shadow-lg">



                <span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /> <polyline points="22 4 12 14.01 9 11.01" /> </svg></span> {{ commentToast }}



              </div>



            </transition>



          </Teleport>



        </div>







        <!-- ====== 全部数据 Tab ====== -->



        <div v-if="activeTabView === 'sample'" class="flex-1 overflow-y-auto px-4 py-2 relative">
        <!-- AI 字段翻译（仅非 yans 库、可编辑角色；翻译写回列备注，表头随之显示中文） -->
        <div v-if="canAiTranslate" class="flex items-center justify-end px-1 pb-1.5">
          <button
            @click="translateTableFields"
            :disabled="translating"
            class="px-2.5 py-1 rounded-lg text-xs border border-indigo-200 text-indigo-600 hover:bg-indigo-50 transition disabled:opacity-50"
          >><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z" /> <path d="M5 3v4" /> <path d="M19 17v4" /> <path d="M3 5h4" /> <path d="M17 19h4" /> </svg></span>{{ translating ? 'AI 翻译中…' : ' AI 翻译字段（写入中文备注）' }}</button>
        </div>
        <!-- 时间筛选（近 N 天 / 自定义起止；仅该表含时间列时显示） -->
        <div v-if="timeColOptions.length" class="sticky top-0 z-[3] flex flex-wrap items-center gap-x-3 gap-y-1 px-1 py-1.5 border-b border-gray-100 bg-gray-50/95 text-xs text-gray-600 mb-2">
          <span class="text-gray-400"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <rect width="18" height="18" x="3" y="4" rx="2" /> <path d="M16 2v4" /> <path d="M8 2v4" /> <path d="M3 10h18" /> </svg></span> 时间筛选</span>
          <select v-model="timeFilterCol" @change="onTimeFilterChange" class="px-1.5 py-1 border border-gray-200 rounded text-xs bg-white max-w-48">
            <option v-for="c in timeColOptions" :key="c.name" :value="c.name">{{ c.label }}</option>
          </select>
          <span class="flex items-center overflow-hidden border border-gray-200 rounded bg-white">
            <button @click="setQuickRange(7)" class="px-2 py-1" :class="quickDays === 7 ? 'bg-primary text-white' : 'text-gray-500 hover:bg-gray-100'">近7天</button>
            <button @click="setQuickRange(30)" class="px-2 py-1 border-l border-gray-100" :class="quickDays === 30 ? 'bg-primary text-white' : 'text-gray-500 hover:bg-gray-100'">近30天</button>
            <button @click="setQuickRange(90)" class="px-2 py-1 border-l border-gray-100" :class="quickDays === 90 ? 'bg-primary text-white' : 'text-gray-500 hover:bg-gray-100'">近90天</button>
          </span>
          <span class="flex items-center gap-1">
            <input type="date" v-model="timeFilterStart" @change="onTimeFilterChange" class="px-1.5 py-1 border border-gray-200 rounded text-xs" />
            <span class="text-gray-300">至</span>
            <input type="date" v-model="timeFilterEnd" @change="onTimeFilterChange" class="px-1.5 py-1 border border-gray-200 rounded text-xs" />
          </span>
          <button v-if="timeFilterActive" @click="clearTimeFilter" class="text-gray-400 hover:text-gray-600"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M18 6 6 18" /> <path d="m6 6 12 12" /> </svg></span> 清除筛选</button>
          <span v-else class="text-gray-300">显示全部时间</span>
        </div>



          <!-- 加载遮罩 -->



          <div v-if="tableDataLoading" class="absolute inset-0 bg-white/60 backdrop-blur-[1px] flex items-center justify-center z-10">



            <span class="text-sm text-gray-400 animate-pulse">加载中...</span>



          </div>



          <div v-if="currentTableData.sample_data?.length" class="overflow-x-auto">



            <table class="w-full text-sm">



              <thead>



                <tr class="text-left text-gray-400 bg-gray-50 sticky top-0 z-10">



                  <th



                    v-for="col in visibleColumns"



                    :key="col.name"



                    class="px-2 py-1.5 font-normal text-sm cursor-pointer select-none group hover:bg-gray-100" @click="toggleSort(col.name)"



                    :title="col.name + (fieldHeaderText(currentTableData.table_name, col) !== col.name ? '（' + fieldHeaderText(currentTableData.table_name, col) + '）' : '') + ' · ' + (col.type || '') + (colMaskOf(currentTableData.table_name, col.name) ? ' · 脱敏:' + colMaskOf(currentTableData.table_name, col.name) : '')"



                  >



                    <span class="inline-flex items-center gap-1">
                    {{ fieldHeaderText(currentTableData.table_name, col) }}
                    <span v-if="sortColumn === col.name" class="text-[10px] leading-none text-primary">{{ sortDir === 'asc' ? '▲' : '▼' }}</span>
                    <span v-else class="text-[10px] leading-none text-gray-300 opacity-0 group-hover:opacity-100">⇅</span>
                    </span>



                    <span v-if="colMaskOf(currentTableData.table_name, col.name)" class="text-[10px] text-amber-500" title="该字段按权限脱敏展示"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <rect width="18" height="11" x="3" y="11" rx="2" ry="2" /> <path d="M7 11V7a5 5 0 0 1 10 0v4" /> </svg></span></span>



                  </th>



                </tr>



              </thead>



              <tbody>



                <tr



                  v-for="(row, rowIdx) in currentTableData.sample_data"



                  :key="rowIdx"



                  class="border-t border-gray-50 hover:bg-gray-50/50 transition"



                >



                  <td



                    v-for="col in visibleColumns"



                    :key="col.name"



                    class="px-2 py-1 text-gray-600 font-mono text-sm max-w-[150px] truncate relative"



                    :title="String(row[col.name] ?? '')"



                    @dblclick="isEditor() && startEditSample(row, col.name, rowIdx, row[col.name])"



                  >



                    <!-- 编辑状态 -->



                    <div v-if="editingSample.rowIdx === rowIdx && editingSample.cellKey === col.name" class="flex items-center gap-1">



                      <input



                        ref="sampleInputRef"



                        v-model="editingSample.value"



                        @keyup.enter="saveSampleEdit(row, col.name)"



                        @keyup.esc="cancelSampleEdit"



                        class="w-full px-1 py-0.5 text-sm font-mono border border-primary rounded outline-none"



                        :class="editingSample.loading ? 'opacity-50' : ''"



                        :disabled="editingSample.loading"



                      />



                      <span v-if="editingSample.loading" class="text-[10px] text-gray-400 animate-pulse">⏳</span>



                    </div>



                    <!-- 显示状态 -->



                    <div v-else class="cursor-text hover:bg-primary/5 rounded px-1 py-0.5 transition min-h-[24px]">



                      {{ row[col.name] !== null && row[col.name] !== undefined ? row[col.name] : 'null' }}



                    </div>



                  </td>



                </tr>



              </tbody>



            </table>



            <!-- 底部信息 + 分页 -->



            <div class="flex items-center justify-between text-xs text-gray-400 mt-2 flex-wrap gap-2">



              <span>



                共 {{ currentTableData.total_count ?? currentTableData.sample_data.length }} 行全部数据 · {{ visibleColumns.length }} 个字段



              </span>



              <span class="text-[10px]" :class="isEditor() ? 'text-gray-300' : 'text-gray-400'"><span v-if="isEditor()"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5" /> <path d="M9 18h6" /> <path d="M10 22h4" /> </svg></span></span><span v-else><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <rect width="18" height="11" x="3" y="11" rx="2" ry="2" /> <path d="M7 11V7a5 5 0 0 1 10 0v4" /> </svg></span></span>{{ isEditor() ? ' 双击单元格可编辑（修改会直接写入本地数据库）' : ' 数据为只读，编辑需管理员权限' }}</span>



            </div>



            <div class="flex items-center justify-end gap-2 mt-2 pb-1 text-xs">



              <button



                @click="goToPage(currentPage - 1)"



                :disabled="currentPage <= 1"



                class="px-2.5 py-1 border border-gray-200 rounded text-gray-500 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"



              >‹ 上一页</button>



              <span class="text-gray-500">第 {{ currentPage }} / {{ totalPages }} 页</span>



              <button



                @click="goToPage(currentPage + 1)"



                :disabled="currentPage >= totalPages"



                class="px-2.5 py-1 border border-gray-200 rounded text-gray-500 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"



              >下一页 ›</button>



            </div>



          </div>



          <div v-else-if="!tableDataLoading" class="flex items-center justify-center h-32 text-gray-400 text-sm">



            暂无数据



          </div>



        </div>







        <!-- ====== 表关系 Tab ====== -->



        <div v-if="activeTabView === 'relationships'" class="flex-1 overflow-y-auto px-4 py-2">



          <div v-if="relationshipsLoading" class="flex items-center justify-center h-32 text-gray-400 text-sm">



            加载中...



          </div>



          <div v-else-if="relationships.length === 0" class="flex items-center justify-center h-32 text-gray-400 text-sm">



            暂无表间关系数据



          </div>



          <template v-else>



            <!-- 关系总览入口：ER 图在「全局查看」全屏中展示（小面板放不下，全屏才能看清） -->



            <div class="border border-gray-200 rounded-lg mb-3 bg-gradient-to-r from-indigo-50/60 to-transparent">



              <div class="flex items-center justify-between px-4 py-3">



                <div>



                  <div class="text-sm font-medium text-gray-700"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <circle cx="12" cy="18" r="3" /> <circle cx="6" cy="6" r="3" /> <circle cx="18" cy="6" r="3" /> <path d="M18 9v1a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V9" /> <path d="M12 12v3" /> </svg></span> 表关系 ER 图</div>



                  

                </div>



                <button



                  @click="erFullscreen = true"



                  class="px-4 py-1.5 rounded-lg bg-indigo-600 text-white text-xs font-medium hover:bg-indigo-700 transition shadow-sm flex items-center gap-1.5"



                >



                  ⛶ 查看 ER 图



                </button>



              </div>



              <!-- 图例 -->



              <div class="flex flex-wrap items-center gap-4 px-4 pb-3 text-[10px] text-gray-400">



                <span class="flex items-center gap-1"><span class="w-2.5 h-2.5 rounded-sm bg-indigo-600 mr-1"></span>实体表</span>



                <span class="flex items-center gap-1"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <circle cx="7.5" cy="15.5" r="5.5" /> <path d="m21 2-9.6 9.6" /> <path d="m15.5 7.5 3 3L22 7l-3-3" /> </svg></span> 主键</span>



                <span class="flex items-center gap-1"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M9 17H7A5 5 0 0 1 7 7h2" /> <path d="M15 7h2a5 5 0 1 1 0 10h-2" /> <line x1="8" x2="16" y1="12" y2="12" /> </svg></span> 外键</span>



            

                <span class="text-gray-300 ml-1"> 连线直接标注字段映射 · 点击查看说明</span>



              </div>



            </div>







            <!-- ER 图全屏弹层（ER 图只在这里展示） -->



            <Teleport to="body">



              <div v-if="erFullscreen" class="fixed inset-0 z-[100] bg-white flex flex-col">



                <div class="flex items-center justify-between px-4 py-2.5 border-b border-gray-200 bg-gray-50/80">



                  <div class="flex items-center gap-2">



                    <span class="text-sm font-medium text-gray-700"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <circle cx="12" cy="18" r="3" /> <circle cx="6" cy="6" r="3" /> <circle cx="18" cy="6" r="3" /> <path d="M18 9v1a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V9" /> <path d="M12 12v3" /> </svg></span> 表关系 ER 图（全局查看）</span>



                    <button v-if="!erChoosing" @click="backToErChoosing" class="text-xs px-2 py-1 rounded bg-gray-100 text-gray-600 hover:bg-gray-200 transition"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" /> <path d="M21 3v5h-5" /> <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" /> <path d="M8 16H3v5" /> </svg></span> 重新选表</button>



                  </div>



                  <div class="flex items-center gap-2">



                    <button @click="zoomErBy(1.25)" class="text-xs px-2 py-1 rounded bg-gray-100 text-gray-600 hover:bg-gray-200 transition" title="放大"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M5 12h14" /> <path d="M12 5v14" /> </svg></span> 放大</button>



                    <button @click="zoomErBy(0.8)" class="text-xs px-2 py-1 rounded bg-gray-100 text-gray-600 hover:bg-gray-200 transition" title="缩小">－ 缩小</button>



                    <button @click="fitErFullscreen" class="text-xs px-2 py-1 rounded bg-gray-100 text-gray-600 hover:bg-gray-200 transition">⊞ 适应窗口</button>



                    <button @click="clearErLayout" class="text-xs px-2 py-1 rounded bg-gray-100 text-gray-600 hover:bg-gray-200 transition" title="清除记忆的布局，恢复默认排列">↺ 重置布局</button>



                    <button @click="downloadErPng" class="text-xs px-2 py-1 rounded bg-gray-100 text-gray-600 hover:bg-gray-200 transition"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /> <polyline points="7 10 12 15 17 10" /> <line x1="12" x2="12" y1="15" y2="3" /> </svg></span> 下载 PNG</button>



                    <button @click="erFullscreen = false" class="px-3 py-1 text-xs rounded bg-gray-100 text-gray-600 hover:bg-gray-200 transition"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M18 6 6 18" /> <path d="m6 6 12 12" /> </svg></span> 关闭</button>



                  </div>



                </div>



                <div class="flex-1 min-h-0 overflow-hidden p-4 bg-slate-50">



                  <!-- 选表面板：先选择要查看的表，再生成 ER 图 -->



                  <div v-if="erChoosing" class="flex items-start justify-center py-6">



                    <div class="w-full max-w-4xl bg-white rounded-2xl border border-gray-200 shadow-sm p-6">



                      <div class="flex items-center justify-between mb-4">



                        <div>



                          <div class="text-sm font-semibold text-gray-700">请选择要查看的表</div>



                          <div class="text-xs text-gray-400 mt-0.5">勾选后生成 ER 图，仅展示所选表及其之间的关系（共 {{ allTables.length }} 张表）</div>



                        </div>



                        <div class="flex items-center gap-2">



                          <button @click="toggleAllErTables" class="text-xs px-3 py-1.5 rounded bg-gray-100 text-gray-600 hover:bg-gray-200 transition">清空</button>



                          <button @click="startErRender" :disabled="!erSelectedTables.length" class="text-xs px-4 py-1.5 rounded bg-indigo-600 text-white font-medium hover:bg-indigo-700 transition disabled:opacity-40 disabled:cursor-not-allowed">生成 ER 图（{{ erSelectedTables.length }}）</button>



                        </div>



                      </div>



                      <!-- 搜索框：表多时快速过滤 -->



                      <div class="mb-3">



                        <div class="flex items-center border border-gray-200 rounded-lg px-3 py-2 bg-white focus-within:border-indigo-400 focus-within:ring-1 focus-within:ring-indigo-400/20 transition">



                          <span class="text-gray-400 mr-2 text-xs"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <circle cx="11" cy="11" r="8" /> <path d="m21 21-4.3-4.3" /> </svg></span></span>



                          <input v-model="erTableSearch" type="text" placeholder="搜索表名或中文名…" class="flex-1 text-xs outline-none bg-transparent text-gray-700" />



                          <button v-if="erTableSearch" @click="erTableSearch = ''" class="text-gray-400 hover:text-gray-600 text-xs px-1"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M18 6 6 18" /> <path d="m6 6 12 12" /> </svg></span></button>



                        </div>



                      </div>



                      <div class="max-h-[60vh] overflow-y-auto space-y-4">



                        <section v-for="group in groupedErTables" :key="group.scene">



                          <div class="flex items-center gap-2 mb-2 text-xs font-semibold text-gray-600">



                            <span class="w-1.5 h-4 rounded-full bg-indigo-400"></span>



                            {{ group.scene }}



                            <span class="font-normal text-gray-400">{{ group.tables.length }} 张表</span>



                          </div>



                          <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-2">



                            <label v-for="t in group.tables" :key="t.table_name" class="flex items-center gap-2 px-3 py-2 rounded-lg border cursor-pointer transition" :class="erSelectedTables.includes(t.table_name) ? 'border-indigo-300 bg-indigo-50/60' : 'border-gray-200 bg-white hover:border-gray-300'">



                              <input type="checkbox" :value="t.table_name" v-model="erSelectedTables" class="accent-indigo-600" />



                              <span class="font-mono text-xs text-gray-700 truncate" :title="t.table_name">{{ t.table_name }}</span>



                              <span v-if="t.chinese_name" class="text-[10px] text-gray-400 truncate">{{ t.chinese_name }}</span>



                            </label>



                          </div>



                        </section>



                        <div v-if="!filteredErTables.length" class="col-span-full text-center text-xs text-gray-400 py-8">未找到匹配的表，请调整搜索词</div>



                      </div>



                    </div>



                  </div>







                  <!-- 图渲染区（选表完成后显示） -->



                  <template v-else>



                  <!-- 操作提示：sticky 停留在滚动视口顶部 -->



                  <div class="sticky top-0 left-0 z-[5] mb-2 w-fit text-[10px] text-gray-500 bg-white/90 border border-gray-200 rounded px-2 py-1 shadow-sm">



                   单击节点查看说明



                  </div>



                  <!-- 图例：主键/外键/关系类型/业务域 -->



                  <div class="sticky top-8 left-0 z-[5] mb-2 w-fit max-w-full flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-gray-500 bg-white/90 border border-gray-200 rounded px-3 py-1.5 shadow-sm">



                    <span><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <circle cx="7.5" cy="15.5" r="5.5" /> <path d="m21 2-9.6 9.6" /> <path d="m15.5 7.5 3 3L22 7l-3-3" /> </svg></span> 主键(PK)</span>



                    <span><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M9 17H7A5 5 0 0 1 7 7h2" /> <path d="M15 7h2a5 5 0 1 1 0 10h-2" /> <line x1="8" x2="16" y1="12" y2="12" /> </svg></span> 外键(FK)</span>



                    



                  </div>



                  <!-- 正在载入提示 -->



                  <div v-if="erRendering" class="sticky top-8 left-0 z-[6] mb-2 w-fit">



                    <div class="flex items-center gap-2.5 bg-white/90 border border-gray-200 rounded-lg px-4 py-2.5 shadow-sm">



                      <div class="w-5 h-5 border-[3px] border-indigo-500 border-t-transparent rounded-full animate-spin"></div>



                      <span class="text-sm text-gray-500">正在载入 ER 图...</span>



                    </div>



                  </div>



                  <!-- 画布：按住鼠标拖拽即可平移整个图（不用页面滚动条） -->



                  <div ref="erFullscreenRef" class="w-full h-full cursor-grab active:cursor-grabbing" style="touch-action: none;"></div>



                  </template>



                  <!-- 字段详情弹窗：单击实体卡片弹出，居中大窗展示该表全部字段（点遮罩/✕ 关闭） -->



                  <div v-if="erSelectedTable" class="fixed inset-0 z-20 flex items-center justify-center">



                    <div class="absolute inset-0 bg-slate-900/40" @click="erSelectedTable = null"></div>



                    <div class="relative w-[560px] max-w-[92vw] max-h-[84vh] bg-white rounded-2xl shadow-2xl border border-gray-200 flex flex-col overflow-hidden">



                      <div class="flex items-center justify-between px-4 py-3 bg-indigo-600 text-white">



                        <span class="text-sm font-semibold truncate"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <line x1="8" x2="21" y1="6" y2="6" /> <line x1="8" x2="21" y1="12" y2="12" /> <line x1="8" x2="21" y1="18" y2="18" /> <line x1="3" x2="3.01" y1="6" y2="6" /> <line x1="3" x2="3.01" y1="12" y2="12" /> <line x1="3" x2="3.01" y1="18" y2="18" /> </svg></span> {{ erSelectedTable.table_name }}{{ erSelectedTable.chinese_name ? ' · ' + erSelectedTable.chinese_name : '' }}</span>



                        <button @click="erSelectedTable = null" class="text-white/80 hover:text-white text-sm px-1.5"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M18 6 6 18" /> <path d="m6 6 12 12" /> </svg></span></button>



                      </div>



                      <div class="px-4 py-2 text-xs text-gray-500 bg-indigo-50/60 border-b border-gray-100 flex items-center gap-3">



                        <span>{{ erSelectedTable.columns?.length || 0 }} 个字段</span>



                        <span v-if="erSelectedTable.row_count !== undefined">· {{ Number(erSelectedTable.row_count).toLocaleString() }} 行</span>



                        <button



                          @click="jumpToTable(erSelectedTable.table_name)"



                          class="ml-auto text-xs px-2.5 py-1 rounded bg-white border border-indigo-200 text-indigo-600 hover:bg-indigo-50 transition"



                        >



                          去数据页查看 →



                        </button>



                      </div>



                      <div class="overflow-y-auto flex-1">



                        <div



                          v-for="(col, i) in erSelectedTable.columns"



                          :key="i"



                          class="px-4 py-2 border-b border-gray-50 flex items-center gap-3 text-sm hover:bg-gray-50/70"



                        >



                          <span class="flex-shrink-0 w-5 text-center"><span v-if="col.primary_key"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <circle cx="7.5" cy="15.5" r="5.5" /> <path d="m21 2-9.6 9.6" /> <path d="m15.5 7.5 3 3L22 7l-3-3" /> </svg></span></span><span v-else-if="fkMap.get(erSelectedTable.table_name)?.has(col.name)"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M9 17H7A5 5 0 0 1 7 7h2" /> <path d="M15 7h2a5 5 0 1 1 0 10h-2" /> <line x1="8" x2="16" y1="12" y2="12" /> </svg></span></span></span>



                          <span class="font-mono font-medium text-gray-800">{{ col.name }}</span>



                          <!-- 外键：显示关联目标（目标表.目标列） -->



                          <span v-if="findFkTarget(erSelectedTable.table_name, col.name)" class="text-[11px] text-blue-500 font-mono flex-shrink-0">→ {{ findFkTarget(erSelectedTable.table_name, col.name) }}</span>



                          <span v-if="!col.nullable" class="text-[10px] text-gray-300 flex-shrink-0">NOT NULL</span>



                          <span class="ml-auto font-mono text-xs text-gray-400">{{ col.type }}</span>



                        </div>



                        <div v-if="!erSelectedTable.columns?.length" class="p-6 text-center text-sm text-gray-400">暂无字段信息</div>



                      </div>



                      <div



                        v-if="hasColumnComments(erSelectedTable)"



                        class="px-4 py-2.5 bg-gray-50 border-t border-gray-100 text-xs text-gray-500 max-h-40 overflow-y-auto"



                      >



                        <div v-for="(col, i) in erSelectedTable.columns.filter((c: any) => fieldCommentCn(erSelectedTable.table_name, c))" :key="i" class="flex gap-2 py-1">



                          <span class="font-mono text-gray-700 flex-shrink-0">{{ col.name }}</span>



                          <span class="text-gray-300 flex-shrink-0">—</span>



                          <span>{{ fieldCommentCn(erSelectedTable.table_name, col) }}</span>



                        </div>



                      </div>



                    </div>



                  </div>



                </div>



              </div>



            </Teleport>







            <!-- 表间关系明细列表（可折叠） -->



            <details class="border border-gray-200 rounded-lg overflow-hidden group">



              <summary class="px-4 py-2 bg-gray-50 border-b border-gray-200 cursor-pointer text-sm font-medium text-gray-700 select-none">



                <span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <line x1="8" x2="21" y1="6" y2="6" /> <line x1="8" x2="21" y1="12" y2="12" /> <line x1="8" x2="21" y1="18" y2="18" /> <line x1="3" x2="3.01" y1="6" y2="6" /> <line x1="3" x2="3.01" y1="12" y2="12" /> <line x1="3" x2="3.01" y1="18" y2="18" /> </svg></span> 表间关系明细（{{ relationships.length }} 条）<span class="text-gray-400 font-normal text-xs ml-1">点击展开</span>



              </summary>



              <div class="divide-y divide-gray-100 max-h-72 overflow-y-auto">



                <div



                  v-for="(rel, idx) in relationships"



                  :key="idx"



                  class="px-4 py-2 flex items-center gap-3 hover:bg-gray-50/50 transition text-xs"



                >



                  <span class="font-mono text-primary">{{ rel.source_table }}.{{ rel.source_column }}</span>



                  <svg class="w-3.5 h-3.5 text-gray-300 flex-shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">



                    <path stroke-linecap="round" stroke-linejoin="round" d="M13 7l5 5m0 0l-5 5m5-5H6" />



                  </svg>



                  <span class="font-mono text-gray-700">{{ rel.target_table }}.{{ rel.target_column }}</span>



                  <span



                    class="ml-auto text-[10px] px-2 py-0.5 rounded-full flex-shrink-0"



                    :class="rel.type === 'foreign_key' ? 'bg-blue-100 text-blue-600' : 'bg-purple-100 text-purple-600'"



                  >



                    {{ relDocDesc(rel) }}



                  </span>



                </div>



              </div>



            </details>







            <!-- 图例 -->



            <div class="mt-3 flex items-center gap-4 text-xs text-gray-400 border-t border-gray-100 pt-3">



              <span><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5" /> <path d="M9 18h6" /> <path d="M10 22h4" /> </svg></span> 表间关系说明：</span>



              <span><span class="inline-block w-2 h-2 rounded-full bg-blue-500 mr-1"></span> 外键约束</span>



              <span><span class="inline-block w-2 h-2 rounded-full bg-purple-500 mr-1"></span> 业务关联</span>



              <span class="ml-auto text-[10px]">关系总数：{{ relationships.length }}</span>



            </div>



          </template>



        </div>



      </div>







      <!-- 空状态 -->



      <div v-else-if="loading" class="flex-1 flex items-center justify-center text-gray-400 text-sm">



        加载中...



      </div>



      <div v-else class="flex-1 flex items-center justify-center text-gray-400 text-sm">



        请从左侧选择一张表



      </div>



    </div>



    </div>



  </div>







  <!-- ====== 反馈 / 申请授权 弹窗（普通员工 → 管理员） ====== -->



  <Teleport to="body">



    <div v-if="requestModalOpen" class="fixed inset-0 z-[90] flex items-center justify-center bg-slate-900/40 p-4">



      <div class="absolute inset-0" @click="closeRequestModal"></div>



      <div class="relative w-[680px] max-w-[94vw] max-h-[88vh] bg-white rounded-2xl shadow-2xl border border-gray-200 flex flex-col overflow-hidden">



        <!-- 标题栏 -->



        <div class="flex items-center justify-between px-5 py-3.5 border-b border-gray-200">



          <div>



            <h3 class="text-sm font-semibold text-gray-800"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="m3 21 1.9-5.7a8.5 8.5 0 1 1 3.8 3.8z" /> </svg></span> 反馈 / 申请授权</h3>



            <p class="text-[11px] text-gray-400 mt-0.5">向管理员申请访问特定数据表与字段，或提交反馈说明；提交后由管理员在审批中心处理。</p>



          </div>



          <button @click="closeRequestModal" :disabled="requestSaving" class="text-gray-400 hover:text-gray-600 text-sm px-2"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M18 6 6 18" /> <path d="m6 6 12 12" /> </svg></span></button>



        </div>







        <!-- 提交成功态 -->



        <div v-if="requestSaved" class="flex-1 flex flex-col items-center justify-center text-center p-8">



          <div class="text-4xl mb-3"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /> <polyline points="22 4 12 14.01 9 11.01" /> </svg></span></div>



          <h4 class="text-sm font-semibold text-gray-700">申请已提交</h4>



          <p class="text-xs text-gray-400 mt-2 max-w-xs leading-relaxed">管理员将在审批中心收到您的请求，审批通过后即可在数据页查看授权内容。</p>



        </div>







        <!-- 表单主体 -->



        <div v-else class="flex-1 overflow-y-auto px-5 py-3 min-h-0">



          <div class="text-xs font-medium text-gray-500 mb-2">选择需要申请的数据表 / 字段（不选则仅提交文字反馈）</div>



          <div v-if="!requestSchema.length" class="text-xs text-gray-400 py-6 text-center">加载可申请库表中...</div>



          <div v-else class="space-y-2 max-h-[42vh] overflow-y-auto pr-1">



            <div v-for="t in requestSchema" :key="t.table_name" class="border border-gray-200 rounded-lg overflow-hidden">



              <div class="flex items-center gap-2 px-3 py-2 bg-gray-50/70">



                <input type="checkbox" :checked="reqTableChecked(t.table_name)" @change="toggleReqTable(t.table_name)" class="accent-primary w-4 h-4" />



                <span class="font-mono text-sm text-gray-700 flex-1 truncate">{{ t.table_name }}</span>



                <span class="text-[10px] text-gray-400">{{ (t.columns || []).length }} 字段</span>



                <button @click="toggleReqExpand(t.table_name)" class="text-[11px] text-gray-400 hover:text-primary px-1">



                  {{ requestExpand.has(t.table_name) ? '收起' : '展开字段' }}



                </button>



              </div>



              <div v-if="requestExpand.has(t.table_name)" class="px-3 py-2 border-t border-gray-100 bg-white">



                <div class="flex items-center justify-between mb-1.5">



                  <span class="text-[11px] text-gray-400">勾选具体字段（默认整表全部字段）</span>



                  <button



                    @click="toggleReqTable(t.table_name)"



                    class="flex items-center gap-1 text-[11px] px-2 py-0.5 rounded border cursor-pointer transition"



                    :class="reqIsWholeTable(t.table_name)



                      ? 'border-primary bg-primary/10 text-primary' : 'border-gray-200 text-gray-500 hover:bg-gray-50'"



                  >



                    <span v-if="reqIsWholeTable(t.table_name)"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <polyline points="20 6 9 17 4 12" /> </svg></span></span> 整表



                  </button>



                </div>



                <div v-if="reqIsWholeTable(t.table_name)" class="text-[11px] text-gray-400 mb-1.5">



                  已选整表全部字段；取消「整表」后可单独勾选字段。



                </div>



                <div class="flex flex-wrap gap-1.5">



                  <label



                    v-for="c in (t.columns || [])" :key="c.name"



                    class="flex items-center gap-1 px-2 py-1 rounded border text-xs cursor-pointer transition"



                    :class="reqColChecked(t.table_name, c.name)



                      ? 'border-primary bg-primary/10 text-primary'



                      : 'border-gray-200 text-gray-500 hover:border-gray-300'"



                    :title="c.type"



                  >



                    <input type="checkbox" class="accent-primary w-3.5 h-3.5"



                           :checked="reqColChecked(t.table_name, c.name)"



                           :disabled="reqColDisabled(t.table_name)"



                           @change="toggleReqCol(t.table_name, c.name)" />



                    <span class="font-mono">{{ c.name }}</span>



                  </label>



                </div>



              </div>



            </div>



          </div>







          <!-- 申请说明 -->



          <div class="mt-3">



            <label class="text-xs font-medium text-gray-500">申请说明 / 反馈内容</label>



            <textarea



              v-model="requestReason" rows="3"



              placeholder="请说明需要访问的数据用途，例如：需要查看 test_orders 的订单金额用于月度对账…"



              class="mt-1 w-full px-2.5 py-2 text-sm border border-gray-300 rounded outline-none focus:border-primary focus:ring-1 focus:ring-primary resize-none"



            ></textarea>



          </div>



        </div>







        <!-- 底部操作 -->



        <div v-if="!requestSaved" class="flex items-center justify-between gap-3 px-5 py-3 border-t border-gray-200 bg-gray-50/60">



          <span class="text-[11px] text-gray-400">已选 {{ reqSelectedCount }} 个数据表</span>



          <div class="flex items-center gap-2">



            <button @click="closeRequestModal" :disabled="requestSaving" class="px-3 py-1.5 rounded text-sm text-gray-500 border border-gray-200 hover:bg-gray-100 disabled:opacity-40">取消</button>



            <button @click="submitRequest" :disabled="requestSaving" class="px-4 py-1.5 rounded text-sm bg-primary text-white hover:opacity-90 disabled:opacity-50 flex items-center gap-1.5">



              <span v-if="requestSaving" class="w-3.5 h-3.5 border-2 border-white/60 border-t-transparent rounded-full animate-spin"></span>



              {{ requestSaving ? '提交中...' : '提交申请' }}



            </button>



          </div>



        </div>



      </div>



    </div>



  </Teleport>

    <!-- ====== ER 图字段详情自定义弹框 ====== -->

  <Teleport to="body">

    <div v-if="erFieldModal.show" class="fixed inset-0 z-[200] flex items-center justify-center bg-slate-900/40">

      <div class="absolute inset-0" @click="erFieldModal.show = false"></div>

      <div class="relative w-[520px] max-w-[92vw] bg-white rounded-2xl shadow-2xl border border-gray-200 overflow-hidden">

        <div class="flex items-center justify-between px-4 py-3 bg-indigo-600 text-white">

          <span class="text-sm font-semibold truncate"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <line x1="8" x2="21" y1="6" y2="6" /> <line x1="8" x2="21" y1="12" y2="12" /> <line x1="8" x2="21" y1="18" y2="18" /> <line x1="3" x2="3.01" y1="6" y2="6" /> <line x1="3" x2="3.01" y1="12" y2="12" /> <line x1="3" x2="3.01" y1="18" y2="18" /> </svg></span> 字段详情 · {{ erFieldModal.name }}</span>

          <button @click="erFieldModal.show = false" class="text-white/80 hover:text-white text-sm px-1.5"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M18 6 6 18" /> <path d="m6 6 12 12" /> </svg></span></button>

        </div>

        <div class="px-4 py-4 space-y-3">

          <div class="flex gap-2 text-sm">

            <span class="text-gray-400 font-medium w-16 flex-shrink-0">字段名</span>

            <span class="text-gray-800 font-mono break-all leading-relaxed">{{ erFieldModal.name }}</span>

          </div>

          <div class="flex gap-2 text-sm">

            <span class="text-gray-400 font-medium w-16 flex-shrink-0">字段类型</span>

            <span class="text-gray-800 font-mono">{{ erFieldModal.type }}</span>

          </div>

          <div class="flex gap-2 text-sm">

            <span class="text-gray-400 font-medium w-16 flex-shrink-0">字段说明</span>

            <span class="text-gray-700 leading-relaxed whitespace-pre-wrap break-words">{{ erFieldModal.comment || '（暂无说明）' }}</span>

          </div>

          <!-- <div class="flex gap-2 text-sm">

            <span class="text-gray-400 font-medium w-16 flex-shrink-0">样例值</span>

            <span class="text-gray-600 font-mono">{{ erFieldModal.sample !== undefined && erFieldModal.sample !== null ? erFieldModal.sample : '（无）' }}</span>

          </div> -->

        </div>

        <div class="flex justify-end px-4 py-3 bg-gray-50 border-t border-gray-100">

          <button @click="erFieldModal.show = false" class="px-4 py-1.5 text-xs rounded bg-gray-100 text-gray-600 hover:bg-gray-200 transition">关闭</button>

        </div>

      </div>

    </div>

  </Teleport>



</template>







<script setup lang="ts">



// 多根组件（顶层另有 Teleport 弹窗）：关闭自动继承，改由模板根元素显式 v-bind="$attrs"
defineOptions({ inheritAttrs: false })

// 显式声明「接收」的事件。App.vue 对每个页面组件统一广播 navigate / navigate-ask /
// question-consumed / account-updated / database-switched，本页只做展示、不向外发事件。
// 不声明它们就会留在 $attrs 里，被模板根元素的 v-bind="$attrs" 当作原生 DOM 事件监听器
// 挂到 <div> 上——永远不可能触发，只是白白往根节点上挂五个无效监听器，也与其它页面的写法不一致。
defineEmits<{
  (e: 'navigate', page: string): void
  (e: 'navigate-ask', q: string): void
  (e: 'question-consumed'): void
  (e: 'account-updated', partial: { display_name?: string; avatar?: string }): void
  (e: 'database-switched'): void
}>()

import { ref, computed, onMounted, watch, nextTick, onBeforeUnmount } from 'vue'



import echarts, { type EChartsType } from '../echarts'



import { isEditor, isAdmin, isLoggedIn, getUser } from '../auth'







// ========== 类型定义 ==========



interface Column {



  name: string



  type: string



  nullable: boolean



  comment: string



  primary_key: boolean



  translation?: string   // 字段说明（AI 生成，后端 explain_field_detailed 返回）



}







interface TableDetail {



  table_name: string



  chinese_name?: string



  columns: Column[]



  sample_data: Record<string, any>[]



  row_count: number



  total_count?: number



  page?: number



  page_size?: number



}







// ========== 状态 ==========



const loading = ref(false)







// 数据库切换权限：仅管理员可切换（后端 activate 挂 require_roles("admin")，非 admin 会 403）



const canSwitchDb = computed(() => isAdmin())







// ── 员工数据授权（per-user 表/字段白名单）：按权限过滤可见表/字段 ──



// 仅对「已登录且非管理员」的员工生效；管理员与匿名访客（开放模式）显示全部。



const authMode = computed(() => isLoggedIn() && !isAdmin())



const authCatalog = ref<any>(null)            // /data-catalog 返回



const authCatalogError = ref(false)           // 授权目录加载失败（避免永久加载死屏）



const catalogColMap = computed(() => {



  const m = new Map<string, Set<string>>()



  const maskMap = new Map<string, string>()



  for (const t of (authCatalog.value?.tables || []) as any[]) {



    const tn = String(t.table_name).toLowerCase()



    m.set(tn, new Set((t.columns || []).map((c: any) => c.name.toLowerCase())))



    for (const c of (t.columns || [])) if (c.masked) maskMap.set(`${tn}.${c.name.toLowerCase()}`, c.mask)



  }



  return { cols: m, masks: maskMap }



})



// 当前选中表在授权目录中的可见字段（已过滤无权字段）



const visibleColumns = computed(() => {



  const t = currentTableData.value



  if (!t) return []



  const cols = t.columns || []



  if (!authMode.value || !authCatalog.value) return cols



  const entry = (authCatalog.value.tables || []).find((x: any) =>



    String(x.table_name).toLowerCase() === String(t.table_name).toLowerCase())



  if (!entry) return []



  const allowed = new Set((entry.columns || []).map((c: any) => c.name.toLowerCase()))



  return cols.filter((c: any) => allowed.has(c.name.toLowerCase()))



})



const colMaskOf = (table: string, col: string) =>



  catalogColMap.value.masks.get(`${String(table).toLowerCase()}.${col.toLowerCase()}`) || ''







// ── 反馈 / 申请授权（员工 → 管理员）──



const requestModalOpen = ref(false)



const requestSaving = ref(false)



const requestSaved = ref(false)         // 提交成功后的短暂成功态



const requestSchema = ref<any[]>([])    // 可申请的库表清单（available-schema）



const requestSelected = ref<Record<string, string[]>>({})  // { 表: [字段] }，空数组=整表



const requestReason = ref('')



const requestExpand = ref<Set<string>>(new Set())  // 已展开字段列表的表







const loadCatalog = async () => {



  // 仅对已登录非管理员生效；拉取个人授权目录，驱动可见表/字段裁剪



  if (!authMode.value) return



  authCatalogError.value = false



  try {



    const res = await fetch('/api/permission/data-catalog')



    if (!res.ok) throw new Error(`HTTP ${res.status}`)



    authCatalog.value = await res.json()



    const cat = authCatalog.value



    // 授权为空时不默认选中任何表；否则把 activeTab 校正到授权表集合内



    // （loadTables 可能先把 activeTab 设为第一张表，若不校正会出现右侧指向无权表的竞态）



    if (cat?.empty) {



      activeTab.value = ''



    } else if (activeTab.value) {



      const names = new Set((cat?.tables || []).map((x: any) => String(x.table_name).toLowerCase()))



      if (!names.has(String(activeTab.value).toLowerCase())) {



        activeTab.value = (cat?.tables || [])[0]?.table_name || ''



      }



    }



  } catch (e) {



    console.error('加载数据授权目录失败', e)



    authCatalogError.value = true



  }



}







// ── 申请弹窗交互 ──



const openRequestModal = async () => {



  requestReason.value = ''



  requestSelected.value = {}



  requestSaved.value = false



  requestExpand.value = new Set()



  requestModalOpen.value = true



  try {



    const res = await fetch('/api/permission/available-schema')



    if (res.ok) {



      const data = await res.json()



      requestSchema.value = data.tables || []



    } else {



      requestSchema.value = []



    }



  } catch (e) {



    console.error('加载可申请库表失败', e)



    requestSchema.value = []



  }



}







const closeRequestModal = () => {



  if (requestSaving.value) return



  requestModalOpen.value = false



}







const reqTableChecked = (t: string) => t in requestSelected.value



const reqIsWholeTable = (t: string) => {



  const v = requestSelected.value[t]



  return v !== undefined && v.length === 0   // 整表（全部字段）



}



const reqColDisabled = (t: string) => {



  const v = requestSelected.value[t]



  return v !== undefined && v.length === 0  // 整表模式下列勾选禁用



}



const reqColChecked = (t: string, c: string) => {



  const v = requestSelected.value[t]



  if (v === undefined) return false



  if (v.length === 0) return true            // 整表：全部字段视为已选



  return v.includes(c)



}



const toggleReqTable = (t: string) => {



  if (reqTableChecked(t)) delete requestSelected.value[t]



  else requestSelected.value[t] = []         // 整表（全部字段）



}



const toggleReqExpand = (t: string) => {



  const s = new Set(requestExpand.value)



  if (s.has(t)) s.delete(t); else s.add(t)



  requestExpand.value = s



}



const toggleReqCol = (t: string, c: string) => {



  const cur = requestSelected.value[t]



  if (cur === undefined) { requestSelected.value[t] = [c]; return }



  if (cur.length === 0) return               // 整表模式不允许单独操作列



  if (cur.includes(c)) {



    const next = cur.filter((x) => x !== c)



    if (next.length === 0) delete requestSelected.value[t]



    else requestSelected.value[t] = next



  } else {



    requestSelected.value[t] = [...cur, c]



  }



}



const reqSelectedCount = computed(() => Object.keys(requestSelected.value).length)







const submitRequest = async () => {



  if (requestSaving.value) return



  const tables = Object.keys(requestSelected.value)



  const reason = requestReason.value.trim()



  if (!tables.length && !reason) {



    alert('请至少选择需要申请的数据表，或填写反馈说明')



    return



  }



  requestSaving.value = true



  const me = getUser()



  if (!me || !me.username) {



    requestSaving.value = false



    alert('未获取到登录信息，请重新登录后再提交申请')



    return



  }



  const grants: Record<string, string[]> = {}



  for (const [t, cols] of Object.entries(requestSelected.value)) {



    grants[t] = cols   // 空数组 = 整表全部字段



  }



  const payload = { action: 'set_user_grants', username: me.username, grants }



  try {



    const res = await fetch('/api/permission/requests', {



      method: 'POST',



      headers: { 'Content-Type': 'application/json' },



      body: JSON.stringify({ kind: 'data_access', payload, reason }),



    })



    if (!res.ok) {



      const err = await res.json().catch(() => ({}))



      throw new Error(err.detail || '提交失败')



    }



    requestSaved.value = true



    setTimeout(() => {



      requestModalOpen.value = false



      requestSaved.value = false



      requestSelected.value = {}



    }, 1500)



  } catch (e: any) {



    alert('提交失败：' + (e?.message || e))



  } finally {



    requestSaving.value = false



  }



}







const allTables = ref<TableDetail[]>([])



const activeTab = ref('')



const activeTabView = ref<'fields' | 'sample' | 'relationships'>('fields')







// ========== 数据分页 ==========




// ===== 全部数据：按时间筛选（近 N 天 / 自定义区间）=====
const timeColOptions = ref<{ name: string; label: string }[]>([])
const timeFilterCol = ref('')
const timeFilterStart = ref('')
const timeFilterEnd = ref('')
const quickDays = ref(0)
const timeFilterActive = computed(() =>
  !!(timeFilterCol.value && (timeFilterStart.value || timeFilterEnd.value)))

const _ymd = (offsetDays: number) => {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  d.setDate(d.getDate() + offsetDays)
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${mm}-${dd}`
}

// 当前表时间列识别（date/time/datetime/timestamp）
const syncTimeCols = () => {
  const tbl = currentTableData.value
  const cols = (tbl && tbl.columns) || []
  const list = cols
    .filter((c) => /date|time/i.test(String(c.type || '')))
    .map((c) => ({
      name: c.name,
      label: `${fieldHeaderText(tbl!.table_name, c)}（${c.name}）`,
    }))
  timeColOptions.value = list
  if (!list.some((c) => c.name === timeFilterCol.value)) {
    timeFilterCol.value = list[0]?.name || ''
    timeFilterStart.value = ''
    timeFilterEnd.value = ''
    quickDays.value = 0
  }
}

// 筛选 / 排序变化统一重载（回到第 1 页）
const reloadWithFilter = () => {
  if (currentPage.value === 1) {
    if (activeTab.value) loadTableDetail(activeTab.value, 1)
  } else {
    currentPage.value = 1
  }
}

// ===== 全部数据：列排序（点击表头切换 升序 → 降序 → 取消）=====
const sortColumn = ref('')
const sortDir = ref<'asc' | 'desc'>('asc')
const toggleSort = (col: string) => {
  if (sortColumn.value === col) {
    if (sortDir.value === 'asc') {
      sortDir.value = 'desc'
    } else {
      sortColumn.value = ''
      sortDir.value = 'asc'
    }
  } else {
    sortColumn.value = col
    sortDir.value = 'asc'
  }
  reloadWithFilter()
}

// ===== AI 字段翻译（yans 库除外；翻译写回列备注）=====
const translating = ref(false)
const canAiTranslate = computed(
  () => isEditor() && !String(activeDatabase.value || '').toLowerCase().includes('yans'))
const translateTableFields = async () => {
  const tbl = activeTab.value
  if (!tbl || translating.value) return
  if (!window.confirm('将用 AI 把本表英文字段翻译成中文并写入数据库备注（yans 库除外；已有中文备注的字段不会被覆盖）。确认执行？')) return
  translating.value = true
  try {
    const res = await fetch(`/api/tables/${encodeURIComponent(tbl)}/ai-translate-fields`, { method: 'POST' })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`)
    if (data.yans) { window.alert(data.message); return }
    const demo = (data.translated || []).slice(0, 5).map((t: any) => `${t.name} → ${t.cn}`).join('、')
    window.alert((data.message || '') + (demo ? `\n示例：${demo}` : ''))
    if (data.translated?.length) {
      loadTableDetail(tbl, currentPage.value)   // 刷新备注 → 表头立即显示中文
    }
  } catch (e: any) {
    window.alert('翻译失败：' + (e?.message || e))
  } finally {
    translating.value = false
  }
}

// 快捷「近 N 天」：区间右端=今天，左端=今天-N+1
const setQuickRange = (days: number) => {
  quickDays.value = days
  timeFilterStart.value = _ymd(-(days - 1))
  timeFilterEnd.value = _ymd(0)
  reloadWithFilter()
}
const clearTimeFilter = () => {
  timeFilterStart.value = ''
  timeFilterEnd.value = ''
  quickDays.value = 0
  reloadWithFilter()
}
const onTimeFilterChange = () => {
  quickDays.value = 0  // 手动改起止 → 快捷高亮失效
  reloadWithFilter()
}

const PAGE_SIZE = 100



const currentPage = ref(1)



const tableDataLoading = ref(false)



let detailRequestSeq = 0  // 请求序号，用于防竞态







const totalPages = computed(() => {



  const total = currentTableData.value?.total_count ?? currentTableData.value?.sample_data?.length ?? 0



  return Math.max(1, Math.ceil(total / PAGE_SIZE))



})







// ========== 备注编辑相关 ==========



const commentToast = ref('')                                   // 保存成功提示



let commentToastTimer: number | null = null



const commentDetail = ref<{ name: string; text: string } | null>(null)   // 查看完整备注弹框



const commentEditing = ref(false)                              // 弹框内是否处于编辑



const commentEditValue = ref('')                               // 弹框编辑输入值







// ========== 样例数据编辑相关 ==========



const editingSample = ref({



  rowIdx: -1,



  cellKey: '',



  value: '',



  loading: false



})



const sampleInputRef = ref<HTMLInputElement | null>(null)







// ========== 搜索相关 ==========



const filterKeyword = ref('')



const tableFilterKeyword = ref('')







// ========== 表关系相关 ==========



const relationships = ref<any[]>([])



const relationshipsLoading = ref(false)



const businessSceneMap = ref<Record<string, string>>({})







const loadBusinessScenes = async () => {



  try {



    const res = await fetch('/api/knowledge/scenes')



    if (!res.ok) return



    const data = await res.json()



    const map: Record<string, string> = {}



    Object.values(data.scenes || {}).forEach((scene: any) => {



      const sceneName = scene.name || scene.key || '其他'



      ;(scene.objects || []).forEach((object: any) => {



        if (object.table && !map[object.table]) map[object.table] = sceneName



      })



    })



    businessSceneMap.value = map



  } catch (error) {



    console.error('加载业务场景失败:', error)



  }



}







// ========== ER 图（表卡片式：每表=卡片含字段列表，关系线连卡片，分层布局避免交叉） ==========



// 注意：ER 图只在全屏（全局查看）中渲染，主视图不内嵌（小容器放不下 20 张卡片）



// 主键集合（表名 -> 主键列名集合），用于判断关系基数



const pkMap = computed(() => {



  const m = new Map<string, Set<string>>()



  allTables.value.forEach((t) => {



    m.set(t.table_name, new Set(t.columns.filter((c) => c.primary_key).map((c) => c.name)))



  })



  return m



})







// 外键列集合（表名 -> 出现在关系中的列），用于字段标注



const fkMap = computed(() => {



  const m = new Map<string, Set<string>>()



  ;(relationships.value || []).forEach((r) => {



    if (!m.has(r.source_table)) m.set(r.source_table, new Set())



    if (!m.has(r.target_table)) m.set(r.target_table, new Set())



    m.get(r.source_table)!.add(r.source_column)



    m.get(r.target_table)!.add(r.target_column)



  })



  return m



})







// ── ER 图布局记忆（用户拖动卡片后，下次打开恢复位置，按库隔离）──



// v3：旧版布局（环形/网格早版本）的坐标会导致卡片严重重叠，作废后重新生成



const erLayoutKey = () => `er_layout_v3_${activeDatabase.value || 'default'}`







const saveErLayout = () => {



  if (!erFullChart) return



  try {



    // GraphView 拖动时更新的是 seriesModel.getData() 的 itemLayout；



    // 实测确认 getItemLayout 返回的就是「数据坐标」（不受 zoom 影响），直接保存即可



    const model = (erFullChart as any).getModel()



    const series = model.getSeriesByIndex(0)



    const data = series?.getData?.()



    if (!data) return



    const positions: Record<string, { x: number; y: number }> = {}



    data.each((idx: number) => {



      const name = data.getName(idx)



      const layout = data.getItemLayout(idx)



      if (name && layout && layout.length >= 2 && Number.isFinite(layout[0]) && Number.isFinite(layout[1])) {



        positions[name] = { x: layout[0], y: layout[1] }



      }



    })



    window.localStorage.setItem(erLayoutKey(), JSON.stringify(positions))



  } catch (e) { /* ignore */ }



}







const clearErLayout = () => {



  try { window.localStorage.removeItem(erLayoutKey()) } catch (e) { /* ignore */ }



  scheduleErRender()



}



// 碰撞消解：遍历所有卡片，把互相重叠的矩形沿「重叠更小的轴」推开，



// 迭代直到不再重叠。网格布局本身不重叠（空转一次），但「记忆布局」可能保存了旧的重叠坐标，



// 此函数作为最终兜底，保证任何情况下卡片都不重叠。



const resolveErOverlaps = (nodes: any[], gap = 1000) => {



  for (let iter = 0; iter < 180; iter++) {



    let moved = false



    for (let i = 0; i < nodes.length; i++) {



      for (let j = i + 1; j < nodes.length; j++) {



        const a = nodes[i]



        const b = nodes[j]



        const aw = a.symbolSize?.[0] ?? 0



        const ah = a.symbolSize?.[1] ?? 0



        const bw = b.symbolSize?.[0] ?? 0



        const bh = b.symbolSize?.[1] ?? 0



        const ox = (aw + bw) / 2 + gap - Math.abs(a.x - b.x)



        const oy = (ah + bh) / 2 + gap - Math.abs(a.y - b.y)



        if (ox <= 0 || oy <= 0) continue



        moved = true



        const dx = b.x - a.x



        const dy = b.y - a.y



        if (ox <= oy) {



          const sign = dx >= 0 ? 1 : -1



          const push = ox / 2 + 4



          a.x -= sign * push



          b.x += sign * push



        } else {



          const sign = dy >= 0 ? 1 : -1



          const push = oy / 2 + 4



          a.y -= sign * push



          b.y += sign * push



        }



      }



    }



    if (!moved) break



  }



}







// ⭐ 以下按《数据介绍.md》维护“制造业生产质量”数据集的语义字典：

// 仅当库中确实存在这些表时生效；其它数据库无此表则自动回退到通用逻辑，互不影响。

const DOC_TABLE_CN: Record<string, string> = {

  dim_product: '产品主数据',

  dim_process: '工序主数据',

  dim_production_line: '产线主数据',

  dim_equipment: '设备主数据',

  mes_work_order: '生产工单',

  mes_process_output: '工序产量',

  qms_inspection: '质量检验',

  qms_defect_detail: '不良明细',

  eqp_downtime_record: '设备停机记录',

  inv_inventory_snapshot: '库存快照',

}



const DOC_FIELD_CN: Record<string, Record<string, string>> = {

  dim_product: {

    product_id: '产品ID，主键', product_code: '产品编码', product_name: '产品名称',

    product_model: '产品型号', product_category: '产品类别', unit: '单位', is_active: '是否启用',

  },

  dim_process: {

    process_id: '工序ID，主键', process_code: '工序编码', process_name: '工序名称',

    process_seq: '工序顺序', standard_yield_rate: '标准良率', is_key_process: '是否关键工序',

  },

  dim_production_line: {

    line_id: '产线ID，主键', line_code: '产线编码', line_name: '产线名称',

    workshop_name: '车间名称', line_manager: '产线负责人', line_status: '产线状态',

  },

  dim_equipment: {

    equipment_id: '设备ID，主键', equipment_code: '设备编码', equipment_name: '设备名称',

    equipment_type: '设备类型', line_id: '所属产线ID', install_date: '安装日期', equipment_status: '设备状态',

  },

  mes_work_order: {

    work_order_id: '工单ID，主键', work_order_no: '工单编号', product_id: '产品ID',

    line_id: '产线ID', plan_qty: '计划数量', start_date: '计划开始日期', end_date: '计划结束日期', order_status: '工单状态',

  },

  mes_process_output: {

    output_id: '产量记录ID，主键', work_order_id: '工单ID', product_id: '产品ID',

    process_id: '工序ID', line_id: '产线ID', stat_date: '统计日期', input_qty: '投入数量',

    good_qty: '合格数量', defect_qty: '不良数量', rework_qty: '返工数量', shift_code: '班次(D白/N夜)',

  },

  qms_inspection: {

    inspection_id: '检验记录ID，主键', inspection_no: '检验单号', work_order_id: '工单ID',

    product_id: '产品ID', process_id: '工序ID', inspection_date: '检验日期',

    sample_qty: '抽检数量', defect_qty: '不良数量', inspection_result: '检验结果(pass合格/fail不合格)',

  },

  qms_defect_detail: {

    defect_id: '不良明细ID，主键', inspection_id: '检验记录ID', defect_type: '不良类型',

    defect_code: '不良代码', defect_qty: '不良数量',

    severity_level: '严重等级(minor轻微/major主要/critical严重)', responsible_process_id: '责任工序ID',

  },

  eqp_downtime_record: {

    downtime_id: '停机记录ID，主键', equipment_id: '设备ID', line_id: '产线ID',

    start_time: '停机开始时间', end_time: '停机结束时间', downtime_minutes: '停机分钟',

    downtime_reason: '停机原因', is_planned: '是否计划停机',

  },

  inv_inventory_snapshot: {

    snapshot_id: '库存快照ID，主键', snapshot_date: '快照日期', product_id: '产品ID',

    warehouse_code: '仓库编码', available_qty: '可用库存', frozen_qty: '冻结库存', safety_stock_qty: '安全库存',

  },

}



// 表间关系（来源表.字段 → 目标表.字段），label=菱形内动词，desc=文档“说明”（悬停用）

const DOC_RELS: { st: string; sc: string; tt: string; tc: string; label: string; desc: string }[] = [

  { st: 'dim_equipment', sc: 'line_id', tt: 'dim_production_line', tc: 'line_id', label: '属于', desc: '设备所属产线' },

  { st: 'mes_work_order', sc: 'product_id', tt: 'dim_product', tc: 'product_id', label: '对应', desc: '工单对应产品' },

  { st: 'mes_work_order', sc: 'line_id', tt: 'dim_production_line', tc: 'line_id', label: '属于', desc: '工单对应产线' },

  { st: 'mes_process_output', sc: 'work_order_id', tt: 'mes_work_order', tc: 'work_order_id', label: '对应', desc: '工序产量对应工单' },

  { st: 'mes_process_output', sc: 'product_id', tt: 'dim_product', tc: 'product_id', label: '对应', desc: '工序产量对应产品' },

  { st: 'mes_process_output', sc: 'process_id', tt: 'dim_process', tc: 'process_id', label: '对应', desc: '工序产量对应工序' },

  { st: 'mes_process_output', sc: 'line_id', tt: 'dim_production_line', tc: 'line_id', label: '属于', desc: '工序产量对应产线' },

  { st: 'qms_inspection', sc: 'work_order_id', tt: 'mes_work_order', tc: 'work_order_id', label: '对应', desc: '检验记录对应工单' },

  { st: 'qms_inspection', sc: 'product_id', tt: 'dim_product', tc: 'product_id', label: '对应', desc: '检验记录对应产品' },

  { st: 'qms_inspection', sc: 'process_id', tt: 'dim_process', tc: 'process_id', label: '对应', desc: '检验记录对应工序' },

  { st: 'qms_defect_detail', sc: 'inspection_id', tt: 'qms_inspection', tc: 'inspection_id', label: '对应', desc: '不良明细对应检验单' },

  { st: 'qms_defect_detail', sc: 'responsible_process_id', tt: 'dim_process', tc: 'process_id', label: '对应', desc: '不良责任工序' },

  { st: 'eqp_downtime_record', sc: 'equipment_id', tt: 'dim_equipment', tc: 'equipment_id', label: '记录', desc: '停机记录对应设备' },

  { st: 'eqp_downtime_record', sc: 'line_id', tt: 'dim_production_line', tc: 'line_id', label: '属于', desc: '停机记录对应产线' },

  { st: 'inv_inventory_snapshot', sc: 'product_id', tt: 'dim_product', tc: 'product_id', label: '对应', desc: '库存快照对应产品' },

]



// 未收录在文档中的关系：按基数给出通用动词兜底（避免出现“关联/业务”或字段名）

const relVerbFallback = (r: any, card: string): string => {

  if (card === '1:N') return '包含'

  if (card === 'N:1') return '属于'

  if (card === '1:1') return '引用'

  return r.type === 'foreign_key' ? '引用' : '对应'

}



// 关系的“说明”：优先取后端 /relationships 返回的中文说明（非“外键：/业务关系：/推导关系：”默认前缀者）；

// 后端没有时再查本地文档字典；再没有且两端表能猜出中文名则拼“中文名对应中文名”；否则回退“外键/业务关联”

const relDocDesc = (rel: any): string => {

  if (!rel) return ''

  const backend = (rel.description || '') as string

  if (backend && !backend.startsWith('外键：') && !backend.startsWith('业务关系：')

    && !backend.startsWith('推导关系：') && !backend.toUpperCase().startsWith('FK')) {

    return backend

  }

  const k = `${rel.source_table}.${rel.source_column}|${rel.target_table}.${rel.target_column}`

  const hit = DOC_RELS.find((d) => `${d.st}.${d.sc}|${d.tt}.${d.tc}` === k)

  if (hit) return hit.desc

  const sCn = guessTableCn(rel.source_table)

  const tCn = guessTableCn(rel.target_table)

  if (sCn && tCn) return `${sCn}对应${tCn}`

  return rel.type === 'foreign_key' ? '外键' : '业务关联'

}



// ⭐ 制造业“概念推断”兜底：表名/字段名与文档不完全一致时，按关键词猜测中文含义，

// 这样切到“类似叫法”的制造库（不同库表名略不同）也能有接近的效果。

const _tname = (raw: string) => String(raw || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '')



const _TABLE_CN_PATTERNS: Array<[RegExp, string]> = [

  [/work_?order/, '生产工单'],

  [/process_?output/, '工序产量'],

  [/production_?line/, '产线'],

  [/inventory_?snapshot/, '库存快照'],

  [/downtime|stop_record/, '设备停机记录'],

  [/defect/, '不良明细'],

  [/inspection|quality_check|qc_record/, '质量检验'],

  [/equipment|machine|device/, '设备'],

  [/product/, '产品'],

  [/process/, '工序'],

  [/workshop/, '车间'],

  [/order/, '工单'],

  [/inventory|stock/, '库存'],

  [/material/, '物料'],

  [/line/, '产线'],

]



const guessTableCn = (raw: string): string => {

  const s = _tname(raw)

  if (!s) return ''

  for (const [re, cn] of _TABLE_CN_PATTERNS) if (re.test(s)) return cn

  return ''

}



const _FIELD_PARTS: Record<string, string> = {

  id: 'ID', no: '编号', code: '编码', name: '名称', model: '型号', category: '类别', type: '类型',

  qty: '数量', count: '数量', num: '数量', date: '日期', time: '时间', start: '开始', end: '结束',

  plan: '计划', good: '合格', defect: '不良', bad: '不良', input: '投入', rework: '返工', sample: '抽检',

  available: '可用', frozen: '冻结', safety: '安全', stock: '库存', reason: '原因', shift: '班次',

  unit: '单位', workshop: '车间', line: '产线', seq: '顺序', yield: '良率', standard: '标准',

  rate: '率', status: '状态', state: '状态', key: '关键', equipment: '设备', product: '产品',

  process: '工序', work: '工单', order: '工单', minute: '分钟', minutes: '分钟', is: '是否',

  planned: '计划', inspection: '检验', result: '结果', severity: '严重', level: '等级',

  responsible: '责任', warehouse: '仓库', snapshot: '快照', downtime: '停机', normal: '正常',

  material: '物料', lot: '批次', batch: '批次', version: '版本', remark: '备注', memo: '备注',

  desc: '描述', description: '描述', address: '地址', phone: '电话', email: '邮箱', owner: '负责人',

}



const guessFieldCn = (raw: string): string => {

  const s = String(raw || '').trim().toLowerCase()

  if (!s) return ''

  const tokens = s.split(/_+/).filter(Boolean)

  if (!tokens.length) return ''

  const mapped = tokens.map((t) => _FIELD_PARTS[t])

  if (mapped.some((m) => !m)) return '' // 出现不认识的关键词就不猜，避免半中半英

  return mapped.join('')

}



// 表卡片式 ER 图：每张表 = 一张卡片（表名头 + 字段列表，🔑主键/🔗外键），



// 关系线从"外键字段"所在卡片连到目标卡片，线端标基数。



// 布局 = 按外键依赖分层（父表在左、子表在右），层内垂直排布 —— 线短、交叉少、可读性强。



const buildCardEr = (maxFields: number) => {



  // 只保留有效的关系



  // 关系由下方“后端关系 + 《数据介绍.md》文档关系”合并生成



  



  // 只展示用户勾选过的表



  let tables = allTables.value



  if (erSelectedTables.value.length) {



    const sel = new Set(erSelectedTables.value)



    tables = tables.filter((t) => sel.has(t.table_name))



  }



  const pk = pkMap.value

  const selectedSet = erSelectedTables.value.length

    ? new Set(erSelectedTables.value)

    : new Set(tables.map((t) => t.table_name))



  // 表间关系：后端已识别 + 《数据介绍.md》文档关系（两端表都在展示范围内才纳入）

  const relMap = new Map<string, any>()

  ;(relationships.value || []).filter((r) => r.type !== 'inferred').forEach((r) => {

    const k = `${r.source_table}.${r.source_column}|${r.target_table}.${r.target_column}`

    relMap.set(k, { ...r })

  })

  DOC_RELS.forEach((dr) => {

    if (!selectedSet.has(dr.st) || !selectedSet.has(dr.tt)) return

    const k = `${dr.st}.${dr.sc}|${dr.tt}.${dr.tc}`

    const ex = relMap.get(k)

    if (ex) {

      ex._verb = dr.label

    } else {

      const isFk = !!pk.get(dr.tt)?.has(dr.tc)

      relMap.set(k, {

        source_table: dr.st, source_column: dr.sc,

        target_table: dr.tt, target_column: dr.tc,

        type: isFk ? 'foreign_key' : 'business',

        _verb: dr.label,

      })

    }

  })

  const rels = Array.from(relMap.values())



  // ── 1. 创建实体节点（矩形：代表表）──

  const nodes: any[] = []

  const nodeById = new Map<string, any>()



  tables.forEach((t) => {

    const name = t.table_name

    const cn = (t as any).chinese_name || DOC_TABLE_CN[name] || guessTableCn(name) || ''



    // ⭐ 自动换行函数：每行最多 10 个字符，超出就加 \n 换行

    const wrapText = (str: string, maxLen = 10) => {

      if (!str) return ''

      const chars = str.split('')

      const lines = []

      let currentLine = ''

      for (let i = 0; i < chars.length; i++) {

        currentLine += chars[i]

        if ((i + 1) % maxLen === 0) {

          lines.push(currentLine)

          currentLine = ''

        }

      }

      if (currentLine) lines.push(currentLine)

      return lines.join('\n')

    }



    const wrappedName = wrapText(name, 10)  // 强制英文表名每行10个字符

    const wrappedCn = wrapText(cn, 10)      // 强制中文名每行10个字符



    const node = {

      id: name,

      name,

      _entity: name,

      _rowCount: t.row_count,

      symbol: 'rect',

      symbolSize: [120, 120],

      itemStyle: { color: '#e8f2ff', borderColor: '#1d4ed8', borderWidth: 2 },

      label: {

        show: true,

        position: 'inside',

        // ⭐ 直接使用预先处理好的 \n 换行字符串

        formatter: cn ? `{name|${wrappedName}}\n{cn|${wrappedCn}}` : `{name|${wrappedName}}`,

        rich: {

          name: {

            color: '#1e3a8a',

            fontSize: 10,

            fontWeight: 'bold',

            fontFamily: 'Microsoft YaHei',

            align: 'center',

            verticalAlign: 'middle',

            lineHeight: 14

          },

          cn: {

            color: '#475569',

            fontSize: 9,

            fontFamily: 'Microsoft YaHei',

            align: 'center',

            verticalAlign: 'middle',

            lineHeight: 14

          }

        }

      },

      x: 0, y: 0,

    }

    nodes.push(node)

    nodeById.set(name, node)

  })



    // ── 2. 创建属性节点（椭圆：代表字段）──

  const attributeNodes: any[] = []

  const entityColsMap: Record<string, any[]> = {}

  

  tables.forEach((t) => {

    const cols = t.columns.slice(0, maxFields)

    entityColsMap[t.table_name] = cols

    cols.forEach((col) => {

      // ⭐ 只显示字段名，不显示类型，避免矩形太长；最长超10字符自动截断加省略号

      const displayName = col.name.length > 10 ? col.name.slice(0, 10) + '…' : col.name

      attributeNodes.push({

        id: `attribute:${t.table_name}:${col.name}`,

        name: displayName,

        _fieldName: col.name,          // ⭐ 完整字段名（供点击弹框展示，椭圆内被截断的用不上）

        _entity: t.table_name,

        _parentEntity: t.table_name,

        _fieldType: col.type,

        _comment: DOC_FIELD_CN[t.table_name]?.[col.name] || guessFieldCn(col.name) || displayComment(col),

        _sample: t.sample_data && t.sample_data[0] ? t.sample_data[0][col.name] : undefined,

        symbol: 'circle',

        symbolSize: [140, 45],  // ⭐ 缩小宽度，更圆润居中

        itemStyle: { color: '#fff8d9', borderColor: '#d97706', borderWidth: 1.5 },

        label: { 

          show: true, 

          position: 'inside',       // ⭐ 强制居中

          formatter: displayName,   // ⭐ 显示截断后的字段名

          color: '#92400e', 

          fontSize: 11, 

          fontFamily: 'Microsoft YaHei',

          align: 'center',          // ⭐ 水平居中

          verticalAlign: 'middle',  // ⭐ 垂直居中

          overflow: 'truncate',     // ⭐ 超出部分截断

          width: 100

        },

        x: 0, y: 0,

      })

    })

  })





  // ── 3. 创建联系节点（菱形：代表关系）──



  const relationNodes: any[] = []



  const linkSegments: any[] = []







  // 实体 -> 属性（使用极淡的线）



  tables.forEach((t) => {



    const cols = t.columns.slice(0, maxFields)



    cols.forEach((col) => {



      linkSegments.push({



        source: t.table_name,



        target: `attribute:${t.table_name}:${col.name}`,



        lineStyle: { color: 'gray',



         width: 1, type: 'solid', 



         opacity: 0.6 },



        silent: true



      })



    })



  })







  // 实体 -> 菱形 -> 实体（每条关系都保留，不去重）



  rels.forEach((r, relationIndex) => {



    const srcNode = nodeById.get(r.source_table)



    const tgtNode = nodeById.get(r.target_table)



    if (!srcNode || !tgtNode) return







    const srcPk = pk.get(r.source_table)?.has(r.source_column)



    const tgtPk = pk.get(r.target_table)?.has(r.target_column)



    let card = 'N:N'



    if (srcPk && tgtPk) card = '1:1'



    else if (srcPk) card = '1:N'



    else if (tgtPk) card = 'N:1'



    const relColor = card === '1:N' ? '#2563eb' : card === 'N:1' ? '#16a34a' : '#64748b'







    const relationId = `relation:${r.source_table}:${r.source_column}:${r.target_table}:${r.target_column}:${relationIndex}`



    



    relationNodes.push({



      id: relationId,



      name: (r as any)._verb || relVerbFallback(r, card), // ⭐ 按《数据介绍.md》显示关系动词（对应/属于/记录…），未收录关系用通用兜底



      _rel: r,



      _cardinality: card,



      title: `${r.source_table}.${r.source_column} → ${r.target_table}.${r.target_column}\n基数: ${card}`,



      symbol: 'diamond',



      symbolSize: [80, 55],



      itemStyle: { color: '#e7f8ec', borderColor: '#16a34a', borderWidth: 2 },



      label: { show: true, color: '#166534', fontSize: 11, fontWeight: 'bold', fontFamily: 'Microsoft YaHei' },



      x: 0, y: 0,



    })







   



    linkSegments.push({



      source: r.source_table,



      target: relationId,



      label: { 



        show: true, 



        formatter: card === '1:N' || card === '1:1' ? '1' : 'N',



        fontSize: 12, 



        color: relColor, 



        fontWeight: 'bold',



        position: 'end',



        distance: 55



      },



      lineStyle: { color: relColor, width: 2, type: r.type === 'foreign_key' ? 'solid' : 'dashed' }



    })



    linkSegments.push({



      source: relationId,



      target: r.target_table,



      label: { 



        show: true, 



        formatter: card === '1:N' ? 'N' : '1',



        fontSize: 12, 



        color: relColor, 



        fontWeight: 'bold',



        position: 'start',



        distance: 55



      },



      lineStyle: { color: relColor, width: 2, type: r.type === 'foreign_key' ? 'solid' : 'dashed' }



    })



  })





  // ── 4. 实体散落布局 ──

const n = tables.length

const centerX = 4000

const centerY = 2000

const radius = Math.max(1800, n * 450)  // 根据表数量决定圆环半径



tables.forEach((t, i) => {

  const nd = nodeById.get(t.table_name)

  if (!nd) return

  // 将表均匀分布在圆环上（这才是导致重叠的真正原因！）

  const angle = (2 * Math.PI * i) / Math.max(1, n) - Math.PI / 2

  nd.x = centerX + radius * Math.cos(angle)

  nd.y = centerY + radius * Math.sin(angle)

})





  // ── 5. 属性“花瓣式”围绕实体散开，并强制拉大距离 ──



  attributeNodes.forEach((attr) => {



    const entity = attr._parentEntity



    const entityNode = nodeById.get(entity)



    if (!entityNode) return



    const cols = entityColsMap[entity] || []



    const colIdx = cols.findIndex(c => c.name === attr.name)







    const count = Math.max(1, cols.length)



    const attrAngle = (2 * Math.PI * colIdx) / count - Math.PI / 2



    const attrRadius = 850



    attr.x = entityNode.x + attrRadius * Math.cos(attrAngle)



    attr.y = entityNode.y + attrRadius * Math.sin(attrAngle)



  })







  // ── 6. 关系（菱形）放置在两个实体的连线正中间 ──



  relationNodes.forEach((rel) => {



    const src = rel._rel.source_table



    const tgt = rel._rel.target_table



    const srcNode = nodeById.get(src)



    const tgtNode = nodeById.get(tgt)



    if (!srcNode || !tgtNode) return



    



    // 放置在正中间，且稍微往下偏一点，不会挡住属性



    rel.x = (srcNode.x + tgtNode.x) / 2



    rel.y = (srcNode.y + tgtNode.y) / 2 + 40



  })







  // ── 7. 计算尺寸并整体平移 ──



  const allNodes = [...nodes, ...attributeNodes, ...relationNodes]



  resolveErOverlaps(allNodes, 120)







  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity



  allNodes.forEach((nd) => {



    minX = Math.min(minX, nd.x - nd.symbolSize[0] / 2)



    minY = Math.min(minY, nd.y - nd.symbolSize[1] / 2)



    maxX = Math.max(maxX, nd.x + nd.symbolSize[0] / 2)



    maxY = Math.max(maxY, nd.y + nd.symbolSize[1] / 2)



  })



  const pad = 400



  const totalW = maxX - minX + pad * 2



  const totalH = maxY - minY + pad * 2







  allNodes.forEach((nd) => {



    nd.x += pad - minX



    nd.y += pad - minY



  })







  return { nodes: allNodes, links: linkSegments, totalW, totalH }



}



const buildErOption = (maxFields: number, zoom: number) => {



  const { nodes, links } = buildCardEr(maxFields)



  return {



    // tooltip: {



    //   formatter: (p: any) => {



    //     if (p.dataType === 'node') {



    //       const n = p.data



    //       // 关系节点（菱形）：展示关系字段映射与基数



    //       if (n._rel) {



    //         const type = n._rel.type === 'foreign_key' ? '外键约束' : '业务关联'



    //         return `<b>${n._rel.source_table}.${n._rel.source_column}</b> → <b>${n._rel.target_table}.${n._rel.target_column}</b><br/>基数：<b>${n._cardinality}</b> · 类型：${type}`



    //       }



    //       // 属性节点（字段）：展示字段名、类型、说明、样例值



    //       if (n._fieldType !== undefined) {



    //         let html = `<b>${n._entity}.${n.name}</b>`



    //         html += `<br/>字段类型：${n._fieldType}`



    //         if (n._comment) html += `<br/>字段说明：${n._comment}`



    //         if (n._sample !== undefined && n._sample !== null) html += `<br/>样例值：${String(n._sample)}`



    //         return html



    //       }



    //       // 实体节点（数据表）：展示表名、中文名、行数、字段数、关联表数



    //       const pkList = [...(pkMap.value.get(n.name) || [])]



    //       const relCount = (relationships.value || []).filter((r) => r.source_table === n.name || r.target_table === n.name).length



    //       const cn = n._chineseName || allTables.value.find((t) => t.table_name === n.name)?.chinese_name || ''



    //       let html = `<b>${n.name}</b>${cn ? ` · ${cn}` : ''}`



    //       html += `<br/>📊 行数：${Number(n._rowCount || 0).toLocaleString()}`



    //       html += `<br/>字段数：${n._fieldCount ?? 0}`



    //       html += `<br/>关联表数：${relCount}`



    //       if (pkList.length) html += `<br/>🔑 主键：${pkList.join(', ')}`



    //       return html



    //     }



    //     const rel = p.data?._rel



    //     if (rel) {



    //       const type = rel.type === 'foreign_key' ? '外键约束' : '业务关联'



    //       return `<b>${rel.source_table}.${rel.source_column}</b> → <b>${rel.target_table}.${rel.target_column}</b><br/>基数：<b>${p.data?._cardinality}</b> · 类型：${type}`



    //     }



    //     return ''



    //   },



    // },

     tooltip: {

      trigger: 'item',

      confine: true,

      formatter: (p: any) => {

        // 仅菱形（关系节点）悬停弹出“来源表/来源字段/目标表/目标字段/说明”表格；字段、实体仍不弹

        if (p.dataType !== 'node') return undefined

        const n = p.data

        const rel = n._rel

        if (!rel) return undefined

        const desc = relDocDesc(rel) || n._verb || ''

        const td = (v: string) => `<td style="border:1px solid #d1d5db;padding:3px 10px;font-family:Consolas,monospace;font-size:12px">${v}</td>`

        return `<table style="border-collapse:collapse;font-size:12px"><thead><tr>

          <th style="border:1px solid #9ca3af;padding:3px 10px;background:#eef2ff;color:#3730a3">来源表</th>

          <th style="border:1px solid #9ca3af;padding:3px 10px;background:#eef2ff;color:#3730a3">来源字段</th>

          <th style="border:1px solid #9ca3af;padding:3px 10px;background:#eef2ff;color:#3730a3">目标表</th>

          <th style="border:1px solid #9ca3af;padding:3px 10px;background:#eef2ff;color:#3730a3">目标字段</th>

          <th style="border:1px solid #9ca3af;padding:3px 10px;background:#eef2ff;color:#3730a3">说明</th>

        </tr></thead><tbody><tr>

          ${td(rel.source_table)}${td(rel.source_column)}${td(rel.target_table)}${td(rel.target_column)}

          <td style="border:1px solid #d1d5db;padding:3px 10px;font-size:12px;color:#92400e;font-weight:bold">${desc}</td>

        </tr></tbody></table>`

      },

    },



    animationDuration: 300,



   series: [{



      type: 'graph',



      layout: 'none', // ⭐ 改回手动坐标布局！千万不要用 force！



      roam: 'move',



      draggable: true,



      zoom,



      data: nodes,



      links,



      label: { show: true, position: 'inside' },



      lineStyle: { color: '#64748b', width: 1.5, curveness: 0.1, opacity: 0.7 },



      emphasis: { 



        focus: 'adjacency', 



        scale: true,



        lineStyle: { width: 4 }



      },



      blur: {



        itemStyle: { opacity: 0.15 },



        lineStyle: { opacity: 0.1 },



        label: { opacity: 0.15 },



      },



      edgeSymbol: ['none', 'none'], // 去掉箭头，经典 ER 图通常无箭头



    }],



  }



}



// ── ER 图全屏（全局查看，ER 图唯一展示位置） ──



const erFullscreen = ref(false)



const erFullscreenRef = ref<HTMLElement | null>(null)



const erFullscreenHeight = ref(700)



const erSelectedTable = ref<any>(null)  

   // 双击卡片 → 字段详情面板

// ⭐ ER 图字段详情弹窗状态

const erFieldModal = ref({

  show: false,

  name: '',

  type: '',

  comment: '',

  sample: undefined

})



const erHighlightedTable = ref<string | null>(null)   // 单击高亮的表（其他节点半透明）



const erSelectedTables = ref<string[]>([])   // 用户勾选要查看的表（多选）



const erChoosing = ref(true)                 // 是否处于「选表」阶段（打开全屏先选表，再生成图）



const erTableSearch = ref('')                // 选表界面搜索关键词



const filteredErTables = computed(() => {



  const q = erTableSearch.value.trim().toLowerCase()



  if (!q) return allTables.value



  return allTables.value.filter((t) =>



    (t.table_name || '').toLowerCase().includes(q) || ((t as any).chinese_name || '').toLowerCase().includes(q)



  )



})







const groupTablesByScene = (tables: TableDetail[]) => {



  const groups = new Map<string, TableDetail[]>()



  tables.forEach((table) => {



    const scene = businessSceneMap.value[table.table_name] || '其他'



    if (!groups.has(scene)) groups.set(scene, [])



    groups.get(scene)!.push(table)



  })



  return Array.from(groups, ([scene, groupedTables]) => ({ scene, tables: groupedTables }))



}







const groupedErTables = computed(() => groupTablesByScene(filteredErTables.value))



const erPlan = ref<any>(null)            // 渲染规划 Agent 的方案（布局/间距/字段数）



const erRendering = ref(false)           // 正在载入 ER 图提示



let erFullChart: EChartsType | null = null



// 当前缩放倍数（1 = 卡片原始大小；缩放时同步改容器尺寸，滚动条随之变化）



let _erZoom = 1



// 打开 ER 图时自动适配全图（fit 到容器，全部卡片可见），首次渲染后置 false



let _erAutoFit = true



// 滚轮缩放监听：记录绑定的元素与 handler（全屏容器是 Teleport v-if，每次打开重建 DOM，



// 若用一次性标志会导致第二次打开后新元素没有监听 → 滚轮失效）



let _erWheelEl: HTMLElement | null = null



let _erWheelHandler: ((e: WheelEvent) => void) | null = null


// 容器 mouseleave 回调。
// 此前写成 el.addEventListener('mouseleave', () => ...) —— 匿名函数每次调用都新建一个，
// 而 renderErFullscreenChart() 会被 scheduleErRender()（缩放 / 拖动 / 数据变更）反复触发，
// 容器又是同一个 DOM 节点，于是回调越叠越多且永远无法移除（滚轮监听当时已做了具名 + 解绑，
// 这里漏了）。改为模块级具名回调 + 记录绑定元素，重绑前先解绑。
let _erMouseLeaveEl: HTMLElement | null = null

let _erMouseLeaveHandler: (() => void) | null = null



let _erRenderTimer: number | null = null


// ER 图载入提示的 12 秒兜底定时器。此前是裸 setTimeout，组件被 KeepAlive 驱逐或切页销毁后
// 回调仍会执行并写已卸载实例的 ref；同时重复打开会叠加多个定时器。统一登记、重入前先清、卸载时清。
let _erLoadingFallbackTimer: number | null = null







// 从渲染规划 Agent 获取当前库的渲染方案（表少→环形、表多→分层等）



// 按库缓存：同一库只拉一次，切换库时才重新请求（后端也已预热缓存，打开 ER 图秒出）



let erPlanDbName = ''



const loadErPlan = async () => {



  const dbName = activeDatabase.value



  if (erPlan.value && erPlanDbName === dbName) return



  try {



    const res = await fetch('/api/visualize/plan')



    if (res.ok) {



      erPlan.value = await res.json()



      erPlanDbName = dbName



    }



  } catch (e) {



    // 方案加载失败时回退默认参数



  }



}







// 详情面板是否有字段注释（含文档中文含义）



const hasColumnComments = (t: any) => (t?.columns || []).some((c: any) => fieldCommentCn(t?.table_name || '', c))







// 查找外键字段的关联目标（目标表.目标列），详情弹窗中展示"这个字段连到哪里"



const findFkTarget = (tableName: string, colName: string) => {



  const r = relationships.value.find(



    (x) => x.source_table === tableName && x.source_column === colName,



  )



  return r ? `${r.target_table}.${r.target_column}` : ''



}







// 从详情面板跳转到数据页的字段视图



const jumpToTable = (tableName: string) => {



  erFullscreen.value = false



  activeTab.value = tableName



  activeTabView.value = 'fields'



  loadTableDetail(tableName, 1)



}







// 防抖调度（#3）：同一 tick 内多次触发只执行最后一次，避免重复 init/dispose 闪烁



const scheduleErRender = () => {



  if (_erRenderTimer) window.clearTimeout(_erRenderTimer)



  _erRenderTimer = window.setTimeout(() => {



    _erRenderTimer = null



    renderErFullscreenChart()



  }, 10)



}







const renderErFullscreenChart = () => {



  if (erChoosing.value) return   // 选表阶段：先选表，不渲染图



  // Teleport 挂载时序兜底（#2）：ref 未就绪时用 rAF 重试，最多 10 帧



  let tries = 0



  const doRender = () => {



    const el = erFullscreenRef.value



    if (!el) {



      if (tries++ < 10) { requestAnimationFrame(doRender); return }



      return



    }



    if (erFullChart) {



      erFullChart.off('click')



      erFullChart.dispose()



      erFullChart = null



    }



        const maxFields = erPlan.value?.cardMaxFields ?? 5



    const { totalW, totalH } = buildCardEr(maxFields) // 拿到计算好的宽高



    const scroller = el.parentElement



    const sw = scroller?.clientWidth || 1200



    const sh = scroller?.clientHeight || 700



    if (_erAutoFit) {



      const fitZ = Math.min((sw - 40) / totalW, (sh - 40) / totalH, 1) * 0.92



      _erZoom = Math.max(0.32, Math.min(1, fitZ))



      _erAutoFit = false



    }



    // 让容器跟着图表尺寸变



    const cw = Math.max(totalW * _erZoom, sw)



    const ch = Math.max(totalH * _erZoom, sh)



    el.style.width = cw + 'px'



    el.style.height = ch + 'px'



    erFullChart = echarts.init(el)



    erFullChart.setOption(buildErOption(maxFields, _erZoom), true)



    erRendering.value = false



    



    // 滚动到中间



    requestAnimationFrame(() => {



      const scroller = el.parentElement



      if (!scroller || !erFullChart) return



      scroller.scrollLeft = (cw - sw) / 2



      scroller.scrollTop = (ch - sh) / 2



    })



         // 单击：如果是属性（字段椭圆），打开自定义弹框；如果是表，高亮关联。

    erFullChart.on('click', (params: any) => {

      if (params.dataType === 'node') {

        const data = params.data

        // ⭐ 如果点到了属性（椭圆）节点

        if (data._fieldType !== undefined) {

          // 填充弹框数据并显示（字段名用完整名 _fieldName，椭圆内截断名仅作显示用）

          erFieldModal.value = {

            show: true,

            name: data._fieldName || data.name,

            type: data._fieldType,

            comment: data._comment || '',

            sample: data._sample

          }

          return // 弹出说明后不再执行后面的高亮逻辑

        }



        // 如果点到了实体表（矩形）：单击 → 弹出该表完整字段说明

        if (data._entity) {

          erFullChart?.dispatchAction({ type: 'downplay' })

          const tableName = data._entity

          erSelectedTable.value =

            allTables.value.find((t) => t.table_name === tableName) || { table_name: tableName, columns: [] }

          return

        }

      } else if (erHighlightedTable.value) {

        erHighlightedTable.value = null

        scheduleErRender()

      }

    })



    // 拖动卡片结束后恢复强调状态 + 保存布局（记忆功能）



    // 注意：chart 级 'dragend' 事件在部分环境不触发，改用 zrender 底层事件 + 拖动中节流保存兜底



    const _onErDragEnd = () => {



      erFullChart?.dispatchAction({ type: 'downplay' })



      saveErLayout()



    }



    erFullChart.on('dragend', _onErDragEnd)



    erFullChart.getZr().on('dragend', _onErDragEnd)



    // 拖动过程中节流保存（最保险：即使 dragend 未触发，拖动结束前也保存了）



    let _lastErSave = 0



    const _onErDrag = () => {



      const now = Date.now()



      if (now - _lastErSave > 400) {



        _lastErSave = now



        saveErLayout()



      }



    }



    erFullChart.getZr().on('drag', _onErDrag)



    // ── 滚轮缩放：改 _erZoom + 防抖重渲染（容器尺寸同步变化，滚动条随之更新）──



    // 全屏容器是 <Teleport v-if>，每次打开都会重建 DOM：



    // 旧监听绑定的元素 ≠ 当前元素 → 先移除旧监听，再给新元素重新绑定。



    if (_erWheelHandler && _erWheelEl && _erWheelEl !== el) {



      _erWheelEl.removeEventListener('wheel', _erWheelHandler)



      _erWheelHandler = null



      _erWheelEl = null



    }



    if (!_erWheelHandler) {



      const handler = (e: WheelEvent) => {



        e.preventDefault()



        const factor = e.deltaY < 0 ? 1.2 : 0.83



        const next = Math.min(4, Math.max(0.3, _erZoom * factor))



        if (next !== _erZoom) {



          _erZoom = next



          scheduleErRender()



        }



      }



      _erWheelHandler = handler



      _erWheelEl = el



      el.addEventListener('wheel', handler, { passive: false })



    }



    // ── 强制恢复：focus:'adjacency' 的 hover 变灰在部分交互下会卡住不恢复 ──



    // 监听 echarts 的 mouseout（离开图表区域）与容器 mouseleave（离开画布），



    // 统一 downplay 清除所有强调/变灰状态，保证"移开必恢复"。



    erFullChart.on('mouseout', () => erFullChart?.dispatchAction({ type: 'downplay' }))



    if (_erMouseLeaveHandler && _erMouseLeaveEl && _erMouseLeaveEl !== el) {
      _erMouseLeaveEl.removeEventListener('mouseleave', _erMouseLeaveHandler)
      _erMouseLeaveHandler = null
      _erMouseLeaveEl = null
    }
    if (!_erMouseLeaveHandler) {
      const handler = () => erFullChart?.dispatchAction({ type: 'downplay' })
      _erMouseLeaveHandler = handler
      _erMouseLeaveEl = el
      el.addEventListener('mouseleave', handler)
    }



  }



  doRender()



}







const openErFullscreen = async () => {



  try {



    window.localStorage.removeItem(erLayoutKey())



  } catch (e) { /* ignore */ }



  erFullscreenHeight.value = Math.max(700, window.innerHeight - 60)



  // 进入「选表」阶段：先让用户勾选要查看的表，生成后再渲染图



  erChoosing.value = true



  erRendering.value = false



  erHighlightedTable.value = null



  // 每次打开都从空选择开始，由用户手动勾选需要查看的表。



  erSelectedTables.value = []



  // 后台刷新渲染方案（首次打开或切库后），生成图时直接用缓存



  const needPlan = !erPlan.value || erPlanDbName !== activeDatabase.value



  if (needPlan) {



    await loadErPlan()



  }



}







// 选表完成 → 生成 ER 图（只展示勾选的表及其关系）



const startErRender = () => {



  if (!erSelectedTables.value.length) return



  erChoosing.value = false



  erRendering.value = true



  _erAutoFit = true



  _erZoom = 1



  // 兜底：最多 12 秒后隐藏加载提示



  if (_erLoadingFallbackTimer !== null) clearTimeout(_erLoadingFallbackTimer)
  _erLoadingFallbackTimer = window.setTimeout(() => {
    _erLoadingFallbackTimer = null
    erRendering.value = false
  }, 12000)



  scheduleErRender()



}







// 返回选表界面（重新选择要查看的表）



const backToErChoosing = () => {



  erChoosing.value = true



  erSelectedTable.value = null



  erHighlightedTable.value = null



  erTableSearch.value = ''



  if (erFullChart) {



    erFullChart.dispose()



    erFullChart = null



  }



}







// 清空选表

const toggleAllErTables = () => {

  erSelectedTables.value = []

}





// 放大/缩小按钮：改缩放倍数 + 重渲染（容器尺寸同步变化，滚动条随之更新）



const zoomErBy = (factor: number) => {



  const next = Math.min(4, Math.max(0.3, _erZoom * factor))



  if (next !== _erZoom) {



    _erZoom = next



    scheduleErRender()



  }



}







// 适应窗口：计算能完整放进滚动容器的缩放倍数，缩放后滚动条归位



const fitErFullscreen = () => {



  const el = erFullscreenRef.value



  if (!el) return



  const { totalW, totalH } = buildCardEr(erPlan.value?.cardMaxFields ?? 5)



  const scroller = el.parentElement



  const fw = scroller?.clientWidth || window.innerWidth



  const fh = scroller?.clientHeight || 700



  // 与打开时一致：卡片约占容器 80% 区域



  const fitZ = Math.min((fw - 40) / totalW, (fh - 40) / totalH, 1) * 0.8



  _erZoom = Math.max(0.3, Math.min(1, fitZ))



  scheduleErRender()



}







const downloadErPng = () => {



  if (!erFullChart) return



  try {



    // 导出完整全图：临时用原始大小(zoom=1)重渲染，导出后恢复当前缩放



    const prevZoom = _erZoom



    if (_erZoom !== 1) {



      _erZoom = 1



      renderErFullscreenChart()



    }



    const url = erFullChart.getDataURL({ pixelRatio: 2, backgroundColor: '#ffffff' })



    if (prevZoom !== 1) {



      _erZoom = prevZoom



      renderErFullscreenChart()



    }



    const a = document.createElement('a')



    a.href = url



    a.download = `表关系ER图-${new Date().toISOString().slice(0, 10)}.png`



    a.click()



  } catch (e) {



    console.error('导出 ER 图失败', e)



  }



}







watch(erFullscreen, (v) => {



  if (v) {



    openErFullscreen()



  } else {



    // 关闭前兜底保存布局（即使 dragend 事件未触发，拖动后的位置也保存）



    saveErLayout()



    erSelectedTable.value = null



    erHighlightedTable.value = null



    erChoosing.value = true



    if (_erRenderTimer) {



      window.clearTimeout(_erRenderTimer)



      _erRenderTimer = null



    }



    if (erFullChart) {



      erFullChart.dispose()



      erFullChart = null



    }



    // 移除滚轮监听（容器即将销毁，显式清理避免残留）



    if (_erWheelHandler && _erWheelEl) {



      _erWheelEl.removeEventListener('wheel', _erWheelHandler)



      _erWheelHandler = null



      _erWheelEl = null



    }



  }



})







// 关闭详情弹窗时恢复所有卡片状态（防止 hover 强调残留导致其他卡片一直变灰）



watch(erSelectedTable, (v) => {



  if (!v) {



    erFullChart?.dispatchAction({ type: 'downplay' })



  }



})







watch(() => relationships.value, () => {



  if (activeTabView.value !== 'relationships') return



  // 数据变化时，仅刷新全屏实例（主视图不内嵌 ER 图）



  if (erFullscreen.value) scheduleErRender()



})







// ========== 计算属性 ==========



const tableTabs = computed(() => allTables.value.map(t => t.table_name))







const filteredTableTabs = computed(() => {



  const base = (authMode.value && authCatalog.value)



    ? tableTabs.value.filter(n => catalogColMap.value.cols.has(n.toLowerCase()))



    : tableTabs.value



  if (!tableFilterKeyword.value.trim()) {



    return base



  }



  const keyword = tableFilterKeyword.value.trim().toLowerCase()



  return base.filter(name => name.toLowerCase().includes(keyword))



})







const groupedTableTabs = computed(() => {



  const tableMap = new Map(allTables.value.map((table) => [table.table_name, table]))



  const groups = new Map<string, string[]>()



  filteredTableTabs.value.forEach((name) => {



    const scene = businessSceneMap.value[name] || '其他'



    if (!groups.has(scene)) groups.set(scene, [])



    groups.get(scene)!.push(name)



  })



  return Array.from(groups, ([scene, names]) => ({



    scene,



    names: names.filter((name) => tableMap.has(name)),



  }))



})







const currentTableData = computed(() => {



  return allTables.value.find(t => t.table_name === activeTab.value)



})







// 时间列识别：仅依赖 currentTableData（computed，须在其声明之后注册 watch）
watch(() => currentTableData.value?.columns, syncTimeCols)

const getTableData = (name: string) => {



  return allTables.value.find(t => t.table_name === name)



}







const filteredColumns = computed(() => {



  const columns = visibleColumns.value



  if (!filterKeyword.value.trim()) {



    return columns



  }



  const keyword = filterKeyword.value.trim().toLowerCase()



  return columns.filter(col =>



    col.name.toLowerCase().includes(keyword) ||



    (col.comment && col.comment.toLowerCase().includes(keyword)) ||



    (col.translation && col.translation.toLowerCase().includes(keyword))



  )



})







// ========== 获取主键 ==========



const getPrimaryKey = (tableName: string): string => {



  const table = allTables.value.find(t => t.table_name === tableName)



  if (!table) return 'id'



  const pkCol = table.columns.find(col => col.primary_key === true)



  return pkCol?.name || 'id'



}







// ========== 备注编辑方法 ==========



// 备注展示：有意义的数据库注释优先；无意义占位（如纯数字 "1"）回退到字段说明



const displayComment = (col: Column): string => {



  const c = (col.comment || '').trim()



  if (c && !/^\d+$/.test(c)) return c



  return col.translation || c



}







// 备注尽量与《数据介绍.md》中文含义一致：文档命中优先；未命中再按制造业关键词猜中文，最后回退库注释

const fieldCommentCn = (table: string, col: Column): string =>



  DOC_FIELD_CN[table]?.[col.name] || guessFieldCn(col.name) || displayComment(col)

// 全部数据表头中文（按备注优先）：字段备注含中文就以备注为准；
// 备注缺失/纯英文时用文档字典或字段名猜测，最后才退回英文字段名。
const fieldHeaderText = (table: string, col: Column): string => {
  // 表头不要主键/外键标注（词典里如「产品ID，主键」），只保留业务名
  const stripKeyTag = (t: string) => t
    .replace(/[（(]?(主键|外键|PK|FK|联合主键)[）)]?$/g, "")
    .replace(/[\s,，、;；]+$/g, "")
    .trim()
  const cm = String(col.comment || "").trim()
  if (cm && /[\u4e00-\u9fff]/.test(cm)) return stripKeyTag(cm) || col.name
  const cn = fieldCommentCn(table, col) || ""
  if (/[\u4e00-\u9fff]/.test(cn)) return stripKeyTag(cn) || col.name
  return col.name
}




// 保存成功提示（2.5 秒后自动消失）



const showCommentToast = (msg: string) => {



  commentToast.value = msg



  if (commentToastTimer) window.clearTimeout(commentToastTimer)



  commentToastTimer = window.setTimeout(() => { commentToast.value = '' }, 2500)



}







// 单击备注 → 弹框查看完整备注（不再一行截断）



const openCommentDetail = (col: Column) => {



  commentDetail.value = { name: col.name, text: fieldCommentCn(activeTab.value, col) }



  commentEditing.value = false



  commentEditValue.value = ''



}







// 通用保存：执行 PATCH + 本地更新 + 提示，返回是否成功



const saveCommentValue = async (col: Column, newComment: string) => {



  const tableName = activeTab.value



  if (!tableName) return false



  const oldComment = col.comment



  try {



    const targetTable = allTables.value.find(t => t.table_name === tableName)



    const targetCol = targetTable?.columns.find(c => c.name === col.name)



    if (targetCol) targetCol.comment = newComment







    const res = await fetch(`/api/tables/tables/${encodeURIComponent(tableName)}/columns/${encodeURIComponent(col.name)}/comment`, {



      method: 'PATCH',



      headers: { 'Content-Type': 'application/json' },



      body: JSON.stringify({ comment: newComment })



    })







    if (!res.ok) {



      if (targetCol) targetCol.comment = oldComment



      const err = await res.json()



      console.error('保存备注失败:', err)



      alert('保存失败: ' + (err.detail || '未知错误'))



      return false



    }



    showCommentToast('备注已保存')



    return true



  } catch (error) {



    console.error('保存备注失败:', error)



    const targetTable = allTables.value.find(t => t.table_name === tableName)



    const targetCol = targetTable?.columns.find(c => c.name === col.name)



    if (targetCol) targetCol.comment = oldComment



    alert('保存失败，请检查网络连接')



    return false



  }



}







// 弹框内点「编辑」→ 进入弹框内编辑模式



const editFromDetail = () => {



  if (!commentDetail.value) return



  commentEditValue.value = commentDetail.value.text



  commentEditing.value = true



}







// 弹框内保存



const saveFromDetail = async () => {



  if (!commentDetail.value) return



  const table = allTables.value.find(t => t.table_name === activeTab.value)



  const col = table?.columns.find(c => c.name === commentDetail.value!.name)



  if (!col) return



  const newComment = commentEditValue.value.trim()



  if (newComment === (col.comment || '')) {



    commentEditing.value = false



    return



  }



  const ok = await saveCommentValue(col, newComment)



  if (ok) {



    commentDetail.value = { name: col.name, text: newComment }



    commentEditing.value = false



  }



}







// 弹框内取消编辑



const cancelFromDetail = () => {



  commentEditing.value = false



  commentEditValue.value = ''



}







// ========== 样例数据编辑方法 ==========



const startEditSample = (_row: Record<string, any>, key: string, rowIdx: number, val: any) => {



  if (editingSample.value.loading) return







  editingSample.value.rowIdx = rowIdx



  editingSample.value.cellKey = key



  editingSample.value.value = val !== null && val !== undefined ? String(val) : ''







  nextTick(() => {



    sampleInputRef.value?.focus()



    sampleInputRef.value?.select()



  })



}







const cancelSampleEdit = () => {



  editingSample.value.rowIdx = -1



  editingSample.value.cellKey = ''



  editingSample.value.value = ''



  editingSample.value.loading = false



}







const saveSampleEdit = async (row: Record<string, any>, key: string) => {



  // 防重入：正在提交或已取消时忽略重复触发（连按 enter / blur 等）



  if (editingSample.value.loading) return



  if (editingSample.value.rowIdx === -1 || editingSample.value.cellKey !== key) return







  const newValue = editingSample.value.value.trim()



  const oldValue = row[key]







  if (String(newValue) === String(oldValue !== null && oldValue !== undefined ? oldValue : '')) {



    cancelSampleEdit()



    return



  }







  const tableName = activeTab.value



  if (!tableName) {



    cancelSampleEdit()



    return



  }







  const pkColumn = getPrimaryKey(tableName)



  const pkValue = row[pkColumn]







  if (!pkValue) {



    alert('无法找到主键，请检查数据表结构')



    cancelSampleEdit()



    return



  }







  // 二次确认：直接写入本地数据库，需用户明确确认



  const oldDisplay = oldValue !== null && oldValue !== undefined ? String(oldValue) : 'null'



  const newDisplay = newValue === '' ? 'null' : newValue



  if (!window.confirm(



    `确认修改数据库中的数据？此操作会直接写入本地数据库，无法撤销。\n\n` +



    `表：${tableName}\n` +



    `字段：${key}\n` +



    `原值：${oldDisplay}\n` +



    `新值：${newDisplay}`



  )) {



    cancelSampleEdit()



    return



  }







  // 修复（P0）：类型校验必须在发请求【之前】完成。
  // 原实现把数值/布尔校验放在 PATCH 成功返回之后，导致两个后果：
  //   ① 非法值（如 "abc" 写入数值列）在弹提示前就已经写进数据库；
  //   ② 提前 return 时 editingSample.loading 不复位（函数内原本没有 finally），
  //      而 startEditSample/saveSampleEdit 的防重入守卫都以 loading 为门闸 →
  //      此后双击任何单元格都毫无反应，必须刷新整页。
  {
    const preTable = allTables.value.find((t: any) => t.table_name === tableName)
    const preCol = preTable?.columns?.find((c: any) => c.name === key)
    const preType = String(preCol?.type || '').toUpperCase()
    if (/INT|NUMERIC|DECIMAL|FLOAT|DOUBLE|REAL|SERIAL/.test(preType)) {
      if (newValue !== '' && Number.isNaN(Number(newValue))) {
        alert(`"${key}" 是数值列，请输入数字`)
        cancelSampleEdit()
        return
      }
    } else if (preType.includes('BOOL')) {
      const lv = newValue.toLowerCase()
      if (newValue !== '' && lv !== 'true' && lv !== 'false') {
        alert(`"${key}" 是布尔列，请输入 true 或 false`)
        cancelSampleEdit()
        return
      }
    }
  }

  editingSample.value.loading = true







  try {



    const res = await fetch('/api/tables/data/update', {



      method: 'PATCH',



      headers: { 'Content-Type': 'application/json' },



      body: JSON.stringify({



        table_name: tableName,



        column_name: key,



        row_id: String(pkValue),



        id_column: pkColumn,



        new_value: newValue === '' ? null : newValue



      })



    })







    if (!res.ok) {



      const err = await res.json()



      throw new Error(err.detail || '更新失败')



    }







    // 更新本地数据



    const targetTable = allTables.value.find(t => t.table_name === tableName)



    if (targetTable) {



      const targetRow = targetTable.sample_data.find(



        (r: any) => String(r[pkColumn]) === String(pkValue)



      )



      if (targetRow) {



        const originalCol = targetTable.columns.find(col => col.name === key)



        if (originalCol) {



          const colType = String(originalCol.type || '').toUpperCase()



          if (colType.includes('INT') || colType.includes('NUMERIC') || colType.includes('DECIMAL')) {



            const num = newValue === '' ? null : Number(newValue)



            if (num !== null && Number.isNaN(num)) {



              alert(`"${key}" 是数值列，请输入数字`)



              return



            }



            targetRow[key] = num



          } else if (colType.includes('BOOL')) {



            targetRow[key] = newValue === '' ? null : newValue.toLowerCase() === 'true'



          } else {



            targetRow[key] = newValue === '' ? null : newValue



          }



        } else {



          targetRow[key] = newValue === '' ? null : newValue



        }



      }



    }







    cancelSampleEdit()







  } catch (error: any) {



    console.error('更新失败:', error)



    alert('更新失败: ' + (error.message || '未知错误'))



  } finally {
    // 修复（P0）：成功走 cancelSampleEdit() 复位，但任何提前 return / 抛错的路径都必须兜底复位，
    // 否则 editingSample.loading 永久为 true，防重入守卫会让单元格编辑彻底失效。
    editingSample.value.loading = false
  }



}







// ========== 加载表关系 ==========



const loadRelationships = async () => {



  relationshipsLoading.value = true



  try {



    const res = await fetch('/api/tables/relationships')
    // 修复（P1）：fetch 只在网络层失败时 reject，500/403 都会正常 resolve。
    // 原实现直接 data.relationships || []，后端异常时显示"暂无表间关系数据"，
    // 把"接口报错"伪装成"业务上没有关系"，排查方向被彻底误导。
    if (!res.ok) throw new Error(`加载表关系失败(${res.status})`)
    const data = await res.json()
    relationships.value = data.relationships || []



  } catch (error) {



    console.error('加载表关系失败:', error)



  } finally {



    relationshipsLoading.value = false



  }



}







// ========== 加载数据 ==========



// 数据库工作区切换



const databases = ref<string[]>([])



const activeDatabase = ref('')



const databaseToDelete = ref('')







// 数据源导入



const csvFileInput = ref<HTMLInputElement | null>(null)



const importTableName = ref('')



const importMode = ref<'file' | 'url'>('file')



const importUrl = ref('')



const importing = ref(false)



const importMessage = ref('')



const importMessageType = ref<'success' | 'error'>('success')







const formatImportError = (error: unknown): string => {



  if (error instanceof Error) return error.message



  if (typeof error === 'string') return error



  if (Array.isArray(error)) {



    return error.map(item => {



      if (item && typeof item === 'object' && 'msg' in item) {



        const detail = item as { loc?: unknown[]; msg: string }



        return `${detail.loc?.slice(1).join(' / ') || '请求'}：${detail.msg}`



      }



      return formatImportError(item)



    }).join('；')



  }



  if (error && typeof error === 'object') {



    const detail = error as { detail?: unknown; message?: unknown }



    if (detail.detail !== undefined) return formatImportError(detail.detail)



    if (typeof detail.message === 'string') return detail.message



  }



  return '导入失败，请检查文件格式或后端服务日志'



}







const parseImportResponse = async (res: Response) => {



  const payload = await res.json().catch(() => ({}))



  if (!res.ok) throw payload.detail ?? payload



  if (payload.success === false) throw payload.message ?? payload



  return payload



}







const handleImportCsv = async () => {



  if (importing.value) return







  if (importMode.value === 'url') {



    if (!importUrl.value.trim()) {



      importMessage.value = '请输入远程数据源地址'



      importMessageType.value = 'error'



      return



    }



    importing.value = true



    importMessage.value = ''



    try {



      const form = new FormData()



      form.append('source_type', 'url')



      form.append('source_url', importUrl.value.trim())



      form.append('table_name', importTableName.value.trim())



      const res = await fetch('/api/tables/import-csv', { method: 'POST', body: form })



      const data = await parseImportResponse(res)



      const tables = data.mode === 'folder' || data.mode === 'sqlite'



        ? (data.files || data.tables || []).map((t: any) => t.table_name).join('、')



        : data.table_name



      importMessage.value = `导入成功：${tables}（共 ${data.row_count ?? data.tables?.length ?? 0} 行）`



      importMessageType.value = 'success'



      importUrl.value = ''



      importTableName.value = ''



      await loadTables()



    } catch (error) {



      importMessage.value = formatImportError(error)



      importMessageType.value = 'error'



    } finally {



      importing.value = false



    }



    return



  }







  // 文件模式



  const files = csvFileInput.value?.files



  if (!files || files.length === 0) {



    importMessage.value = '请先选择要导入的文件'



    importMessageType.value = 'error'



    return



  }







  importing.value = true



  importMessage.value = ''



  try {



    const form = new FormData()



    for (const file of Array.from(files)) {



      form.append('files', file)



    }



    form.append('table_name', importTableName.value.trim())



    form.append('source_type', 'file')



    const res = await fetch('/api/tables/import-csv', { method: 'POST', body: form })



    const data = await parseImportResponse(res)



    const tables = data.mode === 'folder' || data.mode === 'sqlite'



      ? (data.files || data.tables || []).map((t: any) => t.table_name).join('、')



      : data.table_name



    importMessage.value = `导入成功：${tables}（共 ${data.row_count ?? data.tables?.length ?? 0} 行）`



    importMessageType.value = 'success'



    importTableName.value = ''



    if (csvFileInput.value) csvFileInput.value.value = ''



    await loadTables()



  } catch (error) {



    importMessage.value = formatImportError(error)



    importMessageType.value = 'error'



  } finally {



    importing.value = false



  }



}







const loadDatabases = async () => {



  try {



    const res = await fetch('/api/config/databases')



    const data = await res.json()



    databases.value = data.databases || []



    activeDatabase.value = data.active || ''



  } catch (error) {



    console.error('加载数据库列表失败:', error)



  }



}







const switchDatabase = async () => {



  if (!activeDatabase.value) return



  try {



    const res = await fetch(`/api/config/databases/${encodeURIComponent(activeDatabase.value)}/activate`, { method: 'POST' })
    // 修复（P1）：切库属于状态变更。原实现只看 data.success 不看 HTTP 状态，
    // 后端返回 403/500 时错误体里没有 success 字段 → 静默走进"失败"分支却不给任何提示，
    // 用户以为已切到新库，实际仍连在旧库上（后续所有查询都会落在错误的库）。
    if (!res.ok) throw new Error(`切换数据库失败(${res.status})`)



    const data = await res.json()



    if (data.success) {



      databases.value = data.profiles || databases.value



      activeDatabase.value = data.active || activeDatabase.value



      relationships.value = []



      await loadTables()



      await loadBusinessScenes()



    } else {



      alert(data.message || '切换失败')



      await loadDatabases()



    }



  } catch (error) {



    console.error('切换数据库失败:', error)



    alert('切换数据库失败，请检查后端服务')



  }



}







const deleteDatabase = async () => {



  if (!databaseToDelete.value) return



  if (!window.confirm(`确认永久删除数据库"${databaseToDelete.value}"及其中全部数据表吗？此操作不可恢复。`)) return



  try {



    const res = await fetch(`/api/config/databases/${encodeURIComponent(databaseToDelete.value)}`, { method: 'DELETE' })
    // 修复（P1）：删除数据库是不可逆操作。原实现不校验 HTTP 状态，
    // 403/500 时错误体无 success 字段 → 既不提示也不刷新，用户无法判断到底删没删掉。
    if (!res.ok) throw new Error(`删除数据库失败(${res.status})`)



    const data = await res.json()



    if (data.success) {



      databases.value = data.profiles || databases.value



      databaseToDelete.value = ''



    } else {



      alert(data.message || '删除失败')



    }



  } catch (error) {



    console.error('删除数据库失败:', error)



    alert('删除数据库失败，请检查后端服务')



  }



}







const loadTables = async () => {



  loading.value = true



  try {



    const res = await fetch('/api/tables/')



    const data = await res.json()



    allTables.value = data.tables || []



    if (allTables.value.length > 0) {



      // 保留当前选中表（导入/刷新后不丢失）；仅当原选中项已不存在时才回退到第一张



      const stillExists = allTables.value.some(t => t.table_name === activeTab.value)



      if (!stillExists) {



        activeTab.value = allTables.value[0].table_name



      }



    }



  } catch (error) {



    console.error('加载表结构失败:', error)



  } finally {



    loading.value = false



  }



}







const loadTableDetail = async (tableName: string, page: number = 1) => {



  // 分页拉取数据，避免大表全量加载卡顿



  // 请求 ID 防竞态：快速切表/翻页时只保留最新一次请求的结果



  const requestId = ++detailRequestSeq



  tableDataLoading.value = true



  try {



    // 全部数据分页拉取（带时间筛选参数：time_column/start_date/end_date）
    let url = `/api/tables/${encodeURIComponent(tableName)}?page=${page}&page_size=${PAGE_SIZE}`
    if (timeFilterCol.value && (timeFilterStart.value || timeFilterEnd.value)) {
      url += `&time_column=${encodeURIComponent(timeFilterCol.value)}`
      if (timeFilterStart.value) url += `&start_date=${encodeURIComponent(timeFilterStart.value)}`
      if (timeFilterEnd.value) url += `&end_date=${encodeURIComponent(timeFilterEnd.value)}`
    }
    if (sortColumn.value) {
      url += `&order_column=${encodeURIComponent(sortColumn.value)}&order_dir=${sortDir.value}`
    }
    const res = await fetch(url)



    if (requestId !== detailRequestSeq) return // 已被更新的请求覆盖



    const data = await res.json()



    const index = allTables.value.findIndex(t => t.table_name === tableName)



    if (index !== -1) {



      // 详情接口返回精确 total_count，用它刷新行数（编辑/导入后能反映真实值）



      const newRowCount = data.total_count ?? allTables.value[index].row_count ?? 0



      allTables.value[index] = {



        ...data,



        chinese_name: allTables.value[index].chinese_name || data.chinese_name || '',



        row_count: newRowCount



      }



    } else {



      allTables.value.push(data)



    }



  } catch (error) {



    console.error('加载表详情失败:', error)



  } finally {



    if (requestId === detailRequestSeq) {



      tableDataLoading.value = false



    }



  }



}







// ========== 监听 ==========



// 切换表：重置页码触发加载（watch currentPage 统一负责拉取）



watch(activeTab, (newTab, oldTab) => {
  sortColumn.value = ''
  sortDir.value = 'asc'
  



  if (newTab && newTab !== oldTab) {



    if (currentPage.value === 1) {



      // 页码未变时 watch(currentPage) 不触发，需手动加载



      loadTableDetail(newTab, 1)



    } else {



      currentPage.value = 1



    }



  }



})







// 翻页加载



watch(currentPage, (page) => {



  if (activeTab.value) {



    loadTableDetail(activeTab.value, page)



  }



})







const goToPage = (page: number) => {



  if (page < 1 || page > totalPages.value) return



  if (page === currentPage.value) return



  currentPage.value = page



}







watch(activeTab, () => {



  activeTabView.value = 'fields'



})







watch(activeTabView, (newTab) => {



  if (newTab === 'relationships') {



    if (relationships.value.length === 0) {



      loadRelationships().then(() => {



        nextTick(() => scheduleErRender())



      })



    } else {



      nextTick(() => scheduleErRender())



    }



  }



})







const handleOpenTable = (e: Event) => {



  // 响应总览页「去数据页查看」：跳转后自动选中目标表



  const tableName = (e as CustomEvent).detail



  if (tableName && typeof tableName === 'string') {



    activeTab.value = tableName



  }



}







onMounted(() => {



  loadDatabases()



  loadTables()



  loadBusinessScenes()



  loadCatalog()



  window.addEventListener('resize', handleErResize)



  window.addEventListener('open-table', handleOpenTable)



})







onBeforeUnmount(() => {



  window.removeEventListener('resize', handleErResize)



  window.removeEventListener('open-table', handleOpenTable)



  // 定时器与全屏容器监听一并清理：否则组件销毁后兜底回调仍会写 erRendering，
  // 容器上的 mouseleave / wheel 也会随元素被销毁而留下悬空引用
  if (_erRenderTimer !== null) { clearTimeout(_erRenderTimer); _erRenderTimer = null }
  if (_erLoadingFallbackTimer !== null) { clearTimeout(_erLoadingFallbackTimer); _erLoadingFallbackTimer = null }
  if (_erMouseLeaveHandler && _erMouseLeaveEl) {
    _erMouseLeaveEl.removeEventListener('mouseleave', _erMouseLeaveHandler)
  }
  if (_erWheelHandler && _erWheelEl) {
    _erWheelEl.removeEventListener('wheel', _erWheelHandler)
  }
  _erMouseLeaveHandler = null
  _erMouseLeaveEl = null
  _erWheelHandler = null
  _erWheelEl = null



  if (erFullChart) {



    erFullChart.dispose()



    erFullChart = null



  }



})







const handleErResize = () => {



  erFullChart?.resize()



}



</script>