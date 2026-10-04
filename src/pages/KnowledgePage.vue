<template>
  <!-- 根元素显式接管 attrs：本页顶层还有多个 <Teleport>（弹窗挂 body），组件是多根，
       Vue 不会自动继承父级传入的 class="h-full"（还会刷 Extraneous non-props attributes 警告）。
       显式绑定后，.kb-shell 才能拿到内容区高度，图谱/术语词典视图才撑得住底部。 -->
  <div class="kb-shell" v-bind="$attrs">

    <!-- ================= 主区 ================= -->
    <div class="kb-main">

      <!-- 顶部导航栏（导航居中 + 全局搜索与工具） -->
      <nav class="kb-tabs">
        <div class="kb-tabs-nav">
          <button
            v-for="view in viewTabs"
            :key="view.key"
            class="kb-tab"
            :class="{ active: activeView === view.key }"
            @click="activeView = view.key"
          ><AppIcon :name="view.icon" :size="14" /> {{ view.name }}</button>
        </div>
        <div class="kb-tabs-tools">
          <div class="kb-global-search">
            <AppIcon name="search" :size="14" class="kb-global-search-icon" />
            <input v-model="globalSearchQ" type="text" placeholder="搜索对象/指标/规则/术语…" @input="onGlobalSearchInput" @focus="globalSearchFocus = true" @blur="blurSearch" />
            <div v-if="globalSearchFocus && globalSearchResults.length" class="kb-search-dropdown">
              <div v-for="r in globalSearchResults" :key="r.key" class="kb-search-item" @mousedown.prevent="jumpToSearchResult(r)">
                <AppIcon :name="r.icon || 'file-text'" :size="14" class="kb-search-item-icon" />
                <span class="kb-search-item-type">{{ r.type }}</span>
                <span class="kb-search-item-title">{{ r.title }}</span>
                <span class="kb-search-item-sub">{{ r.subtitle }}</span>
              </div>
            </div>
          </div>
          <!-- 导出需 export 操作权限（与数据总览报告同口径）；未开通时整个按钮不展示 -->
          <div v-if="canDo('export')" class="kb-export-menu">
            <button class="kb-tool-btn" title="导出知识" :disabled="!!exportLoading"><AppIcon name="download" :size="15" /></button>
            <div class="kb-export-dropdown">
              <button @click="exportKnowledge('terms','md')">导出术语词典 (MD)</button>
              <button @click="exportKnowledge('terms','csv')">导出术语词典 (CSV)</button>
              <button @click="exportKnowledge('metrics','md')">导出指标口径 (MD)</button>
              <button @click="exportKnowledge('metrics','csv')">导出指标口径 (CSV)</button>
            </div>
          </div>
        </div>
      </nav>

      <!-- 加载 / 错误状态 -->
      <div v-if="loading" class="kb-state">
        <div class="kb-spinner"></div>
        <span>正在从数据库加载业务知识…</span>
      </div>
      <div v-else-if="errorMsg" class="kb-state kb-state-error">
        <span><AppIcon name="alert-triangle" :size="14" /> 加载失败：{{ errorMsg }}</span>
        <div class="kb-state-sub">请确保后端服务已启动（python main.py）</div>
      </div>

      <!-- ================= 视图1：场景知识 ================= -->
      <template v-else-if="activeView === 'scene'">
        <!-- 场景选择胶囊 + 概要条（替代大网格场景卡：首屏聚焦业务内容） -->
        <div class="kb-scene-wrap">
          <div class="kb-scene-pills" aria-label="业务场景">
            <button
              v-for="scene in scenes"
              :key="scene.key"
              class="kb-scene-pill"
              :class="{ active: activeScene === scene.key }"
              @click="activeScene = scene.key"
            >
              <AppIcon :name="sceneIcon(scene.name)" :size="15" />
              <span>{{ scene.name }}</span>
              <i>{{ sceneObjects(scene.key).length }}</i>
            </button>
          </div>
          <div class="kb-scene-summary">
            <div class="kb-summary-title">
              <b>{{ currentSceneInfo.name }}</b>
              <span class="kb-summary-intro">{{ sceneIntro(activeScene) }}</span>
            </div>
          </div>
        </div>
      </template>

      <!-- 详情弹框（对象 / 指标 / 规则 / 分析主题）：刻意放在视图 v-if 链之外 + Teleport 到 body。
           这样侧边「业务域」在任意页面点击都能直接弹出解释（Tab: 概览/字段/关系），
           不用先切到场景知识，也不会让页面跳转 / 换场景。 -->
      <Teleport to="body">
        <div v-if="selectedDetail" class="kb-detail-mask" @click.self="closeDetail">
          <div class="kb-detail kb-detail-modal">
          <div class="kb-detail-head">
            <span class="kb-detail-icon"><AppIcon :name="selectedDetail.icon" :size="21" /></span>
            <div class="kb-detail-title-wrap">
              <div class="kb-detail-title">{{ selectedDetail.title }}</div>
              <div class="kb-detail-meta">
                {{ selectedDetail.kind }} · {{ selectedDetail.sceneName }}
                <template v-if="selectedDetail.table"> · <span class="mono">{{ selectedDetail.table }}</span></template>
              </div>
            </div>
            <span class="kb-badge" :class="'kb-badge-' + badgeKey(selectedDetail.kind)">{{ selectedDetail.kind }}</span>
            <button v-if="isEditor()" class="kb-detail-action" title="编辑知识" @click="openEditModal"><AppIcon name="pencil" :size="14" /></button>
            <button class="kb-detail-action" :class="{ fav: isFav(selectedDetail) }" :title="isFav(selectedDetail) ? '取消收藏' : '收藏'" @click="toggleFav(selectedDetail)"><AppIcon name="star" :size="14" /></button>
            <div class="kb-detail-vote">
              <button class="kb-vote-btn" :class="{ active: userData.feedback?.[selectedDetail.id]?.vote === 'up' }" title="该知识有用" @click="setFeedback(selectedDetail.id, 'up')"><AppIcon name="thumbs-up" :size="13" /></button>
              <button class="kb-vote-btn" :class="{ active: userData.feedback?.[selectedDetail.id]?.vote === 'down' }" title="知识有误/需纠正（填写后提交给管理员）" @click="openFeedback"><AppIcon name="thumbs-down" :size="13" /></button>
            </div>
            <button v-if="selectedDetail.kind === '业务对象' && selectedDetail.table" class="kb-detail-action" title="在知识图谱中定位该对象" @click="locateInGraph(selectedDetail.table!)"><AppIcon name="git-fork" :size="14" /></button>
            <button v-if="['业务对象', '业务指标', '分析主题'].includes(selectedDetail.kind)" class="kb-detail-action kb-detail-askbtn" title="去智能问析分析该知识" @click="askForDetail"><AppIcon name="send" :size="13" /></button>
            <button class="kb-detail-close" title="关闭详情" @click="closeDetail"><AppIcon name="x" :size="13" /></button>
          </div>
          <div class="kb-detail-tabs">
            <button
              v-for="t in detailTabs"
              :key="t.key"
              class="kb-detail-tab"
              :class="{ active: detailTab === t.key }"
              @click="detailTab = t.key"
            ><AppIcon :name="t.icon" :size="13" /> {{ t.label }}</button>
          </div>
          <div class="kb-detail-body">
            <!-- 概览 -->
            <div v-if="detailTab === 'overview'" class="kb-overview">
              <div class="kb-overview-desc">{{ selectedDetail.desc || '暂无描述' }}</div>
              <div class="kb-overview-grid">
                <div class="kb-ov-card"><span>业务场景</span><b>{{ selectedDetail.sceneName }}</b></div>
                <div v-if="selectedDetail.table" class="kb-ov-card"><span>数据表</span><b class="mono">{{ selectedDetail.table }}</b></div>
                <div v-if="selectedDetail.row_count !== undefined" class="kb-ov-card"><span>数据行数</span><b>{{ selectedDetail.row_count.toLocaleString() }}</b></div>
                <div v-if="selectedDetail.heat" class="kb-ov-card"><span>知识热度</span><b><AppIcon name="zap" :size="12" /> {{ selectedDetail.heat }}</b></div>
                <div v-if="selectedDetail.is_core" class="kb-ov-card"><span>属性</span><b class="kb-core-text">核心表</b></div>
                <div v-if="selectedDetail.unit" class="kb-ov-card"><span>计量单位</span><b>{{ selectedDetail.unit }}</b></div>
                <div v-if="selectedDetail.formula" class="kb-ov-card kb-ov-wide"><span>口径公式</span><b class="mono">{{ selectedDetail.formula }}</b></div>
                <div v-if="selectedDetail.condition" class="kb-ov-card kb-ov-wide"><span>判定规则</span><b>{{ selectedDetail.condition }}</b></div>
                <div v-if="selectedDetail.questions?.length" class="kb-ov-card kb-ov-wide">
                  <span>示例问题</span>
                  <div class="kb-questions">
                    <button v-for="(q, qi) in selectedDetail.questions" :key="qi" class="kb-question kb-question-btn" @click="goAskQ(q)"><AppIcon name="message-circle" :size="11" /> {{ q }}</button>
                  </div>
                </div>
              </div>
              <!-- P1-B1：分析主题 = 迷你分析台（推荐指标一键跑/看口径） -->
              <div v-if="selectedDetail.kind === '分析主题' && detailSceneMetrics.length" class="kb-topic-workbench">
                <div class="kb-topic-wb-title"><AppIcon name="line-chart" :size="12" /> 本场景推荐指标（点开看口径 / 问析）</div>
                <div class="kb-rel-chips">
                  <button v-for="m in detailSceneMetrics.slice(0, 10)" :key="m.name" class="kb-rel-chip" :title="m.formula" @click="openMetricInDetail(m)">{{ m.name }}</button>
                </div>
                <button class="kb-topic-wb-ask kb-primary-btn" @click="topicWbAsk">
                  <AppIcon name="sparkles" :size="12" /> 围绕「{{ selectedDetail.title }}」去智能问析
                </button>
              </div>
            </div>

            <!-- 字段 -->
            <div v-else-if="detailTab === 'fields'" class="kb-fields">
              <div v-if="!selectedDetail.columns?.length" class="kb-empty">该对象暂无字段信息</div>
              <table v-else class="kb-field-table">
                <thead>
                  <tr><th>字段名</th><th>类型</th><th>键</th><th>含义说明</th></tr>
                </thead>
                <tbody>
                  <tr v-for="col in selectedDetail.columns" :key="col.name">
                    <td class="mono kb-col-name">{{ col.name }}</td>
                    <td class="mono kb-col-type">{{ col.type }}</td>
                    <td class="kb-col-pk"><AppIcon v-if="col.primary_key" name="key" :size="13" /></td>
                    <td class="kb-col-translation">{{ col.translation }}<button v-if="isEditor()" class="kb-field-edit" title="修改业务含义" @click="openFieldEdit(col)"><AppIcon name="pencil" :size="11" /></button></td>
                  </tr>
                </tbody>
              </table>
            </div>

            <!-- 关系（按知识类型展示关联：对象=1跳邻居+关联指标/规则；指标=来源表+口径字段+同表指标；规则=涉及对象） -->
            <div v-else-if="detailTab === 'relations'" class="kb-relations">
              <template v-if="selectedDetail.kind === '业务对象'">
                <div v-if="miniGraphNodes.length" class="kb-mini-graph">
                  <div class="kb-mini-hint">「{{ selectedDetail.title }}」与 <b>{{ neighborCount }}</b> 张表直接关联（1 跳）· 点击节点切换对象详情</div>
                  <KnowledgeGraph ref="miniGraphRef" :nodes="miniGraphNodes" :relationships="miniGraphRels" :height="280" @node-click="onMiniGraphNodeClick" />
                </div>
                <div v-else class="kb-empty">该对象暂无表间外键关系（下方为指标/规则关联）</div>
                <div v-if="metricsOfTableAll(selectedDetail.table || '').length" class="kb-rel-group">
                  <div class="kb-rel-title"><AppIcon name="line-chart" :size="12" /> 关联指标（口径依赖该表）</div>
                  <div class="kb-rel-chips">
                    <button v-for="it in metricsOfTableAll(selectedDetail.table || '')" :key="it.m.name" class="kb-rel-chip" @click="openMetricDetail(it.m, it.sceneKey)">{{ it.m.name }}<small v-if="it.sceneName"> · {{ it.sceneName }}</small></button>
                  </div>
                </div>
                <div v-if="rulesOfObjAll({ table: selectedDetail.table, label: selectedDetail.title }).length" class="kb-rel-group">
                  <div class="kb-rel-title"><AppIcon name="ruler" :size="12" /> 关联规则（判定涉及该对象）</div>
                  <div class="kb-rel-chips">
                    <button v-for="it in rulesOfObjAll({ table: selectedDetail.table, label: selectedDetail.title })" :key="it.r.name" class="kb-rel-chip kb-rel-chip-rule" @click="openRuleDetail(it.r, it.sceneKey)">{{ it.r.name }}</button>
                  </div>
                </div>
                <div v-if="!metricsOfTableAll(selectedDetail.table || '').length && !rulesOfObjAll({ table: selectedDetail.table, label: selectedDetail.title }).length && !miniGraphNodes.length" class="kb-empty">暂无关联知识</div>
              </template>

              <template v-else-if="selectedDetail.kind === '业务指标'">
                <div class="kb-rel-group">
                  <div class="kb-rel-title"><AppIcon name="database" :size="12" /> 口径来源表（{{ selectedDetail.tables?.length || 1 }}）</div>
                  <div class="kb-rel-chips">
                    <button v-for="t in selectedDetail.tables || []" :key="t" class="kb-rel-chip kb-rel-chip-table" @click="openObjByTable(t)">{{ t }}</button>
                  </div>
                </div>
                <div v-if="refFieldsOfMetric(selectedDetail).length" class="kb-rel-group">
                  <div class="kb-rel-title"><AppIcon name="git-commit" :size="12" /> 口径引用字段（血缘）</div>
                  <div class="kb-rel-chips">
                    <span v-for="c in refFieldsOfMetric(selectedDetail)" :key="c.name" class="kb-rel-chip mono" :title="c.translation || c.comment || c.name">{{ c.name }}</span>
                  </div>
                </div>
                <div v-if="selectedDetail.formula" class="kb-rel-group">
                  <div class="kb-rel-title"><AppIcon name="function-square" :size="12" /> 口径公式</div>
                  <div class="kb-rel-formula mono">{{ selectedDetail.formula }}</div>
                </div>
              </template>

              <template v-else-if="selectedDetail.kind === '业务规则'">
                <div v-if="objectsOfRule(selectedDetail).length" class="kb-rel-group">
                  <div class="kb-rel-title"><AppIcon name="package" :size="12" /> 涉及业务对象</div>
                  <div class="kb-rel-chips">
                    <button v-for="it in objectsOfRule(selectedDetail)" :key="it.obj.table" class="kb-rel-chip kb-rel-chip-table" @click="openObjByTable(it.obj.table)">{{ it.obj.label || it.obj.table }}<small> · {{ it.sceneName }}</small></button>
                  </div>
                </div>
                <div v-if="selectedDetail.condition" class="kb-rel-group">
                  <div class="kb-rel-title"><AppIcon name="git-branch" :size="12" /> 判定条件</div>
                  <div class="kb-rel-formula">{{ selectedDetail.condition }}</div>
                </div>
              </template>

              <template v-else-if="selectedDetail.kind === '分析主题'">
                <div class="kb-rel-group">
                  <div class="kb-rel-title"><AppIcon name="message-circle" :size="12" /> 典型问题（点击直达智能问析）</div>
                  <div class="kb-rel-chips">
                    <button v-for="q in selectedDetail.questions || []" :key="q" class="kb-question kb-question-btn" @click="goAskQ(q)"><AppIcon name="message-circle" :size="11" /> {{ q }}</button>
                  </div>
                </div>
                <div v-if="detailSceneMetrics.length" class="kb-rel-group">
                  <div class="kb-rel-title"><AppIcon name="line-chart" :size="12" /> 当前场景核心指标</div>
                  <div class="kb-rel-chips">
                    <button v-for="m in detailSceneMetrics.slice(0, 12)" :key="m.name" class="kb-rel-chip" @click="openMetricInDetail(m)">{{ m.name }}</button>
                  </div>
                </div>
              </template>
            </div>
          </div>
        </div>
        </div>
      </Teleport>

      <!-- 场景知识主区（分析主题 / 业务规则 / 数据支撑 / 核心指标全览）：与上面的场景胶囊同属「场景知识」视图。
           注意：这里必须自己再判一次 loading/errorMsg —— 它已经不在上面那条 v-if 链里了，
           否则加载中/报错时还会把这些卡片（含空状态）一并渲染出来。 -->
      <template v-if="!loading && !errorMsg && activeView === 'scene'">

        <!-- ===== v6 场景知识：分析主题 → 业务规则 → 数据支撑 → 核心指标全览（最底层） ===== -->
        <div class="kb-v6">

          <div v-if="currentSceneTopics.length" class="kb-card kb-v6-card" id="kb-sec-topics">
            <div class="kb-card-head"><span><AppIcon name="target" :size="14" /> 分析主题</span><span class="kb-card-sub">{{ sceneTopicsBiz.length }} 个综合分析 · {{ sceneTopicsMetric.length }} 个指标趋势</span></div>
            <div class="kb-card-body">
              <div v-if="sceneTopicsBiz.length" class="kb-topic-biz">
                <div class="kb-topic-group-title"><span>综合分析</span><small>多指标 · 多对象 · 直接开问</small></div>
                <div class="kb-topic-cards">
                  <div
                    v-for="t in sceneTopicsBiz"
                    :key="t.id"
                    class="kb-topic-card"
                    :class="{ active: selectedDetail?.kind === '分析主题' && selectedDetail?.id === 'topic:' + t.name }"
                    @click="openTopicDetail(t, activeScene)"
                  >
                    <div class="kb-topic-card-head">
                      <span class="kb-topic-icon"><AppIcon name="target" :size="14" /></span>
                      <div class="kb-topic-card-name">{{ t.name }}</div>
                      <button class="kb-ask-btn" title="打开该主题的分析台（推荐指标/问题一键跑）" @click.stop="openTopicDetail(t, activeScene)">分析台</button>
                    </div>
                    <div class="kb-topic-desc">{{ t.desc }}</div>
                    <div v-if="(t.questions || []).length" class="kb-topic-questions">
                      <button v-for="q in t.questions.slice(0, 2)" :key="q" class="kb-question kb-question-btn" title="用该问题去智能问析" @click.stop="goAskQ(q)"><AppIcon name="message-circle" :size="11" /> {{ q }}</button>
                    </div>
                  </div>
                </div>
              </div>
              <div v-if="sceneTopicsMetric.length" class="kb-topic-metric-group">
                <div class="kb-topic-group-title kb-topic-group-title-mt"><span>指标趋势</span><small>围绕单个注册指标按时间跟踪，点击开问</small></div>
                <div class="kb-topic-rows">
                  <div v-for="t in sceneTopicsMetric" :key="t.id" class="kb-topic-row" @click="openTopicDetail(t, activeScene)">
                    <AppIcon name="activity" :size="12" />
                    <span class="kb-topic-row-name">{{ t.name }}</span>
                    <span class="kb-topic-row-q">{{ (t.questions || [])[0] }}</span>
                    <button class="kb-metric-ask-static" title="用该问题去智能问析" @click.stop="goAskQ((t.questions || [])[0] || ('分析' + t.name))"><AppIcon name="send" :size="10" /></button>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div v-if="currentSceneData.rules?.length" class="kb-card kb-v6-card" id="kb-sec-rules">
            <div class="kb-card-head"><span><AppIcon name="ruler" :size="14" /> 业务规则</span><span class="kb-card-sub">异常判定 · 告警边界</span></div>
            <div class="kb-card-body kb-rules-grid">
              <div v-for="(rule, i) in currentSceneData.rules" :key="i" class="kb-rule" @click="openRuleDetail(rule, activeScene)">
                <div class="kb-rule-head">
                  <span class="kb-rule-name">{{ rule.name }}</span>
                  <span v-if="rule.source" class="kb-rule-src">{{ rule.source }}</span>
                </div>
                <div v-if="rule.condition" class="kb-rule-cond">判定：{{ rule.condition }}</div>
                <div v-if="rule.formula" class="mono kb-rule-formula">{{ rule.formula }}</div>
              </div>
            </div>
          </div>

          <!-- ⑤ 数据支撑：该场景对应的数据表（完整字段明细；点表卡看业务详情，放大看完整字段）
               排版与分析主题 / 业务规则卡保持一致：.kb-card-head（灰底标题条）+ .kb-card-body -->
          <div class="kb-card kb-v6-card" id="kb-sec-objects">
            <div class="kb-card-head">
              <span><AppIcon name="package" :size="14" /> 数据支撑 · 数据表</span>
              <span class="kb-card-sub">{{ sceneTables(activeScene).length }} 张表 · {{ sceneTotalCols }} 个字段 · {{ sceneTotalRows }} 行 · 点击表卡查看业务详情 · 放大查看完整字段</span>
            </div>
            <div class="kb-card-body">
              <TableCardGrid :tables="sceneTables(activeScene)" :loading="tableListLoading" @enlarge="openTableEnlarge" @open="openSceneObjFromTable" />
            </div>
          </div>
        </div>

          <!-- ⑥ 核心指标全览（最底层）（该场景全部注册口径，实时值分组） -->
          <div class="kb-card kb-v6-card" id="kb-sec-metrics">
            <div class="kb-card-head"><span><AppIcon name="line-chart" :size="14" /> 核心指标全览</span><span class="kb-card-sub">覆盖该场景全部注册口径 · 实时计算</span><button class="kb-manage-btn" @click="goMetricsPage"><AppIcon name="arrow-right" :size="11" /> 管理全部口径</button></div>
            <div v-if="snapBusy(activeScene) && !sceneSnapshot.groups.length" class="kb-empty">正在计算实时值…</div>
            <template v-else>
              <div v-for="g in sceneSnapshot.groups" :key="g.table" class="kb-fg">
                <div class="kb-fg-h">
                  <span class="kb-fg-g">{{ g.g }}</span>
                  <span class="mono kb-fg-t">{{ g.table }}</span>
                  <span class="kb-fg-live"><i></i> 实时</span>
                </div>
                <div class="kb-fg-grid">
                  <button v-for="it in g.items" :key="it.name" class="kb-fg-item" :class="{ miss: !it.ok }" :title="it.expr || ''" @click="openSnapMetric(it, g)">
                    <span class="kb-fg-name">{{ it.name }}<em v-if="!it.ok">待数据</em></span>
                    <span class="kb-fg-val">{{ fmtVal(it.value, it.unit) }}</span>
                    <span class="kb-fg-unit">{{ it.unit }}</span>
                  </button>
                </div>
              </div>
              <div v-if="sceneSuggests.length" class="kb-fg-suggest">
                <span>该环节还需补口径（到「指标口径」注册后自动出现）：</span>
                <button v-for="sg in sceneSuggests" :key="sg" class="kb-sg-chip" @click="goMetricsPage">{{ sg }}</button>
              </div>
              <div v-if="!sceneSnapshot.groups.length" class="kb-empty">暂无已注册口径，可在「指标口径」页注册后在此看到实时值</div>
            </template>
          </div>

      </template>
      <!-- ================= 视图2：知识图谱 ================= -->
      <template v-else-if="activeView === 'graph'">
        <div class="kb-graph-shell kb-view-fill">
          <div class="kb-graph-toolbar">
            <div class="kb-graph-toolbar-left">
              <span class="kb-graph-title"><AppIcon name="git-fork" :size="14" /> 业务知识图谱</span>
              <span class="kb-graph-hint">{{ graphLoaded ? '实时表间关系图谱 · 拖拽节点 · 滚轮缩放 · 点击查看详情' : '拖拽查看 · 点击连线查看关联字段' }}</span>
              
            </div>
            <div class="kb-graph-toolbar-right">
              <!-- 图谱模式切换（合并自 2026-09-04 版本）：完整=六类知识图谱+ER 表节点；简洁=原表+外键图 -->
              <!-- 图谱加载成功时隐藏旧图专属控件（模式/类型过滤/场景不适用于实时表关系数据） -->
              <template v-if="!lightRagActive">
              <span class="kb-filter-btn" style="opacity:.65;cursor:default;border-color:transparent;padding-left:0;">模式</span>
              <button class="kb-filter-btn" :class="{ active: graphMode === 'full' }" @click="graphMode = 'full'"><AppIcon name="share-2" :size="11" /> 完整图谱</button>
              <button class="kb-filter-btn" :class="{ active: graphMode === 'simple' }" @click="graphMode = 'simple'"><AppIcon name="layout-grid" :size="11" /> 简洁表图</button>
              <button v-for="filter in graphFilters" :key="filter.key" class="kb-filter-btn" :class="{ active: activeGraphFilters.includes(filter.key) }" @click="toggleGraphFilter(filter.key)"><AppIcon :name="filter.icon" :size="11" /> {{ filter.label }}</button>
              </template>
              <button class="kb-ghost-btn" @click="resetGraphZoom"><AppIcon name="refresh-cw" :size="11" /> 重置</button>
              <button class="kb-primary-btn" @click="openGraphFullscreen"><AppIcon name="maximize-2" :size="11" /> 全局查看</button>
            </div>
          </div>
          <!-- P1：场景过滤（按业务场景聚焦图谱，节点随过滤联动） -->
          <div v-if="!lightRagActive" class="kb-graph-scenebar">
            <span class="kb-graph-scene-label"><AppIcon name="folder" :size="11" /> 场景：</span>
            <button class="kb-scene-chip" :class="{ active: activeGraphScene === '' }" @click="activeGraphScene = ''">全部</button>
            <button v-for="sc in graphSceneOptions" :key="sc" class="kb-scene-chip" :class="{ active: activeGraphScene === sc }" @click="activeGraphScene = sc">{{ sc }}</button>
            <span v-if="activeGraphScene" class="kb-scene-chip-hint">已聚焦「{{ activeGraphScene }}」· 共 {{ graphTableCount }} 张表 {{ graphRelCount }} 条关系</span>
          </div>
          <div v-if="!lightRagActive" class="kb-graph-legend">
            <template v-for="(color, scene) in sceneLegend" :key="scene">
              <span class="kb-legend-item"><span class="kb-legend-dot" :style="{ background: color.background, borderColor: color.border }"></span> {{ scene }}</span>
            </template>
            <span class="kb-legend-note">■ 方形=表级节点 · ● 圆形=字段级节点</span>
            <span v-if="!filteredGraphNodes.length" class="kb-legend-warn">请至少勾选一个类型（右上角过滤按钮）</span>
          </div>
          <div class="kb-graph-body">
            <div class="kb-graph-canvas">
              <!-- LightRAG 知识图谱：1:1 复刻 knowledge_graph.html（物理模拟 + 可拖拽 + 悬停提示）
                   不传 height → 由 .kb-graph-canvas 撑满剩余高度（内容不足一屏时不留白），
                   画布最小 420px（见 LightRagGraph 自身 min-height），窗口太矮时页面照常滚动。
                   fit-zoom：适配后再放大 40%，否则 44 个节点的标签过小 -->
              <LightRagGraph ref="graphRef" :fit-zoom="1.4" @node-click="onGraphNodeClick" />
            </div>
            <!-- 节点中文解释：与图谱左右分栏（同一屏内直接可见，不用往下滑） -->
            <GraphNodeDetail
              v-if="selectedGraphNode"
              :title="graphNodeModalTitle"
              :kind="graphNodeKindZh"
              :node-id="selectedGraphNode.id"
              :zh="graphNodeZh"
              :raw-desc="selectedGraphNode.rawDesc"
              :facts="graphNodeFacts"
              :tables="graphNodeTables"
              :relations="graphNodeRelations"
              @close="selectedGraphNode = null"
              @pick="onGraphNodeClick"
              @open-table="openSceneObjectOfTable"
            />
          </div>
        </div>

        <Teleport to="body">
          <div v-if="graphFullscreen" class="kb-fullscreen">
            <div class="kb-fullscreen-head">
              <span><AppIcon name="maximize-2" :size="13" /> 业务知识图谱（全局查看）</span>
              <div class="kb-fullscreen-actions">
                <template v-if="!lightRagActive">
                <span class="kb-filter-btn" style="opacity:.65;cursor:default;border-color:transparent;padding-left:0;">模式</span>
                <button class="kb-filter-btn" :class="{ active: graphMode === 'full' }" @click="graphMode = 'full'"><AppIcon name="share-2" :size="11" /> 完整图谱</button>
                <button class="kb-filter-btn" :class="{ active: graphMode === 'simple' }" @click="graphMode = 'simple'"><AppIcon name="layout-grid" :size="11" /> 简洁表图</button>
                </template>
                <button class="kb-ghost-btn" @click="resetGraphZoom"><AppIcon name="refresh-cw" :size="11" /> 重置</button>
                <button v-if="canDo('download')" class="kb-ghost-btn" @click="downloadGraphPng"><AppIcon name="download" :size="11" /> 下载 PNG</button>
                <button class="kb-ghost-btn" @click="graphFullscreen = false"><AppIcon name="x" :size="11" /> 关闭</button>
              </div>
            </div>
            <div class="kb-fullscreen-body">
              <LightRagGraph ref="graphFullscreenRef" @node-click="onGraphNodeClick" />
              <!-- 全屏查看时同样是左右分栏，不遮图谱、不用下滑 -->
              <GraphNodeDetail
                v-if="selectedGraphNode"
                :title="graphNodeModalTitle"
                :kind="graphNodeKindZh"
                :node-id="selectedGraphNode.id"
                :zh="graphNodeZh"
                :raw-desc="selectedGraphNode.rawDesc"
                :facts="graphNodeFacts"
                :tables="graphNodeTables"
                :relations="graphNodeRelations"
                @close="selectedGraphNode = null"
                @pick="onGraphNodeClick"
                @open-table="openSceneObjectOfTable"
              />
            </div>
          </div>
        </Teleport>

      </template>

      <!-- ================= 视图3：表间关系（原「数据底座概览」弹窗迁移为导航视图） ================= -->
      <template v-else-if="activeView === 'rels'">
        <div class="kb-terms-shell">
          <div class="kb-terms-toolbar">
            <div class="kb-terms-toolbar-left">
              <span class="kb-graph-title"><AppIcon name="share-2" :size="14" /> 表间关系知识图谱</span>
              <span class="kb-terms-count">共 {{ modalGraphData.length }} 条关系 · {{ modalGraphNodes.length }} 个节点</span>
            </div>
            <div class="kb-terms-toolbar-right">
              <button class="kb-ghost-btn" @click="loadRels(true)"><AppIcon name="refresh-cw" :size="11" /> 刷新</button>
            </div>
          </div>
          <div v-if="graphLoading" class="kb-state">
            <div class="kb-spinner"></div>
            <span>正在加载表间关系…</span>
          </div>
          <div v-else-if="modalGraphData.length === 0 && modalGraphNodes.length === 0" class="kb-empty">暂无关系数据</div>
          <div v-else class="kb-rels-body">
            <div class="mb-2 text-xs font-semibold text-gray-600">关系明细（{{ modalGraphData.length }} 条）</div>
            <div class="grid max-h-56 gap-1 overflow-y-auto text-xs text-gray-600 sm:grid-cols-2">
              <div v-for="relation in modalGraphData" :key="relation.description" class="rounded bg-gray-50 px-2 py-1.5">
                {{ relDisplay(relation) }}
              </div>
            </div>
            <div class="mt-4">
              <KnowledgeGraph :nodes="modalGraphNodes" :relationships="modalGraphData" :height="640" />
            </div>
          </div>
        </div>
      </template>

      <!-- ================= 视图4：术语词典 ================= -->
      <template v-else-if="activeView === 'terms'">
        <div class="kb-terms-shell kb-view-fill">
          <div class="kb-terms-toolbar">
            <div class="kb-terms-toolbar-left">
              <span class="kb-graph-title"><AppIcon name="book-open" :size="14" /> 业务术语词典</span>
              <span class="kb-terms-count">共 {{ termDictionary.length }} 个术语</span>
            </div>
            <div class="kb-terms-toolbar-right">
              <input v-model="termSearch" type="text" placeholder="搜索术语、英文名、定义、分类…" class="kb-input" />
              <select v-model="termCategoryFilter" class="kb-select">
                <option value="">全部分类</option>
                <option v-for="category in termCategories" :key="category" :value="category">{{ category }}</option>
              </select>
              <select v-model="termTypeFilter" class="kb-select">
                <option value="">全部类型</option>
                <option v-for="kt in knowledgeTypes" :key="kt" :value="kt">{{ kt }}</option>
              </select>
              <button class="kb-primary-btn" @click="openTermForm"><AppIcon name="plus" :size="11" /> 新增术语</button>
            </div>
          </div>
          <div class="kb-terms-body">
            <div v-if="!filteredTerms.length" class="kb-empty">未找到匹配术语，请尝试更换关键词或清空筛选。</div>
            <div v-for="group in groupedTerms" :key="group.type" class="kb-term-group">
              <div class="kb-term-group-head">
                <span><AppIcon :name="typeIcons[group.type] || 'list'" :size="13" /> {{ group.type }}</span>
                <span class="kb-card-count">{{ group.items.length }} 个术语</span>
              </div>
              <!-- key 用「表.字段」而非 term：后端去重键是「术语+前30字释义」，
                   同名不同释义会同时保留（status/type/created_at 这类跨表重复率很高），
                   term 重复会让 Vue 复用错 DOM，术语卡显示错乱。
                   自定义术语无 mapped_field，退回 term 即可。 -->
              <div v-for="term in group.items" :key="`${term.mapped_table || 'custom'}.${term.mapped_field || term.term}.${term.definition?.slice(0, 8) || ''}`" class="kb-term" @click="openTermDetail(term)">
                <div class="kb-term-main">
                  <div class="kb-term-top">
                    <span class="kb-term-name">{{ term.term_cn || term.term }}</span>
                    <span class="kb-term-badge" :class="'kb-badge-' + badgeKey(term.knowledge_type)">{{ term.knowledge_type }}</span>
                    <span class="kb-term-badge kb-term-badge-cat">{{ term.category }}</span>
                    <span v-if="term.custom" class="kb-term-badge-custom">自定义</span>
                  </div>
                  <div class="kb-term-def">{{ term.definition }}</div>
                </div>
                <div class="kb-term-actions">
                  <button v-if="term.custom" class="kb-term-action" title="删除自定义术语" @click.stop="deleteCustomTerm(term.term)"><AppIcon name="trash-2" :size="13" /></button>
                  <button class="kb-term-action" :class="{ fav: isTermFav(term) }" :title="isTermFav(term) ? '取消收藏' : '收藏'" @click.stop="toggleTermFav(term)"><AppIcon name="star" :size="13" /></button>
                </div>
                <div class="kb-term-side">
                  <div class="mono kb-term-en">{{ term.en }}</div>
                  <div class="kb-term-en-label">英文名</div>
                </div>
                <div class="kb-term-meta">
                  <span v-if="term.data_type" class="mono">类型：{{ term.data_type }}</span>
                  <span v-if="term.abbreviation">简称：{{ term.abbreviation }}</span>
                  <span v-if="term.mapped_table">来源：{{ term.table_cn || term.mapped_table }}{{ term.mapped_field ? '.' + term.mapped_field : '' }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </template>

      <!-- ================= 视图5：分析模板 ================= -->
      <template v-else-if="activeView === 'templates'">
        <div class="kb-templates">
          <div class="kb-templates-toolbar">
            <div>
              <div class="kb-graph-title"><AppIcon name="layout-grid" :size="14" /> 分析模板助手</div>
              <div class="kb-templates-sub">按您的数据权限和使用习惯，推荐您能直接执行的分析模板</div>
            </div>
            <input v-model="templateSearch" type="text" placeholder="搜索模板名称、场景、标签…" class="kb-input kb-input-flex" />
            <select v-model="templateSceneFilter" class="kb-select">
              <option value="">全部场景</option>
              <option v-for="scene in templateScenes" :key="scene" :value="scene">{{ scene }}</option>
            </select>
          </div>
          <div v-if="templateSummary" class="kb-tpl-banner">
            <AppIcon name="shield" :size="14" />
            <span v-if="templateSummary.mode === 'scoped'">
              已按您的数据权限推荐：您可访问 {{ templateSummary.allowed_tables }} 张表，其中
              {{ templateSummary.recommended }} 个模板完全匹配<template v-if="templateSummary.hidden">；{{ templateSummary.hidden }} 个模板因取数表不在授权范围，未展示</template>
            </span>
            <span v-else>已按您的使用习惯排序：常用与收藏的模板排在前面</span>
          </div>
          <div class="kb-template-grid">
            <div v-if="!filteredTemplates.length" class="kb-empty">暂无符合条件的模板。请调整搜索词或清空筛选。</div>
            <div v-for="template in filteredTemplates" :key="template.id" class="kb-template-card" :class="{ 'kb-tpl-recommended': template.recommended }" @click="applyTemplate(template)">
              <div class="kb-template-head">
                <div>
                  <div class="kb-template-icon"><AppIcon :name="templateIcon(template)" :size="24" /></div>
                  <div class="kb-template-name">{{ template.name }}</div>
                </div>
                <span v-if="template.recommended" class="kb-tpl-rec-chip"><AppIcon name="sparkles" :size="11" /> 为您推荐</span>
                <span class="kb-template-scene">{{ template.scene }}</span>
                <button class="kb-template-fav" :class="{ fav: isTemplateFav(template.id) }" :title="isTemplateFav(template.id) ? '取消收藏' : '收藏模板'" @click.stop="toggleTemplateFav(template)"><AppIcon name="star" :size="13" /></button>
              </div>
              <div class="kb-template-desc">{{ template.desc }}</div>
              <div v-if="template.recommend_reason" class="kb-tpl-reason"><AppIcon name="check-circle" :size="12" /> {{ template.recommend_reason }}</div>
              <div class="kb-template-tags">
                <span v-for="tag in template.tags" :key="tag" class="kb-template-tag">{{ tag }}</span>
              </div>
              <div class="kb-template-metrics">
                <div class="kb-template-question"><AppIcon name="lightbulb" :size="12" /> 示例问题：<b>{{ template.example_question }}</b></div>
                <div class="kb-template-count"><AppIcon name="bar-chart-2" :size="12" /> {{ template.metrics_count }} 指标 · {{ template.tables_count }} 张表 · {{ (template.questions || []).length || template.metrics_count }} 个步骤</div>
              </div>
              <div class="kb-template-actions">
                <button
                  type="button"
                  class="kb-template-run"
                  :disabled="runningTplId === template.id"
                  title="按模板步骤一键执行（确定性编译，秒级出报告）"
                  @click.stop="runTemplate(template)"
                >
                  <AppIcon name="zap" :size="12" /> {{ runningTplId === template.id ? '生成中…' : '一键生成报告' }}
                </button>
                <button type="button" class="kb-template-apply" @click.stop="applyTemplate(template)">应用该模板</button>
              </div>
            </div>
          </div>
        </div>
      </template>

      <!-- ================= 视图7：我的收藏 ================= -->
      <template v-else-if="activeView === 'fav'">
        <div class="kb-fav">
          <div class="kb-fav-head">
            <span class="kb-graph-title"><AppIcon name="star" :size="14" /> 我的收藏</span>
            <span class="kb-fav-sub">共 {{ favCount }} 条 · 业务对象 / 指标 / 规则 / 主题 / 术语 / 分析模板的收藏都在这里</span>
            <button v-if="favCount" type="button" class="kb-ghost-btn" @click="clearAllFavs">清空全部</button>
          </div>
          <div v-if="!favCount" class="kb-empty">
            还没有收藏。<br />
            在场景知识里选中对象 / 指标 / 规则 / 主题后，点详情右上角的 ☆；或给术语、分析模板点 ☆，都会出现在这里。
          </div>
          <div v-else class="kb-fav-list">
            <div v-for="(item, idx) in favVisible" :key="item.kind + ':' + item.key + ':' + idx" class="kb-fav-item" :title="'打开：' + item.label" @click="gotoFav(item)">
              <span class="kb-fav-kind"><AppIcon :name="favKindIcon(item)" :size="15" /></span>
              <div class="kb-fav-main">
                <div class="kb-fav-title">{{ item.label }}</div>
                <div class="kb-fav-meta">
                  <span class="kb-fav-tag">{{ favKindZh(item) }}</span>
                  <template v-if="item.scene"><span>{{ item.scene }}</span></template>
                  <template v-if="item.ts"><span>收藏于 {{ new Date(item.ts).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) }}</span></template>
                </div>
              </div>
              <div class="kb-fav-actions">
                <button type="button" class="kb-term-action" title="取消收藏" @click.stop="removeFav(item)"><AppIcon name="star" :size="13" /></button>
              </div>
            </div>
          </div>
        </div>
      </template>

    </div>

    <!-- ================= 右侧业务域面板 ================= -->
    <aside class="kb-panel" :class="{ collapsed: panelCollapsed }">
      <button
        class="kb-panel-toggle"
        type="button"
        :aria-label="panelCollapsed ? '展开业务域' : '收起业务域'"
        :title="panelCollapsed ? '展开业务域' : '收起业务域'"
        @click="togglePanel"
      >
        <span class="kb-panel-toggle-icon">{{ panelCollapsed ? '‹' : '›' }}</span>
      </button>

      <template v-if="!panelCollapsed">
        <div class="kb-panel-head">
          <span><AppIcon name="book-open" :size="14" /> 业务域</span>
          <small>按类型浏览业务知识</small>
        </div>
        <div class="kb-panel-search">
          <AppIcon name="filter" :size="13" class="text-gray-400" />
          <input v-model="domainSearch" type="text" placeholder="筛选业务域…" class="kb-panel-search-input" />
        </div>

        <div class="kb-tree">
          <div v-for="cat in domainTree" :key="cat.key" class="kb-tree-cat">
            <button class="kb-tree-cat-head" @click="toggleDomainCat(cat.key)">
              <span><AppIcon :name="cat.icon" :size="13" /> {{ cat.label }}</span>
              <span class="kb-tree-count">{{ cat.children.length }}</span>
              <AppIcon name="chevron-down" :size="12" class="kb-tree-chevron transition-transform duration-200" :class="expandedCats.has(cat.key) ? '' : '-rotate-90'" />
            </button>
            <div v-if="expandedCats.has(cat.key)" class="kb-tree-children">
              <div
                v-for="node in cat.children"
                :key="node.id"
                class="kb-tree-node"
                :class="{ active: isActiveDomain(node) }"
                @click="selectDomainNode(node)"
              >
                <span class="kb-tree-node-label">{{ node.label }}</span>
                <small class="kb-tree-node-sub">{{ node.sceneName }}</small>
              </div>
            </div>
          </div>
        </div>
      </template>
    </aside>
  </div>

  <!-- ====== 知识编辑弹窗 ====== -->
  <Teleport to="body">
    <div v-if="editModal" class="kb-modal-mask" @click.self="editModal = null">
      <div class="kb-modal">
        <div class="kb-modal-head"><span><AppIcon name="pencil" :size="14" /> 编辑知识</span><button class="kb-detail-close" @click="editModal = null"><AppIcon name="x" :size="14" /></button></div>
        <div class="kb-modal-body">
          <div v-for="f in editModal.fields" :key="f.name" class="kb-form-row">
            <label>{{ f.label }}</label>
            <textarea v-if="f.rows" v-model="f.value" :rows="f.rows" :disabled="f.ro" class="kb-input"></textarea>
            <input v-else v-model="f.value" :disabled="f.ro" class="kb-input" />
          </div>
          <p v-if="editMsg" class="kb-form-msg" :class="{ ok: editMsgOk }">{{ editMsg }}</p>
        </div>
        <div class="kb-modal-foot">
          <button class="kb-ghost-btn" @click="cancelEdit">取消</button>
          <button class="kb-primary-btn" :disabled="editSaving" @click="saveEdit">{{ editSaving ? '保存中…' : '保存修改' }}</button>
        </div>
      </div>
    </div>
  </Teleport>

  <!-- ====== 字段翻译编辑弹窗 ====== -->
  <Teleport to="body">
    <div v-if="fieldEdit" class="kb-modal-mask" @click.self="fieldEdit = null">
      <div class="kb-modal kb-modal-sm">
        <div class="kb-modal-head"><span><AppIcon name="pencil" :size="14" /> 字段业务含义</span><button class="kb-detail-close" @click="fieldEdit = null"><AppIcon name="x" :size="14" /></button></div>
        <div class="kb-modal-body">
          <div class="kb-form-row"><label>字段</label><input :value="fieldEdit.column" disabled class="kb-input" /></div>
          <div class="kb-form-row"><label>业务含义</label><textarea v-model="fieldEdit.value" rows="3" class="kb-input"></textarea></div>
          <p v-if="fieldEditMsg" class="kb-form-msg">{{ fieldEditMsg }}</p>
        </div>
        <div class="kb-modal-foot">
          <button class="kb-ghost-btn" @click="fieldEdit = null">取消</button>
          <button class="kb-primary-btn" @click="saveFieldEdit">保存</button>
        </div>
      </div>
    </div>
  </Teleport>

  <!-- ====== 自定义术语弹窗 ====== -->
  <Teleport to="body">
    <div v-if="termFormModal" class="kb-modal-mask" @click.self="termFormModal = false">
      <div class="kb-modal">
        <div class="kb-modal-head"><span><AppIcon name="plus" :size="14" /> 新增术语</span><button class="kb-detail-close" @click="termFormModal = false"><AppIcon name="x" :size="14" /></button></div>
        <div class="kb-modal-body">
          <div class="kb-form-row"><label>术语名称 *</label><input v-model="termForm.term" placeholder="如：缺陷PPM" class="kb-input" /></div>
          <div class="kb-form-row"><label>英文名</label><input v-model="termForm.en" placeholder="如：Defect PPM" class="kb-input" /></div>
          <div class="kb-form-row"><label>术语定义 *</label><textarea v-model="termForm.definition" rows="2" placeholder="术语的业务含义…" class="kb-input"></textarea></div>
          <div class="kb-form-row"><label>知识类型</label><select v-model="termForm.knowledge_type" class="kb-input"><option v-for="kt in knowledgeTypes" :key="kt" :value="kt">{{ kt }}</option></select></div>
          <div class="kb-form-row"><label>业务分类</label><input v-model="termForm.category" placeholder="如：质量/生产/设备" class="kb-input" /></div>
          <div class="kb-form-row"><label>简称</label><input v-model="termForm.abbreviation" class="kb-input" /></div>
          <div class="kb-form-row"><label>数据类型</label><input v-model="termForm.data_type" placeholder="如：NUMERIC" class="kb-input" /></div>
          <p v-if="termFormMsg" class="kb-form-msg" :class="{ ok: termFormMsgOk }">{{ termFormMsg }}</p>
        </div>
        <div class="kb-modal-foot">
          <button class="kb-ghost-btn" @click="termFormModal = false">取消</button>
          <button class="kb-primary-btn" @click="saveCustomTerm">保存术语</button>
        </div>
      </div>
    </div>
  </Teleport>

  <!-- ====== 数据底座概览弹窗：单表放大 ====== -->
  <ModalDialog :visible="showEnlargeModal" :title="enlargeTable?.chinese_name || enlargeTable?.table_name || ''" width="820px" maxHeight="86vh" @close="showEnlargeModal = false">
    <div v-if="enlargeTable" class="space-y-4">
      <div class="flex items-center gap-3 pb-3 border-b border-gray-100">
        <span class="stat-chip" style="background: rgba(10,132,255,0.10); color: #0A84FF">
          <AppIcon name="table" :size="20" :stroke-width="1.9" />
        </span>
        <div>
          <div class="text-base font-semibold text-gray-900">{{ enlargeTable.chinese_name || enlargeTable.table_name }}</div>
          <div v-if="enlargeTable.chinese_name" class="text-xs text-gray-400 font-mono">{{ enlargeTable.table_name }}</div>
        </div>
        <div class="ml-auto flex gap-4 text-xs text-gray-400">
          <span>{{ enlargeTable.columns?.length || 0 }} 字段</span>
          <span>{{ Number(enlargeTable.row_count ?? 0).toLocaleString() }} 行</span>
          <span>{{ enlargeTable.columns?.filter((c: any) => c.primary_key).length || 0 }} 主键</span>
        </div>
      </div>
      <div class="max-h-[52vh] overflow-y-auto space-y-1.5 pr-1">
        <div
          v-for="col in enlargeTable.columns"
          :key="col.name"
          class="flex items-start gap-2.5 rounded-lg border border-gray-100 bg-gray-50/50 px-3 py-2"
        >
          <AppIcon v-if="col.primary_key" name="key" :size="14" class="mt-0.5 shrink-0 text-amber-500" />
          <span v-else class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-gray-300"></span>
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2">
              <span class="font-mono text-sm font-semibold text-gray-800">{{ col.name }}</span>
              <span class="text-[11px] text-gray-400">{{ col.type }}</span>
              <span v-if="col.masked" class="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-amber-50 text-amber-600 border border-amber-100" title="该字段按你的权限做了脱敏展示">
                <AppIcon name="lock" :size="10" /> 脱敏
              </span>
              <span v-if="!col.nullable" class="text-[10px] text-red-400">NOT NULL</span>
            </div>
            <div class="mt-1 text-[13px] leading-relaxed text-gray-600">{{ col.translation || col.comment || '—' }}</div>
          </div>
        </div>
      </div>
    </div>
  </ModalDialog>

  <!-- ====== 「反馈知识有误」弹窗：填完后作为大号待办消息发送到人工客服（不新开聊天框） ====== -->
  <Teleport to="body">
    <div v-if="fbModal" class="kb-modal-mask" @click.self="fbModal = null">
      <div class="kb-modal kb-modal-sm">
        <div class="kb-modal-head">
          <span><AppIcon name="alert-triangle" :size="14" /> 反馈知识有误</span>
          <button class="kb-detail-close" @click="fbModal = null"><AppIcon name="x" :size="14" /></button>
        </div>
        <div class="kb-modal-body">
          <template v-if="!fbModal.done">
            <div class="kb-fb-target">
              <span class="kb-fb-kind">{{ fbItem?.kind }}</span>
              <b>{{ fbItem?.title }}</b>
              <small>
                {{ fbItem?.sceneName }}
                <template v-if="fbItem?.table"> · <span class="mono">{{ fbItem?.table }}</span></template>
              </small>
            </div>
            <div class="kb-form-row">
              <label>哪里有问题？ *</label>
              <textarea
                v-model="fbModal.note"
                rows="4"
                maxlength="500"
                class="kb-input"
                placeholder="例如：口径公式写错了，缺陷数应按工单去重；或字段业务含义与实际不符…"
              ></textarea>
              <div class="kb-fb-count">{{ fbModal.note.length }}/500</div>
            </div>
            <p class="kb-fb-tip">
              <AppIcon name="info" :size="11" />
              提交后会作为一条待办发到「人工客服」，管理员可直接查看并处理，无需再单独描述一次。
            </p>
            <p v-if="fbModal.err" class="kb-form-msg">{{ fbModal.err }}</p>
          </template>
          <div v-else class="kb-fb-done">
            <AppIcon name="check-circle" :size="26" />
            <b>已提交给管理员</b>
            <p>反馈已进入「人工客服」会话，管理员处理后状态会更新为「已处理」。</p>
          </div>
        </div>
        <div class="kb-modal-foot">
          <template v-if="!fbModal.done">
            <button class="kb-ghost-btn" @click="fbModal = null">取消</button>
            <button class="kb-primary-btn" :disabled="fbModal.busy" @click="submitFeedback">
              {{ fbModal.busy ? '提交中…' : '提交反馈' }}
            </button>
          </template>
          <button v-else class="kb-primary-btn" @click="fbModal = null">知道了</button>
        </div>
      </div>
    </div>
  </Teleport>

  <!-- 术语详情弹窗：与上面的反馈弹窗同样 Teleport 到 body。
       openTermDetail 一直往 detailModal 写内容，但此前模板里从未渲染它 →
       点任意术语卡片「零反应」，escHtml 也因此成了死代码。 -->
  <Teleport to="body">
    <div v-if="detailModal" class="kb-modal-mask" @click.self="detailModal = null">
      <div class="kb-modal" @click.stop>
        <div class="kb-modal-head">
          <span><AppIcon name="book-open" :size="14" /> {{ detailModal.title }}</span>
          <button class="kb-detail-close" @click="detailModal = null"><AppIcon name="x" :size="14" /></button>
        </div>
        <div class="kb-modal-body" v-html="detailModal.content"></div>
        <div class="kb-modal-foot">
          <button class="kb-primary-btn" @click="detailModal = null">关闭</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import KnowledgeGraph from '../components/KnowledgeGraph.vue'
import LightRagGraph from '../components/LightRagGraph.vue'
import GraphNodeDetail from '../components/GraphNodeDetail.vue'
import AppIcon from '../components/AppIcon.vue'
import ModalDialog from '../components/ModalDialog.vue'
import TableCardGrid from '../components/TableCardGrid.vue'
import { findZhEntry, entityTypeZh, type ZhEntry } from '../data/knowledgeGraphZh'
import { canDo, isEditor } from '../auth'
import { isFavorite, toggleFavorite, removeFavorite, knowledgeFavorites, clearFavorites } from '../stores/favorites'
import { sendKnowledgeFeedback, fetchUnread, supportUnread } from '../supportService'

const props = defineProps<{ initialScene?: string }>()
const emit = defineEmits<{
  'navigate-ask': [question: string]
  'navigate': [page: string]
}>()

// 核心指标卡「管理全部口径」：跳转指标口径注册表页（同一注册表的管理视图，消除"以哪个为准"的困惑）
const goMetricsPage = () => emit('navigate', 'metrics')

// ========== 数据表 / 表间关系（顶部导航视图，自原「数据底座概览」弹窗迁移） ==========
const showEnlargeModal = ref(false)

const tableList = ref<any[]>([])
const tableListLoading = ref(false)
const enlargeTable = ref<any>(null)
const graphLoading = ref(false)
const modalGraphData = ref<any[]>([])
const modalGraphNodes = ref<any[]>([])

// 统一请求：检查 res.ok，非 2xx 抛错
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

const loadTables = async (force = false) => {
  if (!force && (tableList.value.length > 0 || tableListLoading.value)) return
  tableListLoading.value = true
  try {
    const data = await fetchJson('/api/tables/')
    tableList.value = data.tables || []
  } catch (e) {
    console.error('加载表列表失败:', e)
  } finally {
    tableListLoading.value = false
  }
}

// 数据支撑表卡「放大」：直接打开该对象的详情弹框（Tab 定位到「字段」看完整字段），
// 不再另开一个「单表放大」弹框。原来 放大 按钮的点击会冒泡到卡片头部再触发一次 open，
// 结果同时弹出两张卡片（对象详情 + 单表放大）；现在合并为一张：
// 详情弹框里已包含 说明 / 字段（完整列表）/ 关系，以及收藏・点赞・去问析等操作。
const openTableEnlarge = (tbl: any) => {
  const obj = (scenesMap.value[activeScene.value]?.objects || []).find((o: any) => o.table === tbl.table_name)
  if (!obj) {
    // 兵底：表不在当前场景对象里（理论上不会发生）→ 退回原来的单表放大弹框
    enlargeTable.value = tbl
    showEnlargeModal.value = true
    return
  }
  openObjectDetail(obj, activeScene.value)
  detailTab.value = 'fields'
}

// 表中文名（与《数据介绍.md》第 2 节一致，用于关系明细展示）
const TABLE_CN_LOOKUP: Record<string, string> = {
  dim_product: '产品主数据', dim_process: '工序主数据', dim_production_line: '产线主数据',
  dim_equipment: '设备主数据', mes_work_order: '生产工单', mes_process_output: '工序产量',
  qms_inspection: '质量检验', qms_defect_detail: '不良明细', eqp_downtime_record: '设备停机记录',
  inv_inventory_snapshot: '库存快照',
}
// 关系明细行：中文表名.字段 → 中文表名.字段（业务说明）
const relDisplay = (r: any) => {
  const s = `${TABLE_CN_LOOKUP[r.source_table] || r.source_table}.${r.source_column}`
  const t = `${TABLE_CN_LOOKUP[r.target_table] || r.target_table}.${r.target_column}`
  const note = r.note ? `（${r.note}）` : ''
  return `${s} → ${t}${note}`
}

const loadRels = async (force = false) => {
  if (!force && (modalGraphData.value.length > 0 || modalGraphNodes.value.length > 0 || graphLoading.value)) return
  graphLoading.value = true
  try {
    const data = await fetchJson('/api/tables/relationships')
    modalGraphData.value = data.relationships || []
    modalGraphNodes.value = data.nodes || []
  } catch (e) {
    console.error('加载关系数据失败:', e)
  } finally {
    graphLoading.value = false
  }
}


// ========== 用户知识数据：收藏 / 反馈 / 人工覆盖 / 自定义术语 ==========
const userData = ref<any>({ username: '', favorites: [], feedback: {}, overrides: {}, custom_terms: [], template_use_count: {} })

const loadUserData = async () => {
  try {
    const res = await fetch('/api/knowledge/user-data')
    if (res.ok) userData.value = await res.json()
  } catch { /* ignore */ }
}
const isFav = (d: any) => !!d && isFavorite(d.kind || '', String(d.id))
const toggleFav = (d: any) => {
  if (!d) return
  toggleFavorite({ kind: d.kind || '', key: String(d.id), label: d.title || String(d.id), scene: d.sceneName })
}

// ========== 我的收藏（统一入口：详情 / 术语 / 分析模板 的收藏聚合）==========
const FAV_META: Record<string, { zh: string; icon: string }> = {
  业务对象: { zh: '业务对象', icon: 'package' },
  业务指标: { zh: '业务指标', icon: 'line-chart' },
  业务规则: { zh: '业务规则', icon: 'ruler' },
  分析主题: { zh: '分析主题', icon: 'target' },
  term: { zh: '术语', icon: 'book-open' },
  template: { zh: '分析模板', icon: 'layout-grid' },
}
const favCount = computed(() => knowledgeFavorites.value.filter((f: any) => f.key && String(f.key) !== 'undefined').length)
const favKindZh = (item: any) => (FAV_META[item.kind] || {}).zh || String(item.kind || '知识')
const favKindIcon = (item: any) => (FAV_META[item.kind] || { icon: 'star' }).icon || 'star'
const favVisible = computed(() => knowledgeFavorites.value.filter((f: any) => f.key && String(f.key) !== 'undefined'))

// 术语收藏（kind='term'，key=术语名）
const isTermFav = (term: any) => isFavorite('term', term?.term)
const toggleTermFav = (term: any) => toggleFavorite({ kind: 'term', key: term.term, label: term.term_cn || term.term })

// 分析模板收藏（kind='template'，key=模板 id）
const isTemplateFav = (id: string) => isFavorite('template', String(id))
const toggleTemplateFav = (tpl: any) => toggleFavorite({
  kind: 'template', key: String(tpl.id), label: tpl.name || String(tpl.id), scene: tpl.scene,
})

// 收藏列表 → 打开原知识（术语/模板切对应视图；对象/指标/规则/主题切场景并打开详情）
// 收藏列表 → 打开原知识详情（关键修复：收藏只存了场景「中文名」，而 scenesMap 的 key 是英文
// sceneKey，直接用中文名取值永远 miss → 需先按 name 还原 sceneKey；对象/指标/规则/主题按
// key 前缀( obj:/met:/rule:/topic: )精确定位后打开详情面板）
const gotoFav = (item: any) => {
  const k = item.kind
  const ks = String(item.key || '')
  // 术语 / 模板：切到对应视图并把搜索框置为该名称 → 列表过滤到该项
  if (k === 'term') { activeView.value = 'terms'; termSearch.value = item.label || ks; return }
  if (k === 'template') { activeView.value = 'templates'; templateSearch.value = item.label || ks; return }

  activeView.value = 'scene'
  const ci = ks.indexOf(':')
  const prefix = ci > 0 ? ks.slice(0, ci) : ''
  const value = ci > 0 ? ks.slice(ci + 1) : item.label
  // 场景 key 还原（收藏存的 item.scene 是中文场景名）
  let sceneKey = activeScene.value
  if (item.scene) {
    const hitScene = scenes.value.find((s: any) => s.name === item.scene)
    if (hitScene) sceneKey = hitScene.key
  }

  const tryPick = (scene: any, name: string) => {
    if (!scene) return null
    const arr = prefix === 'met' || k === '业务指标' ? (scene.metrics || [])
      : prefix === 'rule' || k === '业务规则' ? (scene.rules || [])
      : (scene.topics || [])
    return arr.find((x: any) =>
      String(x.name || '') === name || String(x.table || '') === name || String(x.label || '') === name) || null
  }

  // 业务对象：切到对应场景并打开对象详情（不做任何自动滚动）
  if (prefix === 'obj' || k === '业务对象') {
    const sc = Object.keys(scenesMap.value).find((sk: string) => {
      const s: any = scenesMap.value[sk]
      return (s.objects || []).some((o: any) => o.table === value)
    })
    if (sc) activeScene.value = sc
    const scKey2 = sc || sceneKey
    const obj = (scenesMap.value[scKey2]?.objects || []).find((o: any) => o.table === value)
    if (obj) openObjectDetail(obj, scKey2)
    return
  }

  // 指标 / 规则 / 主题：先命中场景内，跨全部场景兜底
  let hit: any = tryPick(scenesMap.value[sceneKey], value)
  if (!hit) {
    for (const sk of Object.keys(scenesMap.value)) {
      hit = tryPick(scenesMap.value[sk], value)
      if (hit) { sceneKey = sk; break }
    }
  }
  if (!hit) return
  activeScene.value = sceneKey
  if (prefix === 'met' || k === '业务指标') openMetricDetail(hit, sceneKey)
  else if (prefix === 'rule' || k === '业务规则') openRuleDetail(hit, sceneKey)
  else openTopicDetail(hit, sceneKey)
}
const removeFav = (item: any) => removeFavorite(item.kind, item.key)
const clearAllFavs = () => {
  if (window.confirm('确定清空全部收藏吗？')) clearFavorites()
}

const setFeedback = async (key: string, vote: string) => {
  try {
    const res = await fetch('/api/knowledge/feedback', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key, vote }),
    })
    const data = await res.json()
    if (data.success) userData.value.feedback[key] = data.feedback
  } catch { /* ignore */ }
  finally { loadUserData() }
}
// ====== 「反馈知识有误」：先弹框填写，再作为大号待办消息发到人工客服 ======
// 交互设计：👎 不再直接投票，而是打开反馈框；提交成功后同时记录「有误」投票 + 投递反馈工单。
const fbModal = ref<{ note: string; busy: boolean; done: boolean; err: string } | null>(null)
const fbItem = ref<DetailItem | null>(null)

const openFeedback = () => {
  if (!selectedDetail.value) return
  fbItem.value = selectedDetail.value
  fbModal.value = { note: '', busy: false, done: false, err: '' }
}

const submitFeedback = async () => {
  const st = fbModal.value
  const item = fbItem.value
  if (!st || !item) return
  const note = st.note.trim()
  if (!note) {
    st.err = '请描述一下哪部分有误，便于管理员修正'
    return
  }
  st.busy = true
  st.err = ''
  try {
    await sendKnowledgeFeedback({
      item_key: item.id,
      item_kind: item.kind,
      item_title: item.title,
      item_scene: item.sceneName,
      item_table: item.table || '',
      item_desc: item.desc || '',
      note,
    })
    await setFeedback(item.id, 'down')          // 同步记录「有误」投票
    try { supportUnread.value = await fetchUnread() } catch { /* 忽略：轮询也会刷新 */ }
    st.done = true
  } catch (e: any) {
    st.err = `提交失败：${e?.message || e}`
  } finally {
    st.busy = false
  }
}
const saveOverride = async (key: string, kind: string, patch: any) => {
  const res = await fetch('/api/knowledge/override', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key, kind, patch }),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.detail || `保存失败（HTTP ${res.status}）`)
  if (!data.success) throw new Error(data.detail || '保存失败')
  // 覆盖后会改变 scenes/terms 结果，需重新加载。
  // 图谱节点 label 与快照缓存同样来自这些数据：原来只刷 scenes/terms，导致
  // 「场景知识」里已显示新名、「知识图谱」里仍是旧名，同一实体两个名字。
  snapshots.value = {}
  await Promise.all([loadKnowledge(), loadUserData()])
  await loadRelations()
}

// ========== API 基础地址 ==========
const API_BASE = '/api'

// ========== 加载状态 ==========
const loading = ref(true)
const errorMsg = ref('')

// ========== 视图切换 ==========
const viewTabs = [
  { key: 'scene', icon: 'folder', name: '场景知识' },
  { key: 'terms', icon: 'book-open', name: '术语词典' },
  { key: 'graph', icon: 'git-fork', name: '知识图谱' },
  { key: 'rels', icon: 'share-2', name: '表间关系' },
  { key: 'templates', icon: 'file-text', name: '分析模版' },
  { key: 'fav', icon: 'star', name: '我的收藏' },
]
const activeView = ref('scene')
// 导航视图懒加载：进入「数据表 / 表间关系」时拉取对应数据
watch(() => activeView.value, (v) => {
  if (v === 'scene') loadTables()
  else if (v === 'rels') loadRels()
})

// ========== 场景数据 ==========
const scenesMap = ref<Record<string, any>>({})
const topicList = ref<any[]>([])
const activeScene = ref('production')

const scenes = computed(() => {
  const keys = Object.keys(scenesMap.value)
  if (keys.length === 0) return []
  return keys.map(k => ({
    key: scenesMap.value[k].key,
    icon: scenesMap.value[k].icon,
    name: scenesMap.value[k].name,
    desc: scenesMap.value[k].desc,
    roles: scenesMap.value[k].roles || [],
  }))
})

const currentSceneData = computed(() => scenesMap.value[activeScene.value] || { objects: [] })
const currentSceneTopics = computed(() => currentSceneData.value.topics || [])
// 主题分组（C 类）：场景主题=综合分析；指标主题=单指标趋势（机器模板生成，弱化展示）
const sceneTopicsBiz = computed(() =>
  (currentSceneTopics.value as any[]).filter((t: any) => t.kind !== '指标主题'))
const sceneTopicsMetric = computed(() =>
  (currentSceneTopics.value as any[]).filter((t: any) => t.kind === '指标主题'))

// ====== P0（2026-09-03）：场景内容增强辅助 ======
const sceneObjects = (key: string) => scenesMap.value[key]?.objects || []
// 当前场景对应的数据表（来自 /api/tables/ 全量表，按场景对象表名过滤）
const sceneTables = (key: string) => {
  const names = new Set((scenesMap.value[key]?.objects || []).map((o: any) => o.table))
  return tableList.value.filter((t: any) => names.has(t.table_name))
}
const currentSceneInfo = computed(() => {
  const s = scenes.value.find((x) => x.key === activeScene.value)
  return { name: s?.name || activeScene.value, desc: s?.desc || '' }
})
// 场景用途解释：概要条说明"该场景是干什么的"（优先于后端简短 desc 展示）
const SCENE_INTRO: Record<string, string> = {
  production: '面向制造业生产执行环节：追踪产量与产能利用、对比工序良率、评估工单达成与在制负荷，识别瓶颈工序与质量短板，支撑排产优化、交付保障与生产改进决策。',
  quality: '面向产品质量管控环节：监控检验合格率、不良分布与缺陷结构，定位不良高发工序与产品，支撑质量改进优先级判定与缺陷源头治理。',
  equipment: '面向设备运行管理环节：统计停机时长、停机次数与非计划停机占比，关联停机原因与产线表现，支撑设备效率评估、保养计划与停机改进。',
  inventory: '面向库存管理环节：监控库存水位、可用/冻结与安全库存对比，预警缺货与积压风险，支撑补货决策、库存周转优化与呆滞清理。',
}
const sceneIntro = (key: string) => SCENE_INTRO[key] || scenesMap.value[key]?.desc || ''
// 指标去重：注册表内置+自定义可能同名（如 已完成工单数/返工数量），卡片只展示一次
const sceneMetricsUnique = computed(() => {
  const seen = new Set<string>()
  const out: any[] = []
  for (const m of currentSceneData.value.metrics || []) {
    const n = m?.name
    if (!n || seen.has(n)) continue
    seen.add(n)
    out.push(m)
  }
  return out
})
// 对象 → 关联指标（m.tables 含该表）
// ====== v6 实时快照（GET /api/knowledge/scene-snapshot/{scene}，按注册口径实时计算） ======
const snapshots = ref<Record<string, any>>({})
const snapshotBusy = ref<Record<string, boolean>>({})
// 场景可继续补的口径（未注册占位 → 引导到口径页注册后自动出现）
const SCENE_SUGGESTS: Record<string, string[]> = {
  production: ['拖期工单数', '报废数量', '按产线产量排行', '按班次对比'],
  quality: ['复检批次数', '按产品不良排行'],
  equipment: ['按车间停机时长', '稼动率 / OEE'],
  inventory: ['库龄 > 90 天', '月均周转天数'],
}
const loadSnapshot = async (key: string) => {
  if (snapshots.value[key] || snapshotBusy.value[key]) return
  snapshotBusy.value[key] = true
  try {
    const r = await fetch(`/api/knowledge/scene-snapshot/${key}`)
    if (!r.ok) throw new Error('snapshot failed')
    snapshots.value[key] = await r.json()
  } catch {
    snapshots.value[key] = { groups: [], error: 'load failed' }
  } finally {
    snapshotBusy.value[key] = false
  }
}
const snapBusy = (key: string) => !!snapshotBusy.value[key]
const sceneSnapshot = computed(() => snapshots.value[activeScene.value] || { groups: [] })
const sceneSuggests = computed(() => SCENE_SUGGESTS[activeScene.value] || [])
const sceneTotalCols = computed(() =>
  (currentSceneData.value.objects || []).reduce((s: number, o: any) => s + ((o.columns || o.fields || []).length), 0))
const sceneTotalRows = computed(() =>
  (currentSceneData.value.objects || []).reduce((s: number, o: any) => s + (Number(o.row_count) || 0), 0).toLocaleString())
// 数值格式化：大数万级、% 保留 2 位、不可算显示 —
const fmtVal = (v: any, _u = '') => {
  if (v === null || v === undefined || v === '') return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return String(v)
  if (Math.abs(n) >= 100000) return (Math.round((n / 10000) * 10) / 10) + '万'
  if (Number.isInteger(n)) return n.toLocaleString('zh-CN')
  return String(Math.round(n * 100) / 100)
}
// 快照项 → 打开指标详情（优先场景已注册口径对象，否则构造回退）
const openSnapMetric = (item: any, group?: any) => {
  const hit = sceneMetricsUnique.value.find((m: any) => m.name === item.name)
  if (hit) return openMetricDetail(hit, activeScene.value)
  openMetricDetail({
    name: item.name,
    unit: item.unit,
    formula: item.expr || item.formula || '',
    description: `该口径为注册表实时计算值（源表 ${group?.table || item.table || ''}）；点「管理全部口径」可查看/维护。`,
    tables: [group?.table || item.table].filter(Boolean),
  }, activeScene.value)
}
watch(() => activeScene.value, (k) => {
  if (k && Object.keys(scenesMap.value).length) loadSnapshot(k)
})

const objMetrics = (obj: any) =>
  sceneMetricsUnique.value.filter((m: any) => (m.tables || []).includes(obj?.table))
// 对象 → 去问析：优先用关联注册指标组问（确定性秒查）；否则用场景主题典型问题兜底
const askAboutObj = (obj: any) => {
  const label = obj?.label || obj?.table
  const ms = objMetrics(obj)
  let q = ''
  if (ms.length) {
    q = `统计${label}的${ms[0].name}`
  } else {
    const firstQ = (currentSceneTopics.value[0]?.questions || [])[0]
    q = firstQ || `分析${currentSceneInfo.value.name}下${label}的数据情况`
  }
  emit('navigate-ask', q)
}
const goAskQ = (q: string) => { if (q) emit('navigate-ask', q) }

// 场景图标：不渲染后端 emoji 字段，按场景名映射固定 Lucide 图标
const sceneIcon = (name?: string): string => {
  const n = name || ''
  // 周期报告（周报/月报）优先判定：其名称含「生产」等业务域词，需先于业务域规则匹配
  if (n.includes('周期')) return 'clock'
  if (n.includes('生产')) return 'factory'
  if (n.includes('质量')) return 'check-circle'
  if (n.includes('设备')) return 'wrench'
  if (n.includes('库存')) return 'package'
  if (n.includes('销售')) return 'trending-up'
  if (n.includes('采购')) return 'box'
  if (n.includes('人事')) return 'users'
  if (n.includes('财务')) return 'sigma'
  if (n.includes('基础数据')) return 'database'
  return 'target'
}


// ========== 详情面板（概览 / 字段 / 关系） ==========
type DetailKind = '业务对象' | '业务指标' | '业务规则' | '分析主题'
interface DetailItem {
  kind: DetailKind
  id: string
  title: string
  icon: string
  sceneKey: string
  sceneName: string
  table?: string
  desc?: string
  row_count?: number
  heat?: number
  is_core?: boolean
  columns?: any[]
  unit?: string
  formula?: string
  condition?: string
  questions?: string[]
  tables?: string[]
}
const selectedDetail = ref<DetailItem | null>(null)
const detailTab = ref('overview')
const detailTabs = [
  { key: 'overview', icon: 'list', label: '概览' },
  { key: 'fields', icon: 'table', label: '字段' },
  { key: 'relations', icon: 'git-fork', label: '关系' },
]
const badgeKey = (kind?: string) => {
  const map: Record<string, string> = { '业务对象': 'obj', '业务指标': 'met', '业务规则': 'rule', '分析主题': 'topic' }
  return map[kind || ''] || 'obj'
}
const closeDetail = () => { selectedDetail.value = null }
// 弹框内的「本场景指标」按所选知识所属场景取：侧边业务域点击不再切场景，不能沿用 activeScene
const detailSceneMetrics = computed(() => {
  const metrics = scenesMap.value[selectedDetail.value?.sceneKey || activeScene.value]?.metrics || []
  const seen = new Set<string>()
  const out: any[] = []
  for (const m of metrics) {
    const n = m?.name
    if (!n || seen.has(n)) continue
    seen.add(n)
    out.push(m)
  }
  return out
})
// 弹框内点关联指标：同样按所选知识所属场景打开（场景名/来源表标注才不会串场景）
const openMetricInDetail = (m: any) => openMetricDetail(m, selectedDetail.value?.sceneKey || activeScene.value)
// 从任意上下文按表名打开业务对象详情（跨场景定位）
const openObjByTable = (table: string) => {
  const ref = findObjRef(table)
  if (!ref) return
  activeScene.value = ref.sceneKey
  openObjectDetail(ref.obj, ref.sceneKey)
}

// 数据支撑表卡点击：按表名定位到该场景业务对象，打开对象详情
const openSceneObjFromTable = (tbl: any) => {
  const obj = (scenesMap.value[activeScene.value]?.objects || []).find((o: any) => o.table === tbl.table_name)
  if (obj) openObjectDetail(obj, activeScene.value)
}

const openObjectDetail = (obj: any, sceneKey: string) => {
  const scene = scenesMap.value[sceneKey]
  selectedDetail.value = {
    kind: '业务对象',
    id: `obj:${obj.table}`,
    title: obj.label || obj.table,
    icon: 'table',
    sceneKey,
    sceneName: scene?.name || sceneKey,
    table: obj.table,
    desc: obj.desc,
    row_count: obj.row_count,
    heat: obj.heat,
    is_core: obj.is_core,
    columns: obj.columns || [],
  }
  detailTab.value = 'overview'
  // 关系 Tab 迷你图聚焦到该对象（1 跳邻居），而不是整库
  miniTarget.value = obj.table
  // 热度上报：高频知识浮上来
  fetch('/api/knowledge/hit', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key: obj.table }),
  }).catch(() => {})
}

const openMetricDetail = (m: any, sceneKey: string) => {
  const scene = scenesMap.value[sceneKey]
  selectedDetail.value = {
    kind: '业务指标',
    id: `met:${m.name}`,
    title: m.name,
    icon: 'bar-chart-2',
    sceneKey,
    sceneName: scene?.name || sceneKey,
    desc: m.description || `「${m.name}」是${scene?.name || '该场景'}的核心业务指标，采用固定口径统计。`,
    unit: m.unit,
    formula: m.formula,
    tables: m.tables || [],
  }
  detailTab.value = 'overview'
}

const openRuleDetail = (rule: any, sceneKey: string) => {
  const scene = scenesMap.value[sceneKey]
  selectedDetail.value = {
    kind: '业务规则',
    id: `rule:${rule.name}`,
    title: rule.name,
    icon: 'ruler',
    sceneKey,
    sceneName: scene?.name || sceneKey,
    desc: `「${rule.name}」是${scene?.name || '该场景'}的业务判定规则。`,
    condition: rule.condition,
    formula: rule.formula,
  }
  detailTab.value = 'overview'
}

const openTopicDetail = (t: any, sceneKey: string) => {
  const scene = scenesMap.value[sceneKey]
  selectedDetail.value = {
    kind: '分析主题',
    id: `topic:${t.name}`,
    title: t.name,
    icon: 'target',
    sceneKey,
    sceneName: scene?.name || sceneKey,
    desc: t.desc || `「${t.name}」是该场景的分析主题。`,
    questions: t.questions || [],
  }
  detailTab.value = 'overview'
}

// ========== 迷你关系图（当前详情对象的 1 跳邻居） ==========
const miniGraphRef = ref<any>(null)
const miniTarget = ref<string>('')

const miniGraphNodes = computed(() => {
  if (!miniTarget.value) return graphComponentNodes.value
  const target = miniTarget.value
  const neighborIds = new Set<string>([target])
  graphComponentRels.value.forEach((r: any) => {
    if (r.source_table === target) neighborIds.add(r.target_table)
    if (r.target_table === target) neighborIds.add(r.source_table)
  })
  return graphComponentNodes.value.filter((n: any) => neighborIds.has(n.id))
})
const miniGraphRels = computed(() => {
  if (!miniTarget.value) return graphComponentRels.value
  const target = miniTarget.value
  return graphComponentRels.value.filter((r: any) =>
    r.source_table === target || r.target_table === target)
})
const neighborCount = computed(() => Math.max(0, miniGraphNodes.value.length - 1))
const onMiniGraphNodeClick = (id: string) => {
  // 点击迷你图节点 → 打开对应对象的详情（若存在）
  miniTarget.value = id
  for (const [sceneKey, s] of Object.entries(scenesMap.value) as any[]) {
    const obj = (s?.objects || []).find((o: any) => o.table === id)
    if (obj) { activeScene.value = sceneKey; openObjectDetail(obj, sceneKey); return }
  }
  // 非表节点：仅把迷你图聚焦到该节点
  miniTarget.value = id
}

// ========== P1（2026-09-03）：详情「关系」Tab 关联检索（跨场景） ==========
// 按表名跨场景找对象归属（含其所在场景）
const findObjRef = (table: string): { obj: any; sceneKey: string; sceneName: string } | null => {
  if (!table) return null
  for (const [sk, s] of Object.entries(scenesMap.value) as any[]) {
    const obj = (s?.objects || []).find((o: any) => o.table === table)
    if (obj) return { obj, sceneKey: sk, sceneName: s?.name || sk }
  }
  return null
}
// 表 → 全库关联指标（跨场景去重，附带归属场景 key/名）
// 注意 sceneKey 必须一起返回：模板 @click 传 openMetricDetail(it.m, it.sceneKey)，
// 缺失会让弹窗头部的场景名渲染成字面量 undefined（scenesMap[undefined] → undefined）。
const metricsOfTableAll = (table: string) => {
  const seen = new Set<string>()
  const out: any[] = []
  for (const s of Object.values(scenesMap.value) as any[]) {
    for (const m of s?.metrics || []) {
      if ((m.tables || []).includes(table) && m.name && !seen.has(m.name)) {
        seen.add(m.name)
        out.push({ m, sceneKey: s?.key || '', sceneName: s?.name || '' })
      }
    }
  }
  return out
}
// 对象 → 全库关联规则（规则文本提及 表名/中文名 的弱关联，如实标注）
const rulesOfObjAll = (obj: any) => {
  const t = obj?.table || ''
  const lb = obj?.label || ''
  if (!t) return []
  const out: any[] = []
  for (const s of Object.values(scenesMap.value) as any[]) {
    for (const r of s?.rules || []) {
      const text = `${r.name || ''} ${r.condition || ''} ${r.formula || ''} ${r.source || ''}`
      if (text.includes(t) || (lb && lb.length >= 2 && text.includes(lb))) {
        out.push({ r, sceneKey: s?.key || '', sceneName: s?.name || '' })
      }
    }
  }
  return out
}
// 规则 → 涉及对象（文本命中表名/中文名）
const objectsOfRule = (rule: any) => {
  const text = `${rule?.name || ''} ${rule?.condition || ''} ${rule?.formula || ''} ${rule?.source || ''}`
  const out: any[] = []
  const seen = new Set<string>()
  for (const s of Object.values(scenesMap.value) as any[]) {
    for (const o of s?.objects || []) {
      const t = o.table || ''
      const lb = o.label || ''
      const hit = (t && text.includes(t)) || (lb && lb.length >= 2 && text.includes(lb))
      if (hit && !seen.has(t)) {
        seen.add(t)
        out.push({ obj: o, sceneKey: s?.key || '', sceneName: s?.name || '' })
      }
    }
  }
  return out
}
// 指标 → 口径引用字段（公式中出现的表字段，确定性解析，做简单血缘展示）
const refFieldsOfMetric = (m: any) => {
  const formula = m?.formula || ''
  const table = (m?.tables || [])[0]
  if (!formula || !table) return []
  const ref = findObjRef(table)
  const cols: any[] = ref?.obj?.columns || []
  const tokens = (formula.match(/[a-zA-Z_][a-zA-Z0-9_]*/g) || []).map((x: string) => x.toLowerCase())
  return cols.filter((c) => tokens.includes(String(c.name).toLowerCase())).slice(0, 8)
}
// 详情头部「去问析」（对象用其注册指标问句；指标用统计口径；主题用典型问题）
const askForDetail = () => {
  const d = selectedDetail.value
  if (!d) return
  if (d.kind === '业务对象' && d.table) {
    const s = scenesMap.value[d.sceneKey]
    const obj = (s?.objects || []).find((o: any) => o.table === d.table)
    if (obj) return askAboutObj(obj)
  }
  if (d.kind === '业务指标') return goAskQ(`统计${d.title}`)
  const qs = d.questions || []
  if (d.kind === '分析主题' && qs.length) return goAskQ(qs[0])
}
// 分析主题工作台「去问析」：优先主题首个典型问题
const topicWbAsk = () => {
  const d = selectedDetail.value
  if (!d) return
  const q = (d.questions || [])[0]
  goAskQ(q || `分析${currentSceneInfo.value.name}下的${d.title}`)
}

// ========== 知识图谱（P1）：场景过滤 + 从详情/场景定位到图 ==========
const activeGraphScene = ref('')
const graphSceneOptions = computed(() => {
  const seen: string[] = []
  scenes.value.forEach((sc: any) => {
    if (!seen.includes(sc.name)) seen.push(sc.name)
  })
  return seen
})
const locateInGraph = (table: string) => {
  let sc = ''
  for (const s of Object.values(scenesMap.value) as any[]) {
    if ((s?.objects || []).some((o: any) => o.table === table)) { sc = s?.name || ''; break }
  }
  // 详情弹框已移到视图链之外（全局浮层），跳图谱时必须自己关掉，否则会一直压在图谱上
  selectedDetail.value = null
  activeView.value = 'graph'
  activeGraphScene.value = sc || ''
  const n = graphComponentNodes.value.find((x: any) => String(x.id) === table || String(x.name) === table)
  if (n) onGraphNodeClick(String(n.id || n.name))
  else selectedGraphNode.value = null
  setTimeout(() => graphRef.value?.fitView?.(), 160)
}
// 图谱浮层 → 回到场景知识打开该对象详情
const openSceneObjectOfTable = (table?: string) => {
  if (!table) return
  const ref = findObjRef(table)
  if (!ref) return
  activeView.value = 'scene'
  activeScene.value = ref.sceneKey
  selectedGraphNode.value = null // 关闭图谱节点中文解释弹框，避免返回图谱时残留
  openObjectDetail(ref.obj, ref.sceneKey)
}


// ========== 加载知识数据 ==========
const loadKnowledge = async () => {
  loading.value = true
  errorMsg.value = ''
  try {
    const [scenesRes, termsRes, topicsRes] = await Promise.all([
      fetch(`${API_BASE}/knowledge/scenes`),
      fetch(`${API_BASE}/knowledge/terms`),
      fetch(`${API_BASE}/tables/topics`),
    ])
    if (!scenesRes.ok) throw new Error(`场景接口返回 ${scenesRes.status}`)
    if (!termsRes.ok) throw new Error(`术语接口返回 ${termsRes.status}`)
    const scenesJson = await scenesRes.json()
    const termsJson = await termsRes.json()
    const topicsJson = topicsRes.ok ? await topicsRes.json() : { topics: [] }
    scenesMap.value = scenesJson.scenes || {}
    termDictionary.value = termsJson.terms || []
    topicList.value = topicsJson.topics || []
    const keys = Object.keys(scenesMap.value)
    if (keys.length > 0 && !scenesMap.value[activeScene.value]) {
      activeScene.value = keys[0]
    }
  } catch (e: any) {
    errorMsg.value = e.message || '加载失败'
    console.error('知识数据加载失败:', e)
  } finally {
    loading.value = false
  }
}

// ========== 知识图谱 ==========
const graphFilters = [
  { key: '业务场景', icon: 'folder', label: '场景' },
  { key: '业务对象', icon: 'package', label: '对象' },
  { key: '数据表', icon: 'database', label: '数据表' },
  { key: '数据字段', icon: 'hash', label: '字段' },
  { key: '业务指标', icon: 'bar-chart-2', label: '指标' },
  { key: '业务规则', icon: 'ruler', label: '规则' },
  { key: '分析主题', icon: 'target', label: '主题' },
]
const activeGraphFilters = ref<string[]>(['业务场景', '业务对象', '数据表', '数据字段', '业务指标', '业务规则', '分析主题'])
const selectedGraphNode = ref<any>(null)

const onGraphNodeClick = (id: string) => {
  const n = activeGraphNodes.value.find((x: any) => String(x.id) === String(id))
  // 打开「节点中文解释」弹框（口径见 data/knowledgeGraphZh.ts，来源于《数据介绍.md》）
  if (!n) { selectedGraphNode.value = null; return }
  selectedGraphNode.value = {
    id: String(n.id),
    label: n.label || n.name || String(n.id),
    entityType: n.nodeType || (n as any).entityType || '',
    rawDesc: n.desc || '',
  }
}

// ── 弹框内容：中文解释（优先取《数据介绍.md》内置口径，其次回退到实时表结构）
const graphNodeZh = computed<ZhEntry | null>(() => {
  const node = selectedGraphNode.value
  if (!node) return null
  const hit = findZhEntry(node.id) || findZhEntry(node.label)
  if (hit) return hit
  // 回退：物理表名节点（简单表图模式）用业务知识接口返回的实时表结构
  const ref = findObjRef(node.id)
  if (ref?.obj) {
    return {
      kind: '数据表' as const,
      title: ref.obj.label || ref.obj.table,
      table: ref.obj.table,
      rows: ref.obj.row_count,
      scene: ref.sceneName,
      desc: ref.obj.desc || `数据表 ${ref.obj.table}`,
      columns: (ref.obj.columns || []).map((c: any) => ({
        name: c.name,
        type: c.type || '',
        zh: c.translation || c.comment || '—',
        key: c.primary_key ? ('PK' as const) : undefined,
      })),
    }
  }
  return null
})

const graphNodeKindZh = computed(() =>
  graphNodeZh.value?.kind || entityTypeZh(selectedGraphNode.value?.entityType))

// 面板标题只显示节点名字：知识类型在下面那个蓝色徒标里已经有了，不再重复
const graphNodeModalTitle = computed(() =>
  graphNodeZh.value?.title || selectedGraphNode.value?.label || '节点')

// 弹框顶部的事实卡（数据表 / 行数 / 业务场景 / 单位）
const graphNodeFacts = computed(() => {
  const zh = graphNodeZh.value
  const out: Array<{ label: string; value: string; mono?: boolean }> = []
  if (!zh) return out
  if (zh.table) out.push({ label: '数据表', value: zh.table, mono: true })
  if (zh.rows != null) out.push({ label: '数据行数', value: `${Number(zh.rows).toLocaleString()} 行` })
  if (zh.scene) out.push({ label: '业务场景', value: zh.scene })
  if (zh.unit) out.push({ label: '计量单位', value: zh.unit })
  if (zh.columns?.length) out.push({ label: '字段数量', value: `${zh.columns.length} 个` })
  return out
})

// 该节点涉及的数据表（指标/字段类节点用于跳转场景知识）
// 该节点涉及的数据表。
// 「数据表」类节点本身就是一个表：facts 区已经展示了「数据表 <物理表名>」，
// 这里再渲染一遍会出现同一个表名连着出现两次（2026-10-03 实测截图确认），
// 对该节点没有增量信息 —— 只有「指标/字段/概念」类节点才需要列出它跨了哪些表。
const graphNodeTables = computed(() => {
  const zh = graphNodeZh.value
  if (!zh) return []
  if (zh.kind === '数据表') return []
  const list = zh.table ? [zh.table, ...(zh.tables || [])] : (zh.tables || [])
  return Array.from(new Set(list.filter(Boolean)))
})

// 该节点的关联关系（取图谱边，配中文描述，可点击跳转到对端节点）
const graphNodeRelations = computed(() => {
  const id = selectedGraphNode.value?.id
  if (!id) return []
  return activeGraphRels.value
    .filter((r: any) => String(r.source_table) === String(id) || String(r.target_table) === String(id))
    .map((r: any) => {
      const otherId = String(r.source_table) === String(id) ? r.target_table : r.source_table
      const other = activeGraphNodes.value.find((n: any) => String(n.id) === String(otherId))
      const otherZh = findZhEntry(otherId)
      return {
        otherId: String(otherId),
        otherLabel: otherZh?.title || other?.label || String(otherId),
        relType: r.type || '关联',
        out: String(r.source_table) === String(id),
        desc: r.description || '',
      }
    })
})
const graphFullscreen = ref(false)
const graphRef = ref<any>(null)
const graphFullscreenRef = ref<any>(null)
// 当前生效的图谱数据源：full = 六类知识图谱，simple = 原「表+外键」图
const activeGraphNodes = computed(() => graphMode.value === 'full' ? graphFullNodes.value : graphComponentNodes.value)
const activeGraphRels = computed(() => graphMode.value === 'full' ? graphFullRels.value : graphComponentRels.value)
const graphTableCount = computed(() => activeGraphNodes.value.filter((n: any) => n.nodeType === '业务对象' || n.nodeType === '数据表' || !n.nodeType).length || activeGraphNodes.value.length)
const graphRelCount = computed(() => activeGraphRels.value.length)
const openGraphFullscreen = () => {
  graphFullscreen.value = true
}
const toggleGraphFilter = (key: string) => {
  const idx = activeGraphFilters.value.indexOf(key)
  if (idx > -1) activeGraphFilters.value.splice(idx, 1)
  else activeGraphFilters.value.push(key)
}
const filteredGraphNodes = computed(() => {
  // LightRAG 模式下旧的「类型 / 场景」过滤器已从界面隐藏，不能再参与过滤：
  // LightRAG 实体的 entity_type 是 data / concept / UNKNOWN，不在下面 7 类业务词表内，
  // 继续过滤会让结果恒为空。
  if (lightRagActive.value) return activeGraphNodes.value
  const filters = activeGraphFilters.value
  if (!filters.length) return []
  return activeGraphNodes.value.filter((n) => {
    const t = n.nodeType || '业务对象'
    if (!filters.includes(t)) return false
    if (activeGraphScene.value && n.scene !== activeGraphScene.value) return false
    return true
  })
})

const SCENE_COLOR_MAP: Record<string, { background: string; border: string }> = {
  '生产': { background: '#eff6ff', border: '#3b82f6' },
  '质量': { background: '#fef2f2', border: '#ef4444' },
  '设备': { background: '#fffbeb', border: '#f59e0b' },
  '库存': { background: '#e8f3ff', border: '#1677ff' },
  '销售': { background: '#f5f3ff', border: '#8b5cf6' },
  '采购': { background: '#fff7ed', border: '#f97316' },
  '人事': { background: '#fce7f3', border: '#ec4899' },
  '财务': { background: '#f0fdfa', border: '#2563eb' },
  '基础数据': { background: '#f1f5f9', border: '#64748b' },
}
const SCENE_PALETTE_HEX: Array<[string, string]> = [
  ['#eff6ff', '#3b82f6'], ['#fef2f2', '#ef4444'], ['#fffbeb', '#f59e0b'], ['#e8f3ff', '#1677ff'],
  ['#f5f3ff', '#8b5cf6'], ['#fff7ed', '#f97316'], ['#fce7f3', '#ec4899'], ['#f0fdfa', '#2563eb'], ['#f1f5f9', '#64748b'],
]
const _hashScene = (scene: string): number => {
  let h = 0
  for (let i = 0; i < scene.length; i++) h = (h * 31 + scene.charCodeAt(i)) >>> 0
  return h
}
const _sceneColor = (scene: string): { background: string; border: string } =>
  SCENE_COLOR_MAP[scene] || (() => {
    const [bg, bd] = SCENE_PALETTE_HEX[_hashScene(scene) % SCENE_PALETTE_HEX.length]
    return { background: bg, border: bd }
  })()

const sceneLegend = computed(() => {
  const seen = new Set<string>()
  const legend: Record<string, { background: string; border: string }> = {}
  activeGraphNodes.value.forEach((n: any) => {
    const sc = n.scene || '其他'
    if (!seen.has(sc)) {
      seen.add(sc)
      legend[sc] = _sceneColor(sc)
    }
  })
  return legend
})

const resetGraphZoom = () => {
  selectedGraphNode.value = null
  const g = graphFullscreen.value ? graphFullscreenRef.value : graphRef.value
  g?.fitView?.()
}
const downloadGraphPng = () => {
  const g = graphFullscreen.value ? graphFullscreenRef.value : graphRef.value
  g?.downloadPng?.()
}

// ========== Graph component 数据准备 ==========
const graphComponentNodes = ref<any[]>([])
const graphComponentRels = ref<any[]>([])
// ── 完整知识图谱（六类节点：业务场景/业务对象/业务指标/业务规则/数据表/数据字段，
//    合并自 2026-09-04 版本）。simple = 原表+外键图（保留，零行为变化）；full = 六类图谱+ER 式表节点。
const graphFullNodes = ref<any[]>([])
const graphFullRels = ref<any[]>([])
const graphMode = ref<'simple' | 'full'>('full')
// LightRAG 知识图谱（public/knowledge_graph.json）是否生效：
// 生效时视图2 使用 LightRagGraph 组件（复刻 knowledge_graph.html），隐藏旧图谱的 模式/类型/场景 过滤
const lightRagActive = ref(false)
// 图谱数据是否已加载成功（用于工具栏提示语）。
// 语义上就是原来的 lightRagActive —— 名字里的 "lightRag" 是历史遗留：
// 该图谱的数据源 2026-10-03 起已改为实时表间关系接口 /api/tables/relationships，
// 不再读 LightRAG 导出的静态 knowledge_graph.json。
const graphLoaded = computed(() => lightRagActive.value || graphFullNodes.value.length > 0)

// 切换场景过滤 / 图谱模式后，视图自动适配当前可见子图（否则全图 fit 后字太小）
watch([activeGraphScene, graphMode], async () => {
  await nextTick()
  ;(graphFullscreen.value ? graphFullscreenRef.value : graphRef.value)?.fitView?.()
})

// 「节点中文解释」面板**展开**会改变图谱画布宽度：重新适配一次，
// 否则画布变小后视图还停在旧位置，边上节点会跑到画布外，看起来像「图谱少了/没了」。
//
// 只在「无 → 有」时 fit，**关闭时不再重置视图**（2026-10-03 修复）：
// 原实现监听 `!!selectedGraphNode` 的变化，开和关都会跑 fitView()。后果是
// 关掉面板后整张图重新 fit，所有节点位置突变 —— 刚关掉面板再点某个节点，
// 点的是旧坐标，命不中（实测连续点击时命中率极低）；而且物理模拟的节点
// 本来就有位移，每次开关都 fit 会让图谱「一跳一跳」，无法稳定点击。
watch(selectedGraphNode, async (v) => {
  if (!v) return            // 关闭时保持当前视图
  await nextTick()
  // 等两帧：面板展开 → 浏览器布局 → LightRagGraph 的 ResizeObserver 同步完画布尺寸，再 fit
  requestAnimationFrame(() => requestAnimationFrame(() => {
    ;(graphFullscreen.value ? graphFullscreenRef.value : graphRef.value)?.fitView?.()
  }))
})
// /graph 原始返回缓存：完整模式构建时复用（表节点元信息 + 外键边），避免二次请求
const rawGraphNodesById = ref<Map<string, any>>(new Map())
const rawTableEdges = ref<any[]>([])

function classifyFieldType(colName: string, colType: string): string {
  const name = colName.toLowerCase()
  const type = colType.toLowerCase()
  const metricKeys = ['rate', 'count', 'amount', 'qty', 'quantity', 'duration', 'price', 'cost', 'value', 'weight', 'percent', 'ratio', 'score', 'yield', 'output', 'total', 'sum', 'avg', 'max', 'min', 'temperature', 'speed', 'pressure', 'volume', 'length']
  const ruleKeys = ['status', 'type', 'level', 'flag', 'state', 'result', 'grade', 'category', 'class', 'stage', 'phase', 'mode', 'reason', 'check', 'pass', 'fail', 'qualified']
  const isNumeric = ['int', 'float', 'numeric', 'decimal', 'double', 'real'].some(t => type.includes(t))
  if (isNumeric && metricKeys.some(k => name.includes(k))) return '业务指标'
  if (ruleKeys.some(k => name.includes(k))) return '业务规则'
  if (name.endsWith('_id') || name.endsWith('_key')) return '业务对象'
  if (isNumeric) return '业务指标'
  return '业务对象'
}

function classifyTableType(tableName: string): string {
  const name = (tableName || '').split('.').pop()?.toLowerCase() || ''
  if (name.startsWith('dim_') || /(info|department|workshop|line|employee|product|material|supplier|customer|factory)$/.test(name)) return '业务对象'
  if (/(process_output|production_record|downtime|output|yield|attendance|work_order)/.test(name)) return '业务指标'
  if (/(inspection|defect|inventory|quality|maintenance|order_item)/.test(name)) return '业务规则'
  return '业务对象'
}

// 核心：从 scenesMap + /graph 表元信息 + 外键边 构建 6 类节点 + 6 类关系（合并自 2026-09-04 版本）
// 节点：业务场景 / 业务对象 / 业务指标 / 业务规则 / 数据表 / 数据字段（仅 PK+FK，控制密度）
// 关系：scene_object（场景→对象）、object_table（对象→表）、object_metric（对象→指标）、
//       rule_metric（规则→指标）、table_field（表→字段）、foreign_key（表→表）
// 数据全部来自现有接口（/knowledge/scenes + /knowledge/graph + /knowledge/relations），后端零改动
const buildKnowledgeGraphFull = (graphMeta: Map<string, any>, tableEdges: any[]) => {
  const nodes: any[] = []
  const rels: any[] = []
  const nodeIds = new Set<string>()
  const pushNode = (node: any) => { if (!nodeIds.has(node.id)) { nodeIds.add(node.id); nodes.push(node) } }
  const pushRel = (source_table: string, target_table: string, type: string, description = '', extra: any = {}) => {
    if (rels.some((r) => r.source_table === source_table && r.target_table === target_table && r.type === type)) return
    rels.push({ source_table, target_table, source_column: '', target_column: '', type, description, ...extra })
  }

  Object.entries(scenesMap.value).forEach(([sceneKey, s]: [string, any]) => {
    const sceneName = s?.name || sceneKey
    const sceneId = `scene::${sceneKey}`
    // 1) 业务场景节点
    pushNode({ id: sceneId, name: sceneName, label: sceneName, nodeType: '业务场景', scene: sceneName, desc: s?.desc || `${sceneName} 业务场景`, columns: 0, rowCount: 0 })

    ;(s?.objects || []).forEach((o: any) => {
      const table = o.table
      const objLabel = o.label || o.chinese_name || table
      // 2) 业务对象节点（业务概念）
      pushNode({ id: `obj::${table}`, name: objLabel, label: objLabel, nodeType: '业务对象', scene: sceneName, desc: o.desc || o.description || `${objLabel}（业务对象）`, columns: 0, rowCount: 0 })
      pushRel(sceneId, `obj::${table}`, 'scene_object', `${sceneName} 包含 ${objLabel}`)

      // 3) 数据表节点（columnsFull/pkColumns 供 KnowledgeGraph 做 ER 式渲染）
      const meta = graphMeta.get(table) || {}
      const cols = o.columns || []
      pushNode({
        id: table, name: table, label: o.label || meta.label || table,
        nodeType: '数据表', scene: sceneName,
        columns: cols.length, rowCount: o.row_count ?? meta.rowCount ?? 0,
        desc: o.desc || o.description || meta.desc || '',
        icon: o.icon,
        subFields: cols.slice(0, 8).map((c: any) => ({ name: c.name, type: c.type, ktype: classifyFieldType(c.name, c.type) })),
        columnsFull: cols,
        pkColumns: cols.filter((c: any) => c.primary_key).map((c: any) => c.name),
      })
      pushRel(`obj::${table}`, table, 'object_table', `${objLabel} 映射 ${table}`)

      // 4) 数据字段节点（仅主键 + 外键字段，控制密度）
      const fkCols = tableEdges.filter((e: any) => e.source === table).map((e: any) => e.sourceColumn)
      const pkCols = cols.filter((c: any) => c.primary_key).map((c: any) => c.name)
      Array.from(new Set([...pkCols, ...fkCols])).slice(0, 8).forEach((colName: any) => {
        const col = cols.find((c: any) => c.name === colName)
        const fid = `field::${table}.${colName}`
        pushNode({
          id: fid, name: colName, label: colName,
          nodeType: '数据字段', scene: sceneName, table,
          desc: col?.comment || col?.translation || `${table}.${colName}（${col?.type || ''}）`,
          columns: 0, rowCount: 0,
        })
        pushRel(table, fid, 'table_field', `${table} 包含 ${colName}`)
      })
    })

    // 5) 业务指标节点 + 对象→指标
    ;(s?.metrics || []).forEach((m: any) => {
      const mid = `metric::${sceneKey}::${m.name}`
      pushNode({ id: mid, name: m.name, label: m.name, nodeType: '业务指标', scene: sceneName, desc: m.description || m.formula || '', metric: m, columns: 0, rowCount: 0 })
      ;(m.tables || []).forEach((tb: string) => {
        const obj = (s?.objects || []).find((o: any) => o.table === tb)
        if (obj) pushRel(`obj::${tb}`, mid, 'object_metric', `${m.name} 由 ${obj.label || tb} 计算`)
      })
    })

    // 6) 业务规则节点 + 规则→指标
    ;(s?.rules || []).forEach((r: any) => {
      const rid = `rule::${sceneKey}::${r.name}`
      pushNode({ id: rid, name: r.name, label: r.name, nodeType: '业务规则', scene: sceneName, desc: r.condition || '', rule: r, columns: 0, rowCount: 0 })
      ;(s?.metrics || []).forEach((m: any) => {
        if (r.name.includes(m.name) || m.name.includes(r.name)) {
          pushRel(rid, `metric::${sceneKey}::${m.name}`, 'rule_metric', `${r.name} 关联 ${m.name}`)
        }
      })
    })
  })

  // 7) 数据表 → 数据表（外键关联）
  tableEdges.forEach((e: any) => {
    pushRel(e.source, e.target, 'foreign_key', e.description || `${e.source}.${e.sourceColumn} → ${e.target}.${e.targetColumn}`, {
      source_column: e.sourceColumn || '', target_column: e.targetColumn || '',
    })
  })

  graphFullNodes.value = nodes
  graphFullRels.value = rels
}

// 无物理外键的库（实测 yans：10 表 / 物理外键 0 条）→ 按命名约定推断表间关系：
// A 表的 xxx_id 列 → xxx / dim_xxx / mes_xxx 等表（与后端编译器的保守 JOIN 推断同思路）
const inferForeignKeyEdges = (tablesInfo: Map<string, any>) => {
  const edges: any[] = []
  const names = [...tablesInfo.keys()]
  const bare = (t: string) => String(t).split('.').pop() || String(t)
  const strip = (n: string) => String(n).replace(/^(dim_|fact_|mes_|ods_|tb_|t_)/, '')
  for (const [t, info] of tablesInfo) {
    const cols = info.columns || []
    const targets = new Set<string>()
    for (const c of cols) {
      if (!/_id$/.test(String(c.name))) continue
      const base = String(c.name).replace(/_id$/, '')
      if (!base) continue
      const target = names.find((tn) => {
        if (tn === t) return false
        const b = bare(tn)
        return b === base || strip(b) === strip(base) || b.endsWith('_' + base)
      })
      if (target && !targets.has(target)) {
        targets.add(target)
        const pk = (tablesInfo.get(target)?.columns || []).find((x: any) => x.primary_key)
        edges.push({
          source: t, target,
          sourceColumn: String(c.name),
          targetColumn: pk?.name || 'id',
          type: 'foreign_key',
          description: `${bare(t)}.${c.name} → ${bare(target)}（命名约定推断）`,
        })
      }
    }
  }
  return edges
}
// 边去重（物理外键优先，推断边补位）：同 source+target+sourceColumn 只保留一条
const dedupeEdges = (edges: any[]) => {
  const seen = new Set<string>()
  return edges.filter((e) => {
    const k = `${e.source}|${e.target}|${e.sourceColumn || ''}`
    if (seen.has(k)) return false
    seen.add(k)
    return true
  })
}
const buildTablesInfoFromScenes = () => {
  const tablesInfo = new Map<string, any>()
  Object.values(scenesMap.value).forEach((s: any) => {
    ;(s?.objects || []).forEach((o: any) => {
      if (o.table && (o.columns || []).length && !tablesInfo.has(o.table)) tablesInfo.set(o.table, o)
    })
  })
  return tablesInfo
}

const loadRelations = async () => {
  try {
    // ========== 图谱主数据源：实时表间关系接口（2026-10-03）=========
    // 必须与 LightRagGraph 组件画布**同源**：画布从 /api/tables/relationships 取
    // 节点（id = 物理表名，如 dim_product / mes_process_output），点击时按 id 回查
    // activeGraphNodes。若这里仍读 public/knowledge_graph.json（id 是「产品表」
    // 这类中文名，44 个），两组 id 完全不同 → onGraphNodeClick 的 find() 必然
    // 返回 undefined → 点了打不开详情（2026-10-03 修复的正是这个不一致）。
    const relRes = await fetch(`${API_BASE}/tables/relationships`)
    if (relRes.ok) {
      const relJson = await relRes.json()
      const rawNodes = relJson.nodes || []
      const rawRels = relJson.relationships || []
      if (rawNodes.length > 0) {
        // 节点结构与后端 /tables/relationships 一致；补上页面侧用到的字段别名
        const nodes = rawNodes.map((n: any) => ({
          ...n,
          name: n.name || n.id,
          label: n.label || n.name || n.id,
          nodeType: n.nodeType || '业务对象',
          desc: n.description || '',
        }))
        // 边统一成页面侧的关系结构（source_table/target_table）
        const edges = rawRels.map((e: any) => ({
          source_table: e.source_table,
          target_table: e.target_table,
          source_column: e.source_column || '',
          target_column: e.target_column || '',
          type: e.type || 'business',
          description: e.description || '',
        }))

        graphComponentNodes.value = nodes
        graphComponentRels.value = edges
        graphFullNodes.value = nodes
        graphFullRels.value = edges
        lightRagActive.value = true
        // 缓存表节点元信息，供节点详情面板复用（避免二次请求）
        rawGraphNodesById.value = new Map(nodes.map((n: any) => [String(n.id), n]))
        rawTableEdges.value = edges

        console.log(`✅ 已加载实时表间关系图谱: ${nodes.length} 个表节点, ${edges.length} 条关系`)
        return
      }
    }
    lightRagActive.value = false

    // ========== 原有逻辑（回退方案） ==========
    const tableSceneMap = new Map<string, string>()
    Object.entries(scenesMap.value).forEach(([sceneKey, s]: [string, any]) => {
      const sceneName = s?.name || sceneKey
      ;(s?.objects || []).forEach((o: any) => tableSceneMap.set(o.table, sceneName))
    })
    const enrichScene = (nodes: any[]) =>
      (nodes || []).map((n: any) => ({
        ...n,
        scene: n.scene || tableSceneMap.get(n.id) || tableSceneMap.get(n.name) || '其他',
      }))

    const graphRes = await fetch(`${API_BASE}/knowledge/graph`)
    if (graphRes.ok) {
      const graphJson = await graphRes.json()
      const graphNodes = graphJson.nodes || []
      const graphEdges = graphJson.edges || []
      const nodeMap = new Map<string, any>()
      enrichScene(graphNodes).forEach((n: any) => nodeMap.set(n.id, n))
      Object.entries(scenesMap.value).forEach(([sceneKey, s]: [string, any]) => {
        const sceneName = s?.name || sceneKey
        ;(s?.objects || []).forEach((o: any) => {
          if (!nodeMap.has(o.table)) {
            nodeMap.set(o.table, {
              id: o.table,
              name: o.table,
              label: o.label || o.table,
              columns: (o.columns || []).length,
              connected: graphEdges.some((e: any) => e.source === o.table || e.target === o.table),
              nodeType: '业务对象',
              scene: sceneName,
              subFields: (o.columns || []).slice(0, 8).map((c: any) => ({
                name: c.name, type: c.type, ktype: classifyFieldType(c.name, c.type),
              })),
              rowCount: o.row_count,
              icon: o.icon,
            })
          } else {
            const n = nodeMap.get(o.table)
            if (!n.scene) n.scene = sceneName
          }
        })
      })
      graphComponentNodes.value = Array.from(nodeMap.values())
      // 合并「命名约定推断边」（物理外键缺失的库靠它补连线），去重后供两种模式共用
      const mergedEdges = dedupeEdges([...graphEdges, ...inferForeignKeyEdges(buildTablesInfoFromScenes())])
      graphComponentRels.value = mergedEdges.map((e: any) => ({
        source_table: e.source,
        source_column: e.sourceColumn,
        target_table: e.target,
        target_column: e.targetColumn,
        type: e.type,
        description: e.description,
      }))
      // 完整知识图谱（六类节点）构建：缓存 /graph 原始数据供构建复用
      const metaMap = new Map<string, any>()
      ;(graphJson.nodes || []).forEach((n: any) => metaMap.set(n.id, n))
      rawGraphNodesById.value = metaMap
      rawTableEdges.value = mergedEdges
      buildKnowledgeGraphFull(metaMap, mergedEdges)
      return
    }

    const res = await fetch(`${API_BASE}/knowledge/relations`)
    if (!res.ok) throw new Error(`关系接口返回 ${res.status}`)
    const json = await res.json()
    const rels = json.relations || []

    const nodeMap = new Map<string, any>()
    Object.entries(scenesMap.value).forEach(([sceneKey, s]: [string, any]) => {
      const sceneName = s?.name || sceneKey
      ;(s.objects || []).forEach((o: any) => {
        const subFields = (o.columns || []).map((c: any) => ({
          name: c.name,
          type: c.type,
          ktype: classifyFieldType(c.name, c.type),
        }))
        nodeMap.set(o.table, {
          id: o.table,
          name: o.table,
          label: o.label || o.table,
          columns: (o.columns || []).length,
          connected: false,
          nodeType: classifyTableType(o.table),
          scene: sceneName,
          subFields,
          rowCount: o.row_count,
          icon: o.icon,
        })
      })
    })

    rels.forEach((r: any) => {
      if (nodeMap.has(r.source_table)) nodeMap.get(r.source_table).connected = true
      if (nodeMap.has(r.target_table)) nodeMap.get(r.target_table).connected = true
      if (!nodeMap.has(r.source_table)) nodeMap.set(r.source_table, { id: r.source_table, name: r.source_table, label: r.source_table, columns: 0, connected: true, nodeType: classifyTableType(r.source_table), subFields: [], rowCount: 0 })
      if (!nodeMap.has(r.target_table)) nodeMap.set(r.target_table, { id: r.target_table, name: r.target_table, label: r.target_table, columns: 0, connected: true, nodeType: classifyTableType(r.target_table), subFields: [], rowCount: 0 })
      graphComponentRels.value.push({
        source_table: r.source_table,
        source_column: r.source_column,
        target_table: r.target_table,
        target_column: r.target_column,
        type: r.type,
        description: r.description,
      })
    })
    graphComponentNodes.value = Array.from(nodeMap.values())
    // 兜底路径同样补推断边到简洁模式（语义边已在上方 push，按 source+target+column 去重）
    const inferredSimple = inferForeignKeyEdges(buildTablesInfoFromScenes()).map((e: any) => ({
      source_table: e.source, source_column: e.sourceColumn,
      target_table: e.target, target_column: e.targetColumn,
      type: e.type, description: e.description,
    }))
    const seenEdge = new Set(graphComponentRels.value.map((x: any) => `${x.source_table}|${x.target_table}|${x.source_column || ''}`))
    graphComponentRels.value.push(...inferredSimple.filter((x: any) => !seenEdge.has(`${x.source_table}|${x.target_table}|${x.source_column || ''}`)))
    // 兜底路径（/relations）：外键边转 rawTableEdges，完整图谱照常构建（graphMeta 用空 Map，
    // 表元信息由 scenesMap 的 objects 提供，功能不受影响）；同样补推断边
    const fallbackEdges = dedupeEdges([
      ...rels.map((r: any) => ({
        source: r.source_table, target: r.target_table,
        sourceColumn: r.source_column, targetColumn: r.target_column,
        type: r.type, description: r.description,
      })),
      ...inferForeignKeyEdges(buildTablesInfoFromScenes()),
    ])
    rawGraphNodesById.value = new Map()
    rawTableEdges.value = fallbackEdges
    buildKnowledgeGraphFull(new Map(), fallbackEdges)
  } catch (e: any) {
    console.error('加载图谱数据失败', e)
  }
}

// ========== 术语词典 ==========
const termSearch = ref('')
const termDictionary = ref<any[]>([])
const termCategoryFilter = ref('')
const termTypeFilter = ref('')
const knowledgeTypes = ['业务对象', '业务指标', '业务规则', '分析主题']
const typeIcons: Record<string, string> = {
  '业务对象': 'package',
  '业务指标': 'bar-chart-2',
  '业务规则': 'ruler',
  '分析主题': 'target',
}

const termCategories = computed(() => {
  const categories = new Set<string>()
  termDictionary.value.forEach((term: any) => {
    if (term.category) categories.add(term.category)
  })
  return Array.from(categories).sort()
})

const CN_EN_SEARCH_MAP: Record<string, string[]> = {
  '质量': ['quality', 'defect', 'inspection', 'yield', '合格', '不良', '检验'],
  '生产': ['production', 'process', 'output', 'manufacturing', 'work_order', 'mes'],
  '设备': ['equipment', 'machine', 'downtime', 'maintenance', 'uptime', 'eqp'],
  '库存': ['inventory', 'stock', 'warehouse', 'safety', 'material', 'inv'],
  '工序': ['process', 'step', 'stage'],
  '良率': ['yield', 'rate', 'qualified'],
  '缺陷': ['defect', 'fault', 'reject', 'bad'],
  '停机': ['downtime', 'stop', 'halt'],
  '安全': ['safety', 'secure'],
  '检验': ['inspection', 'check', 'test', 'qc'],
  '物料': ['material', 'item'],
  '工单': ['work_order', 'order'],
  '产品': ['product', 'item'],
  '数据': ['data', 'record'],
}

const filteredTerms = computed(() => {
  const q = termSearch.value.trim()
  let result = termDictionary.value
  if (q) {
    const qLower = q.toLowerCase()
    const expandWords = new Set<string>([qLower])
    for (const [cn, enList] of Object.entries(CN_EN_SEARCH_MAP)) {
      if (qLower.includes(cn) || cn.includes(qLower)) {
        enList.forEach(w => expandWords.add(w.toLowerCase()))
      }
    }
    result = result.filter((t: any) => {
      const haystack = [
        t.term || '', t.term_cn || '', t.en || '', t.definition || '',
        t.category || '', t.abbreviation || '', t.knowledge_type || '',
      ].join(' ').toLowerCase()
      return Array.from(expandWords).some(w => haystack.includes(w))
    })
  }
  if (termCategoryFilter.value) {
    result = result.filter((t: any) => t.category === termCategoryFilter.value)
  }
  if (termTypeFilter.value) {
    result = result.filter((t: any) => t.knowledge_type === termTypeFilter.value)
  }
  return result
})

const groupedTerms = computed(() => {
  const order = ['业务对象', '业务指标', '业务规则', '分析主题']
  const groups: Record<string, any[]> = {}
  for (const kt of order) groups[kt] = []
  filteredTerms.value.forEach((term: any) => {
    const kt = term.knowledge_type || '业务对象'
    if (groups[kt]) groups[kt].push(term)
    else {
      if (!groups['其他']) groups['其他'] = []
      groups['其他'].push(term)
    }
  })
  const result = order
    .filter(kt => groups[kt].length > 0)
    .map(kt => ({ type: kt, items: groups[kt] }))
  if (groups['其他'] && groups['其他'].length > 0) {
    result.push({ type: '其他', items: groups['其他'] })
  }
  return result
})

const escHtml = (s: any): string =>
  String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c] as string))

const detailModal = ref<any>(null)

const openTermDetail = (term: any) => {
  const kt = term.knowledge_type || '业务对象'
  const explainMap: Record<string, string> = {
    '业务对象': '业务对象是数据建模中的核心实体，如产品、工序、设备等。它们通过外键相互关联，构成数据分析的基础维度。',
    '业务指标': '业务指标是可量化、可度量的数值，如良率、产量、停机时长等。它们是 KPI 看板和分析报表的核心数据。',
    '业务规则': '业务规则定义了判断逻辑和分类标准，如状态、等级、类型等。它们用于数据筛选、条件判断和异常识别。',
    '分析主题': '分析主题是面向具体业务问题的知识集合，围绕某一分析目标组织相关的对象、指标和规则。',
  }
  const e = escHtml
  const tTerm = e(term.term_cn || term.term || '-')
  const tKt = e(kt)
  const tEn = e(term.en || '-')
  const tAbbr = e(term.abbreviation || '-')
  const tDef = e(term.definition || '暂无详细定义')
  const tCat = e(term.category || '-')
  const tType = e(term.data_type || '-')
  const tTable = e(term.table_cn || term.mapped_table || '-')
  const tField = e(term.mapped_field || '')
  detailModal.value = {
    title: tTerm,
    content: `
      <div class="space-y-4 text-sm">
        <div class="grid grid-cols-3 gap-3">
          <div class="bg-indigo-50 rounded-xl p-3 text-center">
            <div class="text-xs text-indigo-500 mb-1">知识类型</div>
            <div class="font-semibold text-indigo-700">${tKt}</div>
          </div>
          <div class="bg-blue-50 rounded-xl p-3 text-center">
            <div class="text-xs text-blue-500 mb-1">英文名称</div>
            <div class="font-mono font-semibold text-blue-700">${tEn}</div>
          </div>
          <div class="bg-orange-50 rounded-xl p-3 text-center">
            <div class="text-xs text-orange-500 mb-1">简称</div>
            <div class="font-mono font-semibold text-orange-700">${tAbbr}</div>
          </div>
        </div>
        <div class="bg-amber-50 rounded-xl p-3 text-xs text-amber-700 leading-relaxed">
          <strong>${tKt}说明：</strong>${e(explainMap[kt] || '')}
        </div>
        <div class="bg-gray-50 rounded-xl p-4">
          <div class="text-xs text-gray-400 mb-1">术语定义</div>
          <div class="text-gray-700 leading-relaxed">${tDef}</div>
        </div>
        <div class="grid grid-cols-3 gap-3 text-xs">
          <div><span class="text-gray-400">业务分类：</span><span class="font-medium text-gray-700">${tCat}</span></div>
          <div><span class="text-gray-400">数据类型：</span><span class="font-mono text-blue-600">${tType}</span></div>
          <div><span class="text-gray-400">来源表：</span><span class="font-mono text-blue-600">${tTable}</span></div>
        </div>
        ${tField ? `<div class="text-xs"><span class="text-gray-400">映射字段：</span><span class="font-mono text-blue-600">${tField}</span></div>` : ''}
        <div class="mt-2 text-xs text-gray-400">在智能问析中使用以上术语可以更准确地生成 SQL 查询和数据分析结果</div>
      </div>
    `,
  }
}

// ========== 分析模板（后端接口化，失败回退内置） ==========
const templateSearch = ref('')
const templateSceneFilter = ref('')

const FALLBACK_TEMPLATES: any[] = [
  { id: 'tpl_001', name: '周质量分析报告', scene: '质量分析', desc: '自动生成本周质量概况，包括合格率趋势、不良TOP5、各工序质量表现', tags: ['质量', '周报', '自动报告'], example_question: '请生成一份本周质量分析报告', metrics_count: 5, tables_count: 3 },
  { id: 'tpl_002', name: '设备停机分析', scene: '设备分析', desc: '分析非计划停机原因、设备运行率、停机时长排行，定位设备改进点', tags: ['设备', '停机', '效率'], example_question: '分析设备停机时间和不良率是否相关', metrics_count: 4, tables_count: 2 },
  { id: 'tpl_003', name: '工序良率监控', scene: '生产分析', desc: '追踪各工序良率趋势，自动预警良率下降的工序，并提供异常归因', tags: ['生产', '良率', '监控'], example_question: '请分析各工序的良率，找出良率下降的工序', metrics_count: 3, tables_count: 2 },
  { id: 'tpl_004', name: '库存预警看板', scene: '库存分析', desc: '监控库存水位，自动预警低库存和高库存物料，生成补货建议', tags: ['库存', '预警', '补货'], example_question: '找出库存低于安全线的产品，生成补货清单', metrics_count: 3, tables_count: 2 },
  { id: 'tpl_005', name: '生产周报', scene: '周期报告', desc: '一次汇总近 7 天的产量、良率、质量与设备表现，输出可直接汇报的周报', tags: ['周报', '周期报告', '自动报告'], example_question: '近7天的产量和良率表现怎么样', metrics_count: 6, tables_count: 5 },
  { id: 'tpl_006', name: '生产月报', scene: '周期报告', desc: '汇总近 30 天的产量、良率、质量与设备整体表现，输出月度经营概览', tags: ['月报', '周期报告', '自动报告'], example_question: '近30天的产量和良率整体表现如何', metrics_count: 6, tables_count: 5 },
]

// 模板图标：不渲染后端 emoji 字段，按场景/名称映射固定 Lucide 图标
const templateIcon = (tpl: any): string => sceneIcon(`${tpl?.scene || ''}${tpl?.name || ''}`)

const knowledgeTemplates = ref<any[]>([...FALLBACK_TEMPLATES])
// 推荐摘要（后端按当前用户授权表范围+使用习惯计算）：null = 后端不可用/降级中
const templateSummary = ref<any>(null)

const templateScenes = computed(() => {
  const scenes = new Set<string>()
  knowledgeTemplates.value.forEach((tpl: any) => {
    if (tpl.scene) scenes.add(tpl.scene)
  })
  return Array.from(scenes).sort()
})

const filteredTemplates = computed(() => {
  const keyword = templateSearch.value.trim().toLowerCase()
  return knowledgeTemplates.value.filter((tpl: any) => {
    const matchesSearch = !keyword ||
      tpl.name.toLowerCase().includes(keyword) ||
      tpl.scene.toLowerCase().includes(keyword) ||
      tpl.desc.toLowerCase().includes(keyword) ||
      (tpl.tags || []).some((tag: string) => tag.toLowerCase().includes(keyword))
    const matchesScene = !templateSceneFilter.value || tpl.scene === templateSceneFilter.value
    return matchesSearch && matchesScene
  })
})

const applyTemplate = (template: any) => {
  // 记录使用次数
  try { fetch('/api/knowledge/template-use', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ template_id: template.id }) }).catch(() => {}) } catch {}
  emit('navigate-ask', template.example_question)
}

// ── 一键生成报告：模板 questions[] → 后端确定性编译执行 → 单文件 HTML 报告 ──
const runningTplId = ref('')
const runTemplate = async (template: any) => {
  if (runningTplId.value) return
  runningTplId.value = template.id
  const win = window.open('', '_blank')
  if (win) {
    // 模板名来自后端，但这里是字符串拼 HTML —— 名字里出现 < 或 " 就会破坏页面结构。
    // 同一个文件里已有 escHtml（原先只被死代码 openTermDetail 用着），这里复用。
    const safeName = escHtml(template.name || '分析报告')
    win.document.write(
      '<html><head><meta charset="UTF-8"><title>' + safeName + '</title></head>' +
      '<body style="font-family:sans-serif;display:flex;align-items:center;justify-content:center;' +
      'height:100vh;color:#6b7280;font-size:15px;flex-direction:column;gap:10px">' +
      '<div style="font-size:20px;color:#1d2129">正在生成「' + safeName + '」…</div>' +
      '<div style="font-size:13px;color:#9ca3af">按模板步骤确定性编译执行（约 1-3 秒）</div></body></html>'
    )
  }
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), 60000)
  try {
    const resp = await fetch(`${API_BASE}/knowledge/templates/${template.id}/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: ctrl.signal,
    })
    if (!resp.ok) {
      const text = await resp.text().catch(() => '')
      throw new Error(text.slice(0, 120) || `生成失败(${resp.status})`)
    }
    const data = await resp.json()
    if (!data.success || !data.html) throw new Error(data.detail || data.error || '报告生成失败')
    if (win && !win.closed) {
      win.document.open()
      win.document.write(data.html)
      win.document.close()
    } else {
      // 弹窗被拦截 → 下载 HTML 文件
      const blob = new Blob([data.html], { type: 'text/html;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${template.name || '分析报告'}.html`
      document.body.appendChild(a)
      a.click()
      a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 30000)
    }
  } catch (e: any) {
    const msg = e?.name === 'AbortError' ? '生成超时（超过 60 秒），请稍后重试' : (e?.message || '生成失败')
    alert(msg)
    if (win && !win.closed) { try { win.close() } catch { /* ignore */ } }
  } finally {
    clearTimeout(timer)
    runningTplId.value = ''
  }
}

const loadTemplates = async () => {
  try {
    const res = await fetch(`${API_BASE}/knowledge/templates`)
    if (!res.ok) return
    const json = await res.json()
    // 用 Array.isArray 而非 `.length`：后端对**受限用户**会裁掉无权模板并返回
    // 空数组，此时原写法整段跳过 → knowledgeTemplates 仍是初始的 6 个内置模板
    //（还带伪造的 metrics_count/tables_count）→ 用户看到并执行自己无权访问的
    // 模板，点「一键生成报告」后才被后端 404 拦下。
    if (Array.isArray(json.templates)) {
      knowledgeTemplates.value = json.templates
      templateSummary.value = json.summary || null
    }
  } catch (e) {
    console.error('模板加载失败，使用内置模板', e)
  }
}

// ========== 右侧业务域面板 ==========
const PANEL_COLLAPSED_KEY = 'ops.kb.panel.collapsed'
const panelCollapsed = ref(false)
try {
  panelCollapsed.value = window.localStorage.getItem(PANEL_COLLAPSED_KEY) === '1'
} catch { /* ignore */ }
function togglePanel() {
  panelCollapsed.value = !panelCollapsed.value
  try { window.localStorage.setItem(PANEL_COLLAPSED_KEY, panelCollapsed.value ? '1' : '0') } catch { /* ignore */ }
}

const domainSearch = ref('')
const expandedCats = ref<Set<string>>(new Set(['object', 'metric', 'rule', 'topic']))
const toggleDomainCat = (key: string) => {
  const next = new Set(expandedCats.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expandedCats.value = next
}

interface DomainNode {
  id: string
  label: string
  sub: string
  sceneKey: string
  sceneName: string
  kind: DetailKind
  payload: any
}

const domainTree = computed(() => {
  const search = domainSearch.value.trim().toLowerCase()
  const cats: Array<{ key: string; icon: string; label: string; children: DomainNode[] }> = [
    { key: 'object', icon: 'package', label: '业务对象', children: [] },
    { key: 'metric', icon: 'bar-chart-2', label: '业务指标', children: [] },
    { key: 'rule', icon: 'ruler', label: '业务规则', children: [] },
    { key: 'topic', icon: 'target', label: '分析主题', children: [] },
  ]
  Object.entries(scenesMap.value).forEach(([sceneKey, s]: [string, any]) => {
    const sceneName = s?.name || sceneKey
    ;(s?.objects || []).forEach((o: any) => {
      cats[0].children.push({ id: `obj:${o.table}`, label: o.label || o.table, sub: o.table, sceneKey, sceneName, kind: '业务对象', payload: o })
    })
    ;(s?.metrics || []).forEach((m: any) => {
      cats[1].children.push({ id: `met:${m.name}`, label: m.name, sub: m.formula || '', sceneKey, sceneName, kind: '业务指标', payload: m })
    })
    ;(s?.rules || []).forEach((r: any) => {
      cats[2].children.push({ id: `rule:${r.name}`, label: r.name, sub: r.condition || '', sceneKey, sceneName, kind: '业务规则', payload: r })
    })
    ;(s?.topics || []).forEach((t: any) => {
      cats[3].children.push({ id: `topic:${t.name}`, label: t.name, sub: t.desc || '', sceneKey, sceneName, kind: '分析主题', payload: t })
    })
  })
  if (search) {
    cats.forEach(c => {
      c.children = c.children.filter(n =>
        (n.label + ' ' + n.sub + ' ' + n.sceneName).toLowerCase().includes(search))
    })
  }
  return cats.filter(c => c.children.length)
})

const isActiveDomain = (node: DomainNode) => selectedDetail.value?.id === node.id

const selectDomainNode = (node: DomainNode) => {
  // 只弹解释框，不切视图 / 不切场景（页面不跳转）；知识所属场景记录在 selectedDetail.sceneKey 里
  switch (node.kind) {
    case '业务对象': openObjectDetail(node.payload, node.sceneKey); break
    case '业务指标': openMetricDetail(node.payload, node.sceneKey); break
    case '业务规则': openRuleDetail(node.payload, node.sceneKey); break
    case '分析主题': openTopicDetail(node.payload, node.sceneKey); break
  }
}

// 详情面板打开时同步迷你关系图焦点
watch(() => selectedDetail.value?.table, (table) => {
  // 原为 `else if (!miniTarget.value)` —— 只在本就为空时赋值，等于永不生效，
  // 从对象详情切到指标/规则详情后 miniTarget 仍留着上一个对象。
  miniTarget.value = table || ''
})

// ========== 全局搜索 ==========
const globalSearchQ = ref('')
const globalSearchResults = ref<any[]>([])
const globalSearchFocus = ref(false)
const globalSearchTimer = ref<any>(null)
const globalBlurTimer = ref<any>(null)
const runGlobalSearch = () => {
  const q = globalSearchQ.value.trim().toLowerCase()
  globalSearchResults.value = []
  if (!q) return
  const out: any[] = []
  for (const [sk, s] of Object.entries(scenesMap.value) as any[]) {
    const sceneName = s?.name || sk
    for (const o of (s?.objects || [])) {
      // 后端 _build_scene_object 返回的是 desc，不是 description（后者是 metrics 的字段），
      // 原写法读到 undefined → 对象描述文本恒为空，搜描述里的词永远搜不到
      const blob = `${o.label || ''} ${o.table || ''} ${o.desc || ''}`.toLowerCase()
      if (blob.includes(q)) out.push({ key: `object:${o.table || o.label}`, type: '业务对象', title: o.label || o.table, subtitle: `数据表 · ${sceneName}`, scene: sk, icon: 'table' })
    }
    for (const m of (s?.metrics || [])) {
      const blob = `${m.name || ''} ${m.description || ''} ${m.unit || ''}`.toLowerCase()
      if (blob.includes(q)) out.push({ key: `metric:${m.name}`, type: '业务指标', title: m.name, subtitle: m.unit ? `指标 · ${m.unit}` : '业务指标', scene: sk, icon: 'bar-chart-2' })
    }
    for (const r of (s?.rules || [])) {
      const blob = `${r.name || ''} ${r.condition || ''}`.toLowerCase()
      if (blob.includes(q)) out.push({ key: `rule:${r.name}`, type: '业务规则', title: r.name, subtitle: '业务规则', scene: sk, icon: 'ruler' })
    }
  }
  for (const t of termDictionary.value) {
    const name = t.term_cn || t.term || t.name || ''
    const blob = `${name} ${t.term || ''} ${t.definition || ''}`.toLowerCase()
    // key 用 term（英文稳定主键）而非 term_cn：中文名可能重复，且后端 find 按 term 匹配
    if (blob.includes(q)) out.push({ key: `term:${t.term || name}`, type: '术语', title: name, subtitle: t.category ? `术语 · ${t.category}` : '术语', icon: 'book-open' })
  }
  // 分析主题取自 scenesMap（与 openTopicDetail 同源，点击才能真的打开详情）。
  // 原实现只扫 topicList，而它是 /api/tables/topics 返回的**场景级**主题
  // （production→"生产分析"），与 scenesMap[].topics 里的**场景内**主题
  // （"产量趋势"/"良率与质量表现"…）是两套不同的集合、名字也对不上
  // → 搜出「生产分析」后按名字去 scenesMap 里 find 必然 miss，点不动。
  for (const [sk, s] of Object.entries(scenesMap.value) as any[]) {
    for (const tp of (s?.topics || [])) {
      const name = tp.name || ''
      const blob = `${name} ${tp.description || ''}`.toLowerCase()
      if (blob.includes(q)) out.push({ key: `topic:${name}`, type: '分析主题', title: name, subtitle: s?.name || sk, scene: sk, icon: 'target' })
    }
  }
  globalSearchResults.value = out.slice(0, 12)
}
const onGlobalSearchInput = () => {
  if (globalSearchTimer.value) clearTimeout(globalSearchTimer.value)
  globalSearchTimer.value = setTimeout(runGlobalSearch, 250)
}
const blurSearch = () => {
  // 必须用独立定时器：原实现与搜索防抖共用一个 ref 且直接覆盖，
  // 于是「失焦」会把搜索定时器的句柄丢掉（再也取消不掉），
  // 紧接着重新聚焦打字时 clearTimeout 清掉的是「失焦隐藏」→
  // 下拉框关不掉，且搜索结果在失焦之后才被算出来。
  if (globalBlurTimer.value) clearTimeout(globalBlurTimer.value)
  globalBlurTimer.value = setTimeout(() => { globalSearchFocus.value = false }, 200)
}
const jumpToSearchResult = (r: any) => {
  globalSearchQ.value = ''
  globalSearchResults.value = []
  const parts = r.key.split(':')
  const kind = parts[0]
  const id = parts.slice(1).join(':')
  // 只在 kind 对应的集合里找。原实现不分支、四类依次无条件匹配且只比 name，
  // 导致「良率」这类术语被场景里的同名指标劫持，kind 形同虚设。
  const pick = (s: any, k: string): any[] => {
    if (k === 'object') return s?.objects || []
    if (k === 'metric') return s?.metrics || []
    if (k === 'rule') return s?.rules || []
    if (k === 'topic') return s?.topics || []
    return []
  }
  if (kind === 'term') {
    const t = termDictionary.value.find((x: any) => x.term === id || x.term_cn === id)
    activeView.value = 'terms'
    // 找不到时不塞搜索词：塞了会让列表被过滤成空，用户以为「这页没数据」
    if (t) openTermDetail(t)
    return
  }
  for (const [sk, s] of Object.entries(scenesMap.value) as any[]) {
    const hit = pick(s, kind).find((x: any) =>
      kind === 'object' ? x.table === id : x.name === id)
    if (!hit) continue
    activeScene.value = sk
    activeView.value = 'scene'
    if (kind === 'object') openObjectDetail(hit, sk)
    else if (kind === 'metric') openMetricDetail(hit, sk)
    else if (kind === 'rule') openRuleDetail(hit, sk)
    else openTopicDetail(hit, sk)
    return
  }
}

// ========== 导出（术语 / 指标口径） ==========
const exportLoading = ref('')
/** 轻量提示，与 PermissionPage/toast 同款实现（自包含 DOM，不引入新依赖） */
const toast = (msg: string, ok = true) => {
  const el = document.createElement('div')
  el.className = `fixed top-6 left-1/2 -translate-x-1/2 z-[1000] px-4 py-2.5 rounded-xl shadow-lg text-sm ${ok ? 'bg-blue-50 border border-blue-200 text-blue-700' : 'bg-red-50 border border-red-200 text-red-600'}`
  el.textContent = msg
  document.body.appendChild(el)
  setTimeout(() => el.remove(), 2600)
}
const exportKnowledge = async (scope: string, format: string) => {
  if (exportLoading.value) return
  exportLoading.value = `${scope}.${format}`
  try {
    // 必须走 fetch + blob：window.open 无法携带 Authorization 头，
    // 而后端已对导出做操作级鉴权（export），匿名请求会被 401 拒绝。
    const resp = await fetch(`/api/knowledge/export?scope=${scope}&format=${format}`)
    if (!resp.ok) {
      // 401/403 已由 auth.ts 的全局 fetch 包装统一提示，避免重复弹窗
      if (resp.status !== 401 && resp.status !== 403) {
        const err = await resp.json().catch(() => ({} as any))
        toast(String((err as any)?.detail || `导出失败(${resp.status})`), false)
      }
      return
    }
    const blob = await resp.blob()
    const cd = resp.headers.get('content-disposition') || ''
    const m = cd.match(/filename\*=UTF-8''([^;]+)/i) || cd.match(/filename="?([^";]+)"?/i)
    const fname = m ? decodeURIComponent(m[1]) : `knowledge_${scope}.${format}`
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = fname
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 30000)
  } catch (e: any) {
    toast(e?.message || '导出失败', false)
  } finally {
    exportLoading.value = ''
  }
}

// ========== 人工编辑弹窗（覆盖自动生成的知识） ==========
const editModal = ref<any>(null)
const editSaving = ref(false)
const editMsg = ref('')
const editMsgOk = ref(true)
// editForm 结构：{ key, kind, name, fields: [{name,label,value,type,rows}] }
const openEditModal = () => {
  const d = selectedDetail.value
  if (!d) return
  editMsg.value = ''
  if (d.kind === '业务对象') {
    editModal.value = {
      key: `obj:${d.table}`, kind: 'obj',
      fields: [
        { name: 'label', label: '中文名', value: d.title },
        { name: 'desc', label: '描述', value: d.desc || '', rows: 3 },
      ],
    }
  } else if (d.kind === '业务指标') {
    editModal.value = {
      key: `met:${d.title}`, kind: 'met',
      fields: [
        { name: 'name', label: '指标名', value: d.title, ro: true },
        { name: 'formula', label: '口径公式', value: d.formula || '', rows: 2 },
        { name: 'unit', label: '计量单位', value: d.unit || '' },
        { name: 'description', label: '说明', value: d.desc || '', rows: 3 },
      ],
    }
  } else if (d.kind === '业务规则') {
    editModal.value = {
      key: `rule:${d.title}`, kind: 'rule',
      fields: [
        { name: 'name', label: '规则名', value: d.title, ro: true },
        { name: 'condition', label: '判定条件', value: d.condition || '', rows: 3 },
        { name: 'desc', label: '说明', value: d.desc || '', rows: 3 },
      ],
    }
  } else if (d.kind === '分析主题') {
    editModal.value = {
      key: `topic:${d.title}`, kind: 'topic',
      fields: [
        { name: 'name', label: '主题名', value: d.title, ro: true },
        { name: 'desc', label: '描述', value: d.desc || '', rows: 3 },
        { name: 'questions', label: '示例问题（用分号分隔）', value: (d.questions || []).join('；'), rows: 3 },
      ],
    }
  }
}
const saveEdit = async () => {
  const m = editModal.value
  if (!m) return
  editSaving.value = true
  editMsg.value = ''
  const patch: any = {}
  for (const f of m.fields) {
    if (f.ro) continue
    let val = f.value
    if (f.name === 'questions') val = String(val || '').split(/[；;]/).map((x: string) => x.trim()).filter(Boolean)
    patch[f.name] = val
  }
  try {
    await saveOverride(m.key, m.kind, patch)
    editMsg.value = '已保存（会影响当前用户看到的知识）'
    editMsgOk.value = true
    editModal.value = null
    editMsgErr()
  } catch (e: any) {
    editMsg.value = e?.message || '保存失败'
    editMsgOk.value = false
  } finally {
    editSaving.value = false
  }
}
function editMsgErr() { /* 成功关闭后清空全局提示占位 */ }
const cancelEdit = () => { editModal.value = null }

// ========== 字段业务含义编辑（对象详情字段表） ==========
const fieldEdit = ref<any>(null)
const fieldEditMsg = ref('')
const openFieldEdit = (col: any) => {
  if (!selectedDetail.value?.table) return
  fieldEditMsg.value = ''
  fieldEdit.value = { table: selectedDetail.value.table, column: col.name, value: col.translation || '' }
}
const saveFieldEdit = async () => {
  const f = fieldEdit.value
  if (!f) return
  fieldEditMsg.value = ''
  try {
    await saveOverride(`field:${f.table}.${f.column}`, 'field', { translation: f.value })
    fieldEdit.value = null
    // 不再重复 loadKnowledge：saveOverride 内部已 await loadKnowledge+loadUserData，
    // 这里再来一次会让全页闪一次「正在从数据库加载…」。
  } catch (e: any) {
    // 原为 catch{} 空吞 —— 保存失败时弹窗不关、页面无任何变化、也没有提示，
    // 用户只能反复点，以为按钮坏了。
    fieldEditMsg.value = e?.message || '保存失败，请重试'
  }
}

// ========== 自定义术语增加 ==========
const termFormModal = ref(false)
const termForm = ref({
  term: '', en: '', definition: '', category: '', knowledge_type: '业务对象', abbreviation: '', data_type: ''
})
const termFormMsg = ref('')
const termFormMsgOk = ref(true)
const EMPTY_TERM_FORM = () => ({
  term: '', en: '', definition: '', category: '',
  knowledge_type: '业务对象', abbreviation: '', data_type: '',
})
// 打开时必须整体重置：原实现只弹窗不清内容，上一次失败留下的红字提示
// 会继续显示；更严重的是保存成功后只清了 4 个字段，category / data_type /
// knowledge_type 被静默沿用到下一个术语，提交出错的数据。
const openTermForm = () => {
  termFormMsg.value = ''
  termForm.value = EMPTY_TERM_FORM()
  termFormModal.value = true
}
const saveCustomTerm = async () => {
  termFormMsg.value = ''
  if (!termForm.value.term.trim()) { termFormMsg.value = '术语名称不能为空'; termFormMsgOk.value = false; return }
  if (!termForm.value.definition.trim()) { termFormMsg.value = '术语定义不能为空'; termFormMsgOk.value = false; return }
  try {
    const res = await fetch('/api/knowledge/terms', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(termForm.value),
    })
    if (!res.ok) throw new Error(`保存失败（HTTP ${res.status}）`)
    const data = await res.json()
    if (!data.success) throw new Error(data.detail || '保存失败')
    termForm.value = EMPTY_TERM_FORM()
    termFormModal.value = false
    toast('术语已保存')
    await loadKnowledge()
    await loadUserData()
  } catch (e: any) {
    termFormMsg.value = e?.message || '保存失败'
    termFormMsgOk.value = false
  }
}
const deleteCustomTerm = async (term: string) => {
  if (!window.confirm(`确认删除自定义术语「${term}」？`)) return
  try {
    // 原实现只 await fetch 不看 res.ok，且 catch 空吞 → 后端 500 时列表纹丝不动，
    // 用户以为删掉了（刷新后又出现），完全无从判断
    const res = await fetch(`/api/knowledge/terms/${encodeURIComponent(term)}`, { method: 'DELETE' })
    if (!res.ok) {
      let msg = `删除失败（HTTP ${res.status}）`
      try { msg = (await res.json())?.detail || msg } catch { /* 响应非 JSON，用默认文案 */ }
      throw new Error(msg)
    }
    toast('已删除')
    await loadKnowledge()
    await loadUserData()
  } catch (e: any) {
    toast(e?.message || '删除失败，请重试', false)
  }
}

// ========== 初始化 ==========
onMounted(async () => {
  await loadKnowledge()
  await loadRelations()
  loadTemplates()
  loadUserData()
  loadSnapshot(activeScene.value)
  loadTables()
  // 模板 ref：读取一次确保组件实例已挂载（消除 vue-tsc 未使用告警）
  void miniGraphRef
  if (props.initialScene && scenesMap.value[props.initialScene]) {
    activeView.value = 'scene'
    activeScene.value = props.initialScene
  }
})

watch(() => props.initialScene, (scene) => {
  if (scene && scenesMap.value[scene]) {
    activeView.value = 'scene'
    activeScene.value = scene
  }
})

onUnmounted(() => {
  // 页面级定时器此前完全不清理：离开本页后搜索防抖仍会 fire 并写已废弃的 ref，
  // toast 的 setTimeout 还会去操作已卸载的 DOM。
  if (globalSearchTimer.value) clearTimeout(globalSearchTimer.value)
  if (globalBlurTimer.value) clearTimeout(globalBlurTimer.value)
})
</script>

<style scoped>
/* ====== OPS 风格：米白底 + 灰绿边框 + 黄绿点缀 ====== */
.kb-shell {
  display: flex;
  gap: 18px;
  align-items: stretch;
  /* 兜底：父级 .workspace-content 的高度由 flex 撑出，个别场景下 h-full(100%) 会解析为 auto，
     这里按「顶栏 64 + 内容区上下内边距 16/22 = 102」再垫一层最小高度，
     保证知识图谱 / 术语词典等短内容视图始终铺到视口底部（内容更高时 min-height 不设上限）。 */
  min-height: calc(100vh - 102px);
}
.kb-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.mono { font-family: 'Rajdhani', Consolas, monospace; }


/* ====== 数据底座概览（KPI 卡片已随导航重构移除，保留详情面板共用样式） ====== */
.kb-overview { margin-top: 14px; }

.kb-overview-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
}

/* 单表放大弹窗 / 分析主题弹窗复用的图标徽标与字母徽标 */
.stat-chip {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  border-radius: 12px;
  flex-shrink: 0;
}
.topic-tile {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border-radius: 12px;
  font-size: 17px;
  font-weight: 600;
  flex-shrink: 0;
  user-select: none;
}

@media (max-width: 1080px) {
  .kb-overview-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

/* ====== 视角切换 Tab ====== */
.kb-tabs {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  border: 1px solid #e5e6eb;
  border-radius: 14px;
  background: #ffffff;
  box-shadow: 0 6px 20px rgba(16, 24, 40, .06);
  /* 不铺满整屏：按内容收窄 + 居中（窄屏自动拟合到可用宽度） */
  width: fit-content;
  max-width: 100%;
  margin: 0 auto;
}
.kb-tabs-nav {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-wrap: wrap;
  gap: 6px;
}
.kb-tabs-tools {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.kb-tab {
  padding: 10px 22px;
  border: 0;
  border-radius: 10px;
  background: transparent;
  color: #4e5969;
  font-size: 15px;
  font-weight: 600;
  letter-spacing: .01em;
  transition: all .18s ease;
}
.kb-tab:hover { color: #1d2129; background: #f2f3f5; }
.kb-tab.active { color: #ffffff; background: #4D9EFF; box-shadow: 0 6px 16px rgba(77, 158, 255, .35); font-weight: 700; }

/* ====== 加载 / 错误 ====== */
.kb-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 48px 0;
  color: #4e5969;
  font-size: 13px;
}
.kb-state-error { color: #a32d2d; }
.kb-state-sub { font-size: 11px; color: #a9aeb8; }
.kb-spinner {
  width: 26px; height: 26px;
  border: 2px solid #e5e6eb;
  border-top-color: #1677ff;
  border-radius: 50%;
  animation: kb-spin .8s linear infinite;
}
@keyframes kb-spin { to { transform: rotate(360deg); } }

/* ====== 场景卡 ====== */
.kb-scene-cards { display: grid; gap: 10px; }
.kb-scene-card {
  padding: 13px 15px;
  border: 1px solid #e5e6eb;
  border-radius: 12px;
  background: #ffffff;
  cursor: pointer;
  transition: all .18s ease;
}
.kb-scene-card:hover { border-color: #c9cdd4; background: #fff; box-shadow: 0 6px 18px rgba(16, 24, 40, .06); }
.kb-scene-card.active { border: 2px solid #1677ff; background: #e8f3ff; }
.kb-scene-card-head { display: flex; align-items: center; gap: 8px; }
.kb-scene-icon { font-size: 19px; }
.kb-scene-name { font-size: 14px; font-weight: 500; color: #1d2129; }
.kb-scene-desc { margin-top: 4px; font-size: 11px; color: #4e5969; line-height: 1.6; }
.kb-scene-meta { margin-top: 7px; font-size: 10px; color: #86909c; }
.kb-scene-roles { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.kb-role-chip { padding: 1px 7px; border-radius: 99px; background: #f2f3f5; color: #4e5969; font-size: 9px; }

/* ====== 详情面板（弹框浮层：点击卡片后弹框展示，不再平铺在场景顶部） ====== */
.kb-detail-mask {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(20, 26, 20, .4);
  backdrop-filter: blur(4px);
  z-index: 250;
  padding: 24px;
}
.kb-detail-modal {
  width: min(900px, calc(100vw - 48px));
  max-height: calc(100vh - 48px);
  overflow-y: auto;
  overflow-x: hidden;
  box-shadow: 0 24px 60px rgba(0, 0, 0, .25);
}
.kb-detail {
  border: 1px solid #e5e6eb;
  border-radius: 14px;
  background: #ffffff;
  box-shadow: 0 10px 30px rgba(16, 24, 40, .07);
  overflow: hidden;
}
.kb-detail-head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 13px 18px;
  border-bottom: 1px solid #e5e6eb;
  background: #f7f8fa;
}
.kb-detail-icon { font-size: 21px; }
.kb-detail-title-wrap { flex: 1; min-width: 0; }
.kb-detail-title { font-size: 15px; font-weight: 500; color: #1d2129; }
.kb-detail-meta { margin-top: 2px; font-size: 11px; color: #4e5969; }
.kb-detail-close {
  width: 28px; height: 28px;
  border: 1px solid #c9cdd4;
  border-radius: 8px;
  background: #fff;
  color: #4e5969;
  font-size: 12px;
  cursor: pointer;
  transition: all .15s ease;
}
.kb-detail-close:hover { color: #a32d2d; border-color: #f09595; background: #fcebeb; }
.kb-badge { padding: 2px 10px; border-radius: 99px; font-size: 10px; color: #fff; }
.kb-badge-obj { background: #4D9EFF; }
.kb-badge-met { background: #0f6e56; }
.kb-badge-rule { background: #854f0b; }
.kb-badge-topic { background: #993556; }
.kb-detail-tabs { display: flex; gap: 2px; padding: 8px 18px 0; }
.kb-detail-tab {
  padding: 7px 14px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: #4e5969;
  font-size: 12px;
  transition: all .18s ease;
}
.kb-detail-tab:hover { color: #1d2129; }
.kb-detail-tab.active { color: #1d2129; border-bottom-color: #1677ff; font-weight: 500; }
.kb-detail-body { padding: 16px 18px 20px; }

.kb-overview-desc { margin-bottom: 14px; font-size: 13px; color: #4e5969; line-height: 1.7; }
.kb-overview-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; }
.kb-ov-card {
  display: grid;
  gap: 4px;
  padding: 11px 13px;
  border: 1px solid #e5e6eb;
  border-radius: 10px;
  background: #f7f8fa;
}
.kb-ov-wide { grid-column: 1 / -1; }
.kb-ov-card span { font-size: 10px; color: #86909c; }
.kb-ov-card b { font-size: 12px; font-weight: 500; color: #1d2129; word-break: break-all; }
.kb-core-text { color: #a8641d; }
.kb-questions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 2px; }
.kb-question { padding: 4px 10px; border: 1px solid #e5e6eb; border-radius: 99px; background: #fff; font-size: 10px; color: #4e5969; }

/* 字段表 */
.kb-field-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.kb-field-table th {
  padding: 8px 12px;
  text-align: left;
  border-bottom: 1px solid #e5e6eb;
  background: #f7f8fa;
  color: #4e5969;
  font-weight: 500;
  font-size: 11px;
}
.kb-field-table td {
  padding: 8px 12px;
  border-bottom: 1px solid #e5e6eb;
  color: #4e5969;
  vertical-align: top;
}
.kb-field-table tr:hover td { background: #f7f8fa; }
.kb-col-name { color: #1677ff; white-space: nowrap; }
.kb-col-type { color: #86909c; white-space: nowrap; }
.kb-col-pk { width: 34px; text-align: center; }
.kb-col-translation { line-height: 1.6; }

/* 迷你关系图 */
.kb-mini-hint { margin-bottom: 10px; font-size: 11px; color: #4e5969; }
.kb-mini-graph, .kb-graph-canvas {
  border: 1px solid #e5e6eb;
  border-radius: 12px;
  background: #ffffff;
  padding: 8px;
}
.kb-empty { padding: 26px 0; text-align: center; color: #a9aeb8; font-size: 12px; }
.kb-empty-small { padding: 16px 0; font-size: 11px; }

/* ====== 场景内容布局 ====== */
/* 单列顺序：业务对象 → 分析主题 → 核心指标 → 业务规则 → 场景概览（对齐参考版） */
.kb-scene-layout { display: flex; flex-direction: column; gap: 14px; }
.kb-objects, .kb-side { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.kb-card {
  border: 1px solid #e5e6eb;
  border-radius: 14px;
  background: #ffffff;
  box-shadow: 0 6px 20px rgba(16, 24, 40, .04);
  overflow: hidden;
}
.kb-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 13px 16px;
  border-bottom: 1px solid #e5e6eb;
  background: #f7f8fa;
  font-size: 16px;
  font-weight: 700;
  color: #1d2129;
}
.kb-card-count, .kb-card-sub { margin-left: auto; font-size: 11px; font-weight: 400; color: #86909c; }
/* 核心指标卡「管理全部口径」入口：跳指标口径注册表（同源管理视图） */
.kb-manage-btn {
  display: inline-flex; align-items: center; gap: 3px; margin-left: 10px;
  padding: 2px 9px; border-radius: 99px; font-size: 10px; font-weight: 400;
  color: #1677ff; background: #e8f3ff; cursor: pointer;
  transition: background .15s ease;
}
.kb-manage-btn:hover { background: #d3e9ff; }
.kb-card-body { padding: 12px 16px; }

.kb-object-list { display: flex; flex-direction: column; gap: 8px; padding: 12px; max-height: 620px; overflow-y: auto; }
.kb-object {
  padding: 12px 14px;
  border: 1px solid #e5e6eb;
  border-radius: 12px;
  background: #fff;
  cursor: pointer;
  transition: all .18s ease;
}
.kb-object:hover { border-color: #c9cdd4; box-shadow: 0 6px 18px rgba(16, 24, 40, .07); }
.kb-object.active { border-color: #1677ff; background: #e8f3ff; box-shadow: 0 6px 18px rgba(77, 158, 255, .16); }
.kb-object-head { display: flex; align-items: flex-start; gap: 10px; }
.kb-object-icon { font-size: 17px; line-height: 1.4; }
.kb-object-title { flex: 1; min-width: 0; }
.kb-object-name { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-size: 13px; color: #1677ff; font-weight: 500; }
.kb-object-desc { margin-top: 3px; font-size: 11px; color: #4e5969; line-height: 1.6; }
.kb-core-chip { padding: 1px 7px; border-radius: 99px; background: #faede2; color: #a8641d; font-size: 9px; }
.kb-heat-chip { padding: 1px 7px; border-radius: 99px; background: #faf0e2; color: #a8641d; font-size: 9px; }
.kb-object-rows { font-size: 10px; color: #86909c; white-space: nowrap; margin-top: 2px; }
.kb-object-fields { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 9px; }
.kb-field-chip { padding: 2px 8px; border-radius: 6px; background: #f2f3f5; color: #4e5969; font-size: 10px; font-family: 'Rajdhani', Consolas, monospace; }
.kb-field-more { font-size: 10px; color: #a9aeb8; align-self: center; }

/* 主题 / 指标 / 规则卡 */
.kb-topic-list { display: flex; flex-direction: column; gap: 8px; }
.kb-topic { padding: 10px 12px; border: 1px solid #e5e6eb; border-radius: 10px; background: #f7f8fa; cursor: pointer; transition: all .18s ease; }
.kb-topic:hover { border-color: #c9cdd4; background: #fff; }
.kb-topic-head { display: flex; align-items: center; gap: 6px; }
.kb-topic-name { font-size: 12px; font-weight: 500; color: #1d2129; }
.kb-topic-kind { padding: 1px 7px; border-radius: 99px; background: #fff; border: 1px solid #e5e6eb; color: #86909c; font-size: 9px; }
.kb-topic-desc { margin-top: 5px; font-size: 10px; color: #4e5969; line-height: 1.6; }
.kb-topic-questions { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }
.kb-metric { padding: 9px 12px; border: 1px solid #e5e6eb; border-radius: 10px; background: #f7f8fa; cursor: pointer; transition: all .18s ease; }
.kb-metric:hover { border-color: #c9cdd4; background: #fff; }
.kb-metric-head { display: flex; align-items: center; gap: 6px; }
.kb-metric-name { font-size: 12px; font-weight: 500; color: #1d2129; }
.kb-metric-unit { font-size: 10px; color: #86909c; }
.kb-metric-formula { margin-top: 4px; font-size: 10px; color: #4e5969; word-break: break-all; }
.kb-rule { padding: 9px 12px; border: 1px solid #e5e6eb; border-radius: 10px; background: #f7f8fa; cursor: pointer; transition: all .18s ease; }
.kb-rule:hover { border-color: #c9cdd4; background: #fff; }
.kb-rule-name { font-size: 12px; font-weight: 500; color: #1d2129; }
.kb-rule-cond { margin-top: 4px; font-size: 10px; color: #4e5969; }
.kb-overview-rows { display: flex; flex-direction: column; gap: 9px; }
.kb-ov-row { display: flex; justify-content: space-between; align-items: center; font-size: 12px; }
.kb-ov-row span { color: #86909c; }
.kb-ov-row b { color: #1d2129; font-weight: 500; }

/* ====== 知识图谱视图 ====== */
/* 注意：图谱卡片刻意不加 overflow:hidden —— 卡片内下方可能展开「节点中文解释」面板，
   一旦裁剪（overflow:hidden 会让它成为滚动容器、自动最小高度变 0），
   卡片高度会被 flex 压到放下面板之前，面板底部直接被裁掉。
   圆角改为由首个子元素（工具条）与末个子元素（面板）各自声明。 */
.kb-graph-shell { border: 1px solid #e5e6eb; border-radius: 14px; background: #ffffff; box-shadow: 0 6px 20px rgba(16, 24, 40, .04); }
/* 撑满主区剩余高度：知识图谱 / 术语词典内容不足一屏时，卡片一直铺到底（2026-10-02）
   注意：不要加 min-height: 0 —— 空间不够时靠内容撑高、页面滚动，避免裁切内部内容。 */
.kb-view-fill { flex: 1; display: flex; flex-direction: column; }
/* 图谱卡片内的节点面板：右内圆角跟卡片对齐（卡片未用 overflow:hidden，靠这里补） */
.kb-graph-shell :deep(.kb-node-detail) { border-radius: 0 13px 13px 0; }
/* 图谱 + 节点详情左右分栏：同一屏内展示，不用滚动 */
.kb-graph-body { flex: 1; display: flex; min-width: 0; }
.kb-graph-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; padding: 12px 16px; border-bottom: 1px solid #e5e6eb; background: #f7f8fa; border-radius: 13px 13px 0 0; }
.kb-graph-toolbar-left { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.kb-graph-title { font-size: 13px; font-weight: 500; color: #1d2129; }
.kb-graph-hint { font-size: 10px; color: #86909c; }
.kb-graph-count { padding: 2px 9px; border-radius: 99px; background: #f2f3f5; color: #4e5969; font-size: 10px; }
.kb-graph-toolbar-right { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.kb-filter-btn { padding: 4px 10px; border: 1px solid #e5e6eb; border-radius: 99px; background: #fff; color: #4e5969; font-size: 10px; cursor: pointer; transition: all .15s ease; }
.kb-filter-btn.active { border-color: #4D9EFF; color: #ffffff; background: #4D9EFF; }
.kb-ghost-btn { padding: 4px 10px; border: 1px solid #e5e6eb; border-radius: 8px; background: #fff; color: #4e5969; font-size: 11px; cursor: pointer; transition: all .15s ease; }
.kb-ghost-btn:hover { border-color: #1677ff; color: #1d2129; }
.kb-primary-btn { padding: 4px 12px; border: 1px solid #4D9EFF; border-radius: 8px; background: #4D9EFF; color: #ffffff; font-size: 11px; cursor: pointer; transition: all .15s ease; }
.kb-primary-btn:hover { background: #2E7CF0; }
.kb-graph-legend { display: flex; flex-wrap: wrap; gap: 12px; padding: 10px 16px; font-size: 10px; color: #86909c; }
.kb-legend-item { display: flex; align-items: center; gap: 5px; }
.kb-legend-dot { width: 11px; height: 11px; border-radius: 3px; border-width: 1px; border-style: solid; }
.kb-legend-note { color: #a9aeb8; }
.kb-legend-warn { color: #a8641d; }
.kb-graph-canvas { flex: 1; min-width: 0; min-height: 420px; padding: 6px; display: flex; }
.kb-graph-canvas :deep(.lightrag-graph) { flex: 1 1 auto; min-width: 0; }
.kb-fullscreen { position: fixed; inset: 0; z-index: 100; display: flex; flex-direction: column; background: #f2f3f5; }
.kb-fullscreen-head { display: flex; align-items: center; justify-content: space-between; padding: 12px 18px; border-bottom: 1px solid #e5e6eb; background: #f7f8fa; font-size: 13px; font-weight: 500; color: #1d2129; }
.kb-fullscreen-actions { display: flex; gap: 8px; }
.kb-fullscreen-body { flex: 1; min-height: 0; padding: 14px; display: flex; overflow: hidden; }
.kb-fullscreen-body :deep(.lightrag-graph) { flex: 1 1 auto; min-width: 0; }
.kb-graph-node-detail { margin-top: 14px; padding: 16px 18px; border: 1px solid #e5e6eb; border-radius: 14px; background: #ffffff; }
.kb-graph-node-detail-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.kb-graph-node-title { font-size: 13px; font-weight: 500; color: #1d2129; }
.kb-graph-node-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; font-size: 11px; }
.kb-graph-node-grid span { display: block; color: #86909c; font-size: 10px; margin-bottom: 2px; }
.kb-graph-node-grid b { color: #1d2129; font-weight: 500; }
.kb-graph-node-desc { margin-top: 10px; font-size: 12px; color: #4e5969; }
.kb-graph-node-cols { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }

/* ====== 术语词典视图 ====== */
.kb-terms-shell { border: 1px solid #e5e6eb; border-radius: 14px; background: #ffffff; box-shadow: 0 6px 20px rgba(16, 24, 40, .04); overflow: hidden; }
.kb-terms-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; padding: 14px 16px; border-bottom: 1px solid #e5e6eb; background: #f7f8fa; }
.kb-terms-toolbar-left { display: flex; align-items: center; gap: 10px; }
.kb-terms-count { font-size: 10px; color: #86909c; }
.kb-terms-toolbar-right { display: flex; gap: 8px; flex-wrap: wrap; }
.kb-panel-search {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border: 1px solid #c9cdd4;
  border-radius: 9px;
  background: #fff;
  margin: 8px 12px 6px;
}
.kb-panel-search:focus-within { border-color: #1677ff; box-shadow: 0 0 0 3px rgba(146, 166, 62, .12); }
.kb-panel-search-input { flex: 1; min-width: 0; border: 0; outline: none; background: transparent; font-size: 12px; color: #1d2129; }
.kb-panel-search-input::placeholder { color: #a9aeb8; }
.kb-input {
  padding: 7px 12px;
  border: 1px solid #c9cdd4;
  border-radius: 9px;
  background: #fff;
  color: #1d2129;
  font-size: 12px;
  outline: none;
  transition: border-color .15s ease, box-shadow .15s ease;
  min-width: 180px;
}
.kb-input:focus { border-color: #1677ff; box-shadow: 0 0 0 3px rgba(146, 166, 62, .15); }
.kb-input-flex { flex: 1; min-width: 200px; }
.kb-select {
  padding: 7px 10px;
  border: 1px solid #c9cdd4;
  border-radius: 9px;
  background: #fff;
  color: #1d2129;
  font-size: 12px;
  outline: none;
}
.kb-terms-body { flex: 1; min-height: 0; max-height: none; overflow-y: auto; }
.kb-rels-body { padding: 16px; }
.kb-tables-body { padding: 16px; }
.kb-term-group { border-bottom: 1px solid #e5e6eb; }
.kb-term-group:last-child { border-bottom: 0; }
.kb-term-group-head { display: flex; align-items: center; gap: 8px; padding: 9px 16px; background: #f7f8fa; font-size: 12px; font-weight: 500; color: #1d2129; position: sticky; top: 0; z-index: 2; }
.kb-term { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 10px; padding: 12px 16px; cursor: pointer; transition: background .15s ease; }
.kb-term:hover { background: #e8f3ff; }
.kb-term-main { flex: 1; min-width: 220px; }
.kb-term-top { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.kb-term-name { font-size: 13px; font-weight: 500; color: #1d2129; }
.kb-term-badge { padding: 1px 8px; border-radius: 99px; font-size: 9px; color: #fff; }
.kb-term-badge-cat { background: #7f77dd; }
.kb-term-def { margin-top: 4px; font-size: 11px; color: #4e5969; line-height: 1.6; }
.kb-term-side { text-align: right; flex-shrink: 0; }
.kb-term-en { font-size: 11px; color: #1d2129; background: #f2f3f5; padding: 3px 8px; border-radius: 6px; }
.kb-term-en-label { margin-top: 2px; font-size: 9px; color: #a9aeb8; }
.kb-term-meta { display: flex; flex-wrap: wrap; gap: 8px; width: 100%; font-size: 10px; color: #86909c; }

/* ====== 分析模板视图 ====== */
.kb-templates-toolbar { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; padding: 16px; border: 1px solid #e5e6eb; border-radius: 14px; background: #ffffff; }
.kb-templates-sub { margin-top: 3px; font-size: 11px; color: #86909c; }
.kb-template-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }
.kb-template-card {
  padding: 18px;
  border: 1px solid #e5e6eb;
  border-radius: 16px;
  background: #ffffff;
  cursor: pointer;
  transition: all .2s ease;
}
.kb-template-card:hover { border-color: #1677ff; box-shadow: 0 12px 30px rgba(16, 24, 40, .10); transform: translateY(-2px); }
.kb-template-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.kb-template-icon { font-size: 26px; line-height: 1; }
.kb-template-name { margin-top: 8px; font-size: 15px; font-weight: 500; color: #1d2129; }
.kb-template-scene { padding: 3px 10px; border-radius: 99px; background: #f2f3f5; color: #4e5969; font-size: 10px; letter-spacing: .08em; }
.kb-template-desc { margin-top: 10px; font-size: 12px; color: #4e5969; line-height: 1.7; }
/* 推荐相关：按数据权限/使用习惯推荐（浅色主题，与卡片风格一致） */
.kb-tpl-banner { display: flex; align-items: center; gap: 8px; margin: 0 0 14px; padding: 10px 14px; border: 1px solid #d6e4d8; background: #f1f7f1; border-radius: 10px; font-size: 12.5px; color: #3c6e47; }
.kb-tpl-banner svg { flex-shrink: 0; }
.kb-tpl-rec-chip { display: inline-flex; align-items: center; gap: 4px; padding: 3px 9px; border-radius: 99px; background: #0f6e56; color: #fff; font-size: 10px; white-space: nowrap; }
.kb-tpl-reason { margin-top: 6px; display: flex; align-items: flex-start; gap: 6px; font-size: 11.5px; color: #5a7d63; line-height: 1.6; }
.kb-tpl-reason svg { flex-shrink: 0; margin-top: 2px; }
.kb-template-card.kb-tpl-recommended { border-color: #9ecfa8; box-shadow: 0 0 0 1px rgba(15, 110, 86, .12); }
.kb-template-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
.kb-template-tag { padding: 2px 9px; border-radius: 99px; background: #f2f3f5; color: #4e5969; font-size: 10px; }
.kb-template-metrics { display: grid; gap: 8px; margin-top: 14px; font-size: 11px; }
.kb-template-question { padding: 9px 12px; border-radius: 10px; background: #f7f8fa; color: #4e5969; line-height: 1.6; }
.kb-template-question b { color: #1d2129; font-weight: 500; }
.kb-template-count { padding: 9px 12px; border-radius: 10px; background: #f2f3f5; color: #4e5969; }
.kb-template-actions { display: flex; gap: 8px; margin-top: 14px; }
.kb-template-run {
  flex: 1; padding: 9px 0; border: 0; border-radius: 99px;
  background: #0F6E56; color: #fff; font-size: 13px; cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center; gap: 5px;
  transition: background .18s ease;
}
.kb-template-run:hover { background: #085041; }
.kb-template-run:disabled { background: #9fe1cb; cursor: not-allowed; }
.kb-template-apply {
  flex: 1; padding: 9px 0; border: 1px solid #c9cdd4; border-radius: 99px;
  background: #fff; color: #1d2129; font-size: 13px; cursor: pointer;
  transition: background .18s ease, border-color .18s ease;
}
.kb-template-apply:hover { background: #f1efe8; border-color: #1677ff; }

/* ====== 右侧业务域面板 ====== */
.kb-panel {
  position: sticky;
  top: 20px;
  flex: none;
  width: 264px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 14px 12px 12px;
  border: 1px solid #e5e6eb;
  border-radius: 14px;
  background: #ffffff;
  box-shadow: 0 8px 26px rgba(16, 24, 40, .05);
  transition: width .3s cubic-bezier(.4, 0, .2, 1), padding .3s cubic-bezier(.4, 0, .2, 1);
  max-height: calc(100vh - 40px);
  overflow: hidden;
}
.kb-panel.collapsed { width: 46px; padding: 14px 8px; }
.kb-panel-toggle {
  position: absolute;
  top: 12px;
  right: 10px;
  z-index: 5;
  display: grid;
  width: 26px; height: 26px;
  place-items: center;
  border: 1px solid #c9cdd4;
  border-radius: 8px;
  background: #fff;
  color: #4e5969;
  cursor: pointer;
  transition: all .18s ease;
}
.kb-panel-toggle:hover { border-color: #1677ff; color: #1d2129; background: #ffffff; }
.kb-panel-toggle-icon { display: inline-block; font-size: 13px; font-weight: 700; transition: transform .3s cubic-bezier(.4, 0, .2, 1); }
.kb-panel.collapsed .kb-panel-toggle { right: 50%; transform: translateX(50%); }
.kb-panel.collapsed .kb-panel-toggle-icon { transform: rotate(180deg); }
.kb-panel-head { display: flex; align-items: baseline; gap: 8px; padding-right: 32px; }
.kb-panel-head span { font-size: 14px; font-weight: 500; color: #1d2129; }
.kb-panel-head small { font-size: 10px; color: #86909c; }

.kb-tree { display: flex; flex-direction: column; gap: 2px; overflow-y: auto; flex: 1; min-height: 120px; }
.kb-tree-cat { border-bottom: 1px solid #e5e6eb; }
.kb-tree-cat:last-child { border-bottom: 0; }
.kb-tree-cat-head {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  padding: 8px 8px;
  border: 0;
  background: transparent;
  color: #1d2129;
  font-size: 12px;
  font-weight: 500;
  text-align: left;
  cursor: pointer;
  border-radius: 8px;
  transition: background .15s ease;
}
.kb-tree-cat-head:hover { background: #f2f3f5; }
.kb-tree-count { margin-left: auto; font-size: 10px; color: #86909c; font-weight: 400; }
.kb-tree-cat-head i { font-style: normal; font-size: 10px; color: #a9aeb8; }
.kb-tree-children { display: flex; flex-direction: column; gap: 1px; padding: 2px 0 6px 22px; }
.kb-tree-node {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border-radius: 7px;
  cursor: pointer;
  transition: background .15s ease;
}
.kb-tree-node:hover { background: #f2f3f5; }
.kb-tree-node.active { background: #4D9EFF; }
.kb-tree-node-label { font-size: 12px; color: #4e5969; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.kb-tree-node.active .kb-tree-node-label { color: #ffffff; font-weight: 500; }
.kb-tree-node-sub { margin-left: auto; font-size: 9px; color: #a9aeb8; white-space: nowrap; }
.kb-tree-node.active .kb-tree-node-sub { color: #ffffff; }

/* 响应式：窄屏时右栏折叠为抽屉（场景内容已是单列，无需再塌缩为 1 列） */
@media (max-width: 1180px) {
  .kb-panel { width: 46px; padding: 14px 8px; }
  /* 顶栏高 64px：抽屉从 header 下方开始（top 76），避免盖住右上角账户菜单/按钮 */
  .kb-panel:not(.collapsed) { position: fixed; right: 14px; top: 76px; bottom: 14px; z-index: 40; box-shadow: 0 20px 50px rgba(16, 24, 40, .18); }
  .kb-panel:not(.collapsed) .kb-panel-toggle { right: 10px; transform: none; }
}

/* ====== 顶栏工具区：搜索 / 导出 ====== */
.kb-global-search {
  position: relative;
  display: flex;
  align-items: center;
  width: 260px;
  height: 38px;
  padding: 0 10px;
  border: 1px solid #e5e6eb;
  border-radius: 10px;
  background: #fff;
  color: #86909c;
}
.kb-global-search-icon { flex: none; }
.kb-global-search input {
  flex: 1;
  min-width: 0;
  height: 100%;
  padding: 0 8px;
  border: 0;
  outline: none;
  background: transparent;
  font-size: 12px;
  color: #1d2129;
}
/* 搜索框不要聚焦高亮：全局 .workspace-content input:focus 会套一层蓝色光圈，这里覆盖掉 */
.kb-global-search input:focus,
.kb-global-search input:focus-visible {
  outline: none !important;
  border-color: transparent !important;
  box-shadow: none !important;
}
.kb-search-dropdown {
  position: absolute;
  top: 42px;
  left: 0;
  right: 0;
  max-height: 320px;
  overflow-y: auto;
  border: 1px solid #e5e6eb;
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 14px 40px rgba(16, 24, 40, .16);
  z-index: 30;
  padding: 4px;
}
.kb-search-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 8px;
  cursor: pointer;
}
.kb-search-item:hover { background: #f2f3f5; }
.kb-search-item-type {
  flex: none;
  padding: 1px 6px;
  border-radius: 99px;
  background: #f2f3f5;
  color: #4e5969;
  font-size: 10px;
}
.kb-search-item-title { font-size: 12px; font-weight: 600; color: #1d2129; }
.kb-search-item-sub { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; color: #86909c; }
.kb-tool-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 38px;
  padding: 0 12px;
  border: 1px solid #e5e6eb;
  border-radius: 10px;
  background: #fff;
  color: #4e5969;
  font-size: 12px;
  cursor: pointer;
  white-space: nowrap;
}
.kb-tool-btn:hover { border-color: #c9cdd4; color: #1d2129; }
.kb-export-menu { position: relative; }
.kb-export-dropdown {
  position: absolute;
  top: 42px;
  right: 0;
  display: none;
  flex-direction: column;
  min-width: 180px;
  border: 1px solid #e5e6eb;
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 14px 40px rgba(16, 24, 40, .16);
  padding: 4px;
  z-index: 30;
}
.kb-export-menu:hover .kb-export-dropdown, .kb-export-dropdown:hover { display: flex; }
.kb-export-dropdown button {
  padding: 8px 10px;
  border: 0;
  background: transparent;
  text-align: left;
  font-size: 12px;
  color: #1d2129;
  border-radius: 8px;
  cursor: pointer;
}
.kb-export-dropdown button:hover { background: #f2f3f5; }

/* ====== 详情面板动作：编辑 / 收藏 / 反馈 ====== */
.kb-detail-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border: 1px solid #e5e6eb;
  border-radius: 9px;
  background: #fff;
  color: #86909c;
  cursor: pointer;
}
.kb-detail-action:hover { border-color: #c9cdd4; color: #1d2129; }
.kb-detail-action.fav { color: #d8a700; border-color: #e6d59a; background: #fdf6dd; }
.kb-detail-vote { display: inline-flex; gap: 4px; }
.kb-vote-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border: 1px solid #e5e6eb;
  border-radius: 9px;
  background: #fff;
  color: #86909c;
  cursor: pointer;
}
.kb-vote-btn:hover { border-color: #c9cdd4; color: #1d2129; }
.kb-vote-btn.active { color: #2E7CF0; border-color: #4D9EFF; background: #e8f3ff; }
.kb-field-edit {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  margin-left: 6px;
  vertical-align: middle;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: #a9aeb8;
  cursor: pointer;
}
.kb-field-edit:hover { color: #1d2129; background: #f2f3f5; }

/* ====== 术语项动作 ====== */
.kb-term-actions { display: flex; align-items: center; }
.kb-term-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: #c9cdd4;
  cursor: pointer;
}
.kb-term-action:hover { background: #f2f3f5; }
.kb-term-action.fav { color: #d8a700; }
.kb-term-badge-custom { padding: 1px 7px; border-radius: 99px; background: #e8f3ff; color: #1677ff; font-size: 10px; }

/* ====== 模板收藏 ====== */
.kb-template-fav {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: #c9cdd4;
  cursor: pointer;
}
.kb-template-fav:hover { background: #f2f3f5; }
.kb-template-fav.fav { color: #d8a700; }

/* ====== 通用弹窗 ====== */
.kb-modal-mask {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(20, 26, 20, .4);
  backdrop-filter: blur(4px);
  z-index: 300;
}
.kb-modal {
  width: 520px;
  max-width: calc(100vw - 32px);
  max-height: calc(100vh - 80px);
  overflow-y: auto;
  border: 1px solid #e5e6eb;
  border-radius: 16px;
  background: #ffffff;
  box-shadow: 0 24px 60px rgba(0, 0, 0, .22);
}
.kb-modal-sm { width: 420px; }
.kb-modal-lg { width: 640px; }
.kb-modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 18px;
  border-bottom: 1px solid #f2f3f5;
  font-size: 13px;
  font-weight: 600;
  color: #1d2129;
}
.kb-modal-body { padding: 18px; display: grid; gap: 12px; }
.kb-modal-foot { display: flex; justify-content: flex-end; gap: 8px; padding: 14px 18px; border-top: 1px solid #f2f3f5; }
.kb-form-row { display: grid; gap: 5px; }
.kb-form-row label { font-size: 12px; color: #86909c; }
.kb-form-msg { font-size: 12px; color: #d9534f; }
.kb-form-msg.ok { color: #1677ff; }

/* ====== 「反馈知识有误」弹窗 ====== */
.kb-fb-target {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  padding: 9px 11px;
  border: 1px solid #e8eef7;
  border-radius: 10px;
  background: #f7faff;
}
.kb-fb-target b { font-size: 12.5px; color: #1d2129; }
.kb-fb-target small { width: 100%; font-size: 11px; color: #86909c; }
.kb-fb-kind {
  padding: 1px 8px;
  border-radius: 999px;
  background: #e8f3ff;
  color: #2E7CF0;
  font-size: 10.5px;
  font-weight: 600;
}
.kb-fb-count { margin-top: 3px; text-align: right; font-size: 10.5px; color: #b0b8c4; }
.kb-fb-tip {
  display: flex;
  align-items: flex-start;
  gap: 5px;
  margin: 0;
  font-size: 11px;
  line-height: 1.6;
  color: #86909c;
}
.kb-fb-tip :deep(.app-icon) { flex: none; margin-top: 2px; color: #4D9EFF; }
.kb-fb-done {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 14px 4px 6px;
  text-align: center;
}
.kb-fb-done :deep(.app-icon) { color: #23b26d; }
.kb-fb-done b { font-size: 13.5px; color: #1d2129; }
.kb-fb-done p { margin: 0; font-size: 11.5px; line-height: 1.65; color: #86909c; }

/* ====== P0（2026-09-03）：场景胶囊 + 概要条 ====== */
.kb-scene-wrap { display: flex; flex-direction: column; gap: 10px; }
.kb-scene-pills { display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; }
.kb-scene-pill {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 8px 16px; border-radius: 99px; border: 1px solid #e5e6eb;
  background: #fff; color: #4e5969; font-size: 14px; font-weight: 500; cursor: pointer;
  transition: all .18s ease;
}
.kb-scene-pill:hover { border-color: #c9cdd4; background: #f7f8fa; }
.kb-scene-pill.active { border-color: #1677ff; background: #1677ff; color: #fff; box-shadow: 0 6px 16px rgba(22, 119, 255, .24); }
.kb-scene-pill i {
  font-style: normal; min-width: 16px; height: 16px; padding: 0 4px;
  border-radius: 99px; background: rgba(0, 0, 0, .06); color: #4e5969;
  font-size: 10px; line-height: 16px; text-align: center;
}
.kb-scene-pill.active i { background: rgba(255, 255, 255, .24); color: #fff; }
.kb-scene-summary {
  display: flex; align-items: stretch; gap: 14px; flex-wrap: wrap;
  padding: 10px 14px; border: 1px solid #e5e6eb; border-radius: 12px;
  background: #fff;
}
.kb-summary-title { display: flex; flex-direction: column; gap: 4px; min-width: 0; flex: 1; }
.kb-summary-title b { font-size: 15px; font-weight: 600; color: #1d2129; white-space: nowrap; }
.kb-summary-intro { font-size: 12px; color: #4e5969; line-height: 1.7; }

/* ====== P0：业务对象卡片网格 ====== */
.kb-obj-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 10px; padding: 12px; }
.kb-obj-card {
  position: relative; padding: 12px 13px; border: 1px solid #e5e6eb; border-radius: 12px;
  background: #fff; cursor: pointer; transition: all .18s ease; display: flex; flex-direction: column; gap: 8px;
}
.kb-obj-card:hover { border-color: #c9cdd4; box-shadow: 0 6px 18px rgba(16, 24, 40, .07); }
.kb-obj-card.active { border-color: #1677ff; background: #f2f8ff; box-shadow: 0 6px 18px rgba(77, 158, 255, .16); }
.kb-obj-card-top { display: flex; align-items: flex-start; gap: 9px; }
.kb-object-icon { font-size: 16px; line-height: 1.4; flex: none; }
.kb-obj-title { flex: 1; min-width: 0; }
.kb-obj-name { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-size: 13.5px; font-weight: 600; color: #1d2129; }
.kb-obj-sub { margin-top: 3px; font-size: 10.5px; color: #86909c; display: flex; gap: 6px; flex-wrap: wrap; }
.kb-obj-desc { font-size: 11px; color: #4e5969; line-height: 1.55; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.kb-ask-btn {
  flex: none; display: inline-flex; align-items: center; gap: 4px;
  padding: 3.5px 9px; border-radius: 8px; border: 1px solid #bcd8ff; background: #e8f3ff;
  color: #1677ff; font-size: 11px; cursor: pointer; opacity: .92; transition: all .15s ease;
}
.kb-ask-btn:hover { background: #1677ff; color: #fff; border-color: #1677ff; }
.kb-obj-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.kb-obj-tag {
  display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px;
  border-radius: 99px; background: #f2f3f5; color: #4e5969; font-size: 10.5px;
}
.kb-obj-tag.kb-tag-met { background: #e8f3ff; color: #1677ff; }
.kb-obj-tag.kb-tag-rule { background: #fdf1e0; color: #b25e09; }
.kb-obj-fields { display: flex; flex-wrap: wrap; gap: 5px; }

/* ====== P0：指标 / 规则 / 主题 / 概览增强 ====== */
.kb-metric { position: relative; padding: 9px 12px; border: 1px solid #e5e6eb; border-radius: 10px; background: #f7f8fa; cursor: pointer; transition: all .18s ease; }
.kb-metric:hover { border-color: #c9cdd4; background: #fff; }
.kb-metric-head { display: flex; align-items: center; gap: 6px; padding-right: 54px; }
.kb-metric-name { font-size: 12.5px; font-weight: 600; color: #1d2129; }
.kb-metric-badge { flex: none; padding: 0 6px; border-radius: 6px; background: #e6f6ee; color: #1f9e5f; font-size: 9.5px; }
.kb-metric-unit { font-size: 10px; color: #86909c; margin-left: auto; }
.kb-metric-formula { margin-top: 5px; font-size: 10px; color: #4e5969; word-break: break-all; }
.kb-metric-ask {
  position: absolute; top: 9px; right: 10px; display: inline-flex; align-items: center; gap: 3px;
  padding: 2.5px 8px; border-radius: 7px; border: 1px solid #bcd8ff; background: #fff; color: #1677ff;
  font-size: 10px; cursor: pointer; opacity: 0; transition: all .15s ease;
}
.kb-metric:hover .kb-metric-ask { opacity: 1; }
.kb-metric-ask:hover { background: #1677ff; color: #fff; }
.kb-rule { padding: 9px 12px; border: 1px solid #e5e6eb; border-radius: 10px; background: #f7f8fa; cursor: pointer; transition: all .18s ease; }
.kb-rule:hover { border-color: #c9cdd4; background: #fff; }
.kb-rule-head { display: flex; align-items: center; gap: 8px; }
.kb-rule-name { font-size: 12.5px; font-weight: 600; color: #1d2129; }
.kb-rule-src { margin-left: auto; flex: none; padding: 0 6px; border-radius: 6px; background: #f2f3f5; color: #86909c; font-size: 9.5px; }
.kb-rule-cond { margin-top: 5px; font-size: 11px; color: #4e5969; }
.kb-rule-formula { margin-top: 4px; font-size: 10px; color: #b25e09; word-break: break-all; }
.kb-question-btn { display: inline-flex; align-items: center; gap: 4px; max-width: 100%; text-align: left; cursor: pointer; transition: all .15s ease; }
.kb-question-btn:hover { border-color: #1677ff; color: #1677ff; background: #e8f3ff; }
.kb-overview-ask { margin-top: 12px; width: 100%; justify-content: center; }

/* 锚点定位时避开顶部固定头/工具栏 */
#kb-sec-objects, #kb-sec-topics, #kb-sec-metrics, #kb-sec-rules, #kb-sec-overview { scroll-margin-top: 96px; }

/* ====== P1（2026-09-03）：详情动作 / 关系 Tab / 图谱场景过滤 ====== */
.kb-detail-askbtn { border-color: #bcd8ff !important; background: #e8f3ff !important; color: #1677ff !important; }
.kb-detail-askbtn:hover { background: #1677ff !important; color: #fff !important; border-color: #1677ff !important; }

.kb-rel-group { margin-top: 14px; }
.kb-rel-title { display: flex; align-items: center; gap: 6px; font-size: 11.5px; font-weight: 600; color: #1d2129; margin-bottom: 7px; }
.kb-rel-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.kb-rel-chip {
  display: inline-flex; align-items: center; gap: 4px; max-width: 100%;
  padding: 3px 10px; border-radius: 99px; border: 1px solid #e5e6eb; background: #fff;
  color: #4e5969; font-size: 11px; cursor: pointer; transition: all .15s ease;
}
.kb-rel-chip small { font-size: 9.5px; color: #86909c; }
.kb-rel-chip:hover { border-color: #1677ff; color: #1677ff; background: #e8f3ff; }
.kb-rel-chip.kb-rel-chip-table { border-color: #bcd8ff; background: #e8f3ff; color: #1677ff; }
.kb-rel-chip.kb-rel-chip-table:hover { background: #1677ff; color: #fff; }
.kb-rel-chip.kb-rel-chip-rule { border-color: #f4d3a5; background: #fdf6ec; color: #b25e09; }
.kb-rel-chip.kb-rel-chip-rule:hover { background: #b25e09; color: #fff; border-color: #b25e09; }
.kb-rel-formula { padding: 8px 10px; border-radius: 8px; background: #f7f8fa; font-size: 11px; color: #1d2129; word-break: break-all; line-height: 1.6; }

.kb-graph-scenebar { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; padding: 0 16px 8px; }
.kb-graph-scene-label { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; color: #86909c; }
.kb-scene-chip {
  padding: 3px 11px; border-radius: 99px; border: 1px solid #e5e6eb; background: #fff;
  color: #4e5969; font-size: 11px; cursor: pointer; transition: all .15s ease;
}
.kb-scene-chip:hover { border-color: #c9cdd4; background: #f7f8fa; }
.kb-scene-chip.active { border-color: #1677ff; background: #1677ff; color: #fff; }
.kb-scene-chip-hint { font-size: 10px; color: #86909c; margin-left: 4px; }
.kb-graph-node-open { margin-top: 10px; width: 100%; justify-content: center; }

/* ====== 分析主题（A+C+B1）：入口卡 / 分类 / 迷你分析台 ====== */
.kb-topic-group-title { display: flex; align-items: baseline; gap: 8px; font-size: 12px; font-weight: 700; color: #1d2129; margin-bottom: 8px; }
.kb-topic-group-title small { font-weight: 400; font-size: 10px; color: #86909c; }
.kb-topic-biz { margin-bottom: 6px; }
.kb-topic-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 10px; }
.kb-topic-card {
  padding: 12px 13px; border: 1px solid #e5e6eb; border-radius: 12px; background: #fff;
  cursor: pointer; transition: all .18s ease; display: flex; flex-direction: column; gap: 7px;
}
.kb-topic-card:hover { border-color: #c9cdd4; box-shadow: 0 6px 18px rgba(16, 24, 40, .07); }
.kb-topic-card.active { border-color: #1677ff; background: #f2f8ff; box-shadow: 0 6px 18px rgba(77, 158, 255, .16); }
.kb-topic-card-head { display: flex; align-items: center; gap: 8px; }
.kb-topic-icon { flex: none; font-size: 15px; color: #1677ff; display: grid; place-items: center; width: 26px; height: 26px; border-radius: 8px; background: #e8f3ff; }
.kb-topic-card-name { flex: 1; min-width: 0; font-size: 14px; font-weight: 600; color: #1d2129; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.kb-topic-desc { font-size: 11px; color: #4e5969; line-height: 1.55; }
.kb-topic-metric-group { margin-top: 14px; padding-top: 12px; border-top: 1px dashed #e5e6eb; }
.kb-topic-group-title-mt { margin-bottom: 6px; color: #4e5969; }
.kb-topic-rows { display: flex; flex-direction: column; gap: 5px; }
.kb-topic-row {
  display: flex; align-items: center; gap: 8px; padding: 6px 10px; border-radius: 9px;
  border: 1px solid transparent; background: #f7f8fa; cursor: pointer; transition: all .15s ease;
  font-size: 11.5px; color: #1d2129;
}
.kb-topic-row:hover { border-color: #c9cdd4; background: #fff; }
.kb-topic-row-name { font-weight: 600; color: #1677ff; white-space: nowrap; }
.kb-topic-row-q { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #86909c; }
.kb-metric-ask-static {
  display: inline-flex; align-items: center; justify-content: center; flex: none;
  width: 22px; height: 22px; border-radius: 7px; border: 1px solid #bcd8ff; background: #fff; color: #1677ff;
  cursor: pointer; transition: all .15s ease;
}
.kb-metric-ask-static:hover { background: #1677ff; color: #fff; }
.kb-topic-workbench { margin-top: 14px; padding: 12px; border: 1px solid #e5e6eb; border-radius: 12px; background: #f7f8fa; }
.kb-topic-wb-title { display: flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 700; color: #1d2129; margin-bottom: 8px; }
.kb-topic-wb-ask { margin-top: 12px; width: 100%; justify-content: center; }

/* ====== v6 场景主区：实时快照 / 指标全览 / 折叠支撑 ====== */
.kb-v6 { display: flex; flex-direction: column; gap: 14px; margin-top: 4px; }
.kb-v6-card { margin: 0; }
.kb-snap-head { display: flex; align-items: center; gap: 10px; }
.kb-snap-title { font-size: 16px; font-weight: 700; color: #1d2129; display: flex; align-items: center; gap: 7px; }
.kb-snap-sub { font-size: 11px; color: #86909c; }
.kb-snap-head .kb-manage-btn { margin-left: auto; }
.kb-snap-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.kb-snap-card {
  position: relative; display: flex; flex-direction: column; gap: 2px; text-align: left;
  background: #fff; border: 1px solid #e5e8ef; border-top: 3px solid #2E7CF0;
  border-radius: 12px; padding: 12px 15px 10px; cursor: pointer; transition: .15s;
}
.kb-snap-card:hover { box-shadow: 0 4px 14px rgba(31, 70, 140, .09); transform: translateY(-1px); }
.kb-snap-card.dim { border-top-color: #d0d7e2; }
.kb-snap-name { font-size: 12px; color: #5c6675; padding-right: 58px; }
.kb-snap-val { font-size: 24px; font-weight: 700; letter-spacing: -.5px; color: #1d2129; font-variant-numeric: tabular-nums; }
.kb-snap-card.dim .kb-snap-val { color: #a8b0bd; }
.kb-snap-foot { display: flex; align-items: center; gap: 8px; min-height: 16px; }
.kb-snap-unit { font-size: 11px; color: #a8b0bd; }
.kb-snap-def { font-size: 10.5px; color: #2E7CF0; background: #eef4fe; border: 1px solid #d7e6fb; padding: 0 9px; border-radius: 6px; }
.kb-snap-miss { position: absolute; right: 11px; top: 10px; font-size: 9.5px; color: #b45309; background: #fdf3e3; padding: 0 7px; border-radius: 4px; font-weight: 600; }
.kb-snap-loading { font-size: 12px; color: #86909c; padding: 18px 4px; }
.kb-fg { border: 1px solid #eef1f6; border-radius: 10px; padding: 9px 12px 12px; margin-bottom: 10px; }
.kb-fg-h { display: flex; align-items: center; gap: 9px; margin-bottom: 8px; }
.kb-fg-g { font-size: 12px; font-weight: 700; color: #2a3342; }
.kb-fg-t { font-size: 10px; color: #7a8699; }
.kb-fg-live { margin-left: auto; font-size: 10px; color: #188351; display: flex; align-items: center; gap: 4px; }
.kb-fg-live i { width: 6px; height: 6px; border-radius: 50%; background: #2ecc8f; display: inline-block; }
.kb-fg-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
.kb-fg-item {
  position: relative; display: flex; flex-direction: column; gap: 2px; text-align: left;
  border: 1px solid #eef1f6; border-radius: 10px; padding: 8px 11px; background: #fff; cursor: pointer; transition: .12s;
}
.kb-fg-item:hover { background: #f4f9ff; border-color: #cddff8; }
.kb-fg-item.miss { background: #fbfcfe; }
.kb-fg-name { font-size: 11px; color: #5c6675; display: flex; align-items: center; gap: 6px; }
.kb-fg-name em { font-style: normal; font-size: 9px; color: #b45309; background: #fdf3e3; border-radius: 4px; padding: 0 6px; font-weight: 600; }
.kb-fg-val { font-size: 17px; font-weight: 700; color: #1d2129; font-variant-numeric: tabular-nums; line-height: 1.2; }
.kb-fg-item.miss .kb-fg-val { color: #a8b0bd; }
.kb-fg-unit { font-size: 10px; color: #a8b0bd; }
.kb-fg-suggest { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; font-size: 11px; color: #86909c; background: #f7f9fd; border: 1px dashed #d9e2f0; border-radius: 9px; padding: 7px 11px; }
.kb-sg-chip { font-size: 10.5px; background: #fff; border: 1px solid #d8e3f5; color: #55606e; border-radius: 999px; padding: 1px 10px; cursor: pointer; }
.kb-sg-chip:hover { border-color: #2E7CF0; color: #2E7CF0; }
.kb-rules-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 10px; }

/* ====== 我的收藏视图 ====== */
.kb-fav-head { display: flex; align-items: center; gap: 10px; padding: 6px 2px 12px; flex-wrap: wrap; }
.kb-fav-sub { font-size: 11.5px; color: #86909c; flex: 1; min-width: 180px; }
.kb-fav-list { display: flex; flex-direction: column; gap: 6px; }
.kb-fav-item { display: flex; align-items: center; gap: 12px; padding: 10px 14px; background: #fff; border: 1px solid #e5e6eb; border-radius: 10px; cursor: pointer; transition: border-color .15s, box-shadow .15s; }
.kb-fav-item:hover { border-color: #c9cdd4; box-shadow: 0 2px 10px rgba(16, 24, 40, .06); }
.kb-fav-kind { flex: none; width: 30px; height: 30px; display: flex; align-items: center; justify-content: center; border-radius: 8px; background: #f2f6ff; color: #4661c6; }
.kb-fav-main { flex: 1; min-width: 0; }
.kb-fav-title { font-size: 14px; font-weight: 700; color: #1d2129; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.kb-fav-meta { display: flex; align-items: center; gap: 10px; margin-top: 3px; font-size: 11px; color: #86909c; flex-wrap: wrap; }
.kb-fav-tag { background: #f2f3f5; border-radius: 4px; padding: 1px 6px; color: #4e5969; }
.kb-fav-actions { flex: none; display: flex; align-items: center; }
.kb-fav-actions .kb-term-action { width: 28px; height: 28px; display: inline-flex; align-items: center; justify-content: center; }
.kb-fav-actions .kb-term-action.fav { color: #d8a700; }

.kg-zh-origin p { margin: 0; }
</style>
