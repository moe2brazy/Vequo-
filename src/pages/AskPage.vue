<template>
  <div ref="askRootEl" class="flex gap-4 min-h-0 overflow-hidden" style="height: calc(100vh - 96px)">
    <!-- ====== 左侧：历史记录面板（可收纳，收起后右侧聊天区更宽） ====== -->
    <div v-if="!historyCollapsed" class="w-64 flex-shrink-0 border border-gray-200 rounded-xl overflow-hidden flex flex-col bg-white">
      <!-- 头部 -->
      <div class="bg-gray-50 px-3 py-2 border-b border-gray-200 flex items-center justify-between">
        <span class="text-xs font-medium text-gray-600">智能问数</span>
        <div class="flex items-center gap-2">
          <button
            @click="createNewChat"
            class="text-xs text-primary hover:text-primary-dark font-medium transition"
          >
            + 新建对话
          </button>
          <button
            @click="historyCollapsed = true"
            class="text-xs text-gray-400 hover:text-gray-600 transition flex items-center"
            title="收起历史记录"
          ><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="m15 18-6-6 6-6" /> </svg></span></button>
        </div>
      </div>

      <!-- 搜索框 + 搜索按钮 -->
      <div class="px-2 py-1.5 border-b border-gray-100">
        <div class="flex gap-1">
          <input
            v-model="searchInput"
            type="text"
            placeholder="搜索历史记录..."
            class="flex-1 px-2 py-1 text-xs border border-gray-200 rounded outline-none focus:border-primary focus:ring-1 focus:ring-primary"
            @keyup.enter="doSearch"
          />
          <button
            @click="doSearch"
            class="px-2 py-1 text-xs text-white bg-primary rounded hover:bg-primary/80 transition flex-shrink-0"
            title="搜索"
          >
            <span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <circle cx="11" cy="11" r="8" /> <path d="m21 21-4.3-4.3" /> </svg></span>
          </button>
          <button
            v-if="searchKeyword"
            @click="clearSearch"
            class="px-2 py-1 text-xs text-gray-400 hover:text-gray-600 border border-gray-200 rounded transition flex-shrink-0"
            title="清空搜索"
          >
            <span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M18 6 6 18" /> <path d="m6 6 12 12" /> </svg></span>
          </button>
        </div>
      </div>

      <!-- 历史列表（主流 IM 会话列表风格：头像 / 标题+时间 / 最新消息预览，圆角高亮） -->
      <div class="flex-1 overflow-y-auto p-2 space-y-1">
        <div v-if="filteredHistory.length === 0" class="px-3 py-4 text-center text-xs text-gray-400">
          {{ searchKeyword ? '未找到匹配记录' : '暂无历史对话' }}
        </div>
        <div
          v-for="item in filteredHistory"
          :key="item.id"
          class="group w-full rounded-xl px-3 py-2.5 cursor-pointer transition"
          :class="currentChatId === item.id ? 'bg-[#e8f3ff]' : 'hover:bg-gray-50'"
          @click="loadChat(item.id)"
        >
          <div class="flex items-center gap-1">
            <span class="truncate text-xs" :class="currentChatId === item.id ? 'font-semibold text-gray-800' : 'font-medium text-gray-600'">{{ item.title || '新对话' }}</span>
            <span class="ml-auto flex-shrink-0 text-[10px] text-gray-300 whitespace-nowrap">{{ formatTime(item.updatedAt || item.createdAt) }}</span>
          </div>
          <div class="flex items-center gap-1 mt-1">
            <span class="flex-1 truncate text-[11px] text-gray-400">{{ historyPreview(item) }}</span>
            <button
              @click.stop="deleteChat(item.id)"
              class="opacity-0 group-hover:opacity-100 flex-shrink-0 text-[10px] text-gray-300 hover:text-danger transition"
              title="删除这条对话"
            ><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M18 6 6 18" /> <path d="m6 6 12 12" /> </svg></span></button>
          </div>
        </div>
      </div>

      <!-- 底部统计 -->
      <div class="border-t border-gray-100 px-3 py-1.5 text-[10px] text-gray-400">
        共 {{ history.length }} 条对话
        <span v-if="searchKeyword" class="ml-2">(筛选后 {{ filteredHistory.length }} 条)</span>
      </div>
    </div>

    <!-- 收起态：一条窄的展开按钮，贴在左侧，点击恢复历史面板 -->
    <button
      v-else
      @click="historyCollapsed = false"
      class="flex-shrink-0 w-9 border border-gray-200 rounded-xl overflow-hidden flex flex-col items-center justify-start bg-white hover:bg-gray-50 transition"
      title="展开历史记录"
    >
      <span class="w-full py-2 flex items-center justify-center text-gray-400 hover:text-gray-600">
        <span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="m9 18 6-6-6-6" /> </svg></span>
      </span>
      <span class="pb-2 px-1 text-[10px] text-gray-400" style="writing-mode: vertical-rl; letter-spacing: 0.2em">历史记录</span>
    </button>

    <!-- ====== 右侧：聊天区域 ====== -->
    <div class="flex-1 min-w-0 border border-gray-200 rounded-xl overflow-hidden flex flex-col bg-white">
      <!-- 模型栏 -->
      <div class="flex items-center justify-between px-4 py-2 border-b border-gray-100 bg-gray-50/50 flex-shrink-0">
        <span class="text-[11px] text-gray-400">智能问析</span>
        <button
          @click="openModelDialog"
          class="flex items-center gap-1.5 text-[11px] px-2.5 py-1 bg-white border border-gray-200 rounded-full hover:border-primary/50 hover:text-primary transition group"
          title="切换 AI 模型"
        >
          <svg class="w-3 h-3 text-gray-400 group-hover:text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
          </svg>
          <span class="text-gray-600 group-hover:text-primary max-w-[140px] truncate">{{ currentModel }}</span>
          <span class="text-gray-300 group-hover:text-primary/50">切换</span>
        </button>
      </div>
      <!-- 对话区域 -->
      <div class="flex-1 overflow-y-auto p-4 space-y-4" ref="chatContainer">
        <!-- 空态（2026-09-27 重设计）：居中构图，品牌标 + 问候 + 三类场景卡 + 居中输入舱；
             发出第一条消息后本块让位给对话流，输入舱落回底部（见下方 v-if） -->
        <div v-if="currentMessages.length <= 1 && !isLoading" class="ask-empty">
          <div class="ask-empty-mark">V</div>
          <h2 class="ask-empty-title">今天想从数据里弄清什么？</h2>
          <p class="ask-empty-sub">用一句话问，答案带 <b>SQL、表格和出处</b>；口径拿不准，系统先跟你对齐再算。</p>

          <div class="ask-empty-scenes">
            <button class="ask-scene" @click="fillQuestion('全厂现在的良率怎么样？哪条产线最低？')">
              <span class="ask-scene-head">
                <span class="ask-scene-chip"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M3 3v16a2 2 0 0 0 2 2h16" /> <path d="M18 17V9" /> <path d="M13 17V5" /> <path d="M8 17v-3" /> </svg></span></span>
                <span class="ask-scene-tag">查现状</span>
              </span>
              <span class="ask-scene-q">全厂现在的良率怎么样？哪条产线最低？</span>
            </button>
            <button class="ask-scene" @click="fillQuestion('哪台设备停机时间最长？一共停了多久？')">
              <span class="ask-scene-head">
                <span class="ask-scene-chip"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M22 12h-4l-3 9L9 3l-3 9H2" /> </svg></span></span>
                <span class="ask-scene-tag">找异常</span>
              </span>
              <span class="ask-scene-q">哪台设备停机时间最长？一共停了多久？</span>
            </button>
            <button class="ask-scene" @click="fillQuestion('返工最多的产品是哪个？主要集中在哪道工序？')">
              <span class="ask-scene-head">
                <span class="ask-scene-chip"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <circle cx="11" cy="11" r="8" /> <path d="m21 21-4.3-4.3" /> </svg></span></span>
                <span class="ask-scene-tag">追原因</span>
              </span>
              <span class="ask-scene-q">返工最多的产品是哪个？主要集中在哪道工序？</span>
            </button>
          </div>

          <div class="ask-empty-composer">
            <input
              ref="askInputEl"
              v-model="inputText"
              @keydown.enter="onInputKeydown"
              type="text"
              placeholder="请输入分析问题，如：分析各工序的良率"
              class="ask-empty-input"
            />
            <button class="ask-empty-send" :disabled="!inputText.trim()" title="发送" @click="sendMessage">
              <span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" > <path d="M12 19V5" /> <path d="m5 12 7-7 7 7" /> </svg></span>
            </button>
          </div>

          <div class="ask-empty-hint">
            <span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M21 12a9 9 0 1 1-2.64-6.36" /> <polyline points="21 3 21 9 15 9" /> </svg></span>
            点上面的卡片直接填入提问，也可以换个问法自己输——历史会话在左侧，随时接着问
          </div>
        </div>
        <!-- 修复（P0）：原用 :key="idx"。消息数组会 splice（重新回答）/ push，索引 key 会让
             Vue 错位复用 DOM 节点：.echart 容器的"已渲染"标记是挂在 DOM 上的 →
             图表串位/空白，且 data-mid 下钻定位与导出 PNG 可能命中错消息。
             空态时仅剩的开场白气泡由居中引导块取代，不重复渲染。 -->
        <div v-if="currentMessages.length > 1 || isLoading"
             v-for="(msg, idx) in currentMessages" :key="msg._mid || idx"
             class="flex msg-item" :data-mid="msg._mid || ''"
             :class="msg.role === 'user' ? 'justify-end' : 'justify-start'">
          <div class="max-w-[85%]">
            <div class="text-xs text-gray-400 mb-1">{{ msg.role === 'user' ? '我' : 'AI 助手' }}</div>
            <div class="ask-bubble"
                 :class="msg.role === 'user' ? 'ask-bubble-user' : 'ask-bubble-ai'">
              {{ msg.content }}
            </div>
            <!-- 用户消息复制（2026-10-01）：贴气泡右下的迷你按钮，鼠标悬停消息行才显现，
                 默认透明但占位（避免悬停时布局跳动），与 AI 回答操作栏同一套胶囊按钮风格 -->
            <div v-if="msg.role === 'user'" class="ask-actions ask-actions-user">
              <button
                @click="copyUserMsg(msg)"
                class="ask-action-btn ask-action-btn-sm ask-user-copy"
                title="复制这条提问"
              ><span v-if="msg.copiedUser"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <polyline points="20 6 9 17 4 12" /> </svg></span></span><span v-else><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <rect width="14" height="14" x="8" y="8" rx="2" ry="2" /> <path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2" /> </svg></span></span>{{ msg.copiedUser ? ' 已复制' : ' 复制' }}</button>
            </div>
            <!-- ===== 思考过程（2026-10-03 调整位置：放在结果回答**上方**）=====
                 为什么挪上来：思考是「怎么得出这个答案的」，天然是结论的前置说明。
                 放在结果下方时用户先看到结论、再往下翻才能看到依据，
                 反而像"补充说明"；放上方则是"先看推理、再看结论"，
                 与 DeepSeek / ChatGPT 的对话阅读顺序一致。
                 样式也一并改成 DeepSeek 风格：灰底圆角块、低调标题、浅灰正文、无闪烁。 -->
            <div v-if="msg.thinking" class="ask-collapse">
              <div class="ask-collapse-head" @click="msg.thinkingOpen = !msg.thinkingOpen">
                <span class="ask-collapse-mark"><span v-if="msg.thinkingStreaming" class="ask-collapse-spin"></span><span v-else class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M12 2a10 10 0 1 0 10 10" /></svg></span></span>
                <span class="ask-collapse-name">{{ msg.thinkingStreaming ? '正在思考' : '已思考' }}</span>
                <span class="ask-collapse-state">{{ msg.thinkingStreaming ? '' : (msg.thinkingOpen === true ? '收起' : '展开') }}</span>
              </div>
              <div v-show="msg.thinkingStreaming || msg.thinkingOpen === true" class="ask-collapse-body tk-panel">
                <div v-html="formatThinking(msg.thinking)"></div>
              </div>
            </div>

            <!-- ===== 结果回答（最显眼：用户第一眼看到结论，溯源类内容在下方默认折叠）===== -->
            <div v-if="msg.result" class="ask-result-card">
              <div class="ask-result-head">
                <span class="ask-result-title">
                  <span class="ask-result-title-dot"></span>
                  <span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <line x1="18" x2="18" y1="20" y2="10" /> <line x1="12" x2="12" y1="20" y2="4" /> <line x1="6" x2="6" y1="20" y2="14" /> </svg></span> 结果回答
                </span>
                <span v-if="msg.thinkingStreaming" class="ask-result-status">生成中</span>
                <span v-else class="ask-result-status ask-result-status-done">已生成</span>
              </div>
              <div v-html="msg.result" class="ask-result-body" @click="onResultClick"></div>
            </div>

            <!-- 报告卡片：这份回答的产出就是一份报告，卡片常驻在回答下面。
                 预览弹窗关掉之后从这里还能再打开（报告 HTML 挂在消息对象上，不会丢），
                 下载按钮也留在这儿，不用先开预览再下载。 -->
            <div v-if="msg.queryType === 'report' && (msg.reportHtml || msg.reportId)"
                 class="mt-2 flex items-center gap-3 px-3.5 py-3 rounded-lg border border-blue-200 bg-gradient-to-r from-blue-50/80 to-white">
              <span class="text-xl shrink-0"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <line x1="18" x2="18" y1="20" y2="10" /> <line x1="12" x2="12" y1="20" y2="4" /> <line x1="6" x2="6" y1="20" y2="14" /> </svg></span></span>
              <div class="flex-1 min-w-0">
                <div class="text-[13px] font-medium text-gray-800 truncate">{{ msg.reportTitle || '分析报告' }}</div>
                <div class="text-[11px] text-gray-400 mt-0.5">数据来自数据库实时查询（口径 SQL 见报告附录）</div>
              </div>
              <div class="flex items-center gap-2 shrink-0">
                <button
                  @click="openReport(msg)"
                  class="px-3 py-1.5 text-xs text-white rounded-lg transition hover:opacity-90"
                  style="background: linear-gradient(135deg,#2E7CF0,#1F66D6);"
                >查看报告</button>
                <button
                  @click="downloadReport('docx', msg)"
                  class="px-3 py-1.5 text-xs rounded-lg bg-white border border-blue-200 text-blue-600 hover:bg-blue-50 transition"
                >Word</button>
                <button
                  @click="downloadReport('pdf', msg)"
                  class="px-3 py-1.5 text-xs rounded-lg bg-white border border-blue-200 text-blue-600 hover:bg-blue-50 transition"
                >PDF</button>
              </div>
            </div>

            <!-- 思考过程已上移到「结果回答」上方（见本文件上方 ask-collapse 块） -->
            <!-- SQL 语句：独立深色卡片；默认折叠（溯源类不抢结果版面），点击表头展开 -->
            <!-- SQL 语句：独立深色卡片；默认折叠（溯源类不抢结果版面），点击表头展开 -->
            <div v-if="msg.sql" class="ask-sql-card">
              <div class="ask-sql-head" @click="msg.sqlOpen = !msg.sqlOpen">
                <span class="ask-sql-title">
                  <span class="ask-sql-dot"></span>
                  SQL 查询语句
                  <span class="ask-sql-state">{{ msg.sqlOpen === true ? '收起' : '展开' }}</span>
                </span>
                <button
                  @click.stop="copySql(msg)"
                  class="ask-sql-copy"
                  :title="'复制 SQL'"
                ><span v-if="msg.copiedSql"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <polyline points="20 6 9 17 4 12" /> </svg></span></span><span v-else><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <rect width="14" height="14" x="8" y="8" rx="2" ry="2" /> <path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2" /> </svg></span></span>{{ msg.copiedSql ? ' 已复制' : ' 复制' }}</button>
              </div>
              <pre v-show="msg.sqlOpen === true" class="sql-code">{{ msg.sql }}</pre>
            </div>

            <!-- 结果溯源（血缘）：标注每个数字来自哪张表哪个字段（确定性解析，非 LLM 现编） -->
            <div v-if="msg.lineage && msg.lineage.columns && msg.lineage.columns.length"
                 class="ask-collapse">
              <div class="ask-collapse-head" @click="msg.lineageOpen = !msg.lineageOpen">
                <span class="ask-collapse-mark"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M3 3v16a2 2 0 0 0 2 2h16" /> <path d="M18 17V9" /> <path d="M13 17V5" /> <path d="M8 17v-3" /> </svg></span></span>
                <span class="ask-collapse-name">数据溯源（血缘）</span>
                <span class="ask-collapse-state">{{ msg.lineageOpen === true ? '收起' : '展开' }}</span>
              </div>
              <div v-show="msg.lineageOpen === true" class="ask-collapse-body">
                <div v-if="msg.lineage.tables && msg.lineage.tables.length" class="mb-2 flex flex-wrap gap-1.5">
                  <span v-for="(t, ti) in msg.lineage.tables" :key="'lg-t-' + ti"
                        class="px-2 py-0.5 rounded-full bg-blue-50 text-blue-700 border border-blue-100">
                    {{ t.label || t.name }}<span class="text-blue-400"> · {{ t.name }}</span>
                  </span>
                </div>
                <table class="w-full text-left">
                  <thead>
                    <tr class="text-gray-400 border-b border-gray-100">
                      <th class="py-1 pr-2 font-normal">结果列</th>
                      <th class="py-1 pr-2 font-normal">来源</th>
                      <th class="py-1 font-normal">计算</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="(c, ci) in msg.lineage.columns" :key="ci" class="border-b border-gray-50 last:border-0">
                      <td class="py-1 pr-2 font-medium text-gray-700 whitespace-nowrap">{{ c.output }}</td>
                      <td class="py-1 pr-2 text-gray-500">
                        <template v-if="c.table">{{ c.table_label || c.table }}<span class="text-gray-300"> · </span><code class="text-blue-700">{{ c.table }}.{{ c.field }}</code></template>
                        <template v-else><span class="text-gray-400">{{ c.field === '*' ? '全表计数' : (c.field || '常量/表达式') }}</span></template>
                        <div v-if="c.field_desc" class="text-[10.5px] text-gray-400">{{ c.field_desc }}</div>
                      </td>
                      <td class="py-1 text-gray-500">
                        <code v-if="c.chain" class="text-[10.5px] text-indigo-600 bg-indigo-50 px-1.5 py-0.5 rounded font-mono break-all">{{ c.chain }}</code>
                        <span v-else-if="c.agg" class="px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-600 text-[10.5px] whitespace-nowrap">{{ c.agg }}</span>
                        <span v-else class="text-gray-400">—</span>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>

            <!-- 推理过程 Show Work（P1-5，对标 ThoughtSpot Show Work）：
                 不仅展示"做了什么"，还展示每一步的输入/产出/依据，回答"为什么查这张表、
                 为什么这么算"。默认折叠，避免长流程占版面。 -->
            <div v-if="(msg.steps || []).length" class="ask-collapse">
              <div class="ask-collapse-head" @click="msg.stepsOpen = !msg.stepsOpen">
                <span class="ask-collapse-mark"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2" /> <rect x="9" y="3" width="6" height="4" rx="2" /> <path d="m9 14 2 2 4-4" /> </svg></span></span>
                <span class="ask-collapse-name">推理过程（Show Work）</span>
                <span class="ask-collapse-state">{{ (msg.steps || []).length }} 步 · {{ msg.stepsOpen === true ? '收起' : '展开' }}</span>
              </div>
              <div v-show="msg.stepsOpen === true" class="ask-collapse-body">
                <div v-for="(st, si) in msg.steps" :key="si" class="py-1.5">
                  <div class="flex items-baseline gap-2 flex-wrap">
                    <span class="text-[10px] text-slate-400 w-3 shrink-0">{{ st.step || si + 1 }}</span>
                    <span class="text-[11.5px] font-medium text-slate-700">{{ st.name }}</span>
                    <span v-if="st.duration_ms != null" class="text-[10px] text-slate-400">{{ st.duration_ms }}ms</span>
                  </div>
                  <div class="text-[11px] text-slate-500 ml-5">{{ st.detail }}</div>
                  <div v-if="st.evidence" class="ml-5 mt-1 space-y-0.5">
                    <div v-if="st.evidence.input" class="text-[10.5px] leading-relaxed">
                      <span class="text-slate-400">输入：</span>
                      <span class="text-slate-600 whitespace-pre-wrap break-all">{{ st.evidence.input }}</span>
                    </div>
                    <div v-if="st.evidence.output" class="text-[10.5px] leading-relaxed">
                      <span class="text-slate-400">产出：</span>
                      <span class="text-slate-600">{{ st.evidence.output }}</span>
                    </div>
                    <div v-if="st.evidence.basis" class="text-[10.5px] leading-relaxed">
                      <span class="text-slate-400">依据：</span>
                      <span class="text-indigo-600">{{ st.evidence.basis }}</span>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <!-- 结果可靠性：规则告警 + 评价 Agent 质量分（P0-2 第四环） -->
            <div class="mt-2 flex items-center gap-2 flex-wrap">
              <div v-if="msg.quality && msg.quality.warning" class="flex-1 min-w-0 px-3 py-2 bg-amber-50 border border-amber-200 rounded-lg text-xs text-amber-700 leading-relaxed">
                <span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" /> <path d="M12 9v4" /> <path d="M12 17h.01" /> </svg></span> {{ msg.quality.warning }}
              </div>
              <span
                v-if="msg.quality && msg.quality.confidence"
                class="px-2.5 py-1 rounded-full border text-[11px] whitespace-nowrap cursor-help"
                :class="confidenceClass(msg.quality.confidence.level)"
                :title="confidenceTitle(msg.quality.confidence)"
              >
                {{ confidenceLabel(msg.quality.confidence.level) }} {{ msg.quality.confidence.score }}
                <span v-if="(msg.quality.confidence.risks || []).length" class="opacity-70">· 有提示</span>
              </span>
              <span
                v-else-if="msg.quality && msg.quality.score"
                class="px-2.5 py-1 rounded-full border text-[11px] whitespace-nowrap"
                :class="msg.quality.score >= 80
                  ? 'bg-blue-50 border-blue-200 text-blue-700'
                  : (msg.quality.score >= 60 ? 'bg-amber-50 border-amber-200 text-amber-700' : 'bg-rose-50 border-rose-200 text-rose-700')"
                :title="(msg.quality.evaluation?.comment || '评价 Agent 对结果质量的打分（0-100）')
                  + (msg.quality.cross_validated ? ' · 已通过多候选交叉比对择优' : '')"
              >
                质量 {{ msg.quality.score }}
                <span v-if="msg.quality.cross_validated" class="opacity-70">· 已择优</span>
              </span>
            </div>

            <!-- 引用文档（P0-A 混合问答，对标 Spotter 3）：[n] 可点开核实原文 -->
            <div v-if="msg.citedDocs && msg.citedDocs.length" class="ask-collapse">
              <div class="ask-collapse-head" @click="msg.citedDocsOpen = !msg.citedDocsOpen">
                <span class="ask-collapse-mark"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /> <polyline points="14 2 14 8 20 8" /> </svg></span></span>
                <span class="ask-collapse-name">引用文档（{{ msg.citedDocs.length }}）</span>
                <span class="ask-collapse-state">{{ msg.citedDocsOpen === false ? '展开' : '收起' }}</span>
              </div>
              <div v-show="msg.citedDocsOpen !== false" class="ask-collapse-body">
                <div v-for="d in msg.citedDocs" :key="'cd-' + d.index"
                     class="text-[11.5px] text-slate-600 leading-relaxed py-0.5">
                  <span class="inline-block mr-1 px-1 rounded bg-blue-50 text-blue-600 text-[10px] font-mono">[{{ d.index }}]</span>
                  {{ d.content }}
                </div>
              </div>
            </div>

            <!-- 口径歧义澄清卡（P1 口径管理） -->
            <MetricClarifyCard
              v-if="msg.metricClarify"
              :answer="msg.metricClarify.answer"
              :hits="msg.metricClarify.hits"
              @pick="(name) => resendWithMetric(name, msg.metricClarify!.query)"
              @custom="openDefine(msg.metricClarify!.hits[0]?.name || '', msg.metricClarify!.query)"
            />

            <!-- 澄清入口条：弹窗被取消后仍可重新打开，不丢入口 -->
            <div
              v-if="msg.analysisConfirm && !msg.analysisDismissed"
              class="mt-2 px-3 py-2.5 bg-indigo-50 border border-indigo-200 rounded-lg text-xs text-indigo-700 leading-relaxed"
            >
              <div class="flex items-start gap-2">
                <span class="mt-0.5"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5" /> <path d="M9 18h6" /> <path d="M10 22h4" /> </svg></span></span>
                <div class="flex-1 min-w-0">
                  <div>这个结果由 AI 生成，口径可能不准；登记正确口径后，再问同样的问题会直接按它计算。</div>
                  <div class="mt-1.5 flex items-center gap-2">
                    <button
                      @click="openDefine((msg.analysisConfirm?.hints || [])[0] || '', msg.analysisConfirm?.query || '')"
                      class="px-2.5 py-1 bg-indigo-600 text-white rounded-full hover:bg-indigo-700 transition"
                    >去登记口径</button>
                    <button
                      @click="msg.analysisDismissed = true"
                      class="px-2 py-1 text-indigo-500 hover:bg-indigo-100 rounded-full transition"
                    >忽略</button>
                  </div>
                </div>
              </div>
            </div>

            <!-- 回答操作栏：复制回答 / 重新回答 / 反馈闭环 -->
            <div v-if="msg.role === 'assistant' && !(msg.thinkingStreaming && isLoading)"
                 class="ask-actions">
              <button
                @click="copyAnswer(msg)"
                class="ask-action-btn"
                title="复制本条回答（含 SQL 与分析文本）"
              ><span v-if="msg.copiedAnswer"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <polyline points="20 6 9 17 4 12" /> </svg></span></span><span v-else><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <rect width="14" height="14" x="8" y="8" rx="2" ry="2" /> <path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2" /> </svg></span></span>{{ msg.copiedAnswer ? ' 已复制' : ' 复制' }}</button>
              <button
                @click="regenerate(idx)"
                :disabled="isLoading"
                class="ask-action-btn"
                title="重新生成本轮回答（将清除本轮之后的对话）"
              >↻ 重新回答</button>

              <!-- 提问即报告：报告随答复自动弹预览；关掉之后从这里、或回答下方的报告卡片
                   都能再打开（报告 HTML 挂在消息上），不用重新提问一句 -->
              <button
                v-if="msg.queryType === 'report' && (msg.reportHtml || msg.reportId)"
                @click="openReport(msg)"
                class="ask-action-btn ask-action-btn-primary"
                title="再打开这份报告（含 Word / PDF 下载）"
              ><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <line x1="18" x2="18" y1="20" y2="10" /> <line x1="12" x2="12" y1="20" y2="4" /> <line x1="6" x2="6" y1="20" y2="14" /> </svg></span> 打开报告</button>

              <!-- 数据查询反馈闭环：正确 SQL 沉淀为记忆，错误标记不学习 -->
              <template v-if="msg.queryType === 'data_query' && msg.sql">
                <span class="ask-actions-sep"></span>
                <span class="ask-actions-label">结果是否有帮助？</span>
                <button
                  v-if="!msg.feedbackGiven"
                  @click="sendFeedback(msg, true)"
                  class="ask-action-btn ask-action-btn-good"
                ><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M7 10v12" /> <path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z" /> </svg></span> 结果正确</button>
                <button
                  v-if="!msg.feedbackGiven"
                  @click="sendFeedback(msg, false)"
                  class="ask-action-btn ask-action-btn-bad"
                ><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M17 14V2" /> <path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z" /> </svg></span> 结果有误</button>
                <button
                  @click="openFeedbackDialog(msg)"
                  class="ask-action-btn ask-action-btn-warn"
                ><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" /> <path d="M12 9v4" /> <path d="M12 17h.01" /> </svg></span> 纠错反馈</button>
                <!-- P0-3 答案导出：Markdown（表格+结论+SQL）/ PNG（图表） -->
                <span class="ask-actions-sep"></span>
                <button
                  @click="exportMarkdown(msg)"
                  class="ask-action-btn ask-action-btn-ghost"
                  title="导出 Markdown（问题+SQL+数据表格+分析结论）"
                ><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z" /> <polyline points="14 2 14 8 20 8" /> <line x1="16" x2="8" y1="13" y2="13" /> <line x1="16" x2="8" y1="17" y2="17" /> <line x1="10" x2="8" y1="9" y2="9" /> </svg></span> MD</button>
                <button
                  v-if="msg.chartSvg"
                  @click="exportPng(msg)"
                  class="ask-action-btn ask-action-btn-ghost"
                  title="导出图表为 PNG 图片"
                ><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <rect width="18" height="18" x="3" y="3" rx="2" ry="2" /> <circle cx="9" cy="9" r="2" /> <path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21" /> </svg></span> PNG</button>
                <!-- 报告消息不走这块数据查询操作栏（报告没有 SQL 结果行），
                     重新打开报告的入口在「重新回答」旁边，以及回答下方的报告卡片 -->
                <!-- P1-2 图表类型切换：复用已有结果数据双引擎重渲染，不重跑 SQL -->
                <select
                  v-if="hasChartData(msg)"
                  :value="msg.chartTypeOverride || msg.chartType || ''"
                  @change="switchChartType(msg, ($event.target as HTMLSelectElement).value)"
                  class="ask-chart-select"
                  title="切换图表类型（不重新查询数据）"
                >
                  <option value="" disabled>切换图型…</option>
                  <option v-for="t in chartTypeOptions" :key="t.v" :value="t.v"
                          :disabled="!chartTypeRenderable(msg, t.v)"
                          :title="chartTypeDisabledTip(msg, t.v)">{{ t.label }}</option>
                </select>
                <span v-if="msg.feedbackGiven" class="ask-feedback-done">{{ msg.feedbackText }}</span>
              </template>
            </div>
          </div>
        </div>

        <!-- 口径定义弹窗（P1 口径管理）：组件自包含完整弹窗 -->
        <MetricDefineCard
          :visible="showDefineCard"
          :prefill-name="definePrefill"
          @cancel="showDefineCard = false"
          @saved="onDefineSaved"
        />

        <!-- 报告预览弹窗：整份报告是后端返回的单文件 HTML，用 srcdoc 直接渲染，
             不落临时文件、不走下载，用户看完再决定要不要导出 -->
        <div v-if="reportOpen" class="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4"
             @click.self="closeReport">
          <div class="bg-white rounded-xl w-full max-w-[980px] h-[92vh] flex flex-col shadow-2xl overflow-hidden">
            <div class="flex items-center justify-between px-5 py-3 border-b border-gray-100 shrink-0">
              <div class="min-w-0">
                <h3 class="text-sm font-bold text-gray-800 truncate"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <line x1="18" x2="18" y1="20" y2="10" /> <line x1="12" x2="12" y1="20" y2="4" /> <line x1="6" x2="6" y1="20" y2="14" /> </svg></span> {{ reportTitle || '分析报告' }}</h3>
                <p class="text-[11px] text-gray-400 mt-0.5">报告数据来自数据库实时查询（口径 SQL 见报告附录），可直接导出 Word / PDF</p>
              </div>
              <div class="flex items-center gap-2 shrink-0">
                <button @click="openReportInTab"
                        class="px-3 py-1.5 text-xs bg-gray-100 hover:bg-gray-200 rounded-lg text-gray-600">在新标签打开</button>
                <button @click="downloadReport('docx')"
                        class="px-3 py-1.5 text-xs bg-blue-50 hover:bg-blue-100 rounded-lg text-blue-600">下载 Word</button>
                <button @click="downloadReport('pdf')"
                        class="px-3 py-1.5 text-xs bg-blue-50 hover:bg-blue-100 rounded-lg text-blue-600">下载 PDF</button>
                <button @click="closeReport"
                        class="px-3 py-1.5 text-xs text-gray-500 hover:bg-gray-100 rounded-lg">关闭</button>
              </div>
            </div>
            <iframe :srcdoc="reportHtml" class="flex-1 w-full bg-gray-50" sandbox="allow-same-origin"></iframe>
          </div>
        </div>

        <!-- 纠错反馈弹窗：用户标注错误类型 + 说明 → 进入复核队列 -->
        <div v-if="feedbackTarget" class="fixed inset-0 bg-black/30 flex items-center justify-center z-50" @click.self="feedbackTarget = null">
          <div class="bg-white rounded-xl p-5 w-[440px] shadow-xl">
            <h3 class="text-sm font-bold text-gray-800 mb-1"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" /> <path d="M12 9v4" /> <path d="M12 17h.01" /> </svg></span> 纠错反馈</h3>
            <p class="text-xs text-gray-400 mb-3">你的反馈会进入复核队列，管理员修正口径后系统持续改进</p>
            <div class="text-xs mb-3">
              <span class="text-gray-500">问题：</span>
              <span class="text-gray-700">{{ feedbackTarget.query }}</span>
            </div>
            <label class="block text-xs mb-3">
              <span class="text-gray-500">问题类型 *</span>
              <select v-model="feedbackType" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg text-xs bg-white">
                <option v-for="t in feedbackTypes" :key="t" :value="t">{{ t }}</option>
              </select>
            </label>
            <label class="block text-xs mb-4">
              <span class="text-gray-500">补充说明（可选）</span>
              <textarea v-model="feedbackNote" rows="3" class="mt-1 w-full px-2 py-1.5 border border-gray-300 rounded-lg text-xs" placeholder="例如：良率的口径应该是合格数/投入数×100%，而不是……"></textarea>
            </label>
            <div class="flex justify-end gap-2">
              <button @click="feedbackTarget = null" class="px-3 py-1.5 text-xs bg-gray-100 hover:bg-gray-200 rounded-lg">取消</button>
              <button @click="submitFeedbackReport" :disabled="feedbackSubmitting" class="px-3 py-1.5 text-xs text-white bg-amber-600 hover:bg-amber-700 rounded-lg disabled:opacity-50">
                {{ feedbackSubmitting ? '提交中…' : '提交反馈' }}
              </button>
            </div>
          </div>
        </div>

        <!-- 澄清弹窗：未定义口径/模糊/复杂查询 → 只问一句话，用户自然语言补充后由 AI 识别 -->
        <AnalysisConfirmDialog
          ref="analysisDialogRef"
          :visible="showAnalysisConfirm"
          :query="analysisConfirm?.query || ''"
          :reason="analysisConfirm?.reason || 'no_hit'"
          :hints="analysisConfirm?.hints || []"
          :analysis="analysisConfirm?.analysis || null"
          :error="analysisConfirm?.error || ''"
          :gen-error="analysisConfirm?.genError || ''"
          :inferring="inferringGen"
          @clarify="handleClarify"
          @auto="handleAutoQuery"
          @define="onAnalysisDefine"
          @cancel="showAnalysisConfirm = false"
        />

        <!-- 加载状态 -->
        <div v-if="isLoading" class="flex justify-start">
          <div class="ask-bubble ask-bubble-ai ask-bubble-loading">
            <span class="inline-block animate-pulse">{{ streamingStep || '思考中' }}…</span>
          </div>
        </div>
      </div>

      <!-- 输入区（悬浮式输入舱）：对话开始后落回底部；空态时由上方居中输入舱接管 -->
      <div v-if="currentMessages.length > 1 || isLoading" class="ask-composer-wrap flex-shrink-0">
        <div class="ask-composer">
        <div class="flex gap-3">
          <input
            ref="askInputEl"
            v-model="inputText"
            @keydown.enter="onInputKeydown"
            type="text"
            placeholder="请输入分析问题，如：分析各工序的良率"
            class="flex-1 px-4 py-2.5 border border-gray-300 rounded-xl text-sm focus:outline-none focus:border-primary focus:ring-2 focus:ring-primary/20"
          />
          <button
            v-if="!isLoading"
            @click="sendMessage"
            :disabled="!inputText.trim()"
            class="ask-send-btn px-6 py-2.5 text-white rounded-xl text-sm font-semibold transition disabled:opacity-40 disabled:cursor-not-allowed"
          >
            发送
          </button>
          <button
            v-else
            @click="stopGenerating"
            class="px-6 py-2.5 bg-red-500 text-white rounded-lg text-sm font-medium hover:bg-red-600 transition flex items-center gap-1.5"
          >
            <span class="inline-block w-2 h-2 bg-white rounded-sm"></span>
            暂停
          </button>
        </div>
        </div>
      </div>
    </div>

    <!-- ====== 模型配置弹窗 ====== -->
    <ModalDialog :visible="modelDialogVisible" title="切换 AI 模型" width="520px" @close="modelDialogVisible = false">
      <!-- 模型服务：动态加载后端供应商（含 Ollama 本地），选择后自动填入 API 地址与示例模型 -->
      <div class="mb-4">
        <div class="text-xs text-gray-500 mb-2">模型服务（选择后自动填入 API 地址与示例模型，Key 请自行填写）</div>
        <select
          v-model="selectedProvider"
          @change="onProviderChange"
          class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-primary/50"
        >
          <option v-for="p in providerOptions" :key="p.name" :value="p.name">{{ p.label }}</option>
        </select>
      </div>

      <!-- 表单 -->
      <div class="space-y-3">
        <div>
          <label class="text-xs text-gray-500 block mb-1">模型名称 (model)</label>
          <input v-model="llmForm.model" type="text" :placeholder="curProvider?.example_model ? `例如 ${curProvider.example_model}` : '例如 deepseek-v4-flash / gpt-5.2 / glm-4.6'"
                 class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-primary/50" />
        </div>
        <div>
          <label class="text-xs text-gray-500 block mb-1">API 地址 (base_url)</label>
          <input v-model="llmForm.base_url" type="text" placeholder="https://api.deepseek.com/v1"
                 class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-primary/50" />
        </div>
        <div>
          <label class="text-xs text-gray-500 block mb-1">API Key{{ curProvider?.local ? ' · Ollama 可留空' : ' · 请自行填写' }}</label>
          <input v-model="llmForm.api_key" type="password" placeholder="sk-... / 各家密钥"
                 class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-primary/50" />
        </div>
        <div>
          <div class="flex items-center justify-between">
            <label class="text-xs text-gray-500 block mb-1">生成温度 (temperature)</label>
            <span v-if="curProvider?.force_1" class="text-[11px] text-amber-500">Kimi 仅支持 1（已锁定）</span>
          </div>
          <input
            v-model.number="llmForm.temperature"
            type="number" min="0" max="2" step="0.1"
            :disabled="curProvider?.force_1"
            class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-primary/50 disabled:bg-gray-100 disabled:text-gray-400"
          />
          <p class="text-[11px] text-gray-400 mt-1">温度越低，生成的 SQL 越稳定（NL2SQL 建议 0–0.3）；越高越发散。范围 0–2。</p>
        </div>
        <div>
          <label class="text-xs text-gray-500 block mb-1">最大 Token (max_tokens)</label>
          <input
            v-model.number="llmForm.max_tokens"
            type="number" min="256" max="32768" step="256"
            class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-primary/50"
          />
          <p class="text-[11px] text-gray-400 mt-1">单次生成的最大输出长度，范围 256–32768（默认 8192）。</p>
        </div>
      </div>

      <!-- 提示信息 -->
      <div v-if="llmMessage" class="mt-3 text-xs"
           :class="llmSuccess ? 'text-blue-600' : 'text-red-500'">
        {{ llmMessage }}
      </div>

      <!-- 恢复默认 -->
      <div class="flex items-center justify-between mt-4 mb-1">
        <span class="text-xs text-gray-400">系统默认：{{ llmDefault.model }}</span>
        <button
          @click="restoreDefault"
          :disabled="llmRestoring"
          class="px-3 py-1.5 border border-gray-200 text-gray-500 rounded-lg text-xs hover:bg-gray-50 transition disabled:opacity-50"
        >
          {{ llmRestoring ? '恢复中...' : '↺ 恢复默认' }}
        </button>
      </div>

      <!-- 操作按钮 -->
      <div class="flex items-center gap-2 mt-4">
        <button
          @click="testLlmConfig"
          :disabled="llmTesting"
          class="flex-1 px-4 py-2 border border-gray-200 text-gray-600 rounded-lg text-sm hover:bg-gray-50 transition disabled:opacity-50"
        >
          {{ llmTesting ? '测试中...' : '测试连接' }}
        </button>
        <button
          @click="saveLlmConfig"
          :disabled="llmSaving"
          class="flex-1 px-4 py-2 bg-gradient-to-r from-primary to-primary-dark text-white rounded-lg text-sm font-medium hover:opacity-90 transition disabled:opacity-50"
        >
          {{ llmSaving ? '保存中...' : '保存并启用' }}
        </button>
      </div>
    </ModalDialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onActivated, onDeactivated, onUnmounted, nextTick, watch, inject } from 'vue'
// 图表渲染：ECharts（常规图）+ AntV G2Plot（扩展图）双引擎统一入口
import { renderChart, disposeChart, resizeChart, coerceRenderableType, downgradeChartType } from '../charts'
import echarts from '../echarts'
import ModalDialog from '../components/ModalDialog.vue'
import MetricClarifyCard from '../components/MetricClarifyCard.vue'
import MetricDefineCard from '../components/MetricDefineCard.vue'
import AnalysisConfirmDialog from '../components/AnalysisConfirmDialog.vue'
import { getUser } from '../auth'
import { pruneLocalStorage } from '../storage'
// ========== 模型配置 ==========
const modelDialogVisible = ref(false)
const currentModel = ref('deepseek-v4-flash')
const llmTesting = ref(false)
const llmSaving = ref(false)
const llmMessage = ref('')
const llmSuccess = ref(false)
const llmRestoring = ref(false)
const llmDefault = reactive({
  model: 'deepseek-v4-flash',
  api_key: '',
  base_url: 'https://api.deepseek.com/v1',
  temperature: 0.2,
  max_tokens: 8192,
})
// 模型名称与 API Key 默认空着，由用户自己填写；系统只预置各厂商的 base_url
const llmForm = reactive({
  model: '',
  api_key: '',
  base_url: 'https://api.deepseek.com/v1',
  temperature: 0.2,
  max_tokens: 8192,
})

// 模型服务：优先从后端 /api/llm/providers 动态加载（含 Ollama 本地模型等新供应商），
// 加载失败时回退到内置列表（保证功能可用）。
// 2026-10-07：保存/测试/恢复默认已放开为「**只需登录**」（后端从
// require_roles("admin") 改为 require_login()），任何账号都能自己换模型。
// 前端原有的 canManageLlm（= isAdmin()）连同按钮 disabled / title 三元、
// 「仅管理员可修改（当前只读）」提示、非管理员只读分支**已一并删除**——
// 放开权限后它们恒为真或不可达，留着是误导性死代码。
// ⚠ 配置是**全局单份**（后端 config.update_llm_config 写 .env，全进程共享）：
//    任一账号保存后对**所有账号**生效。这是本次的既定取舍（赶时间，优先方便）。
//    若日后要按账号隔离，须后端按 username 存配置、问数链路按当前用户读取，
//    单纯在前端加回判断没用。
interface ProviderOpt {
  name: string
  label: string
  base_url: string
  example_model?: string
  local?: boolean
  force_1?: boolean   // Kimi 等模型强制 temperature=1（后端 temperature 规则 force_1）
  def_temp?: number   // 选择该供应商时的默认温度建议
}
const FALLBACK_PROVIDERS: ProviderOpt[] = [
  // example_model 均为各厂商 2026 年当前使用人数最多的官方模型（旧名已退役的不再使用）
  { name: 'deepseek', label: 'DeepSeek（系统默认）', base_url: 'https://api.deepseek.com/v1', example_model: 'deepseek-v4-flash', def_temp: 0.2 },
  { name: 'openai', label: 'OpenAI', base_url: 'https://api.openai.com/v1', example_model: 'gpt-5.2', def_temp: 0.2 },
  { name: 'qwen', label: '通义千问', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', example_model: 'qwen-plus', def_temp: 0.2 },
  { name: 'zhipu', label: '智谱 GLM', base_url: 'https://open.bigmodel.cn/api/paas/v4', example_model: 'glm-4.6', def_temp: 0.2 },
  { name: 'kimi', label: 'Kimi (Moonshot)', base_url: 'https://api.moonshot.cn/v1', example_model: 'kimi-k3', def_temp: 1, force_1: true },
  { name: 'ollama', label: 'Ollama（本地模型）', base_url: 'http://localhost:11434/v1', example_model: 'qwen2.5:7b', def_temp: 0.2, local: true },
  { name: 'custom', label: '自定义 / 其他', base_url: '', def_temp: 0.2 },
]
const providerOptions = ref<ProviderOpt[]>([])
const selectedProvider = ref('deepseek')
const curProvider = computed(() => providerOptions.value.find(p => p.name === selectedProvider.value))

// 从后端拉取供应商注册表（含 base_url / 示例模型 / 本地标记 / 温度规则）
const loadProviders = async () => {
  try {
    const res = await fetch('/api/llm/providers')
    const data = await res.json()
    const list = (data.providers || []) as any[]
    if (list.length) {
      providerOptions.value = [
        ...list.map((p: any) => ({
          name: p.name,
          label: p.label + (p.local ? '（本地，无需 API Key）' : ''),
          base_url: p.base_url,
          example_model: p.example_model,
          local: p.local,
          force_1: p.temperature === 'force_1',
          def_temp: p.temperature === 'force_1' ? 1 : 0.2,
        })),
        { name: 'custom', label: '自定义 / 其他', base_url: '', def_temp: 0.2 },
      ]
      return
    }
  } catch (e) {
    console.warn('加载 LLM 供应商失败，使用内置列表', e)
  }
  providerOptions.value = FALLBACK_PROVIDERS
}

const openModelDialog = async () => {
  llmMessage.value = ''
  modelDialogVisible.value = true
  // 先加载供应商列表再加载当前配置：loadLlmConfig 需要根据 base_url 反推供应商
  //（并行执行会在列表为空时误判为"自定义"）
  await loadProviders()
  await loadLlmConfig()
  // 2026-10-07：模型配置已放开为「只需登录」，不再有只读态。
  // 原逻辑：非管理员时提示「仅管理员可修改/测试（当前只读）」——
  //   canManageLlm 现恒为 true，该分支自然不再进入，但文案会误导，故一并移除。
}

// 选择服务时帮用户写好 base_url 与示例模型，并按厂商适配温度（Kimi 需 temperature=1）
const onProviderChange = () => {
  const p = providerOptions.value.find((x: any) => x.name === selectedProvider.value)
  if (p) {
    llmForm.base_url = p.base_url
    if (p.example_model) llmForm.model = p.example_model
    llmForm.temperature = p.def_temp ?? 0.1
  } else {
    llmForm.base_url = ''
  }
}

const loadLlmConfig = async () => {
  try {
    const res = await fetch('/api/llm/config')
    const data = await res.json()
    if (data.success) {
      // 仅据 base_url 预置服务下拉；model 与 api_key 默认空着，由用户自行填写
      llmForm.base_url = data.config.base_url || ''
      llmForm.model = ''
      llmForm.api_key = ''
      llmForm.temperature = data.config.temperature
      llmForm.max_tokens = data.config.max_tokens
      const hit = providerOptions.value.find((p: any) => p.base_url && p.base_url === llmForm.base_url)
      selectedProvider.value = hit ? hit.name : 'custom'
      // 按服务适配温度：Kimi 等模型强制 temperature=1，否则会报“只允许1”
      if (hit) llmForm.temperature = hit.def_temp ?? 0.1
      // 顶部模型栏展示后端当前生效的模型名（此前被置空导致刷新后标题栏模型名变空白）
      currentModel.value = data.config.model || currentModel.value
      // 同时保存冻结的“系统内置默认”，供“恢复默认”按钮使用
      if (data.default) {
        llmDefault.model = data.default.model
        llmDefault.api_key = data.default.api_key
        llmDefault.base_url = data.default.base_url
        llmDefault.temperature = data.default.temperature
        llmDefault.max_tokens = data.default.max_tokens
      }
    }
  } catch (e) {
    console.warn('加载模型配置失败:', e)
  }
}

const testLlmConfig = async () => {
  llmTesting.value = true
  llmMessage.value = ''
  try {
    const res = await fetch('/api/llm/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...llmForm, test_only: true }),
    })
    const data = await res.json().catch(() => ({}))
    llmSuccess.value = res.ok && data.success === true
    llmMessage.value = data.message || (llmSuccess.value ? '连接成功' : `连接失败(${res.status})`)
  } catch (e: any) {
    llmSuccess.value = false
    llmMessage.value = '测试失败: ' + (e.message || '网络错误')
  } finally {
    llmTesting.value = false
  }
}

const saveLlmConfig = async () => {
  llmSaving.value = true
  llmMessage.value = ''
  try {
    const res = await fetch('/api/llm/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...llmForm, test_only: false }),
    })
    const data = await res.json().catch(() => ({}))
    // 必须同时检查 HTTP 状态：4xx/5xx 时 data.success 为 undefined，
    // 旧逻辑 `data.success !== false` 会把失败误判为成功（假"配置已保存"）
    llmSuccess.value = res.ok && data.success !== false
    llmMessage.value = data.message || (llmSuccess.value ? '配置已保存' : `保存失败(${res.status})`)
    if (llmSuccess.value && data.config) {
      currentModel.value = data.config.model
    }
  } catch (e: any) {
    llmSuccess.value = false
    llmMessage.value = '保存失败: ' + (e.message || '网络错误')
  } finally {
    llmSaving.value = false
  }
}

// 恢复为系统内置默认（冻结的 DeepSeek 配置，不依赖用户手填 key）
const restoreDefault = async () => {
  llmRestoring.value = true
  llmMessage.value = ''
  try {
    const res = await fetch('/api/llm/restore-default', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    })
    const data = await res.json().catch(() => ({}))
    llmSuccess.value = res.ok && data.success !== false
    llmMessage.value = data.message || (llmSuccess.value ? '已恢复默认' : `恢复失败(${res.status})`)
    if (llmSuccess.value && data.config) {
      llmForm.model = data.config.model
      llmForm.api_key = data.config.api_key
      llmForm.base_url = data.config.base_url
      llmForm.temperature = data.config.temperature
      llmForm.max_tokens = data.config.max_tokens
      currentModel.value = data.config.model
      const hit = providerOptions.value.find((p: any) => p.base_url && p.base_url === data.config.base_url)
      selectedProvider.value = hit ? hit.name : 'custom'
    }
  } catch (e: any) {
    llmSuccess.value = false
    llmMessage.value = '恢复失败: ' + (e.message || '网络错误')
  } finally {
    llmRestoring.value = false
  }
}

// ========== Props ==========
const props = defineProps<{
  initialQuestion?: string
}>()

const emit = defineEmits<{
  'question-consumed': []
}>()

// ========== 类型定义 ==========
interface Message {
  role: 'user' | 'assistant'
  content: string
  result?: string
  thinking?: string
  thinkingOpen?: boolean
  thinkingStreaming?: boolean
  /** token 级思考流是否正在写入（决定 ⟦T⟧ 气泡末尾是否显示打字机光标） */
  thinkingLive?: boolean
  // 当前正在流式输出的思考步骤名（后端 thought 事件的 step 字段）。
  // 用于「换步骤时先换行」——否则上一句和下一句会黏成一行。
  thinkingStep?: string
  // ---- 以下为结果闭环相关字段（供多轮上下文 + 反馈使用）----
  query?: string                 // 本轮用户问题（反馈时回传后端）
  queryType?: string             // 结果类型（data_query / general_chat ...）
  chartSvg?: string              // 图表 SVG（chart 事件兜底存储，done 渲染时优先使用）
  chartType?: string             // 图表类型（后端选型，图表切换用）
  chartTypeOverride?: string     // 用户手动切换的图型（优先于后端选型，不重跑 SQL）
  _mid?: string                  // 消息唯一 id（切换图型时定位图表容器）
  quality?: {
    refined?: boolean
    warning?: string
    from_memory?: boolean
    // 评价 Agent（白泽式四 Agent 闭环第四环）：质量总分与四维评分
    score?: number
    evaluation?: { score: number; dims?: Record<string, number>; comment?: string } | null
    cross_validated?: boolean
    // 确定性置信度 + 不确定性来源（对标白泽）：编译/缓存=high，LLM 路径按风险分层
    confidence?: {
      level: 'high' | 'medium' | 'low'
      score: number
      basis?: string[]
      risks?: string[]
    } | null
  } | null
  sql?: string                   // 生成的 SQL（多轮指代解析 + 反馈记忆沉淀）
  matchedTables?: any[]          // 命中表（反馈时回传 table_name）
  columns?: string[]             // 结果列（供后端 _build_prev_context 使用）
  rows?: any[]                   // 结果行（封顶 30，供多轮会话记忆提取维度实体）
  feedbackGiven?: boolean        // 是否已提交反馈
  feedbackText?: string          // 反馈提交后的提示文案
  // ---- 回答操作栏相关 ----
  answerText?: string            // 纯文本形态的回答（供"复制"按钮使用）
  copiedSql?: boolean            // SQL 复制成功的瞬时提示
  copiedAnswer?: boolean         // 回答复制成功的瞬时提示
  copiedUser?: boolean           // 用户提问复制成功的瞬时提示（2026-10-01）
  sqlOpen?: boolean              // SQL 卡片展开状态（默认折叠）
  // ---- 口径管理（P1）：未命中反馈条 / 歧义澄清卡 ----
  metricResolution?: { status: string; hints?: string[]; hits?: any[] } | null
  metricClarify?: { hits: any[]; answer: string; query: string }
  noHitDismissed?: boolean       // 本问题已忽略口径未命中提示（会话内不再重复弹）
  // ---- 二次确认弹窗（LLM 推断）：持久在消息上，取消后仍可重新打开 ----
  analysisConfirm?: { query: string; reason: string; hints: string[]; analysis: any; error?: string } | null
  analysisDismissed?: boolean    // 已忽略确认入口（会话内不再显示）
  // ---- 结果溯源（血缘）：每个结果列来自哪张表哪个字段 ----
  lineage?: { tables?: any[]; columns?: any[] } | null
  lineageOpen?: boolean           // 溯源面板展开状态
  // ---- 推理过程（P1-5 Show Work）：每步的名称/耗时/证据（输入-输出-依据）----
  steps?: Array<{ step?: number; name?: string; status?: string; detail?: string;
                  duration_ms?: number; evidence?: { input?: string; output?: string; basis?: string } | null }>
  stepsOpen?: boolean             // 推理过程面板展开状态
  // 混合问答引用文档（P0-A，对标 Spotter 3）：[index, content]，前端可点开核实原文
  citedDocs?: Array<{ index?: number; content?: string }> | null
  citedDocsOpen?: boolean         // 引用文档面板展开状态
  // ---- 提问生成报告：用户问「生产周报」「不良设备报告」等，后端识别意图后
  //      直接把报告随 done 事件带回来，前端自动打开预览（提问即报告，无按钮） ----
  reportId?: string                // 后端转存的报告 ID（Word/PDF 导出按 rid 取同一份 HTML）
  reportHtml?: string              // 后端返回的单文件 HTML（iframe srcdoc 预览）
  reportTitle?: string             // 报告标题（下载文件名用）
  reportOpen?: boolean             // 预览弹窗开关
  reportError?: string             // 生成失败的提示
}

interface ChatHistory {
  id: string
  title: string
  messages: Message[]
  createdAt: string
  updatedAt: string
}

// ========== 状态 ==========
const inputText = ref('')
const isLoading = ref(false)
// 空态场景卡：点击填入问题并聚焦输入框（输入舱居中/落底两个实例共用同一 ref）
const askInputEl = ref<HTMLInputElement | null>(null)
function fillQuestion(q: string) {
  inputText.value = q
  askInputEl.value?.focus()
}
const streamingStep = ref('')    // 流式期间实时显示 AI 当前正在做的事
const searchInput = ref('')      // 输入框的值
const searchKeyword = ref('')    // 实际搜索的关键词
const historyCollapsed = ref(false)  // 左侧历史记录面板是否收纳（收起后右侧聊天区更宽）
const chatContainer = ref<HTMLElement | null>(null)
const askRootEl = ref<HTMLElement | null>(null)

// 当前请求的 AbortController（用于暂停 / 切换对话时取消）
let activeAbort: AbortController | null = null
// 2026-10-03 新增（P0 竞态修复）：请求序号。只有「当前最新那次请求」才允许复位
// 共享状态（isLoading / activeAbort）。原实现里旧请求的 finally 无条件复位，
// 而 stopGenerating 只是同步 abort + 置 isLoading=false，并不等旧请求的 finally
// 真正跑完（fetch body reader 的 reject 通常滞后几十毫秒）→ 用户点「暂停」后
// 立刻发新问题 B，B 设 isLoading=true，新旧两条流同时往同一会话 push，
// 答案顺序错乱；且旧请求把 activeAbort 清空后「暂停」再也停不掉新请求。
let reqSeq = 0

// 停止当前生成（暂停按钮 / 新建对话时调用）
const stopGenerating = () => {
  // 让所有在途请求的 finally/catch 失效（它们复位共享状态前会先校验序号）
  reqSeq++
  if (activeAbort) {
    activeAbort.abort()
    activeAbort = null
  }
  isLoading.value = false
}

// 历史记录列表
const history = ref<ChatHistory[]>([])
const currentChatId = ref<string | null>(null)

// ========== 搜索方法 ==========
const doSearch = () => {
  searchKeyword.value = searchInput.value.trim()
}

const clearSearch = () => {
  searchInput.value = ''
  searchKeyword.value = ''
}

// ========== 计算属性 ==========

// 当前对话的消息
const currentMessages = computed(() => {
  if (!currentChatId.value) return []
  const chat = history.value.find(h => h.id === currentChatId.value)
  return chat?.messages || []
})

// 过滤后的历史记录
const filteredHistory = computed(() => {
  if (!searchKeyword.value) return history.value
  const keyword = searchKeyword.value.toLowerCase()
  return history.value.filter(item =>
    item.title?.toLowerCase().includes(keyword) ||
    item.messages.some(m => m.content.toLowerCase().includes(keyword))
  )
})

// ========== 快捷问题 ==========
// 默认引导问题（接口可用时会被当前数据库的动态引导替换）
const quickQuestions = ref([
  '分析各工序的良率',
  '最近一个月不良数量最高的产品',
  '分析设备停机时间和不良率是否相关',
  '统计每条产线最近7天的产量趋势',
  '训练模型预测产量',
  '训练模型预测不良数量',
])

// 引导问题：默认取后端缓存的稳定组合（每次进入看到的都是同一批，不再自动轮换）；
// 只有用户点「换一批」时才带随机 seed 实时重算。
const quickLoading = ref(false)
const loadQuickQuestions = async (shuffle = false) => {
  if (quickLoading.value) return      // 防连点：上一次没回来就不重复请求
  quickLoading.value = true
  try {
    // 不传 seed → 后端复用缓存（稳定）；传随机 seed → 跳过缓存，换一批组合
    const url = shuffle
      ? `/api/suggest/questions?limit=6&seed=${Math.floor(Math.random() * 1e9)}`
      : '/api/suggest/questions?limit=6'
    const res = await fetch(url)
    const data = await res.json()
    if (data && Array.isArray(data.questions) && data.questions.length > 0) {
      // 后端返回结构化条目 {question, category, source, table, weight}
      quickQuestions.value = data.questions.map((q: any) =>
        typeof q === 'string' ? q : q.question
      )
    }
  } catch (e) {
    console.warn('加载动态引导问题失败，使用默认问题:', e)
  } finally {
    quickLoading.value = false
  }
}

// ========== 核心方法 ==========

// 生成唯一 ID
const generateId = () => {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 6)
}

// 格式化时间
const formatTime = (dateStr: string) => {
  const date = new Date(dateStr)
  const now = new Date()
  const diff = now.getTime() - date.getTime()

  if (diff < 60000) return '刚刚'
  if (diff < 3600000) return Math.floor(diff / 60000) + '分钟前'
  if (diff < 86400000) return Math.floor(diff / 3600000) + '小时前'
  if (diff < 604800000) return Math.floor(diff / 86400000) + '天前'

  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  })
}

// 历史会话列表预览：取最后一条消息做摘要（含各类结构化回答）
const historyPreview = (chat: ChatHistory) => {
  const last = chat.messages?.[chat.messages.length - 1]
  if (!last) return '开始新的智能问析对话'
  if (last.metricClarify) return '🤔 请选择口径…'
  if (last.analysisConfirm && !last.analysisDismissed) return 'AI 生成结果（口径未登记，可登记固化）'
  if (last.role === 'assistant' && last.result && !last.content) return '📊 查看分析结果'
  const raw = (last.content || '').trim()
  if (!raw) return chat.title === '新对话' ? '开始新的智能问析对话' : chat.title
  return raw.replace(/\s+/g, ' ').slice(0, 46)
}

// 创建新对话
const createNewChat = () => {
  // 若当前正在生成，先取消请求，避免新对话显示"AI 还在思考"
  stopGenerating()
  // 关闭跨会话残留的二次确认弹窗（方案绑定旧会话，留在新会话执行会串号）
  closeConfirmDialog()
  const newChat: ChatHistory = {
    id: generateId(),
    title: '新对话',
    messages: [
      { role: 'assistant', content: '你好！我是企业数据底座智能问析助手。请提出你想分析的问题，例如产量趋势、良率分析、设备停机等。' }
    ],
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString()
  }
  history.value.unshift(newChat)
  currentChatId.value = newChat.id
  saveToStorage()
}

// 加载对话
const loadChat = (id: string) => {
  // 切换对话前取消在途流式请求（P1 修复）：此前旧对话的流继续后台写入，
  // isLoading 保持 true 导致新对话无法提问、加载条显示在错误对话里
  if (isLoading.value) stopGenerating()
  // 关闭二次确认弹窗：弹窗方案属于上一个会话，留在新会话执行会把结果写错会话
  closeConfirmDialog()
  currentChatId.value = id
  scrollToBottom()
}

// 删除对话
const deleteChat = (id: string) => {
  if (!confirm('确定要删除这条对话吗？')) return
  // 若删除的是当前会话：取消在途请求，并关闭可能残留的二次确认弹窗——
  // 弹窗方案绑定旧会话，currentChatId 切走后确认执行会把结果写进错误会话（串号）
  if (currentChatId.value === id) {
    if (isLoading.value) stopGenerating()
    closeConfirmDialog()
  }
  history.value = history.value.filter(h => h.id !== id)
  if (currentChatId.value === id) {
    currentChatId.value = history.value.length > 0 ? history.value[0].id : null
  }
  saveToStorage()
}

// 滚动到底部
const scrollToBottom = async () => {
  await nextTick()
  if (chatContainer.value) {
    chatContainer.value.scrollTop = chatContainer.value.scrollHeight
  }
}

// ========== 渲染 Agent 结果 ==========

// HTML 转义，防止 XSS
const esc = (s: any): string => {
  if (s === null || s === undefined) return ''
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

// SVG 消毒：白名单过滤（P1 修复——此前是黑名单正则，可被
// 实体编码 on* / javascript: / foreignObject / <style>@import 绕过，直接喂 v-html 有 XSS 风险）。
// 用 DOMParser 解析为 XML 后按白名单标签 + 属性白名单重建，再序列化回字符串。
const sanitizeSvg = (svg: string): string => {
  if (!svg) return ''
  let doc: Document
  try {
    doc = new DOMParser().parseFromString(String(svg), 'image/svg+xml')
    if (doc.querySelector('parsererror')) throw new Error('bad xml')
  } catch {
    // 非 XML（畸形/被污染）→ 极端兜底：整体转义，不渲染任何标签
    return esc(String(svg))
  }
  // 高危标签整体移除
  doc.querySelectorAll('script, style, foreignObject, object, iframe, embed, link, meta, base, form, input').forEach(n => n.remove())
  const ALLOWED_TAGS = new Set([
    'svg', 'g', 'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon',
    'text', 'tspan', 'defs', 'linearGradient', 'radialGradient', 'stop', 'clipPath',
    'mask', 'pattern', 'title', 'desc', 'marker', 'symbol', 'use', 'image', 'a',
  ])
  const sanitizeEl = (el: Element) => {
    if (!ALLOWED_TAGS.has(el.tagName.toLowerCase())) {
      el.remove()
      return
    }
    for (const attr of Array.from(el.attributes)) {
      const n = attr.name.toLowerCase()
      const v = (attr.value || '').trim().toLowerCase()
      if (n.startsWith('on')) { el.removeAttribute(attr.name); continue }          // 事件属性
      if (n === 'href' || n === 'xlink:href' || n === 'src') {
        // 只允许片段内部引用（#id）；javascript:/data:/http(s) 全部拒绝
        if (v.startsWith('#') || v === '') continue
        el.removeAttribute(attr.name)
        continue
      }
      if (v.includes('javascript:')) { el.removeAttribute(attr.name); continue }    // 实体编码变体兜底
    }
    Array.from(el.children).forEach(sanitizeEl)
  }
  sanitizeEl(doc.documentElement)
  try {
    return new XMLSerializer().serializeToString(doc.documentElement)
  } catch {
    return esc(String(svg))
  }
}

// 把后端推送的思考日志渲染为结构化时间线（2026-10-03 增强：聊天式思考过程）
//
// 五种行形态：
//   ▶ 步骤名：detail   → 旧格式（保留兼容，历史消息仍按这个渲染）
//   **动词**　依据：…   → 新格式：后端 _agent_thought_line() 产出的可读决策句
//   ⟦T⟧…        → token 级思考流：模型真实 reasoning_content，**连续一段**
//   ⟦T-END⟧      → 上述这段的收尾标记
//   📌 SQL：…          → SQL 单独成块
//   其余                → 纯文本
//
// 2026-10-03 简化：去掉了 `live` 参数与 .is-writing 打字机光标 ——
// 用户反馈"太闪了"。DeepSeek 的思考区是**静态灰底 + 文字逐段浮现**，
// 没有闪烁光标、没有脉冲圆点，视觉上是安静的（靠内容本身吸引注意，
// 而不是靠动效）。这里对齐那个观感。
const formatThinking = (text: string): string => {
  if (!text) return ''
  const e = (s: string) => esc(s)
  return text.split('\n').map((raw) => {
    const line = raw.replace(/\s+$/, '')
    if (!line.trim()) return ''
    if (line.startsWith('▶ ')) {
      return `<div class="tk-step"><span class="tk-dot"></span><span class="tk-step-name">${e(line.slice(2))}</span></div>`
    }
    if (line.startsWith('📌 SQL：')) {
      return `<pre class="tk-sql">${e(line.slice(6))}</pre>`
    }
    // ── token 级思考流（2026-10-03）────────────────────────────────
    // 后端旁路直连模型拿到的真实 reasoning_content，按 token 逐片推来。
    // 用 ⟦T⟧ 包裹：这类文本是**连续的一段**（没有换行、可能半句），
    // 单独渲染成一个「正在写…」的气泡，末尾跟打字机光标 —— 这才像聊天。
    if (line.startsWith('⟦T⟧')) {
      return `<div class="tk-live">` +
             `<span class="tk-live-tag">AI 思考</span>` +
             `<span class="tk-live-text">${e(line.slice(3))}</span></div>`
    }
    if (line === '⟦T-END⟧') {
      return ''
    }
    // 新格式：**动作**　依据：… → 动作加粗成标签，依据正文
    const bold = line.match(/^\*\*(.+?)\*\*(.*)$/)
    if (bold) {
      const act = e(bold[1])
      const rest = bold[2] || ''
      // 「依据 / 涉及表 / 命中口径 / 原因 / 编译方式」等标签单独着色，便于扫读
      const tagged = rest.replace(
        /(依据|涉及表|命中口径|原因|编译方式|结果行数|候选表|备注)(：)/g,
        '<span class="tk-tag">$1$2</span>')
      return `<div class="tk-line">` +
        `<span class="tk-act">${act}</span>` +
        (tagged.trim() ? `<span class="tk-rest">${tagged.trim()}</span>` : '') +
        `</div>`
    }
    return `<div class="tk-reason">${e(line)}</div>`
  }).join('')
}

// 结果列的中文名映射：来自后端血缘（lineage.columns[].cn），如 rework_qty → 返工数量。
// 后端已按「词典 > 内置语义 > 元数据注释 > AI 补全」给出短中文名，前端只负责展示。
const columnLabelsOf = (data: any, msg?: any): Record<string, string> => {
  const lg = (data && data.lineage) || (msg && msg.lineage) || null
  const out: Record<string, string> = {}
  for (const c of ((lg && lg.columns) || [])) {
    const key = c && c.output
    const cn = c && c.cn
    if (key && cn && cn !== key) out[key] = cn
  }
  return out
}

// 表头文案：中文业务名在前、英文字段名括号在后 → 返工数量（rework_qty）
// （2026-10-02 用户要求统一「中文（英文）」顺序，原先是 rework_qty（返工数量））；
// 列名本身已是中文（SQL 里已 AS 中文别名）则保持原样，不重复标注。
const headerText = (name: string, labels?: Record<string, string>): string => {
  const cn = (labels || {})[name]
  if (!cn || /[\u4e00-\u9fff]/.test(name)) return name
  return `${cn}（${name}）`
}

// 渲染数据表格（labels：结果列中文名映射，可选）
const renderTable = (columns: string[], rows: any[], labels?: Record<string, string>): string => {
  if (!columns || !rows || rows.length === 0) return ''
  let html = '<div class="anl-table-wrap"><table class="anl-table">'
  html += '<thead><tr>'
  columns.forEach(c => {
    const label = labels && labels[c]
    const title = label ? ` title="${esc(label)}（${esc(c)}）"` : ''
    html += `<th${title}>${esc(headerText(c, labels))}</th>`
  })
  html += '</tr></thead><tbody>'
  rows.slice(0, 20).forEach(r => {
    html += '<tr>'
    columns.forEach(c => { html += `<td>${esc(r[c] ?? '')}</td>` })
    html += '</tr>'
  })
  if (rows.length > 20) {
    html += `<tr><td colspan="${columns.length}" class="anl-table-more">… 共 ${rows.length} 行</td></tr>`
  }
  html += '</tbody></table></div>'
  return html
}

// 分析结论渲染：后端返回的是分段文本（【小标题】+「· / 1.」列表项），
// 这里转成结构化 HTML——先 esc 再识别标记，杜绝 XSS。
// 目的是让「结论」成为整条回复中最显眼的一块，而不是淹没在灰色小字里。
//
// 视觉策略（2026-09-29 信息分层重构）：
//  - 【小标题】→ 左竖线 + 小标题，形成清晰的"结论分区"，取代原先扁平的小字标题；
//  - 「· / 1.」列表项 → 圆点引导 + 悬挂缩进，读起来像专业的分析要点而非代码列表；
//  - **加粗** → 关键结论加粗高亮（主色），让数字/判断第一眼跳出来。
const renderAnalysis = (text: any): string => {
  if (!text) return ''
  let out = ''
  for (const raw of String(text).split('\n')) {
    const line = esc(raw)
    if (!line.trim()) { out += '<div class="h-2.5"></div>'; continue }
    const head = line.match(/^\s*【(.+?)】\s*$/)
    if (head) {
      out += `<div class="anl-head"><span class="anl-head-bar"></span>${head[1]}</div>`
      continue
    }
    const body = line.replace(/\*\*(.+?)\*\*/g, '<b class="anl-strong">$1</b>')
    out += /^\s*(·|•|-|\d+[.、)])\s*/.test(line)
      ? `<div class="anl-li"><span class="anl-dot"></span><span>${body}</span></div>`
      : `<div class="anl-li anl-li-plain"><span>${body}</span></div>`
  }
  return out
}

// 根据 agent 返回的 data 生成展示 HTML
const renderAgentResult = (data: any, msg?: any): string => {
  if (!data) return '<div class="text-sm text-gray-500">无返回结果</div>'
  const type = data.type || ''
  let html = ''

  // 提问即报告：报告正文是整份 HTML，由「报告卡片 + 预览弹窗」承载，
  // 这里不重复渲染一份「结果回答」（后端 answer 只是给气泡用的一句话）
  if (type === 'report') return ''

  // 闲聊 / 数据库分析：纯文本
  if (type === 'general_chat' || type === 'analyze_db') {
    html += `<div style="white-space:pre-wrap;line-height:2;font-size:13px">${esc(data.answer || '')}</div>`
    return html
  }

  // 表结构查询
  if (type === 'table_lookup') {
    const mt = data.matched_table
    if (mt) {
      html += `<div class="text-sm font-medium mb-1">${esc(mt.table_alias)}（${esc(mt.table_name)}）</div>`
      html += `<div class="text-xs text-gray-500 mb-2">${esc(mt.category)} · ${mt.row_count ?? '-'} 行 · ${mt.field_count ?? '-'} 字段</div>`
      if (data.detail && data.detail.fields) {
        html += renderTable(['字段', '类型', '键'], data.detail.fields.map((f: any) => ({ '字段': f.name, '类型': f.type, '键': f.key || '' })))
      }
    } else {
      html += '<div class="text-sm text-gray-500">未找到匹配的表</div>'
    }
    return html
  }

  // ML 建模结果
  if (type === 'ml_result' || type === 'ml_error') {
    if (data.ml_result && data.ml_result.success) {
      const mr = data.ml_result
      // 模型名：训练结果是把 model_label / model_name 平铺在 ml_result 顶层的
      //（见后端 ml/trainer.py 的 return），只有"续问预测"那条路径才嵌在 ml_result.model 里。
      // 旧代码只认 ml_result.model?.label，于是训练完永远显示占位符"模型" ——
      // 用户明明说的是"随机森林"，屏幕上却看不出跑的是随机森林还是线性回归。
      const modelTitle = mr.model?.label || mr.model?.name || mr.model_label || mr.model_name || '模型'
      html += `<div class="text-sm mb-1"><b>${esc(modelTitle)}</b></div>`
      // 把「预测目标 + 用到的特征」摆出来：一眼能看出它选对了字段
      const tgt = data.target || mr.target
      const feats = data.features || mr.features
      let subLine = `样本 ${mr.samples ?? '-'} 条`
      if (tgt) subLine += ` · 预测目标 ${tgt}`
      html += `<div class="text-xs text-gray-500 mb-1">${esc(subLine)}</div>`
      if (Array.isArray(feats) && feats.length) {
        html += `<div class="text-xs text-gray-400 mb-2">特征：${esc(feats.join('、'))}</div>`
      } else {
        html += '<div class="text-xs text-gray-400 mb-2"></div>'
      }
      // 跨表取数说明（如"工单表的实际产量记在工序产量表里，按工单号汇总后当目标"）：
      // 目标来自另一张表时必须说清楚，否则"预测目标 good_qty"会让人以为选错了表
      if (mr.join_note) {
        html += `<div class="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1 mb-2">${esc(mr.join_note)}</div>`
      }
      // 数据说明（如"原始 5000 行，剔除含缺失值记录后剩余 3200 行（缺失最多：xxx）"）：
      // 建模数据量骤减时给出原因，避免"有效数据不足"类问题无从解释
      if (mr.data_note) {
        html += `<div class="text-xs text-gray-600 bg-gray-50 border border-gray-200 rounded px-2 py-1 mb-2">${esc(mr.data_note)}</div>`
      }
      if (mr.metrics && Object.keys(mr.metrics).length > 0) {
        html += '<div class="text-xs mb-2 flex flex-wrap gap-x-4 gap-y-1">'
        Object.entries(mr.metrics).forEach(([k, v]) => { html += `<span class="bg-blue-50 text-blue-700 px-2 py-0.5 rounded">${esc(k)}: <b>${esc(v)}</b></span>` })
        html += '</div>'
      }
      if (mr.importance && Object.keys(mr.importance).length > 0) {
        html += '<div class="text-xs text-gray-400 mt-2 mb-1">特征重要性</div>'
        const impHtml = Object.entries(mr.importance).slice(0, 10).map(([k, v]) => {
          const pct = Math.min(100, Math.max(2, Math.abs(Number(v) || 0) * 100))
          return `<div class="flex items-center gap-2 text-xs mb-1"><span class="w-28 text-gray-600 truncate">${esc(k)}</span><div class="flex-1 bg-gray-200 h-2 rounded"><div class="h-2 rounded ${(Number(v) || 0) < 0 ? 'bg-red-400' : 'bg-blue-500'}" style="width:${pct}%"></div></div><span class="w-16 text-right text-gray-500">${esc(v)}</span></div>`
        }).join('')
        html += `<div class="bg-white border border-gray-200 rounded p-2">${impHtml}</div>`
      }
      if (mr.charts) {
        if (mr.charts.importance) html += `<div class="mt-2">${sanitizeSvg(mr.charts.importance)}</div>`
        if (mr.charts.pred_vs_actual) html += `<div class="mt-2">${sanitizeSvg(mr.charts.pred_vs_actual)}</div>`
      }
      if (mr.pred_samples && mr.pred_samples.length > 0 && mr.pred_samples[0]) {
        html += '<div class="text-xs text-gray-400 mt-2 mb-1">预测结果</div>'
        html += renderTable(Object.keys(mr.pred_samples[0]), mr.pred_samples.slice(0, 10))
      }
      html += '<div class="text-xs text-gray-400 mt-2">继续问"预测"可基于该模型推理；格式如：字段=数值</div>'
    } else {
      html += `<div class="text-sm text-red-500">${esc(data.error || data.ml_result?.error || 'ML 执行失败')}</div>`
    }
    return html
  }

  // data_query：SQL + 表格 + 图表 + 分析 + 推荐
  if (type === 'data_query') {
    // 口径溯源：面向非专业业务人员，只说清「按什么口径算的」，
    // 不暴露编译器 / LLM / 可审计等实现细节（用户看不懂也不关心）
    // 把「缺陷数 = COUNT(*)（单位：次）」规整为「缺陷数 = COUNT(*)（次）」，并去掉与指标名重复的前缀
    const fmtCaliber = (definition?: string, metric?: string, unit?: string) => {
      const name = String(metric || '').trim()
      let def = String(definition || '').trim()
      if (name && def.startsWith(name)) def = def.slice(name.length).replace(/^[\s:=：]+/, '')
      def = def.replace(/[（(]\s*单位\s*[:：]\s*([^）)]+)\s*[）)]/g, '（$1）')
      const unitTxt = (unit && !/单位/.test(def)) ? `（${unit}）` : ''
      if (!name) return def + unitTxt
      return `${name}${def ? ' = ' + def : ''}${unitTxt}`
    }
    // LLM 生成 vs 确定性编译：只有确定性编译命中才不标注 AI
    let caliberAI = false
    let caliberName = ''
    if (data.compiled && data.mql) {
      const m = data.mql
      const dimTxt = (m.dimensions && m.dimensions.length) ? `按「${m.dimensions.join('、')}」拆分` : ''
      html += `<div class="anl-caliber">
        <span class="anl-caliber-dot"></span>
        <span class="anl-caliber-text">统计口径：<b>${esc(fmtCaliber(m.metric_definition, m.metric, m.unit))}</b>${dimTxt ? `<span class="anl-caliber-dim">· ${esc(dimTxt)}</span>` : ''}</span>
      </div>`
    } else if (data.metric_hint && data.metric_hint.kind === 'single') {
      // LLM 兜底：命中 1 个注册指标 → 报口径 + AI 生成徽标
      caliberAI = true
      caliberName = String(data.metric_hint.metric || '')
      const m = data.metric_hint
      html += `<div class="anl-caliber anl-caliber-warn">
        <span class="anl-caliber-dot"></span>
        <span class="anl-caliber-text">统计口径：<b>${esc(fmtCaliber(m.metric_definition, m.metric, m.unit))}</b>
        <span class="anl-caliber-badge">AI 生成</span></span>
      </div>`
    } else if (data.metric_hint && data.metric_hint.kind === 'multi') {
      // LLM 兜底：命中多个注册指标 → 列出涉及指标名 + AI 徽标
      caliberAI = true
      caliberName = String((data.metric_hint.metrics || [])[0] || '')
      const names = (data.metric_hint.metrics || []).join('、')
      html += `<div class="anl-caliber anl-caliber-warn">
        <span class="anl-caliber-dot"></span>
        <span class="anl-caliber-text">统计口径：<b>${esc(names)}</b>
        <span class="anl-caliber-badge">AI 生成</span></span>
      </div>`
    } else {
      // 未命中任何注册指标 → 诚实标注，但用业务语言
      caliberAI = true
      html += `<div class="anl-caliber anl-caliber-muted">
        <span class="anl-caliber-dot"></span>
        <span class="anl-caliber-text">未匹配到已登记的统计口径，结果由 AI 生成，请核对后再使用</span>
      </div>`
    }
    // LLM 生成的结果：提供「登记口径」入口 —— 登记后同问法将命中确定性编译，秒查且不再标 AI
    if (caliberAI) {
      const _q = esc(data.query || (msg && msg.query) || '')
      const _n = esc(caliberName)
      html += `<div class="anl-caliber-hint">这个结果由 AI 生成，口径可能不准。<span class="anl-caliber-link" data-define-name="${_n}" data-define-query="${_q}">去登记正确口径，以后自动按它算 →</span></div>`
    }

    // 注意：SQL 不在此渲染 —— 已由消息上方的独立「SQL 查询语句」深色卡片承担，
    // 避免与分析结果混在一起，也便于单独复制。

    // 结论先行：分析结论是整条回复里用户最该看的部分。
    // 用「无边框 + 柔和底 + 品牌竖标」的结论区承载，弱化描边、强化留白，
    // 让结论成为视觉重心，图表/表格降级为「数据依据」放在其后。
    if (data.analysis) {
      html += `<div class="anl-conclusion" data-anl="1">
        <div class="anl-conclusion-title">
          <span class="anl-conclusion-dot"></span>
          分析结论
        </div>
        <div class="anl-conclusion-body">${renderAnalysis(data.analysis)}</div>
      </div>`
    }

    const result = data.result || {}
    // SVG 优先取 done 响应携带的；缺失时回退到流式 chart 事件存入的 msg.chartSvg
    const chartSvg = (data.chart && data.chart.svg) || (msg && msg.chartSvg) || ''
    // 图型：用户手动切换的优先（P1-2 图表切换，不重跑 SQL）；否则用后端选型
    const chartType = (msg && msg.chartTypeOverride) || (data.chart && data.chart.type) || ''
    const hasChart = !!chartSvg && !['none', 'table'].includes(chartType)

    if (result.rows && result.rows.length > 0) {
      if (hasChart) {
        // 优先图表：ECharts 交互图渲染（可缩放/悬浮提示）；数据异常时回退后端 SVG
        html += '<div class="anl-section-title"><span class="anl-section-icon"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <line x1="18" x2="18" y1="20" y2="10" /> <line x1="12" x2="12" y1="20" y2="4" /> <line x1="6" x2="6" y1="20" y2="14" /> </svg></span></span>数据概览</div>'
        const colsJson = esc(JSON.stringify(result.columns || []))
        const rowsJson = esc(JSON.stringify(result.rows || []))
        const fbSvg = esc(sanitizeSvg(chartSvg))
        // P0-4：drillable 层级钻取信息（点击分类可下钻到下一级，如 车间→产线）
        const drillJson = esc(JSON.stringify(data.drillable || null))
        html += `<div class="anl-chart echart" style="height:320px" data-type="${esc(chartType)}" data-cols="${colsJson}" data-rows="${rowsJson}" data-fallback="${fbSvg}" data-drill="${drillJson}"></div>`
      } else {
        // 无法生成图表（纯文本列表 / 单行聚合 / 无数值列）→ 表格兜底展示
        html += '<div class="anl-section-title"><span class="anl-section-icon"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <line x1="18" x2="18" y1="20" y2="10" /> <line x1="12" x2="12" y1="20" y2="4" /> <line x1="6" x2="6" y1="20" y2="14" /> </svg></span></span>数据明细</div>'
        // 表头带中文翻译：rework_qty（返工数量），翻译来自后端血缘 lineage.columns[].cn
        html += renderTable(result.columns || [], result.rows, columnLabelsOf(data, msg))
      }
    }

    // 无 ECharts 渲染条件（如渲染失败）时直接用后端 SVG 兜底（先消毒）
    if (chartSvg && !hasChart) {
      html += `<div class="mt-3">${sanitizeSvg(chartSvg)}</div>`
    }

    if (data.attribution) {
      html += `<div class="anl-attribution">${esc(data.attribution)}</div>`
    }

    if (data.prediction && data.prediction.length > 0 && data.prediction[0]) {
      html += '<div class="anl-section-title"><span class="anl-section-icon"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <path d="M3 3v18h18" /> <path d="m19 9-5 5-4-4-3 3" /> </svg></span></span>趋势预测</div>'
      html += renderTable(Object.keys(data.prediction[0]), data.prediction.slice(0, 10))
    }

    if (data.recommended && data.recommended.length > 0) {
      html += '<div class="anl-recommend">'
      html += '<div class="anl-recommend-label">你可能还想问</div>'
      html += '<div class="anl-recommend-list">'
      data.recommended.forEach((q: string) => {
        html += `<span class="anl-recommend-chip" data-ask="${esc(q)}">${esc(q)}</span>`
      })
      html += '</div></div>'
    }
    return html
  }

  // clarify：极简指标输入 → 澄清候选（可点击快捷提问）
  if (type === 'clarify' && data.candidates && data.candidates.length) {
    let html = `<div style="white-space:pre-wrap;line-height:2;font-size:13px;color:#4b5563">${esc(data.answer || '')}</div>`
    html += '<div class="flex flex-wrap gap-2 mt-2">'
    data.candidates.forEach((c: string) => {
      html += `<span class="text-xs px-3 py-1 bg-blue-50 text-blue-600 rounded-full cursor-pointer hover:bg-blue-100 transition" data-ask="${esc(c)}">${esc(c)}</span>`
    })
    html += '</div>'
    return html
  }

  // 兜底：尝试显示 answer 或原始 JSON
  if (data.answer) return `<div style="white-space:pre-wrap;line-height:2;font-size:13px">${esc(data.answer)}</div>`
  return `<pre class="text-xs overflow-x-auto">${esc(JSON.stringify(data, null, 2))}</pre>`
}

// 共享 ResizeObserver：观察所有图表容器尺寸变化，替代「每个图表一个 window resize 监听器」，
// 避免监听器与图表实例随消息累积而泄漏（AskPage 被 KeepAlive 缓存，长期使用会越积越多）。
let chartResizeObserver: ResizeObserver | null = null
// 2026-10-03 修复（P1）：尺寸记忆表必须与 observer 同为**模块级**。
// 原实现把 chartSizes 建在 renderECharts() 函数体内，而 observer 回调闭包只在
// 第一次创建时捕获了那张 map → 第 2 次及以后的调用（每次新回答、切页回来、切会话
// 都会触发）把新图表尺寸写进新 map，observer 却去查旧 map（无记录）→ prev 为
// undefined → 无条件 resizeChart(node) → 09-28 修复的「入场动画被掐断」缺陷
// **只对第一张图生效，从第二张起全部复现**（表现为「第一个问题的图有动画、
// 后面的都没有」，极难归因）。
let chartSizes = new WeakMap<HTMLElement, { w: number; h: number }>()

// P0-4 层级钻取联动：ECharts 图表点击分类值 → 构造「{值}各{下一级}的{指标}」下钻问题重查。
// 仅当图表节点携带 data-drill（后端编译器返回 drillable）时绑定，其余图表零打扰。
const bindDrillClick = (el: HTMLElement) => {
  const raw = el.dataset.drill
  if (!raw || raw === 'null' || raw === '""') return
  let drill: { current_level?: string; next_level?: string; metric?: string } | null = null
  try { drill = JSON.parse(raw) } catch { return }
  if (!drill || !drill.next_level) return
  const inst = echarts.getInstanceByDom(el)
  if (!inst) return
  inst.on('click', (params: any) => {
    const val = params?.name
    if (val === undefined || val === null || val === '') return
    const metric = drill?.metric || ''
    const q = metric ? `${val}各${drill!.next_level}的${metric}` : `${val}各${drill!.next_level}`
    // 提示下钻动作（轻量，不阻塞）
    sendMessage(q)
  })
}

/**
 * 渲染全部图表容器 —— Apache ECharts + AntV G2Plot 双引擎。
 *
 * 按图表类型自动分工（见 src/charts/index.ts）：
 * - 常规图（柱/线/饼/散点…）→ ECharts
 * - 扩展图（雷达/漏斗/仪表盘/玫瑰/水波/热力…）→ AntV G2Plot（懒加载 chunk）
 * 任一引擎失败都会回退到后端 matplotlib 生成的 SVG，保证结果可见。
 */
const renderECharts = async () => {
  // 只渲染本页容器内的图表（P2 修复）：此前 document 全局查询会误染其他页面
  // （如 OverviewPage 看板）的 .echart 节点
  const nodes = Array.from((askRootEl.value?.querySelectorAll<HTMLElement>('.echart')) || [])
  await Promise.all(nodes.map(async (el) => {
    if (el.dataset.rendered) return
    el.dataset.rendered = '1'
    try {
      const type = el.dataset.type || 'bar'
      const cols = JSON.parse(el.dataset.cols || '[]')
      const rows = JSON.parse(el.dataset.rows || '[]')
      if (!cols.length || !rows.length) throw new Error('bad chart data')
      // 同一 DOM 重复渲染前先销毁旧实例（两种引擎都要清），防止实例堆积
      disposeChart(el)
      let engine = await renderChart(el, type, cols, rows)
      if (!engine) {
        // 目标图型数据形态不满足（如单指标选散点）→ 先降级到最近可渲染图型重试，
        // 再走 SVG 兜底（2026-10-02：直接弹回初始 SVG 会被用户看成"切换没生效"；
        // 二次修复：降级链改用 downgradeChartType，coerce 只做可否渲染判定）
        const alt = downgradeChartType(type, cols, rows)
        if (alt && alt !== type) {
          el.dataset.type = alt
          engine = await renderChart(el, alt, cols, rows)
        }
      }
      if (!engine) throw new Error('no engine rendered')
      // 标记实际使用的引擎，便于样式微调与问题排查
      el.dataset.engine = engine
      // P0-4 层级钻取联动：图表点击分类值 → 自动下钻到下一级（「{值}各{下一级}的{指标}」）
      bindDrillClick(el)
    } catch (e) {
      // 渲染失败 → 回退到后端生成的 SVG
      el.innerHTML = el.dataset.fallback || ''
      el.dataset.engine = 'svg'
    }
  }))
  // 尺寸自适应：所有图表渲染完成后统一挂 observer（G2Plot 是异步的，需等渲染完再观察）
  //
  // 2026-09-28 修复（与 DataChartCard 同一根因）：observer 一挂上浏览器就回调一次「初始尺寸」，
  // 那次 resize 会让 ECharts 以 0 时长整图重绘（resize 的 update payload 里写死 animation.duration=0），
  // 刚起跑的入场动画当场被掐断——仪表盘指针直接落在终值、柱线图没有长出来的过程。
  // 所以按容器记下上次尺寸，只在尺寸真的变了才 resize；同时改用 entries（谁变给谁 resize），
  // 不再一有风吹草动就把页面里所有图表都重画一遍。
  // 尺寸表已是模块级（见上方 chartSizes 声明），与 observer 共享同一生命周期
  if (!chartResizeObserver) {
    chartResizeObserver = new ResizeObserver((entries) => {
      entries.forEach((entry) => {
        const node = entry.target as HTMLElement
        const prev = chartSizes.get(node)
        const w = node.clientWidth
        const h = node.clientHeight
        if (prev && prev.w === w && prev.h === h) return
        chartSizes.set(node, { w, h })
        resizeChart(node)
      })
    })
  }
  nodes.forEach((el) => {
    if (el.dataset.engine !== 'svg') {
      chartSizes.set(el, { w: el.clientWidth, h: el.clientHeight })
      chartResizeObserver!.observe(el)
    }
  })
}

// 组件卸载时清理图表实例与共享 ResizeObserver（配合 KeepAlive deactivate）
const disposeAllCharts = () => {
  // 只清理本页容器内的图表（P2 修复）：此前 document 全局查询会在 AskPage
  // 卸载时误杀其他页面（OverviewPage 看板）的 .echart 实例，导致看板变空白
  ;(askRootEl.value?.querySelectorAll<HTMLElement>('.echart') || []).forEach((el) => {
    disposeChart(el)
  })
  try { chartResizeObserver?.disconnect() } catch { /* 忽略 */ }
  chartResizeObserver = null
}

// 暴露给推荐问题点击的回调（用于快捷填充输入框）
declare global {
  interface Window {
    __askSend?: (q: string) => void
  }
}

// ========== 口径管理（P1：未命中反馈条 / 歧义澄清 / 定义入口）==========
const showDefineCard = ref(false)
const definePrefill = ref('')
const defineQuery = ref('')

// ========== 置信度展示（P0-A，对标白泽「结论置信度 + 偏差来源」）==========
const confidenceLabel = (level?: string) =>
  level === 'high' ? '高置信' : (level === 'low' ? '低置信' : '中置信')

const confidenceClass = (level?: string) =>
  level === 'high'
    ? 'bg-blue-50 border-blue-200 text-blue-700'
    : (level === 'low'
        ? 'bg-rose-50 border-rose-200 text-rose-700'
        : 'bg-amber-50 border-amber-200 text-amber-700')

const confidenceTitle = (c?: { basis?: string[]; risks?: string[] } | null) => {
  if (!c) return ''
  const parts: string[] = []
  if (c.basis?.length) parts.push('依据：' + c.basis.join('；'))
  if (c.risks?.length) parts.push('不确定性：' + c.risks.join('；'))
  return parts.join('\n') || '置信度为确定性信号计算（编译/缓存/规则），非 LLM 自评'
}

const openDefine = (name: string, query: string) => {
  definePrefill.value = name || ''
  defineQuery.value = query || ''
  showDefineCard.value = true
}

const resendWithMetric = (name: string, query: string) => {
  if (!name || !query) return
  sendMessage(`按【${name}】口径：${query}`)
}

const onDefineSaved = () => {
  showDefineCard.value = false
  // 保存成功后按新口径重跑当前问题（新口径已入注册表，重问可命中）
  if (defineQuery.value) {
    sendMessage(defineQuery.value)
  }
}

// ========== 二次确认弹窗（未定义口径/模糊/复杂查询 → LLM 推断 + 遵循执行）==========
// 红线（2026-08-31 用户强调"永远记住"）：未定义口径/模糊查询 → 【立即弹窗】。
// 主链对 no_hit/hit 直接 done(analysis_confirm 空壳，analysis=null)；前端收到即弹窗。
// 2026-09-07 定位调整：弹窗只做「澄清」——用一句话说清哪儿不清楚，用户用自然语言补充，
// 由 AI 识别后重走主链路；不再展示置信度/指标公式/SQL 草稿（业务用户看不懂也不需要看）。
// 用户不想补充时可点「就按你的理解查」，此时才按需生成方案并直接执行。
const showAnalysisConfirm = ref(false)
const analysisConfirm = ref<{ query: string; reason: string; hints: string[]; analysis: any;
                              error?: string; genError?: string } | null>(null)

// 核心：调用 infer_confirm 生成方案（后端只生成不执行——LLM 产物执行必须经二次确认）
const fetchInfer = async (query: string): Promise<any> => {
  const res = await fetch('/api/agent/infer_confirm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, history: [] }),
    signal: AbortSignal.timeout(50000),   // 推断最长 ~40s（2026-09-06 提速），留 10s 余量
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok || !data.analysis) {
    throw new Error(data.message || '方案生成未成功，请稍后重试或点「自定义口径」注册')
  }
  return data.analysis
}

// 弹窗内自动/手动重试生成：analysis 空壳 → 拉取方案填充；失败置 genError（弹窗内可重试）
const inferringGen = ref(false)
const triggerInfer = async () => {
  const ac = analysisConfirm.value
  if (!ac || inferringGen.value) return
  inferringGen.value = true
  ac.genError = ''
  try {
    const analysis = await fetchInfer(ac.query)
    if (analysisConfirm.value !== ac) return   // 弹窗已切换/关闭
    ac.analysis = analysis
    ac.error = ''
  } catch (e: any) {
    if (analysisConfirm.value !== ac) return
    const why = e?.name === 'TimeoutError' ? '生成超时' : (e?.message || '网络错误')
    ac.genError = why
  } finally {
    inferringGen.value = false
  }
}

// 关闭二次确认弹窗：切换会话/新建对话/账号切换时调用。
// 弹窗方案绑定「发起时所在会话」，若切走后仍开着，用户点执行会把结果写入错误会话（串号）。
// 消息上的 analysisConfirm 已持久化，切回原会话可经入口条「查看并确认」随时重开。
const closeConfirmDialog = () => {
  showAnalysisConfirm.value = false
  analysisConfirm.value = null
}

// 方案改为懒加载：弹窗只负责澄清，只有用户点「就按你的理解查」时才生成方案。
// 原来一开弹窗就自动跑 infer_confirm（5~15s），既拖慢首次响应，又让业务用户面对一堆看不懂的方案。

// 已澄清过的问题：同一问题补充过一次仍取不到口径时不再反复追问（避免弹窗死循环）。
// 注意：补充后重发的 query 是「原问题。补充说明：…」，与当初存的原问题并不相等，
// 所以必须按前缀匹配，否则防重复判断永远命中不了（曾因此导致补充完还弹第二次）。
const clarifiedOnce = new Set<string>()

// 用户用自然语言补充说明 → 拼回原问题重走主链路，由 AI 识别。
// 关键：no_confirm=true 让后端跳过二次弹窗（补充过还问 = 最差体验）；
// 后端即便仍返回 analysis_confirm，updateMsg 也会按「补充说明」标记直接执行而不是再弹。
const handleClarify = (text: string) => {
  const ac = analysisConfirm.value
  if (!ac) return
  clarifiedOnce.add(ac.query)
  showAnalysisConfirm.value = false
  analysisConfirm.value = null
  inputText.value = ''
  sendMessage(`${ac.query}。补充说明：${text}`, { noConfirm: true })
}

// 「就按你的理解查」：按需生成方案 → 拿到 SQL 直接执行（不经用户看 SQL）
const handleAutoQuery = async () => {
  const ac = analysisConfirm.value
  if (!ac) return
  if (!ac.analysis) {
    await triggerInfer()
    if (analysisConfirm.value !== ac) return   // 期间弹窗已切换/关闭
  }
  const sql = analysisConfirm.value?.analysis?.sql_draft || ''
  if (!sql.trim()) {
    ac.genError = ac.genError || 'AI 没能生成可执行的查询，建议补充一点信息再试'
    return
  }
  await runConfirmedSql(sql)
}

// 「自定义口径」：打开口径定义弹窗（弹窗化后必可见）；数据保留在消息上，随时可回
const onAnalysisDefine = () => {
  const ac = analysisConfirm.value
  showAnalysisConfirm.value = false
  if (ac) openDefine((ac.hints || [])[0] || '', ac.query || '')
}

// 「遵循 LLM 执行 / 按我的修改执行」：用确认后的 SQL 直接执行并渲染结果
const analysisDialogRef = ref<InstanceType<typeof AnalysisConfirmDialog> | null>(null)
const runConfirmedSql = async (sql: string) => {
  const ac = analysisConfirm.value
  showAnalysisConfirm.value = false
  if (!ac || !sql.trim()) return
  // 绑定发起时的会话 id：异步（fetch）期间若账号切换 watch 清空 history，
  // 结果只回写原会话；原会话不存在则放弃写入，绝不落入新会话造成串号
  const chatId = currentChatId.value
  let chat = history.value.find(h => h.id === chatId)
  if (!chat) return  // 找不到当前对话，直接返回
  analysisDialogRef.value?.setSubmitting(true)
  // 本次执行消息的稳定标识：2026-10-03 用于精确定位，避免 catch/finally 靠
  // messages[last] 猜测（执行期间用户可能又发了新问题，最后一条根本不是这条）
  const execMid = 'm' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6)
  try {
    // 追加一条 assistant 消息，结果渲染复用流式同一条路径
    chat.messages.push({
      role: 'assistant',
        _mid: execMid,
      content: '正在执行确认的查询…',
      result: '',
      thinking: '',
      thinkingOpen: false,
      thinkingStreaming: false,
      query: ac.query,
      queryType: 'data_query',
      sql: '',
      matchedTables: [],
      columns: [],
      answerText: '',
      metricResolution: null,
    })
    const msg = chat.messages[chat.messages.length - 1]!
    const res = await fetch('/api/agent/execute_confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: ac.query, sql }),
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err?.detail || `执行失败(${res.status})`)
    }
    const finalData = await res.json()
    // await 后重新定位原会话（防孤儿）：会话已被账号切换清空 → 放弃写入
    chat = history.value.find(h => h.id === chatId)
    if (!chat) return
    applyFinalData(msg, finalData)
    // 执行成功：清空失败原因；保留原确认消息的入口条（可再次打开查看/修改/重执行）
    ac.error = ''
    const src = chat.messages.find(m => m.analysisConfirm && m.analysisConfirm.query === ac.query)
    if (src && src.analysisConfirm) {
      src.analysisConfirm = { ...src.analysisConfirm, error: '' }
      src.analysisDismissed = false
    }
  } catch (e: any) {
    // 失败：错误写回**本条执行消息**（2026-10-03 修复：原代码取
    // chat.messages[messages.length - 1]，把「执行失败」写到用户另一条提问的气泡上，
    // 覆盖其正在流式输出的内容 —— 用户看到的是「我问的那个问题答错了」，
    // 实际出错的是上一轮的确认查询）。这里按 _mid 精确定位，找不到就不碰消息数组。
    chat = history.value.find(h => h.id === chatId)
    const target = chat?.messages.find(m => m._mid === execMid)
    if (target) {
      target.content = '执行失败：' + (e?.message || '网络错误')
      target.result = `<div class="text-sm text-red-500">${esc(e?.message || '网络错误')}</div>`
      target.thinkingStreaming = false
    }
    ac.error = e?.message || '网络错误'
    // chat 可能已被账号切换整体清空（find 返回 undefined），可选链防止对 undefined 访问
    const src = chat?.messages.find(m => m.analysisConfirm && m.analysisConfirm.query === ac.query)
    if (src && src.analysisConfirm) {
      src.analysisConfirm = { ...src.analysisConfirm, error: ac.error }
    }
    // 切走了就别把弹窗挂回新会话
    analysisConfirm.value = chat ? ac : null
  } finally {
    analysisDialogRef.value?.setSubmitting(false)
    chat = history.value.find(h => h.id === chatId) || chat
    if (chat) chat.updatedAt = new Date().toISOString()
    saveToStorage()
  }
}

// 孤儿防护：异步（fetch 流式）期间若账号切换 watch 整体替换 history，闭包持有的 chat/msg
// 会脱离响应式数组 → 后续修改既不渲染也不保存（用户看到"卡死不出结果"）。
// 定位消息当前归属会话：优先按消息引用反查，其次按当前会话 id；都无 → null（调用方中止，绝不写孤儿）。
const ownerChatOf = (msg?: any): ChatHistory | null => {
  if (!history.value.length) return null
  if (msg) {
    const byMsg = history.value.find(h => (h as any).messages.includes(msg))
    if (byMsg) return byMsg
  }
  return history.value.find(h => h.id === currentChatId.value) || null
}

// ========== 发送消息 ==========
// presetText：供「重新回答」直接复用原问题。
// 注意模板上的 @click / @keydown 会把 Event 传进来，故需做类型判别。
// 输入框回车：中文输入法（IME）选词回车也会触发 keydown.enter（isComposing=true / keyCode=229），
// 若不拦截会把拼音候选词当问题误发送——此处统一拦截后仅对非组合态回车触发发送。
const onInputKeydown = (e: KeyboardEvent) => {
  if (e.isComposing || e.keyCode === 229) return   // 输入法组合中（选词/组词）不发送
  sendMessage(e)
}

const sendMessage = async (presetText?: string | Event, opts?: { noConfirm?: boolean }) => {
  const preset = typeof presetText === 'string' ? presetText.trim() : ''
  const raw = preset || inputText.value
  if (!raw.trim() || isLoading.value) return
  const text = raw.trim()

  // 如果没有当前对话，创建新对话
  if (!currentChatId.value) {
    createNewChat()
  }

  // 添加用户消息
  let chat = history.value.find(h => h.id === currentChatId.value)
  if (!chat) return

  // 修复（P0）：用户消息也必须带 _mid，否则列表 key 退化为索引（见模板 v-for）。
  chat.messages.push({
    role: 'user',
    content: text,
    _mid: 'm' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
  })

  // 更新标题（取第一条用户消息作为标题）
  if (chat.title === '新对话') {
    chat.title = text.length > 20 ? text.slice(0, 20) + '...' : text
  }

  chat.updatedAt = new Date().toISOString()
  if (!preset) inputText.value = ''   // 重新回答时不清空用户正在输入的内容
  saveToStorage()
  await scrollToBottom()

  // 调用 Agent 流式接口（SSE）
  isLoading.value = true
  // 为本次请求创建 AbortController（暂停/新建对话时取消）
  const controller = new AbortController()
  activeAbort = controller
  // 本次请求的序号：后续所有对共享状态的写入都要先校验 isCurrent()
  const mySeq = ++reqSeq
  const isCurrent = () => mySeq === reqSeq
  // 占位助手消息（函数级声明：try 内的流式回写与 try 外的 catch 错误回写共用）
  let msg: any

  try {
    // 组装对话历史（最近 6 条）
    // 助手消息额外回传 sql / matched_tables / columns，供后端 _build_prev_context
    // 解析"把上面的改成按月"这类多轮指代（issue #9）
    const historyPayload = chat.messages
      .filter(m => m.content)
      .slice(-6)
      .map(m => {
        const base: any = { role: m.role === 'user' ? 'user' : 'assistant', content: m.content }
        if (m.role === 'assistant' && m.sql) {
          base.sql = m.sql
          base.matched_tables = m.matchedTables || []
          base.columns = m.columns || []
          base.rows = (m.rows || []).slice(0, 30)   // 会话记忆：回传结果行（封顶 30）
        }
        return base
      })

    // P0-修复：异步期间 history 可能被 authUser 初始化等流程整体替换（watch currentUserId），
    // sendMessage 开头的 chat 会变成孤儿引用 → 后续 push 进旧对象、界面不更新。
    // 写操作前重新定位当前对话的最新对象；若当前会话已不存在（账号切换 watch 已清空并重建），
    // 直接中止本次发送——占位/结果写进孤儿对象只会让用户看到"卡死不出结果"，且可能串号到新会话。
    const latestChat = history.value.find((h: any) => h.id === currentChatId.value)
    if (!latestChat) {
      const ab = new Error('会话已切换，本次请求已中止')
      ab.name = 'AbortError'
      throw ab
    }
    // 先插入一条占位助手消息，思考过程实时更新
    latestChat.messages.push({
      role: 'assistant',
        _mid: 'm' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
      content: '正在分析…',
      result: '',
      thinking: '',
      thinkingOpen: true,
      thinkingStreaming: true,
      query: text,           // 用户本轮问题，反馈闭环回传后端
      queryType: '',
      sql: '',
      matchedTables: [],
      columns: [],
    })
    // 注意：必须通过数组索引取回响应式对象，直接改本地对象不会触发视图更新
    // msg 声明在函数级（try 外），catch 分支（try 外作用域）也要引用它做孤儿定位/错误回写
    msg = latestChat.messages[latestChat.messages.length - 1]!
    chat = latestChat

    const resp = await fetch('/api/agent/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: text, history: historyPayload, no_confirm: opts?.noConfirm ?? false }),
      signal: controller.signal,
    })
    if (!resp.ok || !resp.body) {
      const err = await resp.json().catch(() => null)
      throw new Error(err?.detail || `请求失败(${resp.status})`)
    }

    // 解析 SSE 流
    const reader = resp.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''
    let finalData: any = null
    let lastRenderAt = 0   // 高频 token 渲染节流时间戳
    let rendered = false   // 洞察后置：done 是否已立即渲染（避免流结束二次 applyFinalData）

    const updateMsg = () => {
      if (finalData?.type === 'analysis_confirm') {
        // 兼容旧后端：当前后端版本已不再产生此事件（未命中口径直接走 LLM 生成 + 结果标注）。
        // 万一收到（旧进程）也绝不弹窗 —— 记录上下文后直接按理解生成结果。
        msg.metricResolution = null
        const q = finalData.query || msg.query || ''
        msg.analysisConfirm = {
          query: q,
          reason: finalData.reason || 'no_hit',
          hints: finalData.hints || [],
          analysis: finalData.analysis || null,
          error: '',
          genError: '',
        }
        msg.content = 'AI 正在按理解生成查询结果…'
        msg.thinkingStreaming = false
        analysisConfirm.value = msg.analysisConfirm
        handleAutoQuery()
        return
      }
      applyFinalData(msg, finalData)
    }

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      // 归一化换行（兼容代理/中间层改写为 \r\n\r\n 的分隔），按空行切事件（P2 修复）
      const norm = buffer.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
      const events = norm.split('\n\n')
      buffer = events.pop() || ''

      for (const evt of events) {
        // SSE 规范：事件内多个 data: 行拼接为一个字段（以 \n 连接），
        // 此处先收集全部 data 行再统一 JSON.parse（P2 修复：此前逐行独立解析，
        // 跨行 JSON 会被丢弃）
        const dataLines: string[] = []
        for (const line of evt.split('\n')) {
          if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
        }
        if (!dataLines.length) continue
        let payload: any
        try {
          payload = JSON.parse(dataLines.join('\n'))
        } catch {
          continue  // 畸形事件跳过，不因一个坏行丢弃已流式产生的全部内容
        }
        let handled = true
        let highFreq = false   // thought 逐 token 事件：高频，需节流渲染
        const type = payload.type
        if (type === 'step') {
            // 思考步骤提示，如：意图理解 / 表匹配 / SQL生成...
            // 「意图理解」步骤的 detail 会暴露内部意图标签（如 data/ml），对用户无价值，隐藏不展示
            if (payload.name !== '意图理解') {
              msg.thinking = (msg.thinking ? msg.thinking + '\n' : '') + `▶ ${payload.name || ''}${payload.detail ? '：' + payload.detail : ''}`
            }
            // 2026-10-03：后端在 step 事件里带 thought 字段（可读的「做了什么 + 为什么」）。
            // 它已经把 evidence 里的依据/涉及表/命中口径都拼进去了，信息量比 ▶ 那一行大得多，
            // 所以单独起一行展示；旧格式的 ▶ 行保留（作为步骤标题 + 兼容未升级的后端）。
            if (payload.thought) {
              msg.thinking = (msg.thinking ? msg.thinking + '\n' : '') + payload.thought
              // 首次出现思考内容 → 标记为流式生成中（驱动折叠面板自动展开 + 光标动画）
              msg.thinkingStreaming = true
            }
            // 实时更新底部「思考中…」占位为当前步骤动作
            const stepLabel: Record<string, string> = {
              '意图理解': '理解意图', '表匹配': '匹配数据表', '读取表结构': '读取表结构',
              'SQL生成': '生成 SQL', 'SQL执行': '执行查询', 'SQL重试': '修复 SQL',
              '兜底SQL': '使用备选查询', '结果复核': '复核结果', '图表生成': '生成图表',
              '记忆检索': '检索历史记忆', '思考中': '思考', 'ML建模': '建模准备',
              'ML方案设计': '设计建模方案', '意图分类': '分类意图',
              'AI推理': 'AI 推理生成方案', '语义缓存': '命中语义缓存',
            }
            streamingStep.value = `正在${stepLabel[payload.name || ''] || payload.name || '分析'}…`
            // P0-体验：no_hit 流程中「表匹配完成」后紧接长 LLM 推断（最长 ~25s），
            // 气泡文案改为「生成查询方案中」，避免停在「正在匹配数据表…」造成卡死错觉
            if (payload.name === '表匹配' && String(payload.detail || '').includes('已匹配') && msg.metricResolution) {
              streamingStep.value = '正在生成查询方案（AI 推断中），可点「停止」中止…'
            }
          } else if (type === 'thought') {
            // 流式思考内容（LLM token 逐字输出 / 后端推送的思考句）
            // 后端 step 事件已带整句 thought（见上），这里处理的是**逐 token 增量**：
            // 追加到当前未换行的最后一行里，形成打字机效果。
            const piece = payload.text || ''
            // ── 2026-10-03：token 级思考流（模型的真实 reasoning_content）──
            // 后端旁路直连模型、按 token 推来（payload.kind === 'reasoning'）。
            // 这类文本是**连续的一段**（可能半句、没有换行），不能按行追加 ——
            // 那样每来一个 token 都会换行，屏幕上是一堆碎片。
            // 正确做法：单独开一个 ⟦T⟧ 区块，后续 token 全部追加到它内部，
            // 形成一个「AI 正在写…」的气泡 + 打字机光标（真正的聊天气泡感）。
            if (payload.kind === 'reasoning') {
              if (!msg.thinkingLive) {
                msg.thinkingLive = true
                msg.thinking = (msg.thinking ? msg.thinking : '') +
                  (msg.thinking && !msg.thinking.endsWith('\n') ? '\n' : '') + '⟦T⟧'
              }
              msg.thinking = msg.thinking + piece
              highFreq = true
              msg.thinkingStreaming = true
              if (!streamingStep.value) streamingStep.value = 'AI 正在思考…'
              // ⚠️ 这里**不能** return：下面的 `if (handled) { if (highFreq) {节流渲染} }`
              // 才是真正让 Vue 重渲染 + 自动滚动的出口。提前 return 会让数据在累积
              // 但 DOM 停在第一个 token（实测：气泡出现但长度卡在 34 不动）。
              // 所以走「不 return、继续往下走」的路子——后面的分支都是 else-if，
              // 不会误处理，这里等价于「处理完了，去渲染」。
            } else {
              if (payload.step && msg.thinkingStep !== payload.step) {
                // 换步骤：先把上一行收尾换行，再起新行
                msg.thinking = (msg.thinking ? msg.thinking : '') + (msg.thinking && !msg.thinking.endsWith('\n') ? '\n' : '')
                msg.thinkingStep = payload.step
              }
              msg.thinking = (msg.thinking ? msg.thinking : '') + piece
              highFreq = true
              msg.thinkingStreaming = true
              if (!streamingStep.value || streamingStep.value === '正在生成 SQL…') {
                streamingStep.value = '正在生成 SQL…'
              }
            }
          } else if (type === 'thought_done') {
            // token 流收尾：闭合「正在写」气泡（后端对同一 step 只补发一次 done，
            // 这里按 step 判空，避免重复插入收尾标记）
            if (msg.thinkingLive) {
              msg.thinkingLive = false
              msg.thinking = (msg.thinking || '') + '\n⟦T-END⟧\n'
            }
            msg.thinking = (msg.thinking || '') + '\n'
            msg.thinkingStep = ''
          } else if (type === 'sql') {
            msg.thinking = (msg.thinking ? msg.thinking : '') + (msg.thinking && !msg.thinking.endsWith('\n') ? '\n' : '') + `📌 SQL：${payload.sql || ''}`
            // 实时写入独立 SQL 卡片：SQL 一生成用户立刻可见，不用等整轮结束
            msg.sql = payload.sql || ''
          } else if (type === 'sql_result') {
            // 查询结果先行渲染，让用户在 AI 还在写分析时就能看到数据（done 时会被完整结果覆盖）
            const rows = payload.rows || []
            if (rows.length > 0) {
              msg.result = '<div class="text-xs text-gray-400 mb-1"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <line x1="18" x2="18" y1="20" y2="10" /> <line x1="12" x2="12" y1="20" y2="4" /> <line x1="6" x2="6" y1="20" y2="14" /> </svg></span> 查询结果</div>'
                + renderTable(payload.columns || [], rows)
              msg.content = `已查询到 ${payload.row_count ?? rows.length} 条数据，正在分析…`
            }
          } else if (type === 'chart') {
            // 图表数据：先存入 msg 兜底；done 时 response.chart 也带 svg，统一渲染
            msg.chartSvg = payload.svg || msg.chartSvg || ''
          } else if (type === 'no_hit') {
            // P0-体验（2026-09-02）：口径未注册 → 秒渲染引导条（MetricNoHitCard），
            // 后台继续 LLM 推断；推断成功 done 会自动弹出二次确认方案，失败则由 done(no_hit) 收尾。
            msg.metricResolution = payload.metric_resolution
              || { status: 'no_hit', hints: payload.hints || [] }
            if (!msg.thinking) {
              msg.thinking = `▶ AI 推理：${payload.message || '正在后台生成查询方案…'}`
            }
            if (!streamingStep.value || streamingStep.value === '正在分析…' || streamingStep.value === '思考中…') {
              streamingStep.value = '正在生成查询方案…'
            }
          } else if (type === 'done') {
            streamingStep.value = ''
            finalData = payload.response || {}
            // 洞察后置（2026-10-01）：done 里 analysis 已是规则摘要，立即渲染完整结果
            // （表格+图表+规则摘要），不等流结束；LLM 洞察由后续 analysis 事件补发。
            if (finalData) {
              updateMsg()
              rendered = true
            }
          } else if (type === 'analysis') {
            // 洞察后置：补发 LLM 业务解读，覆盖 done 里的规则摘要。
            // 只做 analysis 区块 DOM 局部替换，不重新 applyFinalData（避免图表重复初始化）。
            if (finalData) {
              finalData.analysis = payload.text || ''
              const _mid = msg && msg._mid
              const _body = _mid
                ? document.querySelector<HTMLElement>(`.msg-item[data-mid="${_mid}"] [data-anl] .anl-conclusion-body`)
                : null
              if (_body) _body.innerHTML = renderAnalysis(finalData.analysis)
            }
          } else if (type === 'error') {
            streamingStep.value = ''
            throw new Error(payload.message || payload.content || 'Agent 执行失败')
          }
        if (handled) {
          if (highFreq) {
            // 逐 token 高频事件：节流渲染，避免每个字都强制一帧拖垮页面。
            // 2026-10-03 从 30ms 放宽到 90ms：上游 token 间隔中位 0.18s，
            // 30ms 节流等于几乎每帧都重排，用户反馈"太闪" —— 视觉抖动
            // 比信息更新更抢眼。90ms 既有"陆续浮现"的感觉，又足够稳。
            const now = Date.now()
            if (now - lastRenderAt > 90) {
              lastRenderAt = now
              await nextTick()
              await scrollToBottom()
            }
          } else {
            // 低频关键事件（步骤/SQL/结果）：强制让出渲染帧，确保逐条可见
            await nextTick()
            await new Promise(r => setTimeout(r, 0))
            await scrollToBottom()
            lastRenderAt = Date.now()
          }
        }
      }
    }

    // 流结束：flush 解码器内部缓冲（尾部多字节 UTF-8 字符可能被截断，P2 修复）
    buffer += decoder.decode()
    // 若 flush 后仍有完整事件残留（最后一段无空行结尾），补一次解析
    const normTail = buffer.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
    const tailEvents = normTail.split('\n\n')
    const lastEvt = tailEvents[tailEvents.length - 1]?.trim()
    if (lastEvt) {
      const dataLines: string[] = []
      for (const line of lastEvt.split('\n')) {
        if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
      }
      if (dataLines.length) {
        try {
          const payload = JSON.parse(dataLines.join('\n'))
          if (payload.type === 'done' && !finalData) finalData = payload.response || {}
          else if (payload.type === 'error' && !finalData) throw new Error(payload.message || 'Agent 执行失败')
          else if (payload.type === 'thought' && payload.text) {
            msg.thinking = (msg.thinking || '') + payload.text
          }
        } catch { /* 尾部畸形事件忽略 */ }
      }
    }

    // 流结束：仅当收到 done 事件（finalData 非空）才做最终整合——
    // 流被服务端干净关闭但未收到 done 时，保留已流式渲染的表格/图表，
    // 不做 applyFinalData(null) 的空覆盖（否则用户已见的结果瞬间消失，P1 修复）
    if (finalData && !rendered) {
      updateMsg()
    } else if (!finalData && msg && !msg.result && (!msg.content || msg.content === '正在分析…')) {
      msg.content = '对话已结束'
    }
    // 2026-10-01：无论是否收到 done 事件，都必须结束加载态。
    // 原逻辑只在上面两个分支里复位 thinkingStreaming ——「有 result 但没等到 done」
    // （服务端正常关闭连接 / 网关中途断开）时两个分支都不进，界面会一直转圈且无任何提示。
    if (msg) {
      msg.thinkingStreaming = false
      if (!finalData && msg.result) {
        msg.content = (msg.content && msg.content !== '正在分析…')
          ? msg.content
          : '结果可能不完整：连接在生成答案前已中断，请重试。'
      }
    }
    const owner = ownerChatOf(msg)
    if (owner) owner.updatedAt = new Date().toISOString()
    saveToStorage()
    await scrollToBottom()
  } catch (e: any) {
    // 用户主动暂停 / 会话被账号切换中止（abort）不视为错误
    if (e?.name === 'AbortError' || controller.signal.aborted) {
      // 保持已生成的部分内容，仅结束加载态；会话若已被切换（占位不在任何会话中）则直接收尾
      const owner = ownerChatOf(msg)
      if (owner && msg) {
        msg.content = msg.content && msg.content !== '正在分析…' ? msg.content : '已停止生成'
        msg.result = msg.result || ''
        msg.thinkingStreaming = false
        owner.updatedAt = new Date().toISOString()
      }
      saveToStorage()
      await scrollToBottom()
      return
    }
    // 网络中断（fetch 网络层失败，message 含 fetch/network 等关键词）：
    // 注意不能用 e instanceof TypeError 判定——本地代码 bug（如引用赋值错误）也是 TypeError，
    // 会被误报成"网络中断"且掩盖真实错误（本次"不出结果"的 const 赋值 bug 正是被它吞掉）。
    if (/fetch|network|ECONNRESET|socket/i.test(String(e?.message || ''))) {
      const owner = ownerChatOf(msg)
      if (owner && msg && msg.role === 'assistant') {
        if (msg.content && msg.content !== '正在分析…') {
          msg.content = msg.content + '\n\n⚠️ 网络中断，结果可能不完整，请重试'
        } else {
          msg.content = '⚠️ 网络中断，请重试'
        }
        msg.thinkingStreaming = false
        owner.updatedAt = new Date().toISOString()
      }
      saveToStorage()
      await scrollToBottom()
      return
    }
    // 其它错误（本地代码异常 / 后端返回业务错误）：把占位消息更新为可见的真实错误，
    // 绝不静默——否则用户只见"正在分析…"卡死而不知何故（可观测性是防回归的第一道闸）
    const owner = ownerChatOf(msg)
    if (owner && msg && msg.thinkingStreaming) {
      msg.content = '请求失败'
      msg.result = `<div class="text-sm text-red-500">${esc(e.message || '未知错误')}</div>`
      msg.thinkingStreaming = false
      owner.updatedAt = new Date().toISOString()
    }
    saveToStorage()
    await scrollToBottom()
  } finally {
    // 2026-10-03 修复：只在「我仍是当前请求」时复位共享状态，避免旧请求的 finally
    // 把新请求刚设好的 isLoading / activeAbort 覆盖掉（双流并发 + 暂停失效）
    if (isCurrent()) {
      isLoading.value = false
      activeAbort = null
    }
  }
}

// 供推荐问题快捷填充
window.__askSend = (q: string) => {
  inputText.value = q
}

// 结果区点击委托：推荐问题/澄清候选用 data-ask 属性承载原文，
// 点击时读取属性回填输入框（避免内联 onclick 注入 XSS）。
const onResultClick = (e: MouseEvent) => {
  // 推荐问题/澄清候选（data-ask）：回填输入框
  const askEl = (e.target as HTMLElement)?.closest?.('[data-ask]') as HTMLElement | null
  if (askEl) {
    const q = askEl.getAttribute('data-ask')
    if (q) inputText.value = q
    return
  }
  // 「去登记正确口径」（data-define-*）：打开口径定义，保存后按新口径重新生成
  const defEl = (e.target as HTMLElement)?.closest?.('[data-define-name]') as HTMLElement | null
  if (defEl) {
    const name = defEl.getAttribute('data-define-name') || ''
    const query = defEl.getAttribute('data-define-query') || ''
    openDefine(name, query)
  }
}

// ========== 回答操作：复制 / 重新回答 ==========

// 剪贴板写入：优先 navigator.clipboard，非安全上下文（如以内网 IP 访问）降级到 execCommand
const copyToClipboard = async (text: string): Promise<boolean> => {
  if (!text) return false
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch { /* 继续走降级方案 */ }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.top = '-9999px'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}

// 从 HTML 结果中提取纯文本（兼容刷新前保存的历史消息，它们没有 answerText）
const stripHtml = (html?: string): string => {
  if (!html) return ''
  const d = document.createElement('div')
  d.innerHTML = html
  return (d.textContent || '').replace(/\n{3,}/g, '\n\n').trim()
}

// 组装可复制的纯文本回答：问题 + SQL + 结果表格(TSV，可直接粘进 Excel) + 分析
// 通用最终数据渲染：流式 done 与二次确认「遵循执行」共用同一渲染路径
const applyFinalData = (msg: any, finalData: any) => {
  msg.content = finalData?.answer || (finalData?.type === 'data_query' ? `已查询到 ${(finalData.result?.rows || []).length} 条数据` : '已收到您的请求')
  msg.result = finalData ? renderAgentResult(finalData, msg) : ''
  msg.thinkingStreaming = false
  // F6 修复：流式完成（done 到达）后思考面板默认折叠（默认折叠体验，用户可再展开）
  msg.thinkingOpen = false
  // 结构化字段回写：供多轮指代解析与反馈记忆沉淀
  if (finalData) {
    // 提问即报告：问「生产周报」「不良设备报告」这类问题时，后端直接带回整份报告
    if (finalData.type === 'report') {
      msg.content = finalData.answer || '报告已生成'
      msg.reportHtml = finalData.report_html || ''
      msg.reportTitle = finalData.report_title || msg.query || '分析报告'
      msg.reportId = finalData.report_id || ''
      msg.thinkingStreaming = false
      msg.thinkingOpen = false
      msg.queryType = 'report'
      msg.answerText = `【问题】${msg.query || ''}\n【报告】${msg.reportTitle}`
      openReport(msg)   // 报告随答复到齐即弹预览；关掉后消息里的报告卡片仍能再打开
      return
    }
    msg.queryType = finalData.type
    msg.quality = finalData.quality || null
    // 引用文档（P0-A 混合问答）：[n] 标注，可点开核实原文
    msg.citedDocs = (finalData.cited_docs && finalData.cited_docs.length)
      ? finalData.cited_docs
      : (msg.citedDocs || null)
    // 口径管理（P1）：未命中反馈条信号 + 歧义澄清卡
    msg.metricResolution = finalData.metric_resolution || null
    if (finalData.type === 'metric_clarify') {
      msg.metricClarify = {
        hits: finalData.hits || [],
        answer: finalData.answer || '',
        query: finalData.query || msg.query || '',
      }
    }
    if (finalData.type === 'data_query') {
      // 保留流式期间已展示的 SQL，避免兜底路径返回空把卡片清掉
      msg.sql = finalData.sql || msg.sql || ''
      msg.matchedTables = finalData.matched_tables || []
      msg.columns = (finalData.result && finalData.result.columns) || []
      // 结果行（封顶 30）：多轮会话记忆用它提取"那 L01 呢"里的维度实体（P1-1）
      const _allRows = (finalData.result && finalData.result.rows) || []
      // 2026-10-03：另存真实行数，供 MD 导出表述「共 N 行」用 ——
      // 否则导出时只能拿到封顶后的 30 行，真实 5000 行时会写成「共 30 行」，
      // 与气泡上的「已查询到 5000 条数据」自相矛盾。
      msg.rowCount = (finalData.result && (finalData.result.row_count
        ?? finalData.result.rowCount)) || _allRows.length
      msg.rows = _allRows.slice(0, 30)
      msg.chartSvg = (finalData.chart && finalData.chart.svg) || msg.chartSvg || ''
      // 图表类型：存到 msg，供图表切换下拉默认值使用
      msg.chartType = (finalData.chart && finalData.chart.type) || msg.chartType || ''
      // 结果溯源（血缘）：每个结果列来自哪张表哪个字段（确定性静态解析，非 LLM 现编）
      msg.lineage = finalData.lineage && finalData.lineage.columns?.length
        ? finalData.lineage
        : (msg.lineage || null)
      // 推理过程（Show Work）：含每步的证据（输入/产出/依据），供用户追溯 AI 为什么这么做
      msg.steps = finalData.steps || msg.steps || []
    }
    msg.answerText = buildAnswerText(finalData, msg)
    nextTick(() => renderECharts())
    // P0-修复：流式期间消息数组原地 push，某些 KeepAlive/异步时序下 computed
    // currentMessages 不失效导致新增消息与内容更新不渲染（DOM 停留在旧条数）。
    // 替换数组引用强制 computed 重算 + 视图刷新（数据本身始终正确）。
    try {
      if (history.value.length) history.value = history.value.slice()
    } catch { /* ignore */ }
  }
}

const buildAnswerText = (data: any, msg: any): string => {
  if (!data) return msg?.content || ''
  const parts: string[] = []
  if (msg?.query) parts.push(`【问题】${msg.query}`)

  if (data.type === 'data_query') {
    const sql = data.sql || msg?.sql || ''
    if (sql) parts.push(`【SQL】\n${sql}`)

    const cols: string[] = data.result?.columns || []
    const rows: any[] = data.result?.rows || []
    if (cols.length && rows.length) {
      const body = rows.slice(0, 200)
        .map(r => cols.map(c => (r[c] === null || r[c] === undefined ? '' : String(r[c]))).join('\t'))
        .join('\n')
      parts.push(`【查询结果】(${rows.length} 行)\n${cols.join('\t')}\n${body}`)
    }
    if (data.analysis) parts.push(`【分析】\n${data.analysis}`)
  } else if (data.answer) {
    parts.push(data.answer)
  }

  return parts.join('\n\n') || msg?.content || ''
}

const copySql = async (msg: any) => {
  const ok = await copyToClipboard(msg?.sql || '')
  msg.copiedSql = ok
  if (ok) setTimeout(() => { msg.copiedSql = false }, 1800)
}

const copyAnswer = async (msg: any) => {
  const text = msg?.answerText || stripHtml(msg?.result) || msg?.content || ''
  const ok = await copyToClipboard(text)
  msg.copiedAnswer = ok
  if (ok) setTimeout(() => { msg.copiedAnswer = false }, 1800)
}

// 复制用户提问（2026-10-01）：与 copySql/copyAnswer 同一套剪贴板 + 瞬时提示模式
const copyUserMsg = async (msg: any) => {
  const ok = await copyToClipboard(msg?.content || '')
  msg.copiedUser = ok
  if (ok) setTimeout(() => { msg.copiedUser = false }, 1800)
}

// 重新回答：截断到本轮提问，用同一个问题重新走完整 Agent 流程
const regenerate = async (idx: number) => {
  if (isLoading.value) return
  const chat = history.value.find(h => h.id === currentChatId.value)
  if (!chat) return
  const target = chat.messages[idx]
  if (!target || target.role !== 'assistant') return

  // 本轮问题：优先取消息自带的 query，否则向上找最近一条用户消息
  let q = target.query || ''
  if (!q) {
    for (let i = idx - 1; i >= 0; i--) {
      const m = chat.messages[i]
      if (m && m.role === 'user') { q = m.content; break }
    }
  }
  if (!q) return

  const from = (idx > 0 && chat.messages[idx - 1]?.role === 'user') ? idx - 1 : idx
  chat.messages.splice(from)
  chat.updatedAt = new Date().toISOString()
  saveToStorage()
  await sendMessage(q)
}

// ========== 结果反馈闭环 ==========
// 用户标记"结果正确"→ 该 SQL 作为高质量样本沉淀进记忆（对齐 Vanna train on feedback），
// 标记"有误"→ 仅记录、不参与学习。这是 issue #8 质量门控 + issue #9 多轮改进的闭环收口。
const sendingFeedback = ref(false)

// ── 纠错反馈（带类型 + 说明 → 复核队列，管理员修正口径后持续改进）──
const feedbackTypes = ['口径不对', '结果不对', '图表不对', '数据范围不对', '其他']
const feedbackTarget = ref<any>(null)
const feedbackType = ref('口径不对')
const feedbackNote = ref('')
const feedbackSubmitting = ref(false)

const openFeedbackDialog = (msg: any) => {
  feedbackTarget.value = msg
  feedbackType.value = '口径不对'
  feedbackNote.value = ''
}

// ========== 图表类型切换（P1-2 对标 SpotterViz 简化版：不重跑 SQL）==========
// 2026-10-02 用户反馈：裁掉非常见图（玫瑰/雷达/漏斗）——普通用户看不懂、
// 且这些类型走 G2Plot 懒加载渲染链路更长。问答侧只保留 ECharts 常规 8 型；
// 雷达/热力等仍可在仪表盘看板使用（后端 dashboard_agent 不受影响）。
const chartTypeOptions = [
  { v: 'bar', label: '柱状图' },
  { v: 'barh', label: '横向柱状' },
  { v: 'stacked', label: '堆叠柱' },
  { v: 'dual', label: '双轴组合' },
  { v: 'line', label: '折线图' },
  { v: 'area', label: '面积图' },
  { v: 'pie', label: '饼图' },
  { v: 'donut', label: '环形图' },
  { v: 'scatter', label: '散点图' },
]

const hasChartData = (msg: any) =>
  !!(msg && msg.queryType === 'data_query' && msg.chartSvg && msg.columns && msg.columns.length > 0)

// 图型可渲染性（2026-10-02 用户反馈「选了还是柱状图」）：按消息数据形态禁用
// 不可渲染的选项——单指标数据禁用散点（需要 x/y 两个数值列）与堆叠柱
// （单系列堆叠=普通柱状，视觉上"没变"）、全 0 数据禁用饼/环。
// 否则选了会静默弹回初始柱状 SVG，看起来就像切换没生效。
const chartTypeRenderable = (msg: any, v: string) => {
  const cols: string[] = msg?.columns || []
  const rows: any[] = msg?.rows || []
  if (!cols.length || !rows.length) return true
  return coerceRenderableType(v, cols, rows) !== null
}
// 禁用项的悬停说明（告知用户为何灰掉，而不是让用户反复点击怀疑坏了）
const chartTypeDisabledTip = (msg: any, v: string) =>
  chartTypeRenderable(msg, v) ? '' : '当前数据不支持该图型（散点/堆叠/双轴需要两个及以上数值列），请先查询多指标数据'

const switchChartType = (msg: any, type: string) => {
  if (!msg || !type) return
  msg.chartTypeOverride = type
  nextTick(() => {
    const mid = msg._mid
    const node = mid
      ? document.querySelector<HTMLElement>(`.msg-item[data-mid="${mid}"] .echart`)
      : null
    if (!node) return
    // 复用容器里已有的全量数据（dataset.cols/rows），改类型重渲，不重新查询
    let cols: any[] = []
    let rows: any[] = []
    try {
      cols = JSON.parse(node.dataset.cols || '[]')
      rows = JSON.parse(node.dataset.rows || '[]')
    } catch {
      cols = []
      rows = []
    }
    if (!cols.length || !rows.length) {
      // 2026-10-03 修复（P1）：原实现直接静默 return。落盘瘦身（防 localStorage 配额爆掉）
      // 会把 data-rows 剥成 "[]"，但 data-cols 保留 → 刷新后下拉框正常渲染、用户选中
      // 「折线图」→ msg.chartTypeOverride 已写、UI 显示已切换，但这里早退 → 图表纹丝不动，
      // 且没有任何一行提示。这正是「切换图型后还是柱状图」在另一条路径上复活，
      // 比修复前更隐蔽（下拉框与图表互相矛盾）。
      // 现在：明确回退到容器里已有的 SVG，并恢复下拉框为初始图型，保证「所选=所显」。
      const fb = node.dataset.fallback || ''
      node.dataset.engine = 'svg'
      node.innerHTML = fb
      delete node.dataset.rendered
      // 下拉框是 :value="msg.chartTypeOverride || msg.chartType || ''" 响应式绑定，
      // 清空 override 即自动回到初始图型，无需手工改 DOM（保证「所选=所显」）
      msg.chartTypeOverride = ''
      return
    }
    node.dataset.type = type
    delete node.dataset.rendered
    disposeChart(node)
    ;(async () => {
      let engine = await renderChart(node, type, cols, rows)
      if (!engine) {
        // 数据形态不满足（如单指标选散点）→ 降级到最近可渲染图型重试，
        // 而不是直接弹回初始柱状 SVG（那正是"选了还是柱状图"的观感来源）。
        // 同步回写下拉值，保证所选=所显。2026-10-02 二次修复：降级链改用
        // downgradeChartType（coerce 现在只做"可否渲染"判定，null=不可渲染）。
        const alt = downgradeChartType(type, cols, rows)
        if (alt && alt !== type) {
          node.dataset.type = alt
          msg.chartTypeOverride = alt
          engine = await renderChart(node, alt, cols, rows)
        }
      }
      if (engine) {
        node.dataset.engine = engine
      } else if (node.dataset.fallback) {
        // 降级后仍失败 → 回退后端 SVG，不留空白
        node.innerHTML = node.dataset.fallback
        node.dataset.engine = 'svg'
      }
    })().catch(() => {
      // 切换失败：回退到容器里保存的后端 SVG（与 renderECharts 的兜底一致，不留空白）
      if (node.dataset.fallback) {
        node.innerHTML = node.dataset.fallback
        node.dataset.engine = 'svg'
      }
    })
  })
}

// ========== 提问生成报告（提问即报告，无按钮）==========
// 用户直接问「生产周报」「不良设备报告」，后端 report_intent 识别意图后，
// 报告随 done 事件整体带回，applyFinalData 自动打开预览。
// 与总览页的「AI 数据总览报告」不是一回事：那个扫全库出总览，这个跟着问题走。
const reportOpen = ref(false)
const reportHtml = ref('')
const reportTitle = ref('')
const reportMsg = ref<any>(null)   // 当前预览的报告属于哪条消息（下载时按 reportId 取同一份 HTML）

// 打开（或再次打开）某条消息的报告：报告 HTML 挂在消息对象上，弹窗关掉不会丢，
// 想再看时从消息里的报告卡片或操作栏的「打开报告」点进来即可，不用重新提问。
// 如果是刷新页面后从本地存储恢复的会话，消息里只剩 rid（HTML 没跟着存），
// 这时按 rid 向后端要回预览件——后端缓存过期就如实说，不摆一个点了没反应的按钮。
const openReport = async (msg: any) => {
  if (!msg) return
  let html = msg.reportHtml || ''
  if (!html && msg.reportId) {
    try {
      const r = await fetch(`/api/ask/report/file/${msg.reportId}?format=html`)
      if (!r.ok) {
        const d = await r.json().catch(() => ({}))
        throw new Error(d?.detail || `HTTP ${r.status}`)
      }
      html = await r.text()
      msg.reportHtml = html
    } catch (e: any) {
      alert('这份报告取不回来了：' + (e?.message || '网络错误'))
      return
    }
  }
  if (!html) return
  reportHtml.value = html
  reportTitle.value = msg.reportTitle || msg.query || '分析报告'
  reportMsg.value = msg
  reportOpen.value = true
}

// 报告文件基名：优先取目标消息自己的标题（从消息卡片直接下载时不依赖弹窗状态）
const reportFileBase = (msg?: any) => {
  const src = msg?.reportTitle || reportTitle.value || msg?.query || reportMsg.value?.query || '分析报告'
  const t = String(src).trim()
  return t.replace(/[\\/:*?"<>|]/g, '_').slice(0, 40)
}

const closeReport = () => {
  reportOpen.value = false
  reportHtml.value = ''
  reportTitle.value = ''
  reportMsg.value = null
}

// 在新标签打开：用 Blob URL 把这份 HTML 当独立页面打开，方便整页阅读/直接 Ctrl+P 存 PDF
const openReportInTab = () => {
  if (!reportHtml.value) return
  const blob = new Blob([reportHtml.value], { type: 'text/html;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  window.open(url, '_blank')
  // 给浏览器留出加载时间再回收，立刻 revoke 会导致新标签白屏
  setTimeout(() => URL.revokeObjectURL(url), 60000)
}

// 下载 Word / PDF：走后端导出。提问即报告链路的消息带 reportId ——
// 按 rid 取回预览的那份 HTML 原样转文档，预览看到什么下载到的就是什么；
// 旧消息（无 rid）退回按 question+sql 重新取数的导出路径。
// target：从消息卡片直接下载时传这条消息（不传就用当前弹窗里的那条）
const downloadReport = async (format: 'docx' | 'pdf', target?: any) => {
  const msg = target || reportMsg.value
  if (!msg) return
  try {
    let resp: Response
    if (msg.reportId) {
      resp = await fetch(`/api/ask/report/file/${msg.reportId}?format=${format}`)
    } else {
      resp = await fetch(`/api/ask/report/download?format=${format}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question: msg.query || '',
          sql: msg.sql || '',
          title: reportTitle.value || msg.query || '',
        }),
      })
    }
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}))
      throw new Error(d?.detail || `导出失败（HTTP ${resp.status}）`)
    }
    const blob = await resp.blob()
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `${reportFileBase(msg)}.${format}`
    a.click()
    setTimeout(() => URL.revokeObjectURL(a.href), 3000)
  } catch (e: any) {
    alert('导出失败：' + (e?.message || '网络错误'))
  }
}

// ========== 答案导出（P0-3 对标 sharing：Markdown / PNG）==========
// Markdown 表格单元格转义（P2 修复）：含 | 换行时结构会错乱
const mdCell = (v: any): string => String(v ?? '')
  .replace(/\|/g, '\\|')
  .replace(/\r?\n/g, ' ')

const exportMarkdown = (msg: any) => {
  if (!msg) return
  const lines: string[] = []
  lines.push(`# ${msg.query || '查询结果'}`)
  if (msg.sql) lines.push(`\n## SQL\n\n\`\`\`sql\n${msg.sql}\n\`\`\``)
  const cols: string[] = msg.columns || []
  const rows: any[] = msg.rows || []
  if (cols.length && rows.length) {
    lines.push('\n## 数据\n')
    lines.push('| ' + cols.map(mdCell).join(' | ') + ' |')
    lines.push('| ' + cols.map(() => '---').join(' | ') + ' |')
    rows.slice(0, 20).forEach((r) => {
      lines.push('| ' + cols.map((c) => mdCell(r[c])).join(' | ') + ' |')
    })
    // 2026-10-03 修复：msg.rows 在 applyFinalData 里被刻意封顶到 30 行（供多轮会话
    // 记忆提取实体），原代码直接用它算「共 N 行」→ 真实结果 5000 行时这里恒显示
    // 「共 30 行」，而气泡上写着「已查询到 5000 条数据」，用户会把截断的表当完整
    // 结果去做分析。改为优先用消息上记录的真实行数。
    const totalRows = (msg as any).rowCount || rows.length
    if (totalRows > 20) lines.push(`\n> 共 ${totalRows} 行，仅导出前 20 行`)
  }
  const analysis = (msg.content || '').slice(0, 5000)
  if (analysis) lines.push(`\n## 分析\n\n${analysis}`)
  const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `${(msg.query || '答案').slice(0, 30)}.md`
  a.click()
  setTimeout(() => URL.revokeObjectURL(a.href), 3000)
}

// SVG 字符串 → PNG dataURL（canvas 绘制，带白色背景防透明）
// P2 修复：加 6s 超时保护，畸形/超大 SVG 导致 onload/onerror 永不回调时不会永久挂起；
// url 提前声明并判空后回收，避免同步异常路径下超时回调引用未初始化变量抛错。
const svgToPngDataUrl = (svg: string): Promise<string> =>
  new Promise((resolve, reject) => {
    let url = ''
    const timer = setTimeout(() => {
      if (url) URL.revokeObjectURL(url)
      reject(new Error('SVG 渲染超时（文件过大或格式异常）'))
    }, 6000)
    try {
      const img = new Image()
      const blob = new Blob([svg], { type: 'image/svg+xml;charset=utf-8' })
      url = URL.createObjectURL(blob)
      img.onload = () => {
        clearTimeout(timer)
        try {
          const w = img.naturalWidth || 800
          const h = img.naturalHeight || 400
          const canvas = document.createElement('canvas')
          canvas.width = w * 2
          canvas.height = h * 2
          const ctx = canvas.getContext('2d')
          if (!ctx) throw new Error('canvas unavailable')
          ctx.fillStyle = '#ffffff'
          ctx.fillRect(0, 0, canvas.width, canvas.height)
          ctx.scale(2, 2)
          ctx.drawImage(img, 0, 0, w, h)
          URL.revokeObjectURL(url)
          resolve(canvas.toDataURL('image/png'))
        } catch (e) {
          URL.revokeObjectURL(url)
          reject(e)
        }
      }
      img.onerror = (e) => { clearTimeout(timer); URL.revokeObjectURL(url); reject(e) }
      img.src = url
    } catch (e) {
      clearTimeout(timer)
      if (url) URL.revokeObjectURL(url)
      reject(e)
    }
  })

const exportPng = async (msg: any) => {
  if (!msg) return
  let dataUrl = ''
  // 优先 ECharts 实例（矢量级高清导出）
  try {
    const mid = msg._mid
    const node = mid
      ? document.querySelector<HTMLElement>(`.msg-item[data-mid="${mid}"] .echart`)
      : null
    if (node) {
      const inst = echarts.getInstanceByDom(node)
      if (inst) dataUrl = inst.getDataURL({ pixelRatio: 2, backgroundColor: '#ffffff' })
    }
  } catch { /* 回退 SVG 转 PNG */ }
  if (!dataUrl && msg.chartSvg) {
    try { dataUrl = await svgToPngDataUrl(msg.chartSvg) } catch { /* 保留空 */ }
  }
  if (!dataUrl) {
    window.alert('当前结果没有可导出的图表')
    return
  }
  const a = document.createElement('a')
  a.href = dataUrl
  a.download = `${(msg.query || '图表').slice(0, 30)}.png`
  a.click()
}

const submitFeedbackReport = async () => {
  if (!feedbackTarget.value) return
  feedbackSubmitting.value = true
  try {
    const resp = await fetch('/api/feedback/report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query: feedbackTarget.value.query || '',
        sql: feedbackTarget.value.sql || '',
        executed_sql: feedbackTarget.value.sql || '',
        feedback_type: feedbackType.value,
        note: feedbackNote.value,
      }),
    })
    const data = await resp.json()
    if (!resp.ok) throw new Error(data?.detail || '提交失败')
    const msg = feedbackTarget.value
    msg.feedbackGiven = true
    msg.feedbackText = '✅ 已提交纠错反馈（进入复核队列）'
    feedbackTarget.value = null
  } catch (e: any) {
    alert('提交失败：' + (e?.message || '网络错误'))
  } finally {
    feedbackSubmitting.value = false
  }
}

const sendFeedback = async (msg: any, correct: boolean) => {
  if (sendingFeedback.value || msg.feedbackGiven) return
  sendingFeedback.value = true
  try {
    const res = await fetch('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query: msg.query || '',
        sql: msg.sql || '',
        correct,
        table_name: (msg.matchedTables && msg.matchedTables[0]?.table_name) || '',
      }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data?.detail || '提交失败')
    msg.feedbackGiven = true
    if (correct) {
      msg.feedbackText = data?.message || '✅ 已学习该正确 SQL，下次同类问题将直接参考'
    } else {
      msg.feedbackText = '已记录，该结果不会用于学习'
    }
  } catch (e: any) {
    // 失败不置 feedbackGiven（P1 修复）：允许用户重试——
    // 此前失败也被标记，按钮永久隐藏，正确 SQL 无法沉淀进记忆影响准确率
    msg.feedbackText = '反馈提交失败：' + (e?.message || '网络错误')
  } finally {
    sendingFeedback.value = false
    // 反馈数据也随历史一起持久化，避免刷新后丢失状态
    saveToStorage()
  }
}

// ========== 存储（历史记录按用户隔离）==========
// 「智能问析记忆」按当前登录用户隔离：不同账号看到各自的对话历史，互不可见。
// 未登录游客共用 guest 命名空间；登录用户用 username 命名空间。
const authUserInjected = inject<{ value: { username?: string } | null } | null>('authUser', null)
const currentUserId = () => {
  try {
    const u = authUserInjected?.value || getUser()
    return u?.username || 'guest'
  } catch { return 'guest' }
}
let activeUserId = currentUserId()
const storageKeyFor = (uid: string) => `ask_chat_history:${uid}`

const saveToStorage = () => {
  const key = storageKeyFor(activeUserId)
  // F2 修复：运行时消息封顶（每会话保留最近 60 条），防 KeepAlive 常驻下 messages 数组
  // 与 localStorage 无限膨胀（此前仅配额满才截断）
  // 2026-09-03 追加：历史会话数也封顶（保留最近 24 个），防多账号/长会话把本地存储写满
  // 导致登录写入 sqlbot_auth_user 配额超限而失败。
  // 修复（P0）：只裁剪「待写入存储的副本」，绝不回写 history.value。
  // 原实现把截断结果赋值回内存 → 会话数 >24 时最旧会话在内存中也被删掉、
  // 长对话第 61 条起消息永久消失（localStorage 里同样已删，无法恢复），
  // 且每次调用都替换整个数组引用，导致整页 computed / v-for 全量重渲染。
  // 2026-10-01 修复：history 数组是「最新在前」（createNewChat 用 unshift），
  // slice(-24) 保留的其实是最旧的 24 个、会静默丢弃最新会话，改为 slice(0, 24)
  const payload = history.value
    .slice(0, 24)
    .map((c: any) => ({
      ...c,
      // 报告 HTML 一份几十 KB，写进本地存储（总配额 5MB）会把对话历史挤掉：
      // 只留 rid，下次要打开时按 rid 从后端把预览件取回来（见 openReport）
      // 2026-10-03 修复：图表容器 result 里内嵌了**全量 rows 的 JSON**
      // （data-rows="[...]"），一条返回几千行的结果就是几百 KB —— 作者显然知道
      // 体积问题（专门剥离了 reportHtml、也把 msg.rows 封顶 30 行），却漏了这块
      // 更大的。几轮之后 5MB 配额写满 → 一路降级到"本轮不持久化"，
      // 用户刷新页面整段对话消失且只 console.warn 不报错。
      // 落盘时把 result 里的 data-rows 清空：刷新后该消息的 .echart 容器拿不到
      // cols/rows，renderECharts 会走 fallback 分支显示后端 SVG，结果依然完整可见
      // （与报告走 rid 回取是同一思路）。
      messages: (c.messages || []).slice(-60).map((m: any) => {
        if (!m) return m
        const { reportHtml, ...rest } = m as any
        if (typeof rest.result === 'string' && rest.result.includes('data-rows=')) {
          rest.result = rest.result.replace(/data-rows="[^"]*"/g, 'data-rows="[]"')
        }
        return rest
      }),
    }))
  try {
    localStorage.setItem(key, JSON.stringify(payload))
  } catch (e: any) {
    // 配额满（P2 修复）：分两级降级——先截断每条对话消息数，再丢 thinking 大字段，
    // 避免「静默失败 → 刷新丢全部对话」
    console.warn('保存历史记录失败，尝试截断:', e?.name)
    try {
      const slim = history.value.map((c: any) => ({
        ...c,
        messages: (c.messages || []).slice(-30),
      }))
      localStorage.setItem(key, JSON.stringify(slim))
    } catch (e2: any) {
      console.warn('历史记录仍超出配额，二级降级（丢弃推理过程）:', e2?.name)
      try {
        const slimmer = history.value.map((c: any) => ({
          ...c,
          messages: (c.messages || []).slice(-20).map((m: any) => {
            const { thinking, ...rest } = m
            return rest
          }),
        }))
        localStorage.setItem(key, JSON.stringify(slimmer))
      } catch {
        // 2026-09-03：本地存储被其它数据(历史/消息中心)写满 → 全局治理后重试一次
        console.warn('历史记录仍超出配额，执行全局清理后重试…')
        try {
          pruneLocalStorage()
          localStorage.setItem(key, JSON.stringify(history.value
            .slice(0, 12)
            .map((c: any) => ({ ...c, messages: (c.messages || []).slice(-20) }))))
        } catch {
          console.warn('历史记录超出存储配额，本轮不持久化（内存中仍可用）')
        }
      }
    }
  }
}

const loadFromStorage = () => {
  try {
    const data = localStorage.getItem(storageKeyFor(activeUserId))
    if (data) {
      const parsed = JSON.parse(data)
      if (Array.isArray(parsed) && parsed.length > 0) {
        history.value = parsed
        currentChatId.value = parsed[0].id
        return true
      }
    }
  } catch (e) {
    console.warn('加载历史记录失败:', e)
  }
  return false
}

// ========== 初始化 ==========
onMounted(() => {
  // 加载当前 LLM 模型名（顶部模型栏显示）
  loadLlmConfig()

  // 根据当前数据库重点动态加载引导问题
  loadQuickQuestions()

  // 尝试从 localStorage 加载
  const hasHistory = loadFromStorage()

  // 如果没有历史记录，创建默认对话
  if (!hasHistory) {
    createNewChat()
  }

  // 初始问题必须在 loadFromStorage 之后消费（原因见 consumeInitialQuestion 上方注释）：
  // 顺序反了会把本地历史覆盖成单条新会话。
  mountedReady.value = true
  consumeInitialQuestion()

  scrollToBottom()
})

onActivated(() => {
  // KeepAlive 切回时：①重新渲染图表（P2 修复：流式期间切走页面，done 到达时 DOM 已隐藏
  // → 图表空白，切回后兜底补渲染）；②滚动到底部——停留在最新问题，而不是停留在第一个问题
  nextTick(() => {
    // F4 修复：先清除本页图表节点的 rendered 标记再重渲（离屏 0 宽渲染过的节点已标
    // rendered，renderECharts 会短路跳过 → 切回仍空白）
    askRootEl.value?.querySelectorAll<HTMLElement>('.echart').forEach((el) => delete el.dataset.rendered)
    renderECharts()
    scrollToBottom()
  })
})

// 2026-10-03 修复（内存泄漏）：本页被 KeepAlive 包裹（上面有 onActivated 即为证），
// 切页只触发 deactivate，onUnmounted 永远不执行 → 原先只在 onUnmounted 里调用的
// disposeAllCharts 形同虚设。加上 deactivate 时就释放（onActivated 会重渲，无需保留）。
onDeactivated(() => {
  disposeAllCharts()
})

// 登录态切换（登出/换账号）→ 切换「智能问析记忆」命名空间：不同账号的历史记录互不可见。
// 登出会整页刷新（无需此处），但「A 登出后 B 登录」不刷新，必须在此重新加载 B 的历史。
watch(
  () => currentUserId(),
  (uid) => {
    if (uid === activeUserId) return
    // 账号切换（登出/换号/401 被动登出）：先中止在途流式请求——
    // 否则旧会话的 fetch 继续运行，done 到达时把结果写进已被清空的孤儿 chat（不渲染不保存），
    // 或 latestChat 重定位到新会话造成跨账号串号。中止后 sendMessage 走 abort 分支安全收尾。
    stopGenerating()
    // 关闭确认弹窗并清空引用：其消息对象即将随 history 整体清空，避免指向已脱离的旧会话
    closeConfirmDialog()
    activeUserId = uid
    history.value = []
    currentChatId.value = null
    const hasHistory = loadFromStorage()
    if (!hasHistory) createNewChat()
    scrollToBottom()
  }
)

onUnmounted(() => {
  // 清理所有 ECharts 实例与共享 ResizeObserver，避免 KeepAlive 场景下泄漏
  disposeAllCharts()
  // 清理挂到 window 的全局快捷提问函数，避免残留
  if ((window as any).__askSend) {
    delete (window as any).__askSend
  }
})

// 监听消息变化，滚动到底部。
// F1 修复：不再 deep 监听（展开/折叠/复制/反馈等任意字段变更都会强制滚到底部，
// 打断用户阅读历史）；只按「消息数量与 _mid 序列」变化触发。
watch(currentMessages, (nw, old) => {
  const key = (arr: any[]) => arr.length + '|' + arr.map((m) => (m as any)._mid ?? (m as any).id ?? '').join(',')
  if (old === undefined || key(nw) === key(old)) return
  scrollToBottom()
  nextTick(() => renderECharts())   // 历史消息/切换对话后补渲染图表（ECharts / G2Plot 双引擎）
})

// 监听外部传入的初始问题（来自分析模板 / 快捷问题 / 总览建议）
//
// ⚠️ 此处**绝不能用 immediate:true**（2026-10-01 修复）：
// immediate 的 watcher 在 setup 同步阶段执行，早于 onMounted 里的 loadFromStorage()。
// 而 consumeInitialQuestion → createNewChat() 会立刻 saveToStorage() 落盘，
// 把「只有 1 条新会话」的数组写进 localStorage；等 loadFromStorage() 再去读，
// 读到的就是被自己覆盖后的数据 —— 该用户此前的全部会话永久丢失，且无任何提示。
// 最常见触发路径：知识页「应用模板」/ Cmd+K 提问（App.vue navigateToAsk）。
// 因此首次值改由 onMounted 在 loadFromStorage 之后消费，watcher 只处理挂载后的变化。
const mountedReady = ref(false)

const consumeInitialQuestion = () => {
  const q = String(props.initialQuestion || '').trim()
  if (!q) return
  // 复用当前会话：若它还是刚创建的空会话（只有一条欢迎语），不要再建一个空的
  // （onMounted 里无历史时会先 createNewChat，否则会留下两条「新对话」）
  const cur: any = history.value.find((h: any) => h.id === currentChatId.value)
  const isBlank = !cur || (cur.messages || []).length <= 1
  if (!isBlank) {
    createNewChat()
  }
  nextTick(() => {
    emit('question-consumed')
    sendMessage(q)
  })
}

watch(() => props.initialQuestion, (question) => {
  if (!mountedReady.value) return   // 首次值交给 onMounted，避免早于 loadFromStorage
  if (question && question.trim()) {
    consumeInitialQuestion()
  }
})
</script>

<style scoped>
/* ====== 聊天气泡（2026-09-14 UI 精修 v2）======
   用户气泡：品牌渐变 + 右下小圆角（对话方向感）；
   AI 气泡：白底描边 + 柔和投影 + 左下小圆角，替代原平灰块，与结果卡同属白色系更统一。
   渐变走 scoped 样式而非 Tailwind 类：全局规则会把 workspace-content 内
   bg-gradient-to-* 类卡片压成白底（防多色渐变），scoped 不受影响。 */
.ask-bubble {
  padding: 10px 16px;
  font-size: 14px;
  line-height: 1.65;
  border-radius: 16px;
  word-break: break-word;
}
.ask-bubble-user {
  border-bottom-right-radius: 4px;
  color: #ffffff;
  background: linear-gradient(135deg, #5FAEFF 0%, #2E7CF0 100%);
  box-shadow: 0 4px 14px rgba(46, 124, 240, .28);
}
.ask-bubble-ai {
  border-bottom-left-radius: 4px;
  border: 1px solid #e5e6eb;
  color: #374151;
  background: #ffffff;
  box-shadow: 0 1px 2px rgba(16, 24, 40, .04), 0 8px 22px -10px rgba(16, 24, 40, .07);
}
.ask-bubble-loading {
  color: #6b7280;
}
/* 悬浮式输入舱（2026-09-14 UI v3）：脱离底边的浮起白卡，替代原贴边输入条 */
.ask-composer-wrap {
  padding: 10px 14px 14px;
  background: linear-gradient(180deg, rgba(255, 255, 255, 0) 0%, rgba(240, 247, 255, .55) 100%);
}
.ask-composer {
  padding: 12px 14px;
  border: 1px solid #e5e6eb;
  border-radius: 16px;
  background: #ffffff;
  box-shadow: 0 2px 4px rgba(16, 24, 40, .04), 0 18px 44px -14px rgba(46, 124, 240, .18);
}
/* 发送按钮：品牌渐变 + 悬浮浮起（同气泡渐变，输入区视觉焦点） */
.ask-send-btn {
  background: linear-gradient(135deg, #5FAEFF 0%, #2E7CF0 100%);
  box-shadow: 0 2px 8px rgba(46, 124, 240, .25);
}
.ask-send-btn:hover:not(:disabled) {
  background: linear-gradient(135deg, #4D9EFF 0%, #1F66D6 100%);
  box-shadow: 0 8px 20px -4px rgba(46, 124, 240, .45);
  transform: translateY(-1px);
}
.ask-send-btn:active:not(:disabled) {
  transform: translateY(0) scale(.98);
}
/* 思考面板正文。⚠️ 面板内容全部由 `v-html`（formatThinking）注入，
   而本组件是 `<style scoped>` —— v-html 动态插入的元素不带 [data-v-xxx] 属性，
   scoped 选择器对它们**完全失效**（2026-10-03 实测：.tk-act 的背景色算出来是
   transparent、圆角 0px，整段思考是纯文本无样式）。
   因此凡是作用于面板**内部**元素的选择器都必须用 :deep() 穿透。 */
.tk-panel { color: #4b5563; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.tk-panel :deep(.tk-step) { display: flex; align-items: flex-start; gap: 6px; padding: 3px 0; }
.tk-panel :deep(.tk-dot) { flex: none; width: 6px; height: 6px; margin-top: 5px; border-radius: 9999px; background: #4D9EFF; }
.tk-panel :deep(.tk-step-name) { font-weight: 600; color: #1f2937; white-space: pre-wrap; word-break: break-word; }
.tk-panel :deep(.tk-reason) { padding: 2px 0 2px 12px; color: #6b7280; white-space: pre-wrap; word-break: break-word; }

/* ===== 思考过程：DeepSeek 风格（2026-10-03 改版）=====
   用户反馈上一版"太闪了"。对照 DeepSeek 的实际观感重做，核心是**把动效全部拿掉**：
     ✗ 打字机光标（1s 闪烁）      ✗ 标题栏脉冲圆点（animate-pulse）
     ✗ 左侧蓝色竖条（视线锚点太抢）  ✗ 渐变底 + 上浮淡入动画
   改成的形态（对齐 DeepSeek「已深度思考（用时 8 秒）」那种块）：
     · 整块浅灰底 + 圆角，无边框色条
     · 标题行低调：小图标 + 灰色小字「已思考 / 正在思考」
     · 正文浅灰、等宽以外的常规字重，靠**内容**而非动效吸引注意
   动效只保留一处：标题前的 8px 小圆点用极慢呼吸（2.4s）表示"在跑"，
   因为完全静止会让用户以为卡死 —— 但绝不闪烁。
   ⚠️ 下面作用于面板**内部**的选择器都必须带 :deep()（v-html 不带 scoped 属性）。 */
.tk-panel :deep(.tk-line) {
  display: flex; align-items: baseline; gap: 6px;
  padding: 3px 0 3px 2px; line-height: 1.6;
}
.tk-panel :deep(.tk-act) {
  flex: none;
  font-weight: 600; font-size: 11px; color: #6b7280;
  background: #EEF1F5; border: 1px solid #E3E8EF;
  padding: 1px 7px; border-radius: 5px; white-space: nowrap;
}
/* .tk-rest / .tk-tag 在 v-html 内，同样要 :deep 穿透 */
.tk-panel :deep(.tk-rest) { color: #8A94A3; white-space: pre-wrap; word-break: break-word; }
.tk-panel :deep(.tk-rest .tk-tag) { color: #A3ABB8; font-weight: 500; }

/* token 级思考流：模型的真实 reasoning_content。
   DeepSeek 式的处理——**不加任何边框与底色块**，直接作为一段浅灰正文
   接在决策链下面，靠「AI 思考」这个小标题区分段落即可。
   之前做成带蓝竖条的"气泡"，视觉上跳出来太多，盯着看会晃。 */
.tk-panel :deep(.tk-live) {
  display: block;
  margin: 8px 0 4px; padding: 0 0 0 2px;
  line-height: 1.8; color: #7A8494;
  font-size: 12px;
  white-space: pre-wrap; word-break: break-word;
}
.tk-panel :deep(.tk-live-tag) {
  display: block; margin-bottom: 2px;
  font-size: 10.5px; font-weight: 600; color: #AAB2BF;
  letter-spacing: .4px;
}
.tk-panel :deep(.tk-live-text) { color: #7A8494; }

@keyframes tk-breathe { 0%, 100% { opacity: .35; } 50% { opacity: 1; } }

@media (prefers-reduced-motion: reduce) {
  .ask-collapse-spin { animation: none !important; }
}

/* .tk-sql 同样在 v-html 内，需 :deep 穿透（见上方 .tk-step 处的说明） */
.tk-panel :deep(.tk-sql) {
  margin: 4px 0 4px 12px; padding: 6px 8px; background: #0f172a; color: #e2e8f0;
  border-radius: 6px; font-size: 10.5px; line-height: 1.45; white-space: pre-wrap;
  word-break: break-word; overflow-x: auto; max-height: 180px;
}
/* 独立 SQL 卡片正文：深色代码块，与浅色「分析结果」形成明确视觉区分 */
.sql-code {
  margin: 0;
  /* 2026-10-02 修复：此前漏设 color/padding——继承浅色区深灰文字落在
     #0F172A 深底上几乎不可见，且 SQL 顶格贴边。显式给浅色 + 内边距。 */
  padding: 12px 14px;
  color: #D8E4F5;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12.5px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 240px;
  overflow-y: auto;
}

/* ===== 空态（2026-09-27 重设计）：居中构图，对话开始后整块让位 ===== */
.ask-empty {
  min-height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: 16px 0 8px;
}
.ask-empty-mark {
  width: 56px;
  height: 56px;
  border-radius: 16px;
  background: linear-gradient(135deg, #4D9EFF, #2E7CF0);
  box-shadow: 0 8px 22px rgba(46, 124, 240, .25);
  color: #fff;
  font-family: Rajdhani, 'Segoe UI', sans-serif;
  font-weight: 700;
  font-size: 30px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.ask-empty-title {
  margin-top: 22px;
  font-size: 25px;
  font-weight: 600;
  color: #111827;
}
.ask-empty-sub {
  margin-top: 10px;
  font-size: 13.5px;
  color: #6b7280;
}
.ask-empty-sub b { color: #374151; font-weight: 600; }
.ask-empty-scenes {
  display: flex;
  gap: 12px;
  margin-top: 30px;
  width: 100%;
  max-width: 708px;
}
.ask-scene {
  flex: 1;
  text-align: left;
  background: #fff;
  cursor: pointer;
  border: 1px solid #e5e7eb;
  border-radius: 13px;
  padding: 15px 16px 16px;
  transition: all .16s ease;
}
.ask-scene:hover {
  border-color: #7DB5FF;
  box-shadow: 0 6px 18px rgba(46, 124, 240, .12);
  transform: translateY(-2px);
}
.ask-scene-head { display: flex; align-items: center; gap: 8px; }
.ask-scene-chip {
  width: 30px;
  height: 30px;
  border-radius: 9px;
  flex-shrink: 0;
  background: #EAF3FF;
  color: #2E7CF0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 15px;
}
.ask-scene-tag { font-size: 13px; font-weight: 600; color: #1f2937; }
.ask-scene-q {
  display: block;
  margin-top: 11px;
  font-size: 12.8px;
  line-height: 1.65;
  color: #6b7280;
}
.ask-scene:hover .ask-scene-q { color: #374151; }
.ask-empty-composer {
  margin-top: 28px;
  width: 100%;
  max-width: 708px;
  height: 50px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 8px 0 18px;
  border: 1px solid #d9dfe9;
  border-radius: 15px;
  background: #fff;
  box-shadow: 0 2px 10px rgba(17, 24, 39, .05);
}
.ask-empty-composer:focus-within {
  border-color: #4D9EFF;
  box-shadow: 0 0 0 3px rgba(77, 158, 255, .14);
}
.ask-empty-input {
  flex: 1;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  font-size: 13.5px;
  color: #1f2937;
}
.ask-empty-input::placeholder { color: #9ca3af; }
.ask-empty-send {
  width: 36px;
  height: 36px;
  border-radius: 11px;
  border: none;
  cursor: pointer;
  flex-shrink: 0;
  background: linear-gradient(135deg, #4D9EFF, #2E7CF0);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 17px;
  box-shadow: 0 3px 8px rgba(46, 124, 240, .3);
  transition: all .15s ease;
}
.ask-empty-send:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 5px 14px rgba(46, 124, 240, .4); }
.ask-empty-send:disabled { opacity: .45; cursor: not-allowed; }
.ask-empty-hint {
  margin-top: 14px;
  font-size: 12px;
  color: #b0b7c3;
  display: flex;
  align-items: center;
  gap: 5px;
}

/* ===== 结果回答卡（2026-09-29 信息分层重构）：去蓝色描边、柔白底 + 品牌头部，
   与结论区同属「柔白 + 品牌蓝点缀」一套语言，弱化边框强化留白 ===== */
.ask-result-card {
  margin-top: 10px;
  border-radius: 14px;
  background: #ffffff;
  border: 1px solid var(--line-200);
  box-shadow: var(--shadow-card);
  overflow: hidden;
}
.ask-result-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 11px 16px;
  border-bottom: 1px solid var(--line-100);
  background: linear-gradient(180deg, #FAFCFF 0%, #FFFFFF 100%);
}
.ask-result-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--ink-900);
}
.ask-result-title-dot {
  width: 8px;
  height: 8px;
  border-radius: 9999px;
  background: linear-gradient(135deg, var(--brand-400), var(--brand-600));
  box-shadow: 0 1px 4px rgba(46, 124, 240, .4);
}
.ask-result-status {
  font-size: 11px;
  color: var(--ink-400);
  letter-spacing: .02em;
}
.ask-result-status-done { color: var(--brand-600); }
.ask-result-body {
  padding: 16px 18px 18px;
  font-size: 13.5px;
  line-height: 1.7;
  color: var(--ink-700);
}

/* ===== 折叠卡统一语言（思考过程 / 溯源 / Show Work / 引用文档）=====
   一套「浅灰底 + 统一表头（图标 + 名称 + 状态）」的折叠卡，替代原先深浅不一的散卡 */
/* ===== 思考过程折叠块（2026-10-03 改 DeepSeek 风格）=====
   这一组 .ask-collapse-* 也被「数据溯源（血缘）」复用，所以只改视觉、
   不动结构。DeepSeek 的思考区是**一块柔和的浅灰圆角块**，标题行更轻，
   与下面的白色结果卡形成"底色分层"而不是"边框分层"。 */
.ask-collapse {
  margin-top: 10px;
  margin-bottom: 10px;            /* 2026-10-03：上移到结果上方后给下方结果卡留间距 */
  border-radius: 10px;
  border: 1px solid var(--line-200);
  background: #FBFCFE;
  overflow: hidden;
}
/* 思考块本体：浅灰底，DeepSeek 式无色条 */
.ask-collapse:has(.tk-panel) {
  background: #F7F8FA;
  border-color: #EDEFF3;
}
.ask-collapse-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 14px;
  cursor: pointer;
  user-select: none;
  transition: background .15s ease;
}
.ask-collapse-head:hover { background: #F0F2F6; }
.ask-collapse-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 5px;
  background: var(--brand-100);
  color: var(--brand-600);
  flex: none;
}
/* 生成中：极慢呼吸的小圆点（2.4s 一次），只表示"在跑"，不闪烁 */
.ask-collapse-spin {
  width: 6px; height: 6px; border-radius: 9999px;
  background: #9AA4B2;
  animation: tk-breathe 2.4s ease-in-out infinite;
}
.ask-collapse-name {
  font-size: 12px;
  font-weight: 600;
  color: var(--ink-700);
}
.ask-collapse-state {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--ink-400);
}
.ask-collapse-body {
  padding: 12px 14px;
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--ink-700);
  border-top: 1px solid var(--line-100);
  max-height: 280px;
  overflow-y: auto;
}

/* ===== SQL 深色卡（代码块保持深色，与浅色结论区形成明确区分）===== */
.ask-sql-card {
  margin-top: 10px;
  border-radius: 10px;
  overflow: hidden;
  border: 1px solid #1E293B;
  background: #0F172A;
}
.ask-sql-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 9px 14px;
  background: #1E293B;
  cursor: pointer;
  user-select: none;
}
.ask-sql-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  font-weight: 600;
  color: #E2E8F0;
}
.ask-sql-dot {
  width: 6px;
  height: 6px;
  border-radius: 9999px;
  background: #60A5FA;
}
.ask-sql-state {
  font-size: 10.5px;
  color: #94A3B8;
  font-weight: 400;
}
.ask-sql-copy {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 4px 9px;
  border-radius: 6px;
  font-size: 11px;
  color: #CBD5E1;
  transition: all .15s ease;
}
.ask-sql-copy:hover { color: #fff; background: #334155; }

/* ===== 回答操作栏（2026-09-29 收敛）：统一胶囊按钮，主操作在前、
   反馈/导出用分隔线分组，减少视觉噪音 ===== */
.ask-actions {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.ask-action-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 11px;
  border-radius: 9999px;
  border: 1px solid var(--line-200);
  background: #fff;
  color: var(--ink-500);
  font-size: 12px;
  line-height: 1.4;
  transition: all .15s ease;
}
.ask-action-btn:hover:not(:disabled) {
  border-color: var(--brand-300);
  color: var(--brand-600);
  background: var(--brand-050);
}
.ask-action-btn:disabled { opacity: .4; cursor: not-allowed; }
.ask-action-btn-primary {
  border: none;
  color: #fff;
  background: linear-gradient(135deg, #2E7CF0, #1F66D6);
}
.ask-action-btn-primary:hover:not(:disabled) { color: #fff; background: linear-gradient(135deg, #1F66D6, #1852B0); }
.ask-action-btn-good { color: #2563EB; }
.ask-action-btn-bad { color: #DC2626; }
.ask-action-btn-warn { color: #B45309; }
.ask-action-btn-ghost { color: var(--ink-500); }
.ask-actions-sep {
  width: 1px;
  height: 14px;
  background: var(--line-200);
  margin: 0 4px;
}
.ask-actions-label {
  font-size: 12px;
  color: var(--ink-400);
}
/* ===== 用户消息复制（2026-10-01）：气泡右下迷你胶囊按钮，默认透明、
   悬停消息行才显现（占位不跳版），比 AI 操作栏更小一号（11px）===== */
.ask-actions-user {
  margin-top: 3px;
  justify-content: flex-end;
}
.ask-action-btn-sm {
  padding: 1px 8px;
  font-size: 11px;
  gap: 3px;
}
.msg-item .ask-user-copy { opacity: 0; }
.msg-item:hover .ask-user-copy,
.ask-user-copy:focus-visible { opacity: 1; }
.ask-chart-select {
  padding: 4px 10px;
  border-radius: 9999px;
  border: 1px solid var(--line-200);
  background: #fff;
  color: var(--ink-500);
  font-size: 12px;
  outline: none;
  cursor: pointer;
}
.ask-chart-select:hover { border-color: var(--brand-300); }
.ask-feedback-done {
  font-size: 12px;
  color: var(--ink-400);
}
</style>