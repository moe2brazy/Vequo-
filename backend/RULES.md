# 口径与查询规则文档（RULES.md）

> 本项目（NL2SQL 智能问析）的口径定义、查询护栏与编译规则的**唯一事实来源**。
> 代码载体：`agent/metric_registry.py`（指标口径）、`agent/metric_compiler.py`（编译规则）、
> `db/executor.py`（查询护栏）、`agent/prompts.py` + `agent/llm_service.py`（意图规则）。
> 更新规则时须同步本文件与对应代码注释。

---

## 一、口径定义规则（metric_registry.py）

### 1.1 指标注册字段规范

每个指标必须包含以下字段，且满足对应要求：

| 字段 | 必填 | 口径要求 |
|---|---|---|
| `name` | ✅ | 指标主名称，全局唯一（与运行时 `metrics_registry.json` 合并后仍唯一） |
| `aliases` | ✅ | 近义词/别名列表（如 销售金额→[销售额,销售总额]），用于模糊匹配 |
| `unit` | ✅ | 单位（件/次/元/天/% 等），注入 prompt 与白盒展示 |
| `tables` | ✅ | **适用表，至少一张**。空表定义会污染 find_metrics（曾致"设备"坏指标误导 LLM），前后端双重校验 |
| `sql_expression` | ✅ | 聚合表达式（**无 SELECT**），须能被 sqlglot 以 `SELECT {expr}` 解析（语法校验） |
| `formula` | ✅ | 自然语言业务公式（如"合格数 ÷ 投入数 × 100%"） |
| `description` | ✅ | **口径白盒说明**：明确"口径：…"，跨表/复合指标必须附完整 SQL 结构指导 LLM 生成 |
| `dims` | 建议 | 支持的维度（产线/工序/产品/日期/状态…），与编译器 `_FACT_META` 对齐 |

### 1.2 口径分层原则

1. **单表可算**（指标落在单一白名单事实表）→ 编译器确定性编译（零 LLM，毫秒级）
2. **跨表/多表** → 编译器回退 LLM，但 `get_metric_hint` 会把 `description`（含完整 SQL 结构）以
   "指标口径定义（最高优先级）"注入 prompt，LLM 必须按注册口径生成
3. **未注册** → no_hit：弹窗二次确认（LLM 推断分析）→ 用户遵循执行/自定义口径/稍后处理

### 1.3 已注册指标清单（39 项内置 + 运行时）

| 域 | 指标 | 表 | 关键口径 |
|---|---|---|---|
| 生产 | 产量/投入量/良率/不良率/合格数/不良数/设备综合效率OEE | mes_process_output | 良率=good_qty/input_qty；OEE 按质量良率近似（缺计划时长字段，见代码注释） |
| 生产 | 工单数/计划数量/实际完成数量/工单完成率 | mes_work_order | 完成率=实际/计划 |
| 质量 | 抽检数/质检合格率/质检不合格率 | qms_inspection | 合格率=(抽检-不良)/抽检 |
| 质量 | 缺陷数 | qms_defect_detail | COUNT(*) |
| 设备 | 停机时长/停机次数/非计划停机次数 | eqp_downtime_record | 非计划=is_planned=FALSE |
| 库存 | 库存量/冻结库存/总库存/安全库存/缺货量 | inv_inventory_snapshot | 总库存=可用+冻结 |
| 库存 | 物料库存量/库存金额 | test_materials | 金额=stock_qty×unit_cost |
| 销售 | 销售金额/销售数量/订单数/**客单价** | test_orders | 客单价=SUM(quantity×unit_price)/COUNT(DISTINCT order_id) |
| 库存 | **库存周转天数** | inv_inventory_snapshot+test_orders | 日均库存×实际天数÷总销量（跨表，**禁止硬编码 90 天**） |
| 工厂 | 工厂数/总产能 | test_factories | SUM(capacity) |
| 123库 | 采购金额/生产不良率/缺勤天数/维护次数/维护费用/设备停机损失金额 | factory.* | 见各自 description |

### 1.4 编译器维度映射（_FACT_META）

- 维度两种形态：**JOIN 维度表**（产线→dim_production_line）或**直接分组字符串列**（状态/仓库/班次…）
- `mes_process_output` 维度：产线/产品/工序/**班次**（shift_code，2026-08-27 补注册）/ **车间**（2026-08-31，层级钻取）/ **工单**（work_order_id，2026-09-01）
- **维度别名**（同列多词，覆盖口语化问法，2026-09-01）：严重度/严重程度(severity)、原因/停机原因(reason)、状态/工单状态(status)——按长度降序匹配，长词命中短词自动去重
- **时间粒度分组**：`_want_date` 含「各日期/各天/逐日」→ `to_char(时间列,'YYYY-MM-DD')` 按天分组（2026-09-01）
- **防静默降级**：查询含「各X/按X/每个X/分X」分组信号但 X 未注册维度 → **必须回退 LLM**，
  禁止按"无维度总量"编译（曾致"各班次的产量"返回总量 9199 的答非所问 Bug）
- **「整个X/全部X/全X」是范围限定词（全量汇总语义），不是分组维度**（2026-08-31 修复：
  注册「车间」维度后「整个车间的总投入数量」曾被 `的{cn}` 模式误判为按车间分组 → 3 题回归）
- **「记录数/条数」= COUNT(\*) 语义**：已注册 COUNT 型指标（产出记录数/缺陷数/工单数/停机次数）直接命中编译；未命中 COUNT 指标的「记录数」仍回退 LLM（防误译成 SUM）
- **Top-1 查询**（2026-09-01）：数值型「X最高/最低/最多/最少的是哪个」→ `ORDER BY {指标} DESC/ASC LIMIT 1`；时间型「最早/最晚」仍回退 LLM（无法安全推断时间排序列）。`_detect_dim` 补「最多/最少/最大/最小的{cn}」模式；值过滤词表移除「最多」（与 Top 语义冲突）
- **二维分组**（2026-09-01）：`各A各B的X` 两维度均为「直接分组」列 → `GROUP BY col1, col2`；含 JOIN 维度表的二维回退 LLM；超过 2 维回退 LLM
- **车间维度**补齐到 `mes_work_order` / `eqp_downtime_record`（line_id→dim_production_line.workshop，2026-09-01）
- **物料总库存**（2026-09-01）：`物料库存量` 别名补「物料总库存/物料库存总量」；`_detect_dim` 的 `的{cn}` 模式加负向前瞻 `(?!总库存|库存|库存量|总量)`——「各供应商的物料总库存」里「的物料」不是维度（「物料」是「物料总库存」指标名前缀），否则误按「供应商×物料」二维分组（gold 只需按供应商）

### 1.5 指标类型扩展（P0-3，2026-08-31）

- `metric_type`：simple（缺省）/ ratio（比率，表达式含除法自动推断）/ cumulative（累计窗口，须 `accumulate.window`）/ conversion（转换率，须 `conversion.base_cond/target_cond`）
- 累计编译触发词（`_CUMULATIVE_INTENT_RE`）：**仅明确时间窗口累计**（本年累计/年初至今/本月累计/本季累计/近N天累计）
  ——裸「累计」（合计语义）不触发，保持既有行为（零回归）
- 累计编译形态：CTE 分组 + 窗口 SUM 累计序列（period + 累计值），mql 打标 `compiled_by=cumulative_compile`

### 1.6 多事实表确定性编译（P0-2，2026-08-31）

- L2 **compile_plan 声明式**：指标注册时声明 `compile_plan.sql_template`（确定性 SQL 骨架，
  含 `{t:列名}` 时间占位符）+ `compile_plan.time_cols`（列→表映射）。编译器只做时间条件参数填充，
  占位符集合必须与 time_cols 键一致（防漏过滤）；无时间词 → `列名 IS NOT NULL`。
  首个落地：**库存周转天数**（inv_inventory_snapshot + test_orders 双事实表 CTE，消除 LLM 兜底）
- L3 **外键图桥接**（`compile_multi_fact_bridge`）：两指标各落不同事实表 + 同一维度表 JOIN 定义时，
  用「维度表 + 两聚合子查询 LEFT JOIN」编译（防行数膨胀）。**默认关闭**（`METRIC_JOIN_AUTO=1` 开启）
- 红线：编译失败一律回退 LLM / 二次确认，绝不新增 LLM 直接执行 SQL 路径

### 1.7 维度层级钻取（P0-4，2026-08-31）

- 指标可声明 `dim_hierarchy`（父→子有序列表，每级必须是 `_FACT_META` 已注册维度）：
  - `产量` → [车间, 产线]（dim_production_line 同表列级：workshop → line_name）
  - `停机时长` → [设备类型, 设备]（dim_equipment 同表列级）
- 钻取能力：①问句带父级值前缀（「一车间各产线的产量」）→ `WHERE d.workshop='一车间'` 值过滤下钻；
  ②含「下钻/钻取」词 → 分组维度切到层级下一级；③命中非末级维度 → 返回 `drillable`（前端图表点击联动）
- 编译结果带 `drillable: {current_level, next_level, hierarchy, metric}`；前端 AskPage 点击分类自动下钻

---

## 二、查询护栏规则（db/executor.py）

| 护栏 | 值 | 说明 |
|---|---|---|
| 只读校验 | SELECT/WITH 开头 | 非 SELECT 直接拒绝（含 DROP/INSERT/UPDATE/DELETE） |
| 危险函数 | 词边界匹配 | pg_sleep/COPY/LOCK 等 DoS/提权函数拦截（剥离字符串字面量后匹配） |
| 多语句 | 仅单条 | `SELECT 1; DROP ...` 拦截 |
| 超时 | 10s（SQL_TIMEOUT_MS） | PG: SET LOCAL statement_timeout；MySQL: MAX_EXECUTION_TIME hint |
| 行数上限 | 5000（SQL_MAX_ROWS） | 结果超限截断 |
| EXPLAIN 闸门 | 100 万行（可配） | 执行前估算扫描行数，超阈值回灌 LLM 改写 |
| 参数占位符 | fill_param_placeholders | LLM 草稿 `BETWEEN %s AND %s` 自动按库内实际日期范围填充 |

## 三、意图规则（llm_service.py）

- `_classify_intent_rule` 规则优先 → `_classify_intent_llm` LLM 兜底
- 意图分诊：chat / gibberish / analyze_db / ml / lookup / data
- 统计意图词（`_STAT_INTENT_RE`）：多少/数量/金额/率/占比/趋势/平均/合计/次数/排行/环比/同比…
- 极简指标输入（≤8 字符命中 `_CLARIFY_METRICS`）→ 澄清候选，不硬猜
- **预测路由（P1-5，2026-08-31）**：未来时间词（未来/接下来/下周/下月/下季度…）+ 趋势/会/将/预估 信号
  → 意图 ml（LLM 细分 train/predict）；单独「走势/趋势」仍是历史查询（data），防回归

## 三之二、经营记忆（P1-2，2026-08-31）

- `agent/memory_center.py`：跨会话系统级记忆（对标 FineBI 经营记忆中心）
  - **指标偏好**：查询执行成功自动累计（指标×维度）组合频次，新会话注入 prompt（软约束）
  - **口径备注**：admin/analyst 人工录入（`/api/memory/notes` CRUD），命中指标时注入，ACL 隔离
  - 只注入、不执行——与「确定性编译为主」红线一致
- 报告资产（P1-4）：`agent/report_assets.py` + `/api/reports` CRUD + regenerate（一键再生成，
  按当前用户 ACL 重建上下文重跑生成链路）；admin/analyst 可见全部，普通角色仅自己

## 三之三、语义层自动构建（P0-1，2026-08-31）

- `agent/semantic_builder.py` + `/api/metrics/scan-schema`：扫描库表结构 → 候选指标
  - 确定性扫描：维度表识别（dim_ 前缀/被外键引用）+ 事实表识别（含数值列）+ 数值列 SUM/AVG 候选 + 时间列标注
  - LLM 只做 NL 命名（中文名/同义词/单位/口径）；候选并入 metric_miner 候选池（source=schema_scan）
  - **human-in-loop**：候选不自动入库，指标管理页人工采纳（口径正确性留给人）

## 三之四、最小编排器（P0-A 演示版，2026-08-31）

- `agent/orchestrator.py` + `/api/agent/orchestrate`（前端 AskPage「多步分析」按钮）
- 任务链规则（`plan_chain`，确定性词表）：归因（为什么/原因/下降…）+预测（下个月/未来…）
  → [查数, 归因, 预测, 报告]；单独归因/预测/洞察/其他 → 对应链路，末步均为报告汇总
- 节点复用现有能力：查数=LLMService(fast=True)（内部确定性编译优先+权限改写+二次确认红线）；
  归因=attribution.detect_and_attribute（规则）；预测=generate_predict；洞察=确定性数值统计；
  报告=LLM 只产 NL 汇总
- **不改变主链路**：独立端点，供参赛演示"Agent 会自己往下走"；失败/弹窗步标记 skipped 不阻断

## 四、数据模型限制（演示库 public schema）

| 限制 | 说明 | 处理 |
|---|---|---|
| 销售/库存无产品键 | test_orders 只有中文 product_name，库存/维度用编码 product_id，0 匹配 | 库存周转天数按**全库总量**口径；按产品分组需补 product_id 或切 factory 库 |
| 无计划运行时长 | OEE 完整口径（可用率×性能×良率）无法计算 | OEE 注册为质量良率近似，待补排产/节拍数据后更新 |
| 数据稀疏 | 演示库近 90 天仅 1 天快照 | 周期天数用实际快照天数，禁止硬编码 |
| 123 库指标 | factory.* 表不在当前连接库 | 切库后自动生效（get_real_tables 按库隔离） |

## 五、变更记录

| 日期 | 变更 |
|---|---|
| 2026-09-10 | **分析模板新增周期报告（周报 / 月报）**：`routers/knowledge.py::_BUILTIN_TEMPLATES` 追加 `tpl_005 生产周报`、`tpl_006 生产月报`（各 8 步，全走确定性编译；实测 8/8 命中、0 skip、0 空结果，约 0.3~0.5s）。**周期口径必须用「近7天 / 近30天」，不能用「本周 / 本月」**——`_build_time_filter` 中相对区间锚点是 `MAX(事实列)`（静态/滞后数据集同样出数），绝对日历周期锚点是 `CURRENT_DATE`（滞后库必空；该语义与评测口径一致，**不得改动**）。同时修正模板 id 生成策略：内置模板 id 显式声明且保持稳定，只给「无 id 的动态场景模板」补号（用户收藏与使用次数按 id 落库于 knowledge_user_data.json，整体重排会让历史收藏/统计错位）。`template_runner` 打通模板 step 的 `note` 字段（此前定义但从未渲染）→ 报告内以「口径说明」行展示统计范围，避免把全量口径（如 qms_defect_detail 无时间列）误读成周期口径。**周期覆盖降级与注释**：模板可声明 `period:{days}`（`label` 省略时由 days 生成「近 N 天」，避免两字段漂移；days 只驱动注释与覆盖检查，**真正的取数窗口由问句里的「近N天」文本决定，改周期须两处一起改**）；`template_runner._period_coverage` 一条查询同时取「窗口内首条记录 / 全表末条记录 / 全表去重天数 / 窗口内去重天数」，报告头部新增「统计口径」块逐表列出 `起止区间 + 周期内有数据天数（该表全量 N 天）`。窗口大于库内数据跨度时，编译层已自动「有几天用几天」（`近N天` 锚点是 `MAX(列)`，不会查空），缺的是把真实天数讲出来；数据最新日期早于系统当前日期时提示「统计的是数据末期区间，非当前周期」。
**注意一个坑**：判定「库内够不够一个周期」必须用 **全表去重天数 all_days**，不能用窗口内去重天数。窗口是 `MAX(时间列) - INTERVAL 'N-1 days'`，对 timestamp 列是滚动 N×24 小时、会在首日按时刻切一刀——实测 `eqp_downtime_record` 的 `MAX(start_time)=2026-07-15 19:10` → 窗口起点 `2026-07-09 19:10`，而 07-09 的 8 条记录全在此之前被排除，窗口内只数到 6 个日历日。首版拿它当覆盖天数，对数据充足的库**误报「数据不足一个完整周期」**，已修正。修正后 yans 库周报/月报均 `full=True`（四表全量各 45 天），仅在声明天数 > 库内跨度时才告警。旧模板 tpl_001~004 输出零变化 |
| 2026-09-01 | **第二批+第三批提分（评测驱动）**：①Top-1 查询（数值型「X最高/最低/最多/最少的是哪个」→ ORDER BY DESC/ASC LIMIT 1）；②二维分组（各A各B的X，两维度均为直接分组列→GROUP BY col1,col2）；③车间维度补到 mes_work_order/eqp_downtime_record；④物料库存量别名补「物料总库存/物料库存总量」+ `的{cn}` 加负向前瞻 `(?!总库存|库存|库存量|总量)`（防「供应商的物料总库存」误按供应商×物料分组）；⑤「每次」排除出防静默降级（`每(?!次|天|日|月|周|季度|年)`，「平均每次停机时长」是 AVG 非分组）；⑥注册「停机原因种类数」COUNT(DISTINCT reason)。postgres 大集 203 题 set 准确率 82.3%→84.2%→**86.7%**（176/203） |
| 2026-09-01 | **维度补齐（评测提分）**：`_FACT_META` 补「工单」（mes_process_output.work_order_id）维度 + 维度别名（严重程度/停机原因/工单状态）；`_want_date` 加「各日期/各天/逐日」→ to_char 按天分组；注册「产出记录数」COUNT 指标；移除过时的「记录数→回退 LLM」防误译保护（现由 COUNT 指标命中兜底）。postgres 大集 203 题 set 准确率 76.8%→82.3%（+11 题） |
| 2026-08-31 | **竞品差距补齐 S1+S2**：①指标类型扩展（metric_type 四型 + 累计窗口确定性编译，mql 打标 cumulative_compile）；②多事实表确定性编译（compile_plan 声明式模板，库存周转天数消除 LLM 兜底；L3 外键图桥接默认关）；③维度层级钻取（dim_hierarchy：产量[车间,产线]、停机时长[设备类型,设备]，值过滤下钻+下钻词+drillable 前端联动）；④语义层自动构建 Agent（semantic_builder 扫描库表→候选池，human-in-loop）；⑤经营记忆中心（指标偏好自动累计+口径备注 CRUD+prompt 注入）；⑥报告资产沉淀（/api/reports CRUD+再生成，角色可见性隔离）；⑦预测融入主链路（未来时间+趋势信号→ml）。新增单测 tests/test_metric_compiler_adv.py、tests/test_semantic_memory_report.py |
| 2026-08-27 | 注册「设备综合效率OEE」（质量良率近似口径）；注册「库存周转天数」（总量口径，禁硬编码 90）；注册「客单价」；补「班次」维度（shift_code）；编译器加防静默降级（未注册维度分组信号回退 LLM）；口径 SQL 语法校验（sqlglot）；适用表必填校验（前端+后端） |
| 2026-08-27 | 语义缓存 TTL 分级：按引用表类型（仅维度表 24h / 含事实表 15min，环境变量 SEMANTIC_CACHE_TTL_DIM/FACT 可配），条目记录引用表集合，命中按最严格 TTL 校验；embedding 三级策略补 n-gram 哈希向量兜底（停用词归一化 + 标点过滤，重复/换说法问题可命中），并支持独立 EMBEDDING_API_URL/KEY/MODEL 配置（不再依赖 LLM 接口是否有 embedding 能力） |
| 2026-08-27 | 编译器支持简单值过滤编译（_parse_value_filter：中文比较词/符号/百分号 → HAVING 聚合后过滤；_detect_dim 补「X的/的X」后缀维度表达）；新增 check_indexes.py 索引核查工具（扫无索引的高频过滤/JOIN 列，输出幂等 CREATE INDEX，可对真实库复用） |
| 2026-08-27 | 性能对标落地后实测：编译直通 1-39ms（含值过滤）、语义缓存命中 1.5ms（ngram 兜底）、LLM 路径 12.8s（日志）——详见《竞品对标性能分析报告.html》 |
| 2026-08-27 | 结果缓存 TTL 分级：cache_store.cache_set 支持条目级 ttl（默认 CACHE_TTL_SECONDS=300）；cache_result 按 SQL 引用表类型分级（仅维度表 24h / 含事实表 15min，复用 _entry_ttl）；内存模式过期判断改用条目自身 ttl。新增 backend/.env.example 环境变量模板（含 EMBEDDING_API_* / SEMANTIC_CACHE_* / CACHE_* / EXPLAIN_* / LLM_MAX_CONCURRENCY / WORKERS 全部可配置项说明） |
| 2026-08-27 | 选表规则优先闸门：_match_tables 重构——关键词匹配 + 真实库过滤后 ≤2 张且全部有关键词命中时（如"订单总数"→test_orders、"各工序的良率"→mes_process_output+dim_process）直接返回，跳过 LLM 选表调用（省 ~1-2s/次）；弱命中/歧义/无关键词（如"OEE趋势"）仍走 LLM 语义选表。校正逻辑抽为 _filter_real_tables 复用。实测 6 强命中全跳过且匹配正确、弱命中仍走 LLM、四套测试 83 用例无回归 |
| 2026-08-27 | 分析有损降级：_llm_analysis 加简单查询闸门——编译直通 / 语义缓存命中 / 简单单表（行数≤20、列数≤5、单表）时跳过 LLM 解读，用强化规则版 _quick_analysis（行数 + 数值统计 + TOP 维度洞察"X 最高为 Y（Z 值）"）；复杂/多表查询保留 LLM 解读。新增模块级 _is_number_like。实测编译直通/缓存命中/简单单表均跳过 LLM（LLM 不可用仍出分析），多表仍走 LLM |
| 2026-08-27 | 语义缓存持久化：条目同步写入 cache_store（key=sem:idx:{db_key}，配置 REDIS_URL 后跨重启/跨实例共享），_semantic_lookup 首次访问懒加载恢复内存（合并去重）；开关 SEMANTIC_CACHE_PERSIST（默认开）、每库持久化上限 SEMANTIC_CACHE_PERSIST_MAX（50）。实测模拟重启后仍命中、权限隔离保持 |
| 2026-08-27 | schema 列级裁剪（对齐 Skopx）：_build_schema_fast 新增 query 参数，run() 传入问题文本后按 _trim_schema_columns 裁剪无关字段——保留主键/JOIN键/时间列/数值列/*_name/*_code 维度列/通用维度词映射（状态→status、类型→type）/query 命中列；每表裁剪后 <4 字段整块保留（宁多勿漏）；query 为空或超 60 字符不裁剪。实测 6 类问题关键字段零丢失、多表问题压缩 15-19% |
