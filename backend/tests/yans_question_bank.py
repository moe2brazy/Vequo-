# -*- coding: utf-8 -*-
"""yans 大赛数据库题库（以 yans 为基准构建，覆盖 10 张表 × 指标 × 维度 × 排行 × 时间 × TOP-N）

每题：{q: 自然语言问句, expect: 期望形态}
  expect 取值: group(分组多行) / total(总量1行) / trend(趋势) / top(排行TOP) / rank(排行不限)
测试脚本据此校验：分组/排行题结果应 >1 行，总量题应 ==1 行，SQL 必须执行成功。
"""

QUESTION_BANK = [
    # ── 1. 工序产量表 mes_process_output（产量/良率/不良/投入/返工/班次）──
    {"q": "各工序的产量是多少", "expect": "group"},
    {"q": "各工序的良率是多少", "expect": "group"},
    {"q": "各工序的产量排行", "expect": "rank"},
    {"q": "产量最高的工序是哪个", "expect": "top"},
    {"q": "各工序的不良数", "expect": "group"},
    {"q": "各工序的投入数量", "expect": "group"},
    {"q": "各工序的返工数量", "expect": "group"},
    {"q": "各产线的产量", "expect": "group"},
    {"q": "各产线的良率", "expect": "group"},
    {"q": "产量最高的产线是哪条", "expect": "top"},
    {"q": "各产品的产量", "expect": "group"},
    {"q": "各产品的良率", "expect": "group"},
    {"q": "各班次的产量", "expect": "group"},
    {"q": "各工单的总合格产量", "expect": "group"},
    {"q": "各车间的产量", "expect": "group"},
    {"q": "总产量是多少", "expect": "total"},
    {"q": "全厂综合良率", "expect": "total"},
    {"q": "总不良数是多少", "expect": "total"},
    {"q": "总返工数量是多少", "expect": "total"},
    {"q": "近30天各产线的产量", "expect": "group"},
    {"q": "每日的产量趋势", "expect": "trend"},
    {"q": "本月的良率是多少", "expect": "total"},
    {"q": "产量大于1000的产线", "expect": "group"},
    {"q": "不良数最多的工序是哪个", "expect": "top"},
    {"q": "返工数量最多的产品", "expect": "top"},
    {"q": "各工序的产量TOP5", "expect": "top"},

    # ── 2. 不良明细表 qms_defect_detail（缺陷类型/严重度/工序/检验）──
    {"q": "各缺陷类型的缺陷数", "expect": "group"},
    {"q": "缺陷类型分布", "expect": "group"},
    {"q": "不良类型排行 TOP10", "expect": "top"},
    {"q": "缺陷数最多的不良类型是哪个", "expect": "top"},
    {"q": "各严重度的缺陷数", "expect": "group"},
    {"q": "严重程度分布", "expect": "group"},
    {"q": "各工序的缺陷数", "expect": "group"},
    {"q": "各检验的缺陷数", "expect": "group"},
    {"q": "总缺陷数是多少", "expect": "total"},
    {"q": "严重缺陷（critical）的数量", "expect": "total"},
    {"q": "各缺陷类型的缺陷件数排行", "expect": "rank"},

    # ── 3. 质量检验表 qms_inspection（检验结果/抽检/合格率）──
    {"q": "各检验结果的数量", "expect": "group"},
    {"q": "各产品的抽检数", "expect": "group"},
    {"q": "各工序的抽检数", "expect": "group"},
    {"q": "各产品的检验合格率", "expect": "group"},
    {"q": "检验合格率是多少", "expect": "total"},
    {"q": "抽检不良数是多少", "expect": "total"},
    {"q": "各产品的抽检不良数", "expect": "group"},
    {"q": "每日的检验数量趋势", "expect": "trend"},
    {"q": "抽检数最多的产品", "expect": "top"},

    # ── 4. 工单表 mes_work_order（工单状态/计划数量）──
    {"q": "各状态的工单数", "expect": "group"},
    {"q": "各工单状态的数量", "expect": "group"},
    {"q": "各产品的工单数", "expect": "group"},
    {"q": "各产线的工单数", "expect": "group"},
    {"q": "各车间的工单数", "expect": "group"},
    {"q": "工单总数是多少", "expect": "total"},
    {"q": "计划数量总和", "expect": "total"},
    {"q": "各产品的计划数量", "expect": "group"},
    {"q": "各产线的计划数量", "expect": "group"},
    {"q": "已完成状态的工单数", "expect": "llm"},
    {"q": "本月工单数", "expect": "total"},

    # ── 5. 设备停机表 eqp_downtime_record（停机原因/时长/次数）──
    {"q": "停机原因分析", "expect": "group"},
    {"q": "停机原因排行", "expect": "rank"},
    {"q": "停机总时长是多少", "expect": "total"},
    {"q": "停机次数是多少", "expect": "total"},
    {"q": "各停机原因的停机时长", "expect": "group"},
    {"q": "各停机原因的停机次数", "expect": "group"},
    {"q": "各设备的停机时长", "expect": "group"},
    {"q": "各设备的停机次数", "expect": "group"},
    {"q": "各设备类型的停机时长", "expect": "group"},
    {"q": "各产线的停机时长", "expect": "group"},
    {"q": "各车间的停机时长", "expect": "group"},
    {"q": "平均停机时长是多少", "expect": "total"},
    {"q": "停机最长的设备是哪个", "expect": "top"},
    {"q": "停机次数最多的设备", "expect": "top"},
    {"q": "计划停机总时长", "expect": "total"},
    {"q": "非计划停机总时长", "expect": "total"},
    {"q": "近7天停机时长", "expect": "total"},
    {"q": "停机时长超过100分钟的原因", "expect": "group"},
    {"q": "各设备的停机时长排行", "expect": "rank"},
    {"q": "停机原因种类数", "expect": "total"},
    {"q": "有多少种停机原因", "expect": "total"},

    # ── 6. 库存快照表 inv_inventory_snapshot（库存/预警/缺货）──
    {"q": "各产品的库存量", "expect": "group"},
    {"q": "各仓库的库存量", "expect": "group"},
    {"q": "总库存量是多少", "expect": "total"},
    {"q": "库存预警数", "expect": "total"},
    {"q": "各产品的库存预警", "expect": "group"},
    {"q": "各仓库的库存预警数", "expect": "group"},
    {"q": "安全库存合计", "expect": "total"},
    {"q": "缺货量是多少", "expect": "total"},
    {"q": "各产品的缺货量", "expect": "group"},
    {"q": "冻结库存合计", "expect": "total"},
    {"q": "库存量最高的产品", "expect": "top"},
    {"q": "库存预警最多的仓库", "expect": "top"},
    {"q": "每日库存变化趋势", "expect": "trend"},

    # ── 7. 设备主数据表 dim_equipment（设备状态/类型）──
    {"q": "设备总数是多少", "expect": "total"},
    {"q": "各设备状态的设备数", "expect": "group"},
    {"q": "各设备类型的设备数", "expect": "group"},
    {"q": "各产线的设备数", "expect": "group"},
    {"q": "运行中的设备数", "expect": "total"},
    {"q": "维护中的设备数", "expect": "total"},

    # ── 8. 产品主数据表 dim_product ──
    {"q": "产品总数是多少", "expect": "total"},
    {"q": "各产品类别的产品数", "expect": "group"},
    {"q": "产品类别分布", "expect": "llm"},

    # ── 9. 工序主数据表 dim_process ──
    {"q": "工序总数是多少", "expect": "total"},
    {"q": "各关键工序的数量", "expect": "llm"},
    {"q": "关键工序有多少个", "expect": "total"},
    {"q": "标准良率最高的工序", "expect": "top"},

    # ── 10. 产线主数据表 dim_production_line ──
    {"q": "产线总数是多少", "expect": "total"},
    {"q": "各车间的产线数", "expect": "group"},

    # ── 11. 跨表/综合（多指标、对比）──
    {"q": "产量和良率的对比", "expect": "group"},
    {"q": "停机时长和停机次数的关系", "expect": "group"},
    {"q": "各产线的产量与停机时长对比", "expect": "group"},
    {"q": "良率趋势", "expect": "trend"},
    {"q": "近7天的产量趋势", "expect": "trend"},
    {"q": "各产品类别的不良数", "expect": "group"},
    {"q": "不良数最多的产品类别", "expect": "top"},
    {"q": "各车间的停机原因排行", "expect": "llm"},
]
