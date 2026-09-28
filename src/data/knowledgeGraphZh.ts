/**
 * knowledgeGraphZh.ts —— 知识图谱节点的中文业务解释
 *
 * 口径来源：《数据介绍.md》（制造业生产质量分析数据集，2026-06-01 ~ 2026-07-15）
 *   - 第 2/3 节：文件清单（中文名 / 行数）与表关系
 *   - 第 4 节：各表字段中文含义
 *   - 第 5 节：常用指标口径
 *   - 第 6 节：枚举值说明
 *
 * 用途：业务知识页「知识图谱」点击节点时弹框展示中文解释（图谱原始描述为英文，
 * 这里统一替换为中文业务口径说明，保证非技术人员也能看懂）。
 */

export interface ZhColumn {
  /** 物理字段名 */
  name: string
  /** 中文含义 */
  zh: string
  /** 数据类型 */
  type: string
  /** 键类型：PK 主键 / FK 外键 */
  key?: 'PK' | 'FK'
  /** 外键指向的表 */
  ref?: string
}

export interface ZhEntry {
  /** 知识类型标签 */
  kind: '数据表' | '数据字段' | '业务指标' | '分析场景' | '分析主题'
  /** 中文名称 */
  title: string
  /** 物理表名 */
  table?: string
  /** 数据行数（数据介绍第 2 节） */
  rows?: number
  /** 业务场景 */
  scene?: string
  /** 一句话中文说明 */
  desc: string
  /** 指标口径公式 */
  formula?: string
  /** 计量单位 */
  unit?: string
  /** 字段中文含义 */
  columns?: ZhColumn[]
  /** 补充说明要点（枚举值 / 用法 / 注意） */
  points?: string[]
  /** 关联的数据表 */
  tables?: string[]
}

/** 图谱节点 id（LightRAG 中文实体名） → 中文解释 */
export const GRAPH_NODE_ZH: Record<string, ZhEntry> = {
  // ─────────────────────────────────── 数据表（10 张） ───────────────────────────────────
  产品表: {
    kind: '数据表', title: '产品主数据', table: 'dim_product', rows: 30, scene: '产品质量',
    desc: '记录产品的编码、名称、型号、类别等基础信息，是产品质量、库存等分析的产品维度表。',
    points: ['数据介绍：产品编码、名称、型号、类别等基础信息', '典型用法：关联工序产量/质量检验，按产品或产品类别统计良率与不良'],
    columns: [
      { name: 'product_id', zh: '产品 ID，主键', type: 'string', key: 'PK' },
      { name: 'product_code', zh: '产品编码', type: 'string' },
      { name: 'product_name', zh: '产品名称', type: 'string' },
      { name: 'product_model', zh: '产品型号', type: 'string' },
      { name: 'product_category', zh: '产品类别', type: 'string' },
      { name: 'unit', zh: '单位', type: 'string' },
      { name: 'is_active', zh: '是否启用', type: 'boolean' },
    ],
  },
  工序表: {
    kind: '数据表', title: '工序主数据', table: 'dim_process', rows: 8, scene: '工序质量',
    desc: '记录工序编码、工序顺序与标准良率，是工序质量与不良责任工序的维度表。',
    points: ['数据介绍：工序编码、工序顺序、标准良率', '典型用法：按工序汇总投入、合格、不良数量，评估工序能力'],
    columns: [
      { name: 'process_id', zh: '工序 ID，主键', type: 'string', key: 'PK' },
      { name: 'process_code', zh: '工序编码', type: 'string' },
      { name: 'process_name', zh: '工序名称', type: 'string' },
      { name: 'process_seq', zh: '工序顺序', type: 'int' },
      { name: 'standard_yield_rate', zh: '标准良率', type: 'decimal' },
      { name: 'is_key_process', zh: '是否关键工序', type: 'boolean' },
    ],
  },
  产线表: {
    kind: '数据表', title: '产线主数据', table: 'dim_production_line', rows: 6, scene: '生产趋势',
    desc: '记录产线、所属车间、负责人与产线状态，是产量趋势与设备影响分析的产线维度表。',
    points: ['数据介绍：产线、车间、负责人、状态', '枚举值：line_status = running 运行中 / maintenance 维护中'],
    columns: [
      { name: 'line_id', zh: '产线 ID，主键', type: 'string', key: 'PK' },
      { name: 'line_code', zh: '产线编码', type: 'string' },
      { name: 'line_name', zh: '产线名称', type: 'string' },
      { name: 'workshop_name', zh: '车间名称', type: 'string' },
      { name: 'line_manager', zh: '产线负责人', type: 'string' },
      { name: 'line_status', zh: '产线状态（running 运行中 / maintenance 维护中）', type: 'string' },
    ],
  },
  设备表: {
    kind: '数据表', title: '设备主数据', table: 'dim_equipment', rows: 48, scene: '设备影响',
    desc: '记录设备编码、设备类型、所属产线与设备状态，是设备停机影响分析的设备维度表。',
    points: ['数据介绍：设备、设备类型、所属产线、设备状态', '枚举值：equipment_status = running 运行中 / idle 空闲 / maintenance 维护中'],
    columns: [
      { name: 'equipment_id', zh: '设备 ID，主键', type: 'string', key: 'PK' },
      { name: 'equipment_code', zh: '设备编码', type: 'string' },
      { name: 'equipment_name', zh: '设备名称', type: 'string' },
      { name: 'equipment_type', zh: '设备类型', type: 'string' },
      { name: 'line_id', zh: '所属产线 ID', type: 'string', key: 'FK', ref: 'dim_production_line' },
      { name: 'install_date', zh: '安装日期', type: 'date' },
      { name: 'equipment_status', zh: '设备状态（running/idle/maintenance）', type: 'string' },
    ],
  },
  工单表: {
    kind: '数据表', title: '生产工单', table: 'mes_work_order', rows: 344, scene: '生产趋势',
    desc: '记录工单编号、产品、产线、计划数量与计划起止日期，是产量与工单执行分析的主表。',
    points: ['数据介绍：工单、产品、产线、计划数量、计划日期、工单状态', '枚举值：order_status = released 已下达 / in_progress 生产中 / completed 已完工 / closed 已关闭'],
    columns: [
      { name: 'work_order_id', zh: '工单 ID，主键', type: 'string', key: 'PK' },
      { name: 'work_order_no', zh: '工单编号', type: 'string' },
      { name: 'product_id', zh: '产品 ID', type: 'string', key: 'FK', ref: 'dim_product' },
      { name: 'line_id', zh: '产线 ID', type: 'string', key: 'FK', ref: 'dim_production_line' },
      { name: 'plan_qty', zh: '计划数量', type: 'int' },
      { name: 'start_date', zh: '计划开始日期', type: 'date' },
      { name: 'end_date', zh: '计划结束日期', type: 'date' },
      { name: 'order_status', zh: '工单状态（released/in_progress/completed/closed）', type: 'string' },
    ],
  },
  工序产量表: {
    kind: '数据表', title: '工序产量', table: 'mes_process_output', rows: 2752, scene: '生产趋势',
    desc: '按工单、工序、产线、日期记录投入、合格、不良、返工数量，是产量与良率类指标的事实表。',
    points: ['数据介绍：按工单、工序、产线、日期记录投入、合格、不良、返工数量', '枚举值：shift_code = D 白班 / N 夜班', '典型用法：按统计日期、产线汇总产量趋势；按工序汇总工序良率'],
    columns: [
      { name: 'output_id', zh: '产量记录 ID，主键', type: 'string', key: 'PK' },
      { name: 'work_order_id', zh: '工单 ID', type: 'string', key: 'FK', ref: 'mes_work_order' },
      { name: 'product_id', zh: '产品 ID', type: 'string', key: 'FK', ref: 'dim_product' },
      { name: 'process_id', zh: '工序 ID', type: 'string', key: 'FK', ref: 'dim_process' },
      { name: 'line_id', zh: '产线 ID', type: 'string', key: 'FK', ref: 'dim_production_line' },
      { name: 'stat_date', zh: '统计日期', type: 'date' },
      { name: 'input_qty', zh: '投入数量', type: 'int' },
      { name: 'good_qty', zh: '合格数量', type: 'int' },
      { name: 'defect_qty', zh: '不良数量', type: 'int' },
      { name: 'rework_qty', zh: '返工数量', type: 'int' },
      { name: 'shift_code', zh: '班次（D 白班 / N 夜班）', type: 'string' },
    ],
  },
  质量检验表: {
    kind: '数据表', title: '质量检验', table: 'qms_inspection', rows: 1376, scene: '不良原因',
    desc: '记录检验单号、产品、工序、抽检数量、不良数量与检验结果，是抽检不良率的来源表。',
    points: ['数据介绍：检验单、产品、工序、抽检数量、不良数量、检验结果', '枚举值：inspection_result = pass 合格 / fail 不合格', '典型用法：抽检不良率 = 不良数量之和 ÷ 抽检数量之和'],
    columns: [
      { name: 'inspection_id', zh: '检验记录 ID，主键', type: 'string', key: 'PK' },
      { name: 'inspection_no', zh: '检验单号', type: 'string' },
      { name: 'work_order_id', zh: '工单 ID', type: 'string', key: 'FK', ref: 'mes_work_order' },
      { name: 'product_id', zh: '产品 ID', type: 'string', key: 'FK', ref: 'dim_product' },
      { name: 'process_id', zh: '工序 ID', type: 'string', key: 'FK', ref: 'dim_process' },
      { name: 'inspection_date', zh: '检验日期', type: 'date' },
      { name: 'sample_qty', zh: '抽检数量', type: 'int' },
      { name: 'defect_qty', zh: '不良数量', type: 'int' },
      { name: 'inspection_result', zh: '检验结果（pass 合格 / fail 不合格）', type: 'string' },
    ],
  },
  不良明细表: {
    kind: '数据表', title: '不良明细', table: 'qms_defect_detail', rows: 2115, scene: '不良原因',
    desc: '记录每张检验单下的不良类型、数量、严重等级与责任工序，是不良排行与责任归属分析的明细表。',
    points: ['数据介绍：检验单下的不良类型、数量、严重等级、责任工序', '枚举值：severity_level = minor 轻微 / major 主要 / critical 严重', '典型用法：按「不良类型」汇总「不良数量」降序排行（不良类型排行）'],
    columns: [
      { name: 'defect_id', zh: '不良明细 ID，主键', type: 'string', key: 'PK' },
      { name: 'inspection_id', zh: '检验记录 ID', type: 'string', key: 'FK', ref: 'qms_inspection' },
      { name: 'defect_type', zh: '不良类型', type: 'string' },
      { name: 'defect_code', zh: '不良代码', type: 'string' },
      { name: 'defect_qty', zh: '不良数量', type: 'int' },
      { name: 'severity_level', zh: '严重等级（minor/major/critical）', type: 'string' },
      { name: 'responsible_process_id', zh: '责任工序 ID', type: 'string', key: 'FK', ref: 'dim_process' },
    ],
  },
  设备停机记录表: {
    kind: '数据表', title: '设备停机记录', table: 'eqp_downtime_record', rows: 336, scene: '设备影响',
    desc: '记录设备停机的开始/结束时间、停机分钟、停机原因与是否计划停机，用于停机时长类指标。',
    points: ['数据介绍：设备停机开始/结束时间、停机分钟、停机原因', '非计划停机时长 = 停机分钟之和，筛选「是否计划停机 = 否」', '典型用法：按日期和产线汇总停机时长，再与产线不良率对比'],
    columns: [
      { name: 'downtime_id', zh: '停机记录 ID，主键', type: 'string', key: 'PK' },
      { name: 'equipment_id', zh: '设备 ID', type: 'string', key: 'FK', ref: 'dim_equipment' },
      { name: 'line_id', zh: '产线 ID', type: 'string', key: 'FK', ref: 'dim_production_line' },
      { name: 'start_time', zh: '停机开始时间', type: 'datetime' },
      { name: 'end_time', zh: '停机结束时间', type: 'datetime' },
      { name: 'downtime_minutes', zh: '停机分钟', type: 'int' },
      { name: 'downtime_reason', zh: '停机原因', type: 'string' },
      { name: 'is_planned', zh: '是否计划停机', type: 'boolean' },
    ],
  },
  库存快照表: {
    kind: '数据表', title: '库存快照', table: 'inv_inventory_snapshot', rows: 1004, scene: '库存预警',
    desc: '记录产品每日在各仓库的可用库存、冻结库存与安全库存，用于库存低于安全线预警。',
    points: ['数据介绍：产品每日库存、仓库、可用库存、冻结库存、安全库存', '枚举值：warehouse_code = WH-A 成品一仓 / WH-B 成品二仓 / WH-QA 质量隔离仓', '预警口径：可用库存 < 安全库存 即低于安全库存'],
    columns: [
      { name: 'snapshot_id', zh: '库存快照 ID，主键', type: 'string', key: 'PK' },
      { name: 'snapshot_date', zh: '快照日期', type: 'date' },
      { name: 'product_id', zh: '产品 ID', type: 'string', key: 'FK', ref: 'dim_product' },
      { name: 'warehouse_code', zh: '仓库编码（WH-A/WH-B/WH-QA）', type: 'string' },
      { name: 'available_qty', zh: '可用库存', type: 'int' },
      { name: 'frozen_qty', zh: '冻结库存', type: 'int' },
      { name: 'safety_stock_qty', zh: '安全库存', type: 'int' },
    ],
  },

  // ─────────────────────────────────── 数据字段（主键 / 外键） ───────────────────────────────────
  产品ID: {
    kind: '数据字段', title: '产品 ID', desc: '产品唯一标识，在 dim_product 中是主键，在其他表中作外键用于关联产品维度。',
    tables: ['dim_product', 'mes_work_order', 'mes_process_output', 'qms_inspection', 'inv_inventory_snapshot'],
    points: ['dim_product 中为主键（PK）', 'mes_work_order / mes_process_output / qms_inspection / inv_inventory_snapshot 中为外键（FK）'],
  },
  工序ID: {
    kind: '数据字段', title: '工序 ID', desc: '工序唯一标识，在 dim_process 中是主键，用于关联工序维度。',
    tables: ['dim_process', 'mes_process_output', 'qms_inspection'],
    points: ['dim_process 中为主键（PK）', '不良明细中另用「责任工序 ID」标注责任工序'],
  },
  产线ID: {
    kind: '数据字段', title: '产线 ID', desc: '产线唯一标识，在 dim_production_line 中是主键，用于按产线汇总产量、停机与质量数据。',
    tables: ['dim_production_line', 'dim_equipment', 'mes_work_order', 'mes_process_output', 'eqp_downtime_record'],
    points: ['dim_production_line 中为主键（PK）', '其余表中为外键（FK），是产量趋势与设备影响分析的分组维度'],
  },
  设备ID: {
    kind: '数据字段', title: '设备 ID', desc: '设备唯一标识，在 dim_equipment 中是主键，停机记录通过它关联设备。',
    tables: ['dim_equipment', 'eqp_downtime_record'],
    points: ['dim_equipment 中为主键（PK）', 'eqp_downtime_record 中为外键（FK）'],
  },
  工单ID: {
    kind: '数据字段', title: '工单 ID', desc: '生产工单唯一标识，在 mes_work_order 中是主键，工序产量与质量检验通过它关联工单。',
    tables: ['mes_work_order', 'mes_process_output', 'qms_inspection'],
    points: ['mes_work_order 中为主键（PK）', '工单状态取值：released / in_progress / completed / closed'],
  },
  产量记录ID: {
    kind: '数据字段', title: '产量记录 ID', desc: '工序产量记录唯一标识（mes_process_output 主键），每条记录对应工单+工序+产线+日期的一次产量填报。',
    tables: ['mes_process_output'],
    points: ['主键（PK）', '同行同时记录投入数量 / 合格数量 / 不良数量 / 返工数量'],
  },
  检验记录ID: {
    kind: '数据字段', title: '检验记录 ID', desc: '质量检验记录唯一标识（qms_inspection 主键），不良明细通过它挂在对应检验单下。',
    tables: ['qms_inspection', 'qms_defect_detail'],
    points: ['qms_inspection 中为主键（PK）', 'qms_defect_detail 中为外键（FK）'],
  },
  不良明细ID: {
    kind: '数据字段', title: '不良明细 ID', desc: '不良明细唯一标识（qms_defect_detail 主键），一条检验单可挂多条不同不良类型/等级的明细。',
    tables: ['qms_defect_detail'],
    points: ['主键（PK）', '记录不良类型、不良数量、严重等级'],
  },
  停机记录ID: {
    kind: '数据字段', title: '停机记录 ID', desc: '设备停机记录唯一标识（eqp_downtime_record 主键），每次停机事件一条记录。',
    tables: ['eqp_downtime_record'],
    points: ['主键（PK）', '同一行含停机开始时间 / 结束时间 / 停机分钟 / 是否计划停机'],
  },
  库存快照ID: {
    kind: '数据字段', title: '库存快照 ID', desc: '库存快照唯一标识（inv_inventory_snapshot 主键），每个产品每天每仓库一条快照。',
    tables: ['inv_inventory_snapshot'],
    points: ['主键（PK）', '库存水位判断：可用库存 < 安全库存 即低于安全库存'],
  },

  // ─────────────────────────────────── 业务指标 / 概念 ───────────────────────────────────
  产量: {
    kind: '业务指标', title: '产量', desc: '统计期内的产出总数，包含合格与不良品数量。',
    formula: '产量 = 合格数量之和 + 不良数量之和（工序产量表）',
    tables: ['mes_process_output'],
    points: ['数据来源：工序产量表', '可按统计日期、产线、工序分组'],
  },
  良率: {
    kind: '业务指标', title: '良率', desc: '投入数量中最终合格的比例，衡量工序或产品的整体质量水平。',
    formula: '良率 = 合格数量之和 ÷ 投入数量之和（工序产量表）',
    tables: ['mes_process_output'],
    unit: '%',
    points: ['工序良率同上口径，按工序分组查看', '可与 dim_process.standard_yield_rate 标准良率对比判断工序是否达标'],
  },
  不良率: {
    kind: '业务指标', title: '不良率', desc: '投入数量中判为不良的比例，是质量波动的核心监控指标。',
    formula: '不良率 = 不良数量之和 ÷ 投入数量之和（工序产量表）',
    tables: ['mes_process_output'],
    unit: '%',
    points: ['数据来源：mes_process_output 工序产量表', '注意与抽检不良率区分：后者分母是抽检数量'],
  },
  返工率: {
    kind: '业务指标', title: '返工率', desc: '投入数量中需要返工处理的比例，反映过程稳定性与返工损失。',
    formula: '返工率 = 返工数量之和 ÷ 投入数量之和（工序产量表）',
    tables: ['mes_process_output'],
    unit: '%',
  },
  工序良率: {
    kind: '业务指标', title: '工序良率', desc: '按工序统计的良率，用于定位瓶颈工序并与标准良率对标。',
    formula: '工序良率 = 合格数量之和 ÷ 投入数量之和（按工序分组）',
    tables: ['mes_process_output', 'dim_process'],
    unit: '%',
    points: ['数据介绍典型分析方向：按工序汇总投入、合格、不良数量', '对标 dim_process.standard_yield_rate 标准良率'],
  },
  抽检不良率: {
    kind: '业务指标', title: '抽检不良率', desc: '质量检验抽检中发现不良的比例，反映来料/在制质量水平。',
    formula: '抽检不良率 = 质量检验表·不良数量之和 ÷ 质量检验表·抽检数量之和',
    tables: ['qms_inspection'],
    unit: '%',
    points: ['数据来源：质量检验表（抽检数量 / 不良数量）'],
  },
  停机时长: {
    kind: '业务指标', title: '停机时长', desc: '统计期内设备累计停机分钟数，用于评估设备对产能与质量的影响。',
    formula: '停机时长 = 设备停机记录表·停机分钟之和',
    tables: ['eqp_downtime_record'],
    unit: '分钟',
    points: ['可按日期与产线汇总，再与产线不良率对比（数据介绍「设备影响」方向）', '长停机提示：单次停机 > 120 分钟需复盘停机原因'],
  },
  非计划停机时长: {
    kind: '业务指标', title: '非计划停机时长', desc: '仅统计非计划（突发故障）造成的停机分钟数，是设备可靠性的关键指标。',
    formula: '非计划停机时长 = 设备停机记录表·停机分钟之和，筛选「是否计划停机 = 否」',
    tables: ['eqp_downtime_record'],
    unit: '分钟',
    points: ['与计划停机分开看：计划停机属于正常保养/换型，非计划停机才是损失'],
  },
  停机原因: {
    kind: '业务指标', title: '停机原因', desc: '记录每次停机的具体原因，是停机 Pareto 分析的主要维度。',
    tables: ['eqp_downtime_record'],
    points: ['字段：eqp_downtime_record.downtime_reason', '典型用法：按停机原因汇总停机时长/次数，找出主要损失项'],
  },
  设备状态: {
    kind: '业务指标', title: '设备状态', desc: '设备当前所处状态，用于统计运行中/空闲/维护中的设备分布。',
    tables: ['dim_equipment'],
    points: ['枚举值：running 运行中 / idle 空闲 / maintenance 维护中'],
  },
  运行效率: {
    kind: '业务指标', title: '运行效率', desc: '设备实际可运行时间占比，用停机时长反推设备可用率。',
    formula: '设备可用率 ≈ 1 − 停机时长 / 计划运行时长',
    tables: ['eqp_downtime_record', 'dim_equipment'],
    unit: '%',
    points: ['数据集中无设备计划运行时长字段，实际口径可结合产线排班/工单计划日期推算'],
  },
  低于安全库存: {
    kind: '业务指标', title: '低于安全库存', desc: '可用库存低于安全库存的产品/仓库记录，用于触发补货预警。',
    formula: '可用库存 < 安全库存 → 触发补货预警（库存快照表）',
    tables: ['inv_inventory_snapshot'],
    points: ['数据介绍口径：低于安全库存 = 可用库存 < 安全库存', '分析粒度：按产品 + 仓库 + 快照日期'],
  },
  安全库存: {
    kind: '业务指标', title: '安全库存', desc: '为应对需求波动与补货周期设定的最低库存水位，是库存预警的阈值基准。',
    tables: ['inv_inventory_snapshot'],
    points: ['字段：inv_inventory_snapshot.safety_stock_qty', '与可用库存比较得出预警'],
  },
  库存水位: {
    kind: '业务指标', title: '库存水位', desc: '当前可用库存相对安全库存的高低程度，用于判断是否缺料或压库。',
    formula: '库存水位 = 可用库存 ÷ 安全库存（< 1 表示低于安全库存）',
    tables: ['inv_inventory_snapshot'],
    points: ['可用库存不含冻结库存', '质量隔离仓 WH-QA 的库存通常为待判定/隔离品'],
  },
  物料周转: {
    kind: '业务指标', title: '物料周转', desc: '衡量库存被消耗速度的指标，周转越快资金占用越低。',
    tables: ['inv_inventory_snapshot'],
    points: ['本数据集仅提供库存快照（无出入库流水），可用快照期间库存下降幅度近似观察周转'],
  },
  不良类型: {
    kind: '业务指标', title: '不良类型', desc: '对不良现象的分类，是不良原因分析（Pareto 排行）的核心维度。',
    formula: '不良类型排行 = 按不良明细表「不良类型」汇总「不良数量」并降序排序',
    tables: ['qms_defect_detail'],
    points: ['字段：qms_defect_detail.defect_type / defect_code', '配合严重等级（minor/major/critical）评估严重程度'],
  },
  不良分布: {
    kind: '业务指标', title: '不良分布', desc: '不良在责任工序、严重等级上的分布情况，用于定位问题来源。',
    tables: ['qms_defect_detail', 'dim_process'],
    points: ['按「责任工序」汇总，配合工序主数据的中文工序名', '按严重等级分层：critical 严重需优先处理'],
  },
  检验结果: {
    kind: '业务指标', title: '检验结果', desc: '检验单的判定结论，用于统计合格批次数与不合格批次占比。',
    tables: ['qms_inspection'],
    points: ['枚举值：pass 合格 / fail 不合格', '可用作批次合格率 = 合格批次数 / 检验批次数'],
  },
  产量趋势: {
    kind: '业务指标', title: '产量趋势', desc: '按日期汇总的产量走势，用于观察产出波动、缺料与产能爬坡。',
    formula: '按统计日期（可按产线 / 产品细分）汇总「合格数量 + 不良数量」（工序产量表）',
    tables: ['mes_process_output', 'dim_production_line'],
    points: ['数据时间范围：2026-06-01 ~ 2026-07-15', '最近 7 天建议按 2026-07-09 ~ 2026-07-15 理解'],
  },
  工单执行: {
    kind: '业务指标', title: '工单执行', desc: '工单从下达到完工的推进情况，用于跟踪计划达成与延期风险。',
    tables: ['mes_work_order'],
    points: ['工单状态：released 已下达 / in_progress 生产中 / completed 已完工 / closed 已关闭', '延期判定：实际完工晚于 end_date 计划结束日期'],
  },
  生产分析: {
    kind: '分析场景', title: '生产分析', desc: '围绕产量趋势、工序良率与工单执行展开的生产侧分析场景。',
    tables: ['mes_work_order', 'mes_process_output', 'dim_production_line', 'dim_product', 'dim_process'],
    points: ['数据介绍典型方向：按统计日期、产线汇总工序产量表观察生产趋势'],
  },
  质量分析: {
    kind: '分析场景', title: '质量分析', desc: '围绕不良类型、检验结果与不良分布展开的质量侧分析场景。',
    tables: ['qms_inspection', 'qms_defect_detail', 'dim_product', 'dim_process'],
    points: ['数据介绍典型方向：关联不良明细表与质量检验表分析不良原因'],
  },
  设备分析: {
    kind: '分析场景', title: '设备分析', desc: '围绕停机时长、设备状态与运行效率展开的设备侧分析场景。',
    tables: ['eqp_downtime_record', 'dim_equipment', 'dim_production_line'],
    points: ['数据介绍典型方向：按日期和产线汇总停机记录，再与产线不良率对比'],
  },
  库存分析: {
    kind: '分析场景', title: '库存分析', desc: '围绕库存水位、物料周转与安全库存展开的库存侧分析场景。',
    tables: ['inv_inventory_snapshot', 'dim_product'],
    points: ['数据介绍典型方向：查看可用库存是否低于安全库存'],
  },
}

/** 实体类型（LightRAG entity_type / 前端 nodeType） → 中文标签 */
export const ENTITY_TYPE_ZH: Record<string, string> = {
  data: '数据表',
  concept: '业务概念',
  UNKNOWN: '数据字段',
  entity: '实体',
  业务场景: '业务场景',
  业务对象: '业务对象',
  数据表: '数据表',
  数据字段: '数据字段',
  业务指标: '业务指标',
  业务规则: '业务规则',
  分析主题: '分析主题',
}

export const entityTypeZh = (type?: string): string => {
  const key = String(type || '').trim()
  if (!key) return '实体'
  return ENTITY_TYPE_ZH[key] || key
}

const _norm = (s: string) => s.replace(/\s+/g, '').toLowerCase()

/** 归一化索引：节点 id / 中文名 / 物理表名 / 大小写与空格差异 都可命中 */
const _INDEX = new Map<string, ZhEntry>()
Object.entries(GRAPH_NODE_ZH).forEach(([key, entry]) => {
  _INDEX.set(_norm(key), entry)
  if (entry.title) _INDEX.set(_norm(entry.title), entry)
  if (entry.table) _INDEX.set(_norm(entry.table), entry)
})

/** 按节点 id / 中文名 / 物理表名 查中文解释（查不到返回 null） */
export const findZhEntry = (idOrName?: string): ZhEntry | null => {
  if (!idOrName) return null
  return _INDEX.get(_norm(String(idOrName))) || null
}
