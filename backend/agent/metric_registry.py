"""指标注册表（metric_registry）— 固定业务口径，防止 LLM 对指标口径自由发挥

背景：NL2SQL 的准确率瓶颈常在"同一指标多种算法"（如良率=合格/投入、产量=SUM 或 COUNT）。
通过注册表把关键指标的口径固化为 SQL 表达式，生成 SQL 时注入 prompt（优先级高于 few-shot），
并在结果复核时校验口径冲突。

- 内置种子指标只读（制造业通用口径，字段以 123 库 factory schema 为准）；
- 用户可通过 /api/metrics 管理自定义指标，持久化到 backend/metrics_registry.json。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

_logger = logging.getLogger("metric_registry")

_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "metrics_registry.json"

# ── 指标类型（P0-3 指标类型扩展，对齐白泽完整指标类型）──
#   simple      基础聚合（SUM/COUNT/AVG），缺省值（零回归）
#   ratio       比率（分子/分母），如 良率=合格/投入；可选显式 numerator/denominator 声明
#   cumulative  累计窗口（YTD/MTD/近N天累计），须带 accumulate: {"window": "year"|"month"|"quarter"|"day"}
#   conversion  转换率（目标状态/基准状态计数比），须带 conversion: {"base_cond": SQL条件, "target_cond": SQL条件}
_METRIC_TYPES = ("simple", "ratio", "cumulative", "conversion")
_CUMULATIVE_WINDOWS = ("year", "month", "quarter", "day")

# 语义召回口径的注入下限（2026-09-13 用户实测驱动）：
# metric_memory 默认 ABS_FLOOR=0.12 太低——「库存周转天数」能召回「库存分档统计」，
# 弱相关口径被当"注册口径参考"注入后 LLM 直接拿 MAX(库存量) 冒充周转天数。
# 关键词命中的口径是确定性的、不受此限；只有 RAG-only 命中（问题词与口径词
# 无任何精确重叠）才要求 combined 相似度达到该下限才允许注入。
# 0.40 ≈ 无 BM25 词重叠时需向量余弦 ~0.62 以上（真同义口径可达，蹭词的达不到）。
import os as _os
_HINT_RAG_FLOOR = float(_os.environ.get("METRIC_HINT_RAG_FLOOR", "0.40"))

# 内置种子指标（只读；表/字段名以当前演示库 postgres 的 public schema 为准）
# 说明：postgres 演示库（public schema）与 123 库（factory schema）表结构不同，
# 本列表主体为 postgres 库口径，123 库专用指标单独标注"(123库)"后缀避免误命中。
BUILTIN_METRICS: list[dict] = [
    # ── 生产域（mes_process_output：投入=合格+不良+损耗，逐工序良率链）──
    {
        "name": "产量",
        # 2026-09-29 摘除别名「产量占比」：本口径是绝对值（SUM good+defect），
        # 「占比」是除法语义，挂在这里会让「X产量占比」命中后把占比静默降级成绝对值。
        "aliases": ["产出量", "总产量", "产出", "产出数量", "产量最多", "实际产量",
                    # 2026-10-04：口语化的「生产趋势」问法（「分析一下这几个月的生产趋势」）
                    # 此前零命中 → 编译回退 LLM。属产量按时间的趋势口径。
                    "生产趋势", "产出趋势"],
        "unit": "件",
        "tables": ["mes_process_output"],
        # 企业口径（2026-09-09 数据介绍.md）：产量 = 合格 + 不良（总产出，不含损耗）
        "sql_expression": "SUM(COALESCE(good_qty, 0) + COALESCE(defect_qty, 0))",
        "formula": "产量 = 合格数量 + 不良数量（mes_process_output.good_qty + defect_qty 求和）",
        # P0-4 维度层级钻取：有序列表（父→子），每级须是编译器 _FACT_META 已注册维度。
        # 车间(dim_production_line.workshop) → 产线(line_name)，同表列级下钻。
        "dim_hierarchy": ["车间", "产线"],
        "description": "总产量（合格+不良，不含损耗；口径见数据介绍.md）。如需合格品口径请用「合格数量」指标",
        "dims": ["工序", "产线", "产品", "日期"],
    },
    {
        "name": "合格数量",
        "aliases": ["合格数", "合格品数量", "合格产量", "合格产出", "良品数量", "良品数", "完成数量"],
        "unit": "件",
        "tables": ["mes_process_output"],
        "sql_expression": "SUM(COALESCE(good_qty, 0))",
        "formula": "合格数量 = mes_process_output.good_qty 求和（企业口径：合格品产出）",
        "description": "合格数量（口径：mes_process_output.good_qty 求和；与「产量（合格+不良）」互补）",
        "dims": ["工序", "产线", "产品", "日期"],
    },
    {
        "name": "投入量",
        "aliases": ["投入数量", "投料量", "投产量"],
        "unit": "件",
        "tables": ["mes_process_output"],
        "sql_expression": "SUM(input_qty)",
        "formula": "SUM(input_qty)",
        # 2026-09-25 审计修正：原描述宣称「恒等式 input = good + defect」被 yans 实测推翻
        # （input 318.7万 vs good+defect 320.1万，差 13848），会诱导 LLM 用 good+defect 冒充投入量。
        "description": "投入数量合计（口径：mes_process_output.input_qty 求和；yans 实测 3187009，"
                       "与 good_qty+defect_qty(3200857) 不相等，不存在恒等关系，禁止用 good+defect 冒充投入量）",
        "dims": ["工序", "产线", "产品", "日期"],
    },
    {
        "name": "产出记录数",
        "aliases": ["产出记录条数", "生产记录数", "记录条数", "产出明细数"],
        "unit": "条",
        "tables": ["mes_process_output"],
        "sql_expression": "COUNT(*)",
        "formula": "COUNT(*)",
        "description": "生产产出记录条数（口径：mes_process_output 逐条记录计数）",
        "dims": ["工序", "产线", "产品", "工单", "日期"],
    },
    {
        "name": "损耗量",
        "aliases": ["损耗", "投入产出损耗", "物料损耗"],
        "unit": "件",
        "tables": ["mes_process_output"],
        "sql_expression": "SUM(input_qty) - SUM(good_qty)",
        "formula": "投入量 - 合格量",
        # 2026-09-25 审计修正：yans 实测 损耗(77889) ≠ 不良数(91737)，两者不同口径禁止混用
        "description": "投入产出损耗（口径：input_qty - good_qty；yans 实测 77889，"
                       "与「不良数」defect_qty(91737) 不是同一指标，禁止互相冒充）",
        "dims": ["工序", "产线", "产品", "日期"],
    },
    {
        "name": "良率",
        # 2026-10-01 补别名：「全厂综合良率/综合良率」是整厂汇总口径（good_qty/input_qty，无维度），
        # 原表只有「全厂良率/整体良率」，实测「全厂综合良率」因「良率」左侧残留「综合」修饰语被
        # _left_modifier_suspect 判定为未注册口径而零命中 → 走 LLM。补整词别名直接命中。
        "aliases": ["合格率", "一次良率", "良品率", "全厂良率", "整体良率", "全厂综合良率", "综合良率", "良率是多少", "良率水平"],
        "unit": "%",
        "tables": ["mes_process_output"],
        "sql_expression": "SUM(good_qty) * 100.0 / NULLIF(SUM(input_qty), 0)",
        "formula": "合格数 / 投入数 × 100%",
        # 2026-09-29 审计修正：原描述断言「良率 + 不良率 = 100%」，前提是 input_qty = good_qty + defect_qty，
        # 而该前提已被 2026-09-25 对「投入量」的审计实测推翻（yans：3187009 ≠ 3200857），此处属漏改。
        "description": "合格数量占投入数量的百分比（口径：good_qty / input_qty；yans 实测 97.556%）。"
                       "注意分母是投入量，不是良率+不良率=100%——yans 实测两者相加为 100.43%，"
                       "因投入量与产出量（good+defect）本身不相等",
        "dims": ["工序", "产线", "产品", "日期"],
    },
    {
        "name": "不良率",
        # 2026-09-29 补别名：「不良品率」是产线上极常见的同义问法（原只有「不合格率/缺陷率」会落空）
        "aliases": ["不合格率", "缺陷率", "不良品率", "不良品比例"],
        "unit": "%",
        "tables": ["mes_process_output"],
        "sql_expression": "SUM(defect_qty) * 100.0 / NULLIF(SUM(input_qty), 0)",
        "formula": "不良数 / 投入数 × 100%",
        "description": "不良数量占投入数量的百分比（口径：defect_qty / input_qty）",
        "dims": ["工序", "产线", "产品", "日期"],
    },
    {
        "name": "不良数",
        "aliases": ["生产不良数", "不良数量", "不良品数",
                    # 2026-10-04：口语「查一下各工序的不良情况」零命中 → 编译回退 LLM。
                    "不良情况", "不良状况"],
        "unit": "件",
        "tables": ["mes_process_output"],
        "sql_expression": "SUM(defect_qty)",
        "formula": "SUM(defect_qty)",
        "description": "生产不良数量合计（口径：mes_process_output.defect_qty 求和，产线全检口径）",
        "dims": ["工序", "产线", "产品", "日期"],
    },
    # ── 质量域（qms_inspection 抽检 / qms_defect_detail 明细）──
    {
        "name": "抽检数",
        "aliases": ["抽样数", "检验数量", "抽检数量", "抽检了多少", "抽检了多少件"],
        "unit": "件",
        "tables": ["qms_inspection"],
        "sql_expression": "SUM(sample_qty)",
        "formula": "SUM(sample_qty)",
        "description": "质量抽检的抽样数量合计（口径：qms_inspection.sample_qty 求和）",
        "dims": ["产品", "工序", "日期"],
    },
    {
        "name": "检验次数",
        # 2026-10-02 补别名：全称「检验结果分布」（短别名「结果分布」左侧紧贴「检验」
        # 会被 find_metrics 的左侧修饰语守卫作废，必须注册全称才能命中）；
        # 「各检验结果的数量」编译正确性由 metric_compiler 的区间起点分组信号修复保证。
        "aliases": ["检验批次", "检验批次数", "检验记录数", "检验单数", "抽检次数", "检验结果的数量", "检验结果分布", "结果分布", "检验结论分布", "检验最多", "检验了几批", "抽检了几批", "检验了多少次"],
        "unit": "批",
        "tables": ["qms_inspection"],
        "sql_expression": "COUNT(*)",
        "formula": "COUNT(*)",
        "description": "质量检验批次/次数（口径：qms_inspection 记录数）",
        "dims": ["产品", "工序", "结果", "日期"],
    },
    {
        "name": "质检合格率",
        "aliases": ["检验合格率", "抽检合格率"],
        "unit": "%",
        "tables": ["qms_inspection"],
        "sql_expression": "SUM(sample_qty - COALESCE(defect_qty,0)) * 100.0 / NULLIF(SUM(sample_qty), 0)",
        "formula": "(抽样数 - 不良数) / 抽样数 × 100%",
        "description": "质量抽检合格率（口径：qms_inspection 的 (sample_qty - defect_qty) / sample_qty，用数值字段计算而非 result 计数）",
        "dims": ["产品", "工序", "日期"],
    },
    {
        "name": "抽检不良数",
        "aliases": ["检验不良数", "抽检缺陷数", "检验不合格数"],
        "unit": "件",
        "tables": ["qms_inspection"],
        "sql_expression": "SUM(COALESCE(defect_qty,0))",
        "formula": "SUM(defect_qty)",
        "description": "质量抽检的不良数量合计（口径：qms_inspection.defect_qty 求和）",
        "dims": ["产品", "工序", "日期"],
    },
    {
        "name": "质检不合格率",
        "aliases": ["检验不合格率", "抽检不合格率", "质检不良率"],
        "unit": "%",
        "tables": ["qms_inspection"],
        "sql_expression": "SUM(COALESCE(defect_qty,0)) * 100.0 / NULLIF(SUM(sample_qty), 0)",
        "formula": "不良数 / 抽样数 × 100%",
        "description": "质量抽检不合格率（口径：qms_inspection 的 defect_qty / sample_qty）",
        "dims": ["产品", "工序", "日期"],
    },
    {
        "name": "缺陷数",
        # 2026-10-01 修复：移除「不良类型」「不良类型数」两个别名——「不良类型」是维度名不是「记录条数」，
        # 挂在 COUNT(*) 口径的「缺陷数」上会劫持「各种不良类型主要出现在哪些产线」这类问法（命中缺陷数却
        # 无「产线」维度与桥接 → 编译失败回退 LLM，慢且答非所问）。已转交「不良类型排行」（带产线桥接）。
        # 2026-10-01 补别名：「严重程度分布」= 各严重度的缺陷数（dims 含「严重度」，维度别名「严重程度」已注册）。
        # 原表无此词，实测零命中走 LLM。
        "aliases": ["缺陷条数", "缺陷明细数", "不良记录数", "缺陷分布", "不良分布", "缺陷类型分布", "严重程度分布", "缺陷最多", "出了几个缺陷", "缺陷有几条",
                    # 2026-10-04：补「严重度分布」（词表里只有全称「严重程度分布」，
                    # 「各缺陷类型的严重度分布」因此零命中 → 编译回退 LLM）。
                    "严重度分布", "缺陷严重度分布"],
        # 2026-09-29 审计修正：原单位「次」不准确——本口径是 COUNT(*) 记录条数（yans 实测 2115 条），
        # 且易与「缺陷件数」(5273 件) 混淆，故改为「条」。
        "unit": "条",
        "tables": ["qms_defect_detail"],
        "sql_expression": "COUNT(*)",
        "formula": "COUNT(*)",
        "description": "质量缺陷明细记录数（口径：qms_defect_detail 逐条缺陷计数；单位是「条」记录，"
                       "与「缺陷件数」(defect_qty 求和，单位「件」) 是两个不同指标）",
        # 2026-09-29 修复：移除失效维度「产品」——qms_defect_detail 无 product_id 列，
        # _FACT_META 也未注册该表的「产品」映射（仅「产品类别」，且 yans 无 product_id），
        # 留着会导致「各产品的缺陷数」编译出坏 SQL。
        "dims": ["缺陷类型", "严重度", "工序"],
    },
    {
        "name": "缺陷件数",
        "aliases": ["缺陷数量", "缺陷总量", "缺陷总数", "不良件数", "缺陷有多少件"],
        "unit": "件",
        "tables": ["qms_defect_detail"],
        "required_cols": ["defect_qty"],
        "sql_expression": "SUM(COALESCE(defect_qty,0))",
        "formula": "Σ(defect_qty)",
        "description": "缺陷不良件数合计（口径：qms_defect_detail 的 defect_qty 求和，区别于「缺陷数」的记录条数）",
        "dims": ["缺陷类型", "严重度", "工序"],
    },
    {
        "name": "严重缺陷数",
        "aliases": ["严重缺陷数量", "重大缺陷数", "critical缺陷数", "严重不良数", "严重缺陷", "严重缺陷件数", "critical有几个"],
        "unit": "件",
        "tables": ["qms_defect_detail"],
        "required_cols": ["severity_level"],
        # 2026-09-29 审计修正：原口径只算 severity_level='critical'（1270 件），
        # 而用户口径「严重缺陷占比」用 critical+major（3356 件）——同一系统里「严重缺陷」两套口径，
        # 问「严重缺陷数」与「严重缺陷占比」会得到互相矛盾的答案。现统一为 critical+major，
        # 与占比口径及别名（重大缺陷数/严重不良数）的业务语义对齐。
        "sql_expression": "SUM(CASE WHEN severity_level IN ('critical', 'major') THEN COALESCE(defect_qty, 0) ELSE 0 END)",
        "formula": "Σ(严重度 ∈ {critical, major} 的 defect_qty 件数)",
        "description": "严重缺陷件数（口径：qms_defect_detail 中 severity_level 属于 critical 或 major 的"
                       " defect_qty 求和，与「严重缺陷占比」分母口径一致）。2026-09-14 曾以 yans 实测校正过"
                       "「记录数 vs 件数」（每条记录平均 2.49 件，记录数口径会低估）；2026-09-29 再校 severity 范围。"
                       "只要 critical 单一等级请用「致命缺陷数」",
        "dims": ["缺陷类型", "严重度", "工序"],
    },
    {
        "name": "致命缺陷数",
        "aliases": ["critical缺陷", "致命缺陷件数", "最高等级缺陷"],
        "unit": "件",
        "tables": ["qms_defect_detail"],
        "required_cols": ["severity_level"],
        # 2026-09-29 新增：把原先「严重缺陷数」里的 critical-only 口径单独保留，
        # 供只想看最高等级的追问使用，避免与 critical+major 口径冲突。
        "sql_expression": "SUM(CASE WHEN severity_level = 'critical' THEN COALESCE(defect_qty, 0) ELSE 0 END)",
        "formula": "Σ(严重度 = critical 的 defect_qty 件数)",
        "description": "最高严重等级（critical）的缺陷件数（口径：qms_defect_detail 中 severity_level='critical' "
                       "的 defect_qty 求和；yans 实测 1270 件。区别于含 major 的「严重缺陷数」）",
        "dims": ["缺陷类型", "严重度", "工序"],
    },
    # ── 工单域（mes_work_order）──
    {
        "name": "工单数",
        # 2026-09-29 补别名：「有多少工单」「多少工单」是最直白的问法，原表却只有「开了多少单」，
        # 实测「有多少工单」find_metrics 零命中（会走 LLM 直生，不稳定）。
        "aliases": ["工单总量", "工单数量", "工单总数", "工单状态的数量", "开了多少单", "工单有多少", "有多少单", "多少单", "工单状态", "各状态的工单", "各状态的工单各有多少", "工单状态分布", "有多少工单", "多少工单"],
        "unit": "单",
        "tables": ["mes_work_order"],
        "sql_expression": "COUNT(*)",
        "formula": "COUNT(*)",
        "description": "生产工单数量（口径：mes_work_order 记录数）",
        # 2026-09-29 补「工单状态」维度名：_detect_dim 对「工单状态分布」解析出「工单状态」，
        # 而本口径 dims 只有「状态」（两者映射同一列 order_status），维度名对不上导致回退 LLM。
        # 同族口径（在产/已完成/已取消工单数）的 dims 早已同时声明「状态」+「工单状态」，此处对齐。
        "dims": ["状态", "工单状态", "产线", "产品", "日期"],
    },
    {
        "name": "计划数量",
        "aliases": ["计划产量", "计划数", "计划了多少", "计划生产多少"],
        "unit": "件",
        "tables": ["mes_work_order"],
        "sql_expression": "SUM(plan_qty)",
        "formula": "SUM(plan_qty)",
        "description": "工单计划生产数量合计（口径：mes_work_order.plan_qty 求和）",
        "dims": ["产品", "产线", "日期"],
    },
    # ── 设备域（eqp_downtime_record）──
    {
        "name": "停机时长",
        # 2026-09-29 补别名：「停机了多少」是现场口语问法（回答的是时长，不是次数），原表只有「停了多久」。
        # 2026-10-01 补别名：「停机原因分析/分布/排行」= 各停机原因的停机时长（dims 含「停机原因」）。
        # 原表无此词，「停机原因」只是维度名不是别名，实测「停机原因分析」零命中走 LLM。
        "aliases": ["停机时间", "停机分钟", "停机总时长", "停了多久", "设备停了多久", "停机了多少", "一共停机多长时间",
                    "停机原因分析", "停机原因分布", "停机原因排行",
                    # 2026-10-04：「哪个车间的停机问题最严重」= 停机时长最多的车间，此前零命中。
                    "停机问题"],
        "unit": "分钟",
        "tables": ["eqp_downtime_record"],
        "sql_expression": "SUM(downtime_minutes)",
        "formula": "SUM(downtime_minutes)",
        # P0-4 维度层级钻取：设备类型 → 设备（dim_equipment 同表列级下钻）
        "dim_hierarchy": ["设备类型", "设备"],
        "description": "设备停机总时长（口径：eqp_downtime_record.downtime_minutes 求和）",
        # 2026-09-29 修复：「停机原因」是停机域最核心维度（_FACT_META 有映射），此前 dims 未声明，
        # 导致「各停机原因的停机时长」命中不了走 LLM；「是否计划」是失效名（映射表叫「计划类型」）。
        # 2026-10-01 补「车间」维度：停机表经 line_id→dim_production_line 桥接出 workshop_name，
        # 支撑「各车间的停机原因排行」这类跨维问法（_FACT_META 已注册该桥接，此前 dims 漏声明）。
        "dims": ["设备", "产线", "车间", "停机原因", "计划类型"],
    },
    # ── 2026-10-04 新增：分母可确定性推导的「率」类复合指标 ──────────────────────
    # 背景（用户产品决策）：AI 兜底路径的第一硬标准是「出结果」，不接受「拒绝生成」。
    # 「停机率」原被 _UNDERIVABLE_METRIC_RULES 判为「缺源字段 → 源头拒绝」，理由是库里
    # 没有「计划/日历工时」列。复核后确认**分母可由现有数据确定性推导**，属可算口径：
    #     日历工时 = 统计期天数 × 设备台数 × 1440（分钟/天/台）
    #       · 统计期天数 = eqp_downtime_record 的 (MAX(start_time)::date − MIN(start_time)::date)
    #       · 设备台数   = dim_equipment 记录数
    # 于是不再回退 LLM、更不拒绝，登记为确定性口径（exec_sql），走 render_exec_sql_metric。
    # 真库实测（yans，2026-08-01~09-15）：15600 分钟 ÷ (45 天 × 48 台 × 1440) → 停机率 0.50%、
    # 稼动率 99.50%；按设备类型 0.36%~0.80%、按产线 0.53%~0.67%，量级合理。
    {
        "name": "停机率",
        # 只收不与他项别名冲突的写法：「停机占比」会与「故障停机占比」构成子串冲突，故不收录。
        "aliases": ["停机时间占比", "设备停机率", "综合设备停机率", "设备综合停机率",
                    "全厂停机率", "设备停机时间占比"],
        "unit": "%",
        "tables": ["eqp_downtime_record", "dim_equipment"],
        "exec_sql": (
            'SELECT ROUND(SUM(d.downtime_minutes) * 100.0 / NULLIF('
            '(SELECT (MAX(start_time)::date - MIN(start_time)::date) FROM eqp_downtime_record)'
            ' * (SELECT COUNT(*) FROM dim_equipment) * 1440, 0), 2) AS "停机率" '
            'FROM eqp_downtime_record d'),
        "exec_sql_by_dim": {
            "_default": (
                'SELECT ROUND(SUM(d.downtime_minutes) * 100.0 / NULLIF('
                '(SELECT (MAX(start_time)::date - MIN(start_time)::date) FROM eqp_downtime_record)'
                ' * (SELECT COUNT(*) FROM dim_equipment) * 1440, 0), 2) AS "停机率" '
                'FROM eqp_downtime_record d'),
            "设备类型": (
                'SELECT e.equipment_type AS "设备类型", ROUND(SUM(d.downtime_minutes) * 100.0 / NULLIF('
                '(SELECT (MAX(start_time)::date - MIN(start_time)::date) FROM eqp_downtime_record)'
                ' * COUNT(DISTINCT e.equipment_id) * 1440, 0), 2) AS "停机率" '
                'FROM eqp_downtime_record d JOIN dim_equipment e ON e.equipment_id = d.equipment_id '
                'GROUP BY e.equipment_type ORDER BY "停机率" DESC'),
            "设备": (
                'SELECT e.equipment_name AS "设备", ROUND(SUM(d.downtime_minutes) * 100.0 / NULLIF('
                '(SELECT (MAX(start_time)::date - MIN(start_time)::date) FROM eqp_downtime_record)'
                ' * 1440, 0), 2) AS "停机率" '
                'FROM eqp_downtime_record d JOIN dim_equipment e ON e.equipment_id = d.equipment_id '
                'GROUP BY e.equipment_name ORDER BY "停机率" DESC'),
            "产线": (
                'SELECT l.line_name AS "产线", ROUND(SUM(d.downtime_minutes) * 100.0 / NULLIF('
                '(SELECT (MAX(start_time)::date - MIN(start_time)::date) FROM eqp_downtime_record)'
                ' * COUNT(DISTINCT e.equipment_id) * 1440, 0), 2) AS "停机率" '
                'FROM eqp_downtime_record d JOIN dim_equipment e ON e.equipment_id = d.equipment_id '
                'JOIN dim_production_line l ON l.line_id = e.line_id '
                'GROUP BY l.line_name ORDER BY "停机率" DESC'),
            "车间": (
                'SELECT l.workshop_name AS "车间", ROUND(SUM(d.downtime_minutes) * 100.0 / NULLIF('
                '(SELECT (MAX(start_time)::date - MIN(start_time)::date) FROM eqp_downtime_record)'
                ' * COUNT(DISTINCT e.equipment_id) * 1440, 0), 2) AS "停机率" '
                'FROM eqp_downtime_record d JOIN dim_equipment e ON e.equipment_id = d.equipment_id '
                'JOIN dim_production_line l ON l.line_id = e.line_id '
                'GROUP BY l.workshop_name ORDER BY "停机率" DESC'),
            "日期": (
                'SELECT to_char(d.start_time::date, \'YYYY-MM-DD\') AS "日期", '
                'ROUND(SUM(d.downtime_minutes) * 100.0 / NULLIF((SELECT COUNT(*) FROM dim_equipment) * 1440, 0), 2) AS "停机率" '
                'FROM eqp_downtime_record d GROUP BY d.start_time::date ORDER BY d.start_time::date'),
        },
        "formula": "SUM(downtime_minutes) ÷ (统计期天数 × 设备台数 × 1440) × 100",
        "description": "设备停机率（**日历工时口径**，yans 实测 0.50%）：停机总时长 ÷ 日历工时，"
                       "日历工时 = 统计期天数 × 设备台数 × 1440 分钟（天数取停机表时间跨度、"
                       "台数取 dim_equipment 记录数）。⚠️ 这是「停机时长占日历时间」的口径，"
                       "与「停机时长占计划运行时间」的车间口径在分母上不同——注册表无计划工时数据，"
                       "如需该口径请登记。确定性执行（exec_sql），不走 LLM。",
        "dims": ["设备类型", "设备", "产线", "车间", "日期"],
    },
    {
        "name": "稼动率",
        "aliases": ["开动率", "设备稼动率", "设备开动率", "设备运转率"],
        "unit": "%",
        "tables": ["eqp_downtime_record", "dim_equipment"],
        "exec_sql": (
            'SELECT ROUND(100 - SUM(d.downtime_minutes) * 100.0 / NULLIF('
            '(SELECT (MAX(start_time)::date - MIN(start_time)::date) FROM eqp_downtime_record)'
            ' * (SELECT COUNT(*) FROM dim_equipment) * 1440, 0), 2) AS "稼动率" '
            'FROM eqp_downtime_record d'),
        "exec_sql_by_dim": {
            "_default": (
                'SELECT ROUND(100 - SUM(d.downtime_minutes) * 100.0 / NULLIF('
                '(SELECT (MAX(start_time)::date - MIN(start_time)::date) FROM eqp_downtime_record)'
                ' * (SELECT COUNT(*) FROM dim_equipment) * 1440, 0), 2) AS "稼动率" '
                'FROM eqp_downtime_record d'),
            "设备类型": (
                'SELECT e.equipment_type AS "设备类型", ROUND(100 - SUM(d.downtime_minutes) * 100.0 / NULLIF('
                '(SELECT (MAX(start_time)::date - MIN(start_time)::date) FROM eqp_downtime_record)'
                ' * COUNT(DISTINCT e.equipment_id) * 1440, 0), 2) AS "稼动率" '
                'FROM eqp_downtime_record d JOIN dim_equipment e ON e.equipment_id = d.equipment_id '
                'GROUP BY e.equipment_type ORDER BY "稼动率" DESC'),
            "设备": (
                'SELECT e.equipment_name AS "设备", ROUND(100 - SUM(d.downtime_minutes) * 100.0 / NULLIF('
                '(SELECT (MAX(start_time)::date - MIN(start_time)::date) FROM eqp_downtime_record)'
                ' * 1440, 0), 2) AS "稼动率" '
                'FROM eqp_downtime_record d JOIN dim_equipment e ON e.equipment_id = d.equipment_id '
                'GROUP BY e.equipment_name ORDER BY "稼动率" DESC'),
            "产线": (
                'SELECT l.line_name AS "产线", ROUND(100 - SUM(d.downtime_minutes) * 100.0 / NULLIF('
                '(SELECT (MAX(start_time)::date - MIN(start_time)::date) FROM eqp_downtime_record)'
                ' * COUNT(DISTINCT e.equipment_id) * 1440, 0), 2) AS "稼动率" '
                'FROM eqp_downtime_record d JOIN dim_equipment e ON e.equipment_id = d.equipment_id '
                'JOIN dim_production_line l ON l.line_id = e.line_id '
                'GROUP BY l.line_name ORDER BY "稼动率" DESC'),
            "日期": (
                'SELECT to_char(d.start_time::date, \'YYYY-MM-DD\') AS "日期", '
                'ROUND(100 - SUM(d.downtime_minutes) * 100.0 / NULLIF((SELECT COUNT(*) FROM dim_equipment) * 1440, 0), 2) AS "稼动率" '
                'FROM eqp_downtime_record d GROUP BY d.start_time::date ORDER BY d.start_time::date'),
        },
        "formula": "100 − 停机率（日历工时口径）",
        "description": "设备稼动率（**日历工时口径近似**，yans 实测 99.50%）：100 − 停机率。"
                       "⚠️ 严格定义的「稼动率 = 运行时间 ÷ 计划时间」需要计划工时数据，本库没有，"
                       "此处以「日历时间 − 停机时间」占日历时间的比例近似，已在结果中声明。"
                       "确定性执行（exec_sql），不走 LLM。",
        "dims": ["设备类型", "设备", "产线", "车间", "日期"],
    },
    # ── 2026-10-05 新增：产能利用率 / 设备利用率 ──────────────────────────────
    # 为什么要登记（实测驱动，不是预防性加指标）：
    #   用户实测「产能利用率是多少」→ LLM 生成
    #     SELECT ... FROM mes_work_order WHERE w.order_status IN ('COMPLETED','IN_PROGRESS')
    #   返回 **NULL**，洞察层只能说「数据缺失，无法提供具体数值」。
    #   实测根因：**状态值大小写不匹配**——库里的值是小写 completed / in_progress，
    #   LLM 写的是大写 → WHERE 过滤掉全部行 → 分母为 NULL。
    #   而它本来选的口径（计划量 ÷ 产线×天数×8h×60）**根本不是产能利用率**：
    #   产能利用率的真分母是「设计产能 / 理论节拍」，本库既无 design_capacity 也无 cycle_time
    #   （已核 dim_equipment 7 列 / dim_production_line 6 列，均无基准列）。
    #   ⇒ 既要修可算性，也要修口径。
    # 本次登记的口径（**可确定性算出 + 显式声明差异**）：
    #   产能利用率 ≈ 投入产出率 = SUM(good_qty) ÷ SUM(input_qty) × 100
    #   为什么这个近似成立：投入量(input_qty) 本身已扣除工艺损耗，等于「实际吃进去的产能」；
    #   故 投入产出率 在多数场景下**高于**真产能利用率，但两者同向、可用于趋势与对比。
    #   yans 实测：97.56%。
    #   ⚠️ 局限已写进 description，且结果卡片会展示 —— 属「标注着估」，不是冒充。
    {
        "name": "产能利用率",
        # ⚠️ 别名刻意**不含**「设备利用率」：设备维度的利用率语义上等于稼动率
        # （运行时间 ÷ 可用时间），已由上面的「稼动率」用 exec_sql 精确覆盖。
        # 若把「设备利用率」收进本指标，问「设备利用率」会拿到「投入产出率」97.56%，
        # 而稼动率明明算得出来（99.50%）——那是**用一个可算口径换掉另一个可算口径**，
        # 比缺口径更糟。「设备利用率」无专属别名时交由 LLM 兜底 + 声明口径。
        "aliases": ["设备产能利用率", "产线利用率", "产能利用情况",
                    "综合产能利用率", "产能利用占比"],
        "unit": "%",
        "tables": ["mes_process_output", "dim_production_line"],
        "exec_sql": (
            'SELECT ROUND(SUM(COALESCE(good_qty, 0)) * 100.0 / '
            'NULLIF(SUM(COALESCE(input_qty, 0)), 0), 2) AS "产能利用率" '
            'FROM mes_process_output'),
        "exec_sql_by_dim": {
            "_default": (
                'SELECT ROUND(SUM(COALESCE(good_qty, 0)) * 100.0 / '
                'NULLIF(SUM(COALESCE(input_qty, 0)), 0), 2) AS "产能利用率" '
                'FROM mes_process_output'),
            "产线": (
                'SELECT d.line_name AS "产线", ROUND(SUM(COALESCE(f.good_qty, 0)) * 100.0 / '
                'NULLIF(SUM(COALESCE(f.input_qty, 0)), 0), 2) AS "产能利用率" '
                'FROM mes_process_output f JOIN dim_production_line d ON d.line_id = f.line_id '
                'GROUP BY d.line_name ORDER BY "产能利用率" DESC'),
            "车间": (
                'SELECT d.workshop_name AS "车间", ROUND(SUM(COALESCE(f.good_qty, 0)) * 100.0 / '
                'NULLIF(SUM(COALESCE(f.input_qty, 0)), 0), 2) AS "产能利用率" '
                'FROM mes_process_output f JOIN dim_production_line d ON d.line_id = f.line_id '
                'GROUP BY d.workshop_name ORDER BY "产能利用率" DESC'),
            "日期": (
                'SELECT to_char(f.stat_date, \'YYYY-MM-DD\') AS "日期", '
                'ROUND(SUM(COALESCE(f.good_qty, 0)) * 100.0 / '
                'NULLIF(SUM(COALESCE(f.input_qty, 0)), 0), 2) AS "产能利用率" '
                'FROM mes_process_output f GROUP BY f.stat_date ORDER BY f.stat_date'),
        },
        "formula": "SUM(good_qty) ÷ SUM(input_qty) × 100（投入产出率口径）",
        "description": "设备/产线产能利用率（**投入产出率近似口径**，yans 实测 97.56%）："
                       "合格产出 ÷ 投入产出。⚠️ **这不是严格定义的产能利用率**"
                       "（严格定义 = 实际产出 ÷ 设计产能，需「设计产能/理论节拍」基准，"
                       "本库 dim_equipment / dim_production_line 均无该字段）。"
                       "此处以「投入量已含工艺损耗」为依据做近似：通常**高于**真产能利用率，"
                       "但两者同向，适合看趋势与横向对比，不可直接用于产能考核。"
                       "确定性执行（exec_sql），不走 LLM。",
        "dims": ["产线", "车间", "日期"],
    },
    {
        "name": "停机次数",
        "aliases": ["停机记录数", "停机总次数", "停了几次机", "停机了几次", "停了多少次",
                    "多少次停机", "发生几次停机", "发生了多少次停机",
                    # 2026-10-04：补「停机最多」（「看看哪些设备停机最多」= 停机次数最多），
                    # 此前零命中 → 编译回退 LLM。
                    "停机最多", "故障最多"],
        "unit": "次",
        "tables": ["eqp_downtime_record"],
        "sql_expression": "COUNT(*)",
        "formula": "COUNT(*)",
        "description": "设备停机总次数（口径：eqp_downtime_record 记录数）",
        "dims": ["设备", "产线", "停机原因", "计划类型"],
    },
    {
        "name": "停机原因种类数",
        "aliases": ["停机原因种类", "停机原因数", "原因种类数", "多少种原因", "多少种停机原因", "停机原因有多少种"],
        "unit": "种",
        "tables": ["eqp_downtime_record"],
        "sql_expression": "COUNT(DISTINCT downtime_reason)",
        "formula": "COUNT(DISTINCT {downtime_reason|reason})",
        "description": "停机原因的种类数（口径：eqp_downtime_record 停机原因列去重计数，自动适配 reason/downtime_reason 命名）",
        "dims": [],
    },
    {
        "name": "非计划停机次数",
        "aliases": ["故障停机次数", "非计划停机数", "故障停了几次", "计划外停了几次"],
        "unit": "次",
        "tables": ["eqp_downtime_record"],
        "sql_expression": "SUM(CASE WHEN is_planned = FALSE THEN 1 ELSE 0 END)",
        "formula": "is_planned=FALSE 的记录数",
        "description": "非计划（故障）停机次数（口径：eqp_downtime_record 中 is_planned=FALSE 的记录数）",
        "dims": ["设备", "产线"],
    },
    {
        "name": "平均停机时长",
        "aliases": ["平均停机时间", "停机平均时长", "平均每次停机时长", "平均停了多久", "每次停多久"],
        "unit": "分钟",
        "tables": ["eqp_downtime_record"],
        "sql_expression": "AVG(downtime_minutes)",
        "formula": "AVG(downtime_minutes)",
        "description": "平均单次停机时长（口径：eqp_downtime_record 的 downtime_minutes 平均值）",
        "dims": ["设备", "产线", "车间", "停机原因", "计划类型"],
    },
    {
        "name": "非计划停机时长",
        # 2026-10-01 补别名：「非计划停机总时长」是最直白的 total 问法，原表只有「计划外停机时长」等，
        # 实测「非计划停机总时长」因多「总」字子串不匹配而零命中。
        "aliases": ["计划外停机时长", "非计划停机时间", "计划外停机时间", "非计划停机总时长"],
        "unit": "分钟",
        "tables": ["eqp_downtime_record"],
        "sql_expression": "SUM(CASE WHEN is_planned = FALSE THEN downtime_minutes ELSE 0 END)",
        "formula": "Σ(is_planned=FALSE 的停机分钟)",
        "description": "非计划停机总时长（口径：eqp_downtime_record 中 is_planned=FALSE 的 downtime_minutes 求和）",
        "dims": ["设备", "产线", "车间"],
    },
    {
        "name": "计划内停机次数",
        "aliases": ["计划停机次数", "计划内停机数", "计划保养次数"],
        "unit": "次",
        "tables": ["eqp_downtime_record"],
        "sql_expression": "SUM(CASE WHEN is_planned THEN 1 ELSE 0 END)",
        "formula": "is_planned=TRUE 的记录数",
        "description": "计划内停机次数（口径：eqp_downtime_record 中 is_planned=TRUE 的记录数）",
        "dims": ["设备", "产线", "车间"],
    },
    {
        "name": "计划内停机时长",
        # 2026-10-01 补别名：「计划停机总时长」是最直白的 total 问法，原表只有「计划停机时长」，
        # 实测「计划停机总时长」因多「总」字子串不匹配而零命中。
        "aliases": ["计划停机时长", "计划内停机时间", "计划保养时长", "计划停机总时长"],
        "unit": "分钟",
        "tables": ["eqp_downtime_record"],
        "sql_expression": "SUM(CASE WHEN is_planned THEN downtime_minutes ELSE 0 END)",
        "formula": "Σ(is_planned=TRUE 的停机分钟)",
        "description": "计划内停机总时长（口径：eqp_downtime_record 中 is_planned=TRUE 的 downtime_minutes 求和）",
        "dims": ["设备", "产线", "车间"],
    },
    {
        "name": "非计划停机占比",
        # 2026-09-29 摘除别名「故障停机占比」：字面「故障」指 downtime_reason='设备故障' 单一原因
        # （占 15.16%），而本口径是 is_planned=FALSE 的全部非计划原因（占 52.72%）——两者差 3.5 倍。
        # 该别名挂在这里会让「故障停机占比」被劫持到非计划口径（实测已发生），现归还给
        # 专管设备故障的「设备故障停机占比」。
        "aliases": ["计划外停机占比", "非计划停机比例", "计划外停机比例"],
        "unit": "%",
        "tables": ["eqp_downtime_record"],
        "sql_expression": "SUM(CASE WHEN is_planned = FALSE THEN downtime_minutes ELSE 0 END) * 100.0 / NULLIF(SUM(downtime_minutes), 0)",
        "formula": "非计划停机时长 / 停机总时长 × 100%",
        "description": "非计划停机时长占比（口径：is_planned=FALSE 停机分钟 / 总停机分钟 × 100%）",
        "dims": ["设备", "产线", "车间"],
    },
    # ── 库存域（inv_inventory_snapshot）──
    {
        "name": "库存量",
        # 2026-10-01（黄金题库 id45/57 批测修复）：inv_inventory_snapshot 是**每日快照表**，
        # 原算式 SUM(available_qty) 全历史累计（3366671）是快照重复累加出来的假库存，
        # 与最新快照真实值（73781，可用+冻结）差约 46 倍；「各仓库的库存量」「库存量最多
        # 的仓库」全中招。口径改为**最新快照日**的可用+冻结合计（与「各仓库库存量」「当前
        # 总库存量」对齐）。sql_expression 含子查询会被 _safe_metric_expr 判不可编译（防
        # 注入红线）→ 确定性执行由 exec_sql/exec_sql_by_dim 承担（与「安全库存达标率」同模式）。
        # 别名「可用库存」「库存变化」移除：前者语义应为不含冻结（防混），后者是时序语义
        # （趋势问法交 LLM 画每日序列，不该落到单值口径）。
        "aliases": ["库存数量",
                    # 2026-10-04：口语「库存变化」「库存情况」「还有多少货」零命中 → 编译回退 LLM。
                    # 均指库存量本身（含按时间的库存变化趋势），不改变口径语义。
                    "库存变化", "库存情况", "库存状况", "还有多少货"],
        "unit": "件",
        "tables": ["inv_inventory_snapshot"],
        # 2026-10-03 修复：原值是 `SUM(available_qty)` —— 已被本条 description 明确
        # 标注为「已废弃」的全历史口径（yans 实测 3366671，与正确值 73781 差 46 倍），
        # 但字段本身没跟着改，于是同一个指标并存两套算式：
        #   · exec_sql / exec_sql_by_dim 走「最新快照日 Σ(available+frozen)」= 73781
        #   · sql_expression 是全历史 SUM           = 3366671（与 formula/description 自相矛盾）
        # 而 get_metric_hint(:1509) 注入 LLM prompt 的正是 sql_expression 那个废弃口径：
        #   「口径算式 SQL = SUM(available_qty)；业务公式 = 最新快照日 Σ(available_qty + frozen_qty)」
        # 于是：① LLM 拿到的口径提示自相矛盾；② 任何绕过 exec_sql 的路径（多指标并列命中、
        # 用户手写列名、LLM 直生）都会用旧口径出数，**数值格式完全正常、无任何提示**。
        # 真库实测（yans，45 个快照日）：全历史 SUM = **3406851**，最新快照日 SUM = **73781**，
        # 差 46 倍。上面 description 里记的 3366671 是更早一次快照的实测值，同样是同一量级。
        #
        # 用 CASE WHEN 表达「只取最新快照日」而不是 `(SELECT MAX(snapshot_date) FROM t)`：
        # 后者含 SELECT 子句，会被 `_EXPR_FORBIDDEN_RE`（本轮为堵「COUNT(*) WHERE result=…」
        # 这类坏口径而加入的 WHERE/SELECT 黑名单）判为不安全 → 指标被编译器拒绝、
        # 静默回退 LLM。前者只含聚合与 CASE，且语义等价（MAX 在子查询外层会被 PG
        # 当聚合错误，这里用 CASE 是唯一能在表达式位置安全表达快照日的写法）。
        "sql_expression": "SUM(CASE WHEN snapshot_date = (SELECT MAX(snapshot_date) FROM inv_inventory_snapshot) THEN available_qty + COALESCE(frozen_qty,0) ELSE 0 END)",
        "exec_sql": "SELECT SUM(available_qty + COALESCE(frozen_qty,0)) AS \"库存量\" FROM inv_inventory_snapshot WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM inv_inventory_snapshot)",
        "exec_sql_by_dim": {
            "_default": "SELECT SUM(available_qty + COALESCE(frozen_qty,0)) AS \"库存量\" FROM inv_inventory_snapshot WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM inv_inventory_snapshot)",
            "仓库": "SELECT warehouse_code AS \"仓库\", SUM(available_qty + COALESCE(frozen_qty,0)) AS \"库存量\" FROM inv_inventory_snapshot WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM inv_inventory_snapshot) GROUP BY warehouse_code ORDER BY \"库存量\" DESC",
            "产品": "SELECT p.product_name AS \"产品\", SUM(i.available_qty + COALESCE(i.frozen_qty,0)) AS \"库存量\" FROM inv_inventory_snapshot i JOIN dim_product p ON p.product_id = i.product_id WHERE i.snapshot_date = (SELECT MAX(snapshot_date) FROM inv_inventory_snapshot) GROUP BY p.product_name ORDER BY \"库存量\" DESC",
            "日期": "SELECT to_char(snapshot_date, 'YYYY-MM-DD') AS \"日期\", SUM(available_qty + COALESCE(frozen_qty,0)) AS \"库存量\" FROM inv_inventory_snapshot GROUP BY snapshot_date ORDER BY snapshot_date",
        },
        "formula": "最新快照日 Σ(available_qty + frozen_qty)",
        "description": "当前库存合计（口径：inv_inventory_snapshot **最新快照日** 的 可用+冻结 求和，"
                       "yans 实测 73781：WH-A 43187 / WH-B 21000 / WH-QA 9594）。⚠️ 该表是每日快照，"
                       "直接全历史 SUM 会重复累加（旧口径 3366671 差 46 倍，已废弃）；库存随时间的"
                       "变化趋势请直接问「每天的库存量」（按快照日序列）。",
        "dims": ["仓库", "产品", "日期"],
    },
    {
        "name": "冻结库存",
        "aliases": ["冻结量", "冻结数量"],
        "unit": "件",
        "tables": ["inv_inventory_snapshot"],
        "sql_expression": "SUM(CASE WHEN snapshot_date = (SELECT MAX(snapshot_date) FROM inv_inventory_snapshot) THEN COALESCE(frozen_qty,0) ELSE 0 END)",
        "formula": "最新快照日 Σ(frozen_qty)",
        "description": "冻结库存合计（口径：**最新快照日**的 frozen_qty 求和）。"
                       "2026-10-04 修复：原表达式 `SUM(COALESCE(frozen_qty,0))` **不带日期条件**，"
                       "而 inv_inventory_snapshot 是每日快照表（yans 45 个快照日）→ "
                       "跨全部快照日累计。实测 **40180 vs 正确 820（49 倍）**，"
                       "而 description 当时只写了「不带日期条件会累计」的提示、**字段本身没改**，"
                       "于是确定性编译（0.1s、格式完全正常、无任何提示）下发给用户的就是 40180。"
                       "与「库存量」的同款 bug（2026-10-03 已修）同型。",
        "dims": ["仓库", "产品", "日期"],
    },
    {
        "name": "总库存",
        "aliases": ["库存总量", "总库存量", "总库存多少"],
        "unit": "件",
        "tables": ["inv_inventory_snapshot"],
        # 2026-10-04 修复（P0·用户可见矛盾）：
        # 本指标算式与「库存量」**完全相同**（Σ(available+frozen)），但口径相反 ——
        # 「库存量」收敛到最新快照日=73781，本指标跨 45 个快照日累加 = 3406851（46 倍）。
        # 而两者的业务别名高度重叠（「库存量」别名含「总产量」型问法，本指标别名含
        # 「总库存多少/库存总量/总库存量」），于是同一个业务问题会因措辞不同得到差 46 倍的答案：
        #   问「库存量是多少」→ 73781（对）；问「总库存是多少」→ 3406851（错）。
        # 2026-09-25 审计只改了 description（注明全历史口径 + 引导到「当前总库存量」），
        # 但表达式没改 → 用户仍会拿到错值。
        # 现在统一收敛到最新快照日；「跨快照累加」这种口径本身没有业务意义
        # （库存是时点值，不是流量），若确需历史累计应另起明确命名的指标。
        "sql_expression": "SUM(CASE WHEN snapshot_date = (SELECT MAX(snapshot_date) FROM inv_inventory_snapshot) THEN available_qty + COALESCE(frozen_qty,0) ELSE 0 END)",
        "formula": "最新快照日 Σ(available_qty + frozen_qty)",
        "description": "总库存 = 可用库存 + 冻结库存（口径：**最新快照日**求和，yans 实测 73781）。"
                       "2026-10-04 修复：原为全历史快照累加（3406851，差 46 倍），"
                       "且与「库存量」算式相同却口径相反，导致同一问题因措辞不同返回差 46 倍的结果"
                       "（实测「库存量是多少」→73781、「总库存是多少」→3406851）。"
                       "库存是时点值不是流量，跨快照累加无业务意义，已统一收敛。",
        "dims": ["仓库", "产品", "日期"],
    },
    {
        "name": "安全库存",
        "aliases": ["安全库存量"],
        "unit": "件",
        "tables": ["inv_inventory_snapshot"],
        "sql_expression": "SUM(CASE WHEN snapshot_date = (SELECT MAX(snapshot_date) FROM inv_inventory_snapshot) THEN safety_stock_qty ELSE 0 END)",
        "formula": "最新快照日 Σ(safety_stock_qty)",
        "description": "安全库存合计（口径：**最新快照日**的 safety_stock_qty 求和，yans 实测 16100）。"
                       "2026-10-04 修复：原表达式 `SUM(safety_stock_qty)` 不带日期条件，"
                       "跨 45 个快照日累计 → 实测 **879300 vs 正确 16100（54.6 倍）**。"
                       "⚠️ 旧 description 曾辩解「安全库存是配置值、数值稳定所以累计无妨」——"
                       "**该辩解已被数据否证**：真库检查发现每个产品都有 **4 个不同的"
                       " safety_stock_qty 值**（P001~P005 均为 4 个），说明期间安全线被上调过，"
                       "并非配置值恒定。库存/安全库存都是时点值，跨快照累加无业务意义。",
        "dims": ["仓库", "产品", "日期"],
    },
    {
        "name": "库存预警数",
        "aliases": ["库存预警", "预警数", "预警项数", "预警库存"],
        "unit": "项",
        "tables": ["inv_inventory_snapshot"],
        "sql_expression": "SUM(CASE WHEN snapshot_date = (SELECT MAX(snapshot_date) FROM inv_inventory_snapshot) AND available_qty < safety_stock_qty THEN 1 ELSE 0 END)",
        "formula": "Σ(最新快照日里 可用库存 < 安全库存 的行数)",
        "description": "库存低于安全库存的预警条目数（口径：**最新快照日**里 available_qty < safety_stock_qty 计数）。"
                       "2026-10-04 修复：原表达式 `SUM(CASE WHEN available_qty < safety_stock_qty THEN 1 ELSE 0 END)` "
                       "**不带日期条件**，而 inv_inventory_snapshot 是每日快照表（yans 45 个快照日）→ "
                       "同一产品只要连续 45 天预警不足，就被计 45 次，**这个数是错的**"
                       "（分母意义上的重复计数，不是「有多少种产品缺货」）。"
                       "已加上 snapshot_date = MAX(snapshot_date) 收敛到当前时点。"
                       "另：安全库存本身是配置值，某产品的安全线跨快照通常不变，故旧口径恰好"
                       "等于「当前预警数 × 快照日数」，很难被肉眼发现。yans 实测当前全部高于安全线，预警数=0。",
        "dims": ["仓库", "产品"],
    },
    # ── 返工域（mes_process_output.rework_qty，yans 大赛数据特有）──
    {
        "name": "返工率",
        "aliases": ["返工比例"],
        "unit": "%",
        "tables": ["mes_process_output"],
        "required_cols": ["rework_qty"],
        "sql_expression": "SUM(COALESCE(rework_qty,0)) * 100.0 / NULLIF(SUM(input_qty), 0)",
        "formula": "返工数量 / 投入量 × 100%",
        "description": "工序返工率（口径：mes_process_output 的 rework_qty / input_qty）",
        "dims": ["工序", "产线", "产品", "班次", "日期"],
    },
    # ── 设备主数据域（dim_equipment，yans 基准：状态/类型分布）──
    {
        "name": "设备数",
        # 2026-09-28 补语序变体：find_metrics 是**子串匹配**，原表只有「有多少台设备」，
        # 而口语里「设备一共有多少台」「设备总共多少台」「设备多少台」把数量短语后置，
        # 子串对不上 → 零命中 → 编译整体回退 LLM。实测「贴片机设备一共有多少台」因此走直生，
        # 且 LLM 挑错了表（拿 eqp_downtime_record 数"停过机的设备"，而非 dim_equipment 数设备总数）。
        # 补的这几条都是「设备」在前、数量短语在后的常见说法。
        "aliases": ["设备总量", "设备台数", "设备数量", "设备总数", "设备最多", "有几台设备", "有多少台设备", "几台设备", "设备总数和状态分布", "设备状态分布", "设备状态",
                    "设备一共有多少台", "设备总共有多少台", "设备总共多少台", "设备一共多少台", "设备多少台", "设备共有多少台"],
        "unit": "台",
        "tables": ["dim_equipment"],
        "sql_expression": "COUNT(*)",
        "formula": "COUNT(*)",
        "description": "设备总数（口径：dim_equipment 记录数）",
        "dims": ["设备状态", "设备类型", "产线"],
    },
    {
        "name": "运行设备数",
        "aliases": ["运行中设备数", "在运行设备数", "运转设备数", "运行中的设备数", "运行中设备", "几台在运行", "运行的有几台", "在运行的有几台"],
        "unit": "台",
        "tables": ["dim_equipment"],
        "required_cols": ["equipment_status"],
        "sql_expression": "SUM(CASE WHEN equipment_status = 'running' THEN 1 ELSE 0 END)",
        "formula": "Σ(状态=running 的设备数)",
        "description": "运行中设备数（口径：dim_equipment 中 equipment_status='running' 计数，确定性值过滤）",
        "dims": ["设备类型", "产线", "车间"],
    },
    {
        "name": "维护设备数",
        "aliases": ["维修设备数", "保养中设备数", "在修设备数", "维护中的设备数", "维修中的设备数"],
        "unit": "台",
        "tables": ["dim_equipment"],
        "required_cols": ["equipment_status"],
        "sql_expression": "SUM(CASE WHEN equipment_status = 'maintenance' THEN 1 ELSE 0 END)",
        "formula": "Σ(状态=maintenance 的设备数)",
        "description": "维护中设备数（口径：dim_equipment 中 equipment_status='maintenance' 计数，确定性值过滤）",
        "dims": ["设备类型", "产线", "车间"],
    },
    {
        "name": "空闲设备数",
        "aliases": ["闲置设备数", "待机设备数", "空闲中的设备数", "闲置中的设备数"],
        "unit": "台",
        "tables": ["dim_equipment"],
        "required_cols": ["equipment_status"],
        "sql_expression": "SUM(CASE WHEN equipment_status = 'idle' THEN 1 ELSE 0 END)",
        "formula": "Σ(状态=idle 的设备数)",
        "description": "空闲设备数（口径：dim_equipment 中 equipment_status='idle' 计数，确定性值过滤）",
        "dims": ["设备类型", "产线", "车间"],
    },
    {
        "name": "产品数",
        # 2026-10-01 补别名：「产品类别分布/排行」= 各产品类别的产品数（dims 含「产品类别」），
        # 原表只有「产品种类」等词，实测「产品类别分布」零命中走 LLM。
        "aliases": ["产品总量", "产品种类数", "产品种类", "产品总数", "产品类别分布", "产品类别排行"],
        "unit": "种",
        "tables": ["dim_product"],
        "sql_expression": "COUNT(*)",
        "formula": "COUNT(*)",
        # 2026-09-25 审计补注：30 = 产品目录总数（含 2 条停用），活跃 28，最新库存快照覆盖 20
        "description": "产品种类数（口径：dim_product 记录数；yans 实测 30 条目录，其中 2 条已停用"
                       "（控制器29/传感器30），活跃在产 28 种（见「活跃产品数」），最新库存快照覆盖 20 种）",
        "dims": ["产品类别"],
    },
    {
        "name": "活跃产品数",
        "aliases": ["在售产品数", "启用产品数", "有效产品数", "几个在售产品", "启用的有几个"],
        "unit": "种",
        "tables": ["dim_product"],
        "sql_expression": "SUM(CASE WHEN is_active THEN 1 ELSE 0 END)",
        "formula": "Σ(is_active=TRUE 的产品数)",
        "description": "活跃（启用）产品数（口径：dim_product 中 is_active=TRUE 计数，确定性值过滤）",
        "dims": ["产品类别"],
    },
    {
        "name": "工序数",
        "aliases": ["工序总量", "工序种类数", "工序总数"],
        "unit": "道",
        "tables": ["dim_process"],
        "sql_expression": "COUNT(*)",
        "formula": "COUNT(*)",
        "description": "工序总数（口径：dim_process 记录数）",
        "dims": ["工序"],
    },
    {
        "name": "关键工序数",
        # 2026-10-01 补别名：「关键工序的数量/各关键工序的数量」是带「的」的口语问法，
        # 原表只有「关键工序数量」，实测多「的」字即零命中。
        "aliases": ["重点工序数", "关键工序数量", "关键工序总数", "关键工序有多少个", "有多少个关键工序", "几道关键工序", "关键工序有几道", "各关键工序的数量", "关键工序的数量"],
        "unit": "道",
        "tables": ["dim_process"],
        "required_cols": ["is_key_process"],
        "sql_expression": "SUM(CASE WHEN is_key_process THEN 1 ELSE 0 END)",
        "formula": "Σ(is_key_process=TRUE 的工序数)",
        "description": "关键工序数（口径：dim_process 中 is_key_process=TRUE 计数，确定性值过滤，替代 LLM 语义漂移）",
        "dims": [],
    },
    {
        "name": "产线数",
        "aliases": ["产线总量", "产线条数", "产线总数"],
        "unit": "条",
        "tables": ["dim_production_line"],
        "sql_expression": "COUNT(*)",
        "formula": "COUNT(*)",
        # 2026-09-25 审计补注：dim 6 条，三车间-6号线无工单，实际投产 5 条
        "description": "产线总数（口径：dim_production_line 记录数；yans 实测 6 条，"
                       "其中「三车间-6号线」无任何生产工单，实际投产 5 条）",
        "dims": ["车间"],
    },
    {
        "name": "标准良率",
        "aliases": ["标准一次良率", "工序标准良率"],
        "unit": "%",
        "tables": ["dim_process"],
        # 2026-09-14：改为**存储无关**。实测两个库的存储口径与列名都不同：
        #   · postgres: dim_process.std_yield_rate      = 96.5 （已是百分数）
        #   · yans:     dim_process.standard_yield_rate = 0.965（小数）
        # 原写法 `MAX(standard_yield_rate) * 100.0` 对 yans 正确、对 postgres 会放大 100 倍
        # （实测生成 9650）。现按值域自适应：≤1.5 视为小数 → ×100 归一；否则视为已百分数 → 原样。
        "sql_expression": ("CASE WHEN MAX({standard_yield_rate|std_yield_rate}) <= 1.5 "
                           "THEN MAX({standard_yield_rate|std_yield_rate}) * 100.0 "
                           "ELSE MAX({standard_yield_rate|std_yield_rate}) END"),
        "formula": "MAX(标准良率列)；存储为小数（如 0.965）时 ×100 归一为百分数，已是百分数（如 96.5）时原样输出",
        # 2026-09-29 审计修正：原描述只说「取最大」，未点明这是「各工序标准良率中的最大值」——
        # 无维度限定时容易被当成「全厂标准良率基准」。yans 实测该最大值 = 99.20%（PR08 包装入库），
        # 是全厂最高的那道工序标准，不是全厂平均、也不是最低标准。
        "description": "各工序标准良率中的最大值（口径：dim_process 标准良率列取 MAX；兼容 standard_yield_rate"
                       "(小数) 与 std_yield_rate(百分数) 两种存储。yans 实测 99.20%，取自 PR08 包装入库工序，"
                       "仅代表最高的那道工序标准，**不是全厂标准良率基准**；要看某工序请加「按工序」维度）",
        "dims": ["工序"],
    },
    # ── 销售域 ──
    # 2026-09-29 审计说明：以下 8 条（销售域 4 条 / 物料域 2 条 / 工厂域 2 条）的事实表名
    # 是 CSV 导入期的历史遗留（test_orders / test_materials / test_factories），
    # 这三张表只存在于 postgres 库。**不需要额外字段限定**——get_effective_metrics 会经
    # _metric_applicable 按当前库的真实表名过滤，在 yans / 123 库下这批口径自动不可见
    # （实测 yans 下 get_effective_metrics 不含它们）。在 postgres 库下仍可正常使用。
    {
        "name": "销售金额",
        "aliases": ["销售额", "销售总额", "销售收入", "订单金额", "订单总额", "订单总金额"],
        "unit": "元",
        "tables": ["test_orders"],
        "sql_expression": "SUM(quantity * unit_price)",
        "formula": "Σ(数量 × 单价)",
        "description": "销售金额合计（口径：test_orders 的 quantity × unit_price，表内无预计算金额字段）",
        "dims": ["客户", "工厂", "产品", "日期"],
    },
    {
        "name": "客单价",
        "aliases": ["平均客单价", "单客消费额", "客单消费"],
        "unit": "元",
        "tables": ["test_orders"],
        "sql_expression": "SUM(quantity * unit_price) * 1.0 / NULLIF(COUNT(DISTINCT order_id), 0)",
        "formula": "销售金额 ÷ 订单数",
        "description": "客单价 = 总销售金额 ÷ 订单数（口径：SUM(quantity × unit_price) / COUNT(DISTINCT order_id)，仅统计已完成订单维度一致时可加 status 过滤）",
        "dims": ["客户", "工厂", "产品", "日期"],
    },
    {
        "name": "销售数量",
        "aliases": ["销量", "销售件数"],
        "unit": "件",
        "tables": ["test_orders"],
        "sql_expression": "SUM(quantity)",
        "formula": "SUM(quantity)",
        "description": "销售数量合计（口径：test_orders.quantity 求和）",
        "dims": ["客户", "产品", "日期"],
    },
    {
        "name": "订单数",
        "aliases": ["订单数量", "订单总量"],
        "unit": "单",
        "tables": ["test_orders"],
        "sql_expression": "COUNT(*)",
        "formula": "COUNT(*)",
        "description": "销售订单数量（口径：test_orders 记录数）",
        "dims": ["状态", "客户", "日期"],
    },
    # ── 物料域（test_materials）──
    {
        "name": "物料库存量",
        "aliases": ["物料库存", "原料库存量", "物料总库存", "物料库存总量", "物料总库存量"],
        "unit": "件",
        "tables": ["test_materials"],
        "sql_expression": "SUM(stock_qty)",
        "formula": "SUM(stock_qty)",
        "description": "物料库存数量合计（口径：test_materials.stock_qty 求和）",
        "dims": ["物料", "供应商"],
    },
    {
        "name": "库存金额",
        "aliases": ["库存价值", "物料库存金额", "库存总价值"],
        "unit": "元",
        "tables": ["test_materials"],
        "sql_expression": "SUM(stock_qty * unit_cost)",
        "formula": "Σ(库存数量 × 单位成本)",
        "description": "物料库存金额（口径：test_materials 的 stock_qty × unit_cost）",
        "dims": ["物料", "供应商"],
    },
    # ── 工厂域（test_factories）──
    {
        "name": "工厂数",
        "aliases": ["工厂数量"],
        "unit": "家",
        "tables": ["test_factories"],
        "sql_expression": "COUNT(*)",
        "formula": "COUNT(*)",
        "description": "工厂数量（口径：test_factories 记录数）",
        "dims": ["城市"],
    },
    {
        "name": "总产能",
        "aliases": ["产能合计", "总生产能力"],
        "unit": "件",
        "tables": ["test_factories"],
        "sql_expression": "SUM(capacity)",
        "formula": "SUM(capacity)",
        "description": "工厂总产能（口径：test_factories.capacity 求和）",
        "dims": ["城市"],
    },
    # ── 123 库（factory schema）专用指标，postgres 演示库无对应表 ──
    {
        "name": "采购金额(123库)",
        "aliases": ["采购额", "采购总额"],
        "unit": "元",
        "tables": ["factory.purchase_order_item", "factory.purchase_order", "factory.material"],
        "sql_expression": "SUM(poi.amount)",
        "formula": "SUM(amount)",
        "description": "采购金额（123 库 factory schema 专用，postgres 演示库无此表）",
        "dims": ["物料", "类别", "供应商", "日期"],
    },
    {
        "name": "生产不良率(123库)",
        "aliases": ["制造不良率", "车间不良率", "不良率"],
        "unit": "%",
        "tables": ["factory.production_record", "factory.production_line", "factory.workshop"],
        "sql_expression": "SUM(pr.qty_defect) / NULLIF(SUM(pr.qty_produced) + SUM(pr.qty_defect), 0) * 100",
        "formula": "不良数 / (产量 + 不良数) × 100%",
        "description": "生产不良率（123 库 factory schema 专用，postgres 演示库无此表）",
        "dims": ["产线", "车间", "日期"],
    },
    {
        "name": "缺勤天数(123库)",
        "aliases": ["缺勤", "请假天数"],
        "unit": "天",
        "tables": ["factory.attendance", "factory.employee"],
        "sql_expression": "SUM(CASE WHEN status <> '正常' THEN 1 ELSE 0 END)",
        "formula": "非正常出勤记录数",
        "description": "缺勤天数（123 库 factory schema 专用，postgres 演示库无此表）",
        "dims": ["员工", "部门", "月份"],
    },
    {
        "name": "维护次数(123库)",
        "aliases": ["维修次数", "保养次数"],
        "unit": "次",
        "tables": ["factory.equipment_maintenance", "factory.equipment"],
        "sql_expression": "COUNT(m.id)",
        "formula": "COUNT(*)",
        "description": "设备维护次数（123 库 factory schema 专用，postgres 演示库无此表）",
        "dims": ["设备", "类型", "日期"],
    },
    {
        "name": "维护费用(123库)",
        "aliases": ["维修费用", "保养费用"],
        "unit": "元",
        "tables": ["factory.equipment_maintenance"],
        "sql_expression": "SUM(m.cost)",
        "formula": "SUM(cost)",
        "description": "设备维护费用（123 库 factory schema 专用，postgres 演示库无此表）",
        "dims": ["设备", "类型", "日期"],
    },
    {
        "name": "产量(123库)",
        "aliases": ["产出量", "总产量", "生产量"],
        "unit": "件",
        "tables": ["factory.production_record"],
        "sql_expression": "SUM(pr.qty_produced)",
        "formula": "SUM(qty_produced)",
        "description": "产量（123 库 factory schema 专用）",
        "dims": ["产线", "车间", "日期", "班次"],
    },
    {
        "name": "不良数(123库)",
        "aliases": ["不良数量", "不良品数"],
        "unit": "件",
        "tables": ["factory.production_record"],
        "sql_expression": "SUM(pr.qty_defect)",
        "formula": "SUM(qty_defect)",
        "description": "不良数量（123 库 factory schema 专用）",
        "dims": ["产线", "车间", "日期", "班次"],
    },
    {
        "name": "质检合格率(123库)",
        "aliases": ["检验合格率", "抽检合格率"],
        "unit": "%",
        "tables": ["factory.quality_inspection"],
        "sql_expression": "SUM(qi.pass_qty) * 100.0 / NULLIF(SUM(qi.sample_qty), 0)",
        "formula": "合格数 / 抽样数 × 100%",
        "description": "质检合格率（123 库 factory schema 专用）",
        "dims": ["产品", "类别", "日期"],
    },
    {
        "name": "质检不合格率(123库)",
        "aliases": ["检验不合格率", "抽检不合格率"],
        "unit": "%",
        "tables": ["factory.quality_inspection"],
        "sql_expression": "SUM(qi.fail_qty) * 100.0 / NULLIF(SUM(qi.sample_qty), 0)",
        "formula": "不良数 / 抽样数 × 100%",
        "description": "质检不合格率（123 库 factory schema 专用）",
        "dims": ["产品", "类别", "日期"],
    },
    {
        "name": "工单完成率(123库)",
        "aliases": ["完工率", "完成率", "达成率"],
        "unit": "%",
        "tables": ["factory.work_order"],
        "sql_expression": "SUM(wo.produced_qty) * 100.0 / NULLIF(SUM(wo.plan_qty), 0)",
        "formula": "实际产量 / 计划产量 × 100%",
        "description": "工单完成率（123 库 factory schema 专用）",
        "dims": ["产线", "车间", "产品", "类别"],
    },
    {
        "name": "销售金额(123库)",
        "aliases": ["销售额", "销售总额", "销售收入"],
        "unit": "元",
        "tables": ["factory.sales_order_item"],
        "sql_expression": "SUM(soi.amount)",
        "formula": "SUM(amount)",
        "description": "销售金额（123 库 factory schema 专用）",
        "dims": ["产品", "类别", "客户", "日期"],
    },
    {
        "name": "销售数量(123库)",
        "aliases": ["销量", "销售件数"],
        "unit": "件",
        "tables": ["factory.sales_order_item"],
        "sql_expression": "SUM(soi.qty)",
        "formula": "SUM(qty)",
        "description": "销售数量（123 库 factory schema 专用）",
        "dims": ["产品", "类别", "客户", "日期"],
    },
    {
        "name": "采购数量(123库)",
        "aliases": ["采购量"],
        "unit": "件",
        "tables": ["factory.purchase_order_item"],
        "sql_expression": "SUM(poi.qty)",
        "formula": "SUM(qty)",
        "description": "采购数量（123 库 factory schema 专用）",
        "dims": ["物料", "类别", "供应商", "日期"],
    },
]

# 指标口径 SQL 中可能引用的"聚合字段"（用于口径冲突校验时核对 SQL 是否引用到）
_METRIC_FIELD_NAMES = (
    # postgres 演示库（public schema）
    "input_qty", "good_qty", "defect_qty", "available_qty", "frozen_qty",
    "safety_stock_qty", "sample_qty", "downtime_minutes", "is_planned",
    "plan_qty", "actual_qty", "quantity", "unit_price", "stock_qty",
    "unit_cost", "capacity",
    # 123 库（factory schema）
    "pass_qty", "fail_qty", "qty_produced", "qty_defect",
    "produced_qty", "amount", "safe_stock",
    "work_hours", "downtime_hours", "cost", "salary",
)


def _load_user_metrics() -> list[dict]:
    try:
        if _REGISTRY_PATH.exists():
            with open(_REGISTRY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
    except Exception as e:
        # 2026-10-03修复：原实现只 warning 然后 return []，而 save_user_metrics 是
        # 「截断→写」，一旦写坏文件，下次加载就静默返回空列表——全部用户指标消失，
        # 且无任何用户可见提示。现在保留损坏文件并显式告警，便于人工恢复。
        _logger.error("指标注册表加载失败（文件可能已损坏，已保留原文件以便恢复）: %s", e)
        try:
            bak = _REGISTRY_PATH.with_suffix(".json.corrupt")
            if _REGISTRY_PATH.exists() and not bak.exists():
                bak.write_bytes(_REGISTRY_PATH.read_bytes())
                _logger.error("已将损坏的注册表备份到 %s", bak)
        except Exception:
            pass
    return []


def save_user_metrics(metrics: list[dict]) -> None:
    """原子写：先写同目录临时文件再 os.replace 替换。

    2026-10-03 修复：原为 open(_REGISTRY_PATH, "w") 原地覆盖 —— 该模式会先 truncate
    原文件，json.dump 途中抛异常（不可序列化对象/磁盘满/进程被杀）就留下半截文件，
    配合 _load_user_metrics 的静默兜底会导致整个用户指标注册表清空且无法恢复。
    """
    tmp = _REGISTRY_PATH.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(metrics, f, ensure_ascii=False, indent=2)
            f.flush()
            _os.fsync(f.fileno())
        _os.replace(tmp, _REGISTRY_PATH)   # 同目录替换，原子生效
    except Exception:
        try:
            if tmp.exists():
                _os.unlink(tmp)
        except OSError:
            pass
        raise


def _infer_metric_type(metric: dict) -> str:
    """指标类型推断（P0-3）：显式声明优先；缺省按表达式特征推断。

    - 显式 metric_type 合法即采用（simple/ratio/cumulative/conversion）；
    - 表达式含除法且含聚合 → ratio（比率口径，如 良率=SUM(good_qty)/SUM(input_qty)）；
    - 带 accumulate 配置 → cumulative；带 conversion 配置 → conversion；
    - 其余 → simple（零回归）。
    """
    t = metric.get("metric_type")
    if t in _METRIC_TYPES:
        return t
    if metric.get("conversion"):
        return "conversion"
    if metric.get("accumulate") and metric["accumulate"].get("window") in _CUMULATIVE_WINDOWS:
        return "cumulative"
    expr = str(metric.get("sql_expression") or metric.get("formula") or "").upper()
    if "/" in expr and any(a in expr for a in ("SUM(", "COUNT(", "AVG(")):
        return "ratio"
    return "simple"


# P0 性能优化（2026-09-01）：get_all_metrics 结果缓存，key=注册表文件 mtime（增删改指标后自动失效）
_all_metrics_cache: dict = {"mtime": None, "value": None}


def get_all_metrics() -> list[dict]:
    """内置（只读）+ 用户自定义（可增删改），返回全部（含历史版本，供管理）。

    P0-3：统一注入 metric_type（显式声明或缺省推断），管理页/前端/编译器口径一致。
    P0 性能优化：按 metrics_registry.json 的 mtime 缓存结果（文件不变复用，管理页增删改后
    mtime 变化自动失效）；调用方均只读遍历（apply_metric_acl 改写时 `dict(m)` 新建），
    返回缓存列表不拷贝，消除每次深拷贝 + 类型推断开销。
    """
    mtime = None
    try:
        mtime = _REGISTRY_PATH.stat().st_mtime if _REGISTRY_PATH.exists() else None
    except Exception:
        mtime = None
    # 用 .get 而非直接下标：dict 被清空/被测试代码 reset 时不会 KeyError
    # （2026-10-04 自查时用 `_all_metrics_cache.clear()` 触发了 KeyError: 'mtime'。
    #  正常路径不会清这个 dict，但一行 .get 就能换来健壮性）。
    if _all_metrics_cache.get("mtime") == mtime and _all_metrics_cache.get("value") is not None:
        return _all_metrics_cache["value"]
    out = []
    for m in BUILTIN_METRICS + _load_user_metrics():
        item = dict(m)
        item["metric_type"] = _infer_metric_type(item)
        out.append(item)
    _all_metrics_cache["mtime"] = mtime
    _all_metrics_cache["value"] = out
    return out


# ── 指标血缘（改造6）：派生指标 -> 依赖的基础指标 + 派生表达式 ──
# 用户自定义指标可在注册时携带 depends_on（指标名列表），自动并入血缘图
_DERIVED: dict[str, dict] = {
    "良率": {"depends_on": ["合格数", "投入量"], "derived_from": "合格数 / 投入数 × 100%"},
    "不良率": {"depends_on": ["不良数", "投入量"], "derived_from": "不良数 / 投入数 × 100%"},
    "缺货量": {"depends_on": ["安全库存", "库存量"], "derived_from": "安全库存 - 可用库存（正值表示缺口）"},
    "质检合格率": {"depends_on": ["抽检数"], "derived_from": "(抽样数 - 不良数) / 抽样数 × 100%"},
    "质检不合格率": {"depends_on": ["抽检数"], "derived_from": "检验不良数 / 抽样数 × 100%"},
    "工单完成率": {"depends_on": ["实际完成数量", "计划数量"], "derived_from": "实际完成数量 / 计划数量 × 100%"},
    "总库存": {"depends_on": ["库存量", "冻结库存"], "derived_from": "可用库存 + 冻结库存"},
    "库存金额": {"depends_on": ["物料库存量"], "derived_from": "Σ(库存数量 × 单位成本)"},
    "销售金额": {"depends_on": ["销售数量"], "derived_from": "Σ(数量 × 单价)"},
    "生产不良率(123库)": {"depends_on": ["产量", "不良数"], "derived_from": "不良数 / (产量 + 不良数) × 100%"},
}


def get_metric_lineage() -> dict:
    """指标血缘（改造6）：返回 {nodes, edges}，供前端血缘图展示。

    - nodes: 全部指标（kind=base 基础 / derived 派生，附 formula 口径）
    - edges: source(依赖指标) -> target(派生指标)
    仅保留「依赖指标也存在」的边，避免悬挂引用。
    """
    all_m = get_all_metrics()
    name_set = {m["name"] for m in all_m}
    nodes: list[dict] = []
    edges: list[dict] = []
    for m in all_m:
        name = m["name"]
        rel = _DERIVED.get(name)
        if not rel:
            rel = {"depends_on": m.get("depends_on") or []}
        deps = rel.get("depends_on") or []
        nodes.append({
            "name": name,
            "kind": "derived" if deps else "base",
            "formula": m.get("formula", ""),
            "unit": m.get("unit", ""),
        })
        for d in deps:
            if d in name_set:
                edges.append({"source": d, "target": name})
    return {"nodes": nodes, "edges": edges}


def _parse_date(s: str):
    """解析 ISO 日期（2026-09-01），失败返回 None"""
    try:
        from datetime import date, datetime
        s = (s or "").strip()
        if not s:
            return None
        if "T" in s or " " in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        return date.fromisoformat(s)
    except Exception:
        return None


def _is_effective(metric: dict, asof=None) -> bool:
    """指标在指定日期（默认今天）是否有效。

    规则：valid_from/valid_to 均缺省 = 永久有效；
          valid_from 缺省 = 至今起有效；valid_to 缺省 = 自 valid_from 起永久有效；
          有效区间为 [valid_from, valid_to)。
    """
    if asof is None:
        from datetime import date
        asof = date.today()
    vf = _parse_date(metric.get("valid_from") or "")
    vt = _parse_date(metric.get("valid_to") or "")
    if vf and asof < vf:
        return False
    if vt and asof >= vt:
        return False
    return True


def _current_db_tables() -> set[str]:
    """当前数据库的真实表名集合（含裸名与 schema.table），供指标按库隔离。"""
    try:
        from db.tools import get_real_tables
        names = {t.get("table_name") for t in get_real_tables() if t.get("table_name")}
        bare = {n.split(".")[-1] for n in names}
        return names | bare
    except Exception:
        return set()


def _metric_applicable(metric: dict, current_tables: set[str] | None = None) -> bool:
    """指标是否适用于当前数据库（其事实表需存在于当前库；无表约束视为通用）。

    背景：内置种子指标的表名以 postgres 库 public schema 为准（如 mes_process_output），
    而 123 库用 factory schema（如 factory.production_record）。若不按当前库过滤，
    问 123 库时会把 postgres 口径注入 prompt，诱导 LLM 生成不存在表名的 SQL。

    current_tables: 调用方提前算好的当前库表名集合（get_effective_metrics 里只算一次，
    避免对每个指标重复调用 _current_db_tables；缺省时回退原逻辑，零行为变化）。
    """
    tables = [t for t in (metric.get("tables") or []) if t]
    if not tables:
        return True
    current = current_tables if current_tables is not None else _current_db_tables()
    if not current:
        return True  # 拿不到表列表时不过滤（降级为全部，避免误伤）
    if not any(t in current for t in tables):
        return False
    # 列级校验：required_cols 指定的列必须存在于该指标的事实表（当前库）
    # 例：yans 工单表无 actual_qty → 工单完成率/实际完成数量不适用；mes_process_output
    # 无 rework_qty → 返工数量不适用（postgres 演示库）。避免表在列缺生成坏 SQL。
    req = [c for c in (metric.get("required_cols") or []) if c]
    if req:
        try:
            from agent.metric_compiler import _table_cols
            for t in tables:
                if t in current and req and not all(c in _table_cols(t) for c in req):
                    return False
        except Exception:
            pass  # 拿不到列集合时跳过列级校验（降级）
    return True


def _resolve_candidates_in(m: dict) -> dict:
    """把口径文本里的 `{a|b}` 候选语法解析为**当前库真实列**（返回新 dict，不改入参）。

    2026-10-04 新增。`{standard_yield_rate|std_yield_rate}` 这类候选语法是给编译器
    `_resolve_expr` 用的，但本函数是「全链路唯一取指标的地方」，于是占位符原样流到
    下游展示层：
      · `get_metric_hint` 注入 LLM 的「业务公式」字段（`formula` 里也可能有占位符）
      · `try_compile_metric` 返回的 `mql.metric_expression` / `metrics[].definition`
        —— 这两项是前端「本次命中哪个指标、口径是什么」的可解释性展示与审计依据
    实测症状：同一段提示里「口径算式 SQL = MAX(standard_yield_rate)」是干净 SQL，
    「业务公式 = COUNT(DISTINCT {downtime_reason|reason})」却是内部模板语法。

    解析规则与编译器 `_resolve_expr` 对齐：按候选顺序取第一个真实存在的列，
    都不存在时保留第一个候选（交由编译器按同规则处理，行为不变）。
    查不到列结构时**原样返回**（绝不因为探测失败而改写口径文本）。

    ⚠️ 2026-10-04 自查修正（P0·切库后口径永久错库）：
    首版是「就地改」(`m[_k] = ...; return m`)，而 `get_all_metrics()` 返回的是
    `_all_metrics_cache["value"]` 里**同一批 dict 对象**（该缓存只按
    metrics_registry.json 的 mtime 失效，**切库不会失效**）。于是：
      1. 在 yans 库调一次 get_effective_metrics() → 缓存里的
         `{standard_yield_rate|std_yield_rate}` 被就地改成 `standard_yield_rate`；
      2. 界面切到 postgres 库 → 缓存直接返回**已污染的值** → 该库真实列是
         `std_yield_rate` → 编译出 `MAX(standard_yield_rate)` → PG 报
         `字段不存在`，确定性编译整条失败。
    实测两个方向的顺序都必现。
    根因是「把依赖运行时上下文（当前库）的结果写进了跨库共享的缓存」。
    修法：**返回新 dict**，绝不动入参 —— 缓存始终保持原始占位符，
    每次调用按当时的库重新解析。
    """
    try:
        from agent.metric_compiler import _resolve_col
    except Exception:
        return m
    tables = [str(t).split(".")[-1] for t in (m.get("tables") or [])]
    fact = tables[0] if tables else ""
    if not fact:
        return m
    out = None          # 懒拷贝：只有真的需要改时才建新 dict，避免无谓开销
    for _k in ("sql_expression", "formula"):
        s = m.get(_k)
        if not isinstance(s, str) or "{" not in s:
            continue
        try:
            fixed = re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_|]*)\}",
                           lambda mm: _resolve_col(fact, mm.group(1).split("|")),
                           s)
        except Exception:
            # 探测失败 → 保持原文，绝不猜
            continue
        if fixed != s:
            if out is None:
                out = dict(m)      # 浅拷贝：不碰入参与缓存
            out[_k] = fixed
    return out if out is not None else m


def get_effective_metrics(asof=None) -> list[dict]:
    """返回指定日期（默认今天）下有效的指标（口径可变：valid_from/valid_to 时间版本）。

    指标/维度级权限（权限中心 v2）在这里统一生效：同一指标按当前用户角色替换成
    对应口径、或直接过滤掉被禁用的指标。这是全链路唯一取指标的地方，
    因此 prompt 注入、确定性编译器、MQL 白盒、向量检索的口径天然一致。
    权限上下文来自 ContextVar（无 acl 时原样返回，零回归）。

    2026-10-04：出口处统一解析 `{a|b}` 候选语法（见 `_resolve_candidates_in`），
    使 prompt 注入 / MQL 展示 / 审计三处的口径字符串完全一致，不再外泄内部模板语法。
    """
    metrics = [m for m in get_all_metrics() if _is_effective(m, asof)]
    # 按当前数据库过滤：只保留事实表存在于当前库的指标（多库隔离，防 postgres 口径泄漏到 123 库）。
    # P0 性能优化：_current_db_tables() 只算一次传入，避免对每个指标重复查表列表（49 次 → 1 次）。
    current_tables = _current_db_tables()
    metrics = [m for m in metrics if _metric_applicable(m, current_tables)]
    metrics = [_resolve_candidates_in(m) for m in metrics]
    try:
        from security.enforcer import apply_metric_acl
        return apply_metric_acl(metrics)
    except Exception as e:
        _logger.warning("指标 ACL 应用失败，按未过滤返回: %s", e)
        return metrics


def _strip_table_names(query: str) -> str:
    """从问句中剥离用户显式提到的表名/表别名，防止表别名子串误命中指标词。

    反例：「工序产量表的不良类型排行」——「工序产量表」是 mes_process_output 的
    表别名，其子串「产量」会误命中「产量」指标 → 编译成无分组总量（答非所问）。
    仅剥离**完整别名/完整表名**（不剥离子串），避免误伤「各工序的产量」中的维度词。
    """
    q = query
    try:
        from db.tools import get_all_tables
        for t in get_all_tables():
            alias = (t.get("table_alias") or "").strip()
            name = (t.get("table_name") or "").strip()
            if len(alias) >= 2 and alias in q:
                q = q.replace(alias, " ")
            if len(name) >= 2 and name in q:
                q = q.replace(name, " ")
    except Exception:
        pass
    return q


# ── 后缀式别名防偷换（2026-09-11）─────────────────────────
# 命中词左侧紧贴的修饰语，剥离这些词后应干净（空）；剥不干净即疑似另一个未注册口径
_LEFT_STOP_CHARS = set("的是与和、，,。．？?！!：:（）()%内中里")


def _left_guard_vocab() -> set[str]:
    """守卫剥离词表：全部指标的 dims 词 + 常用量词/时间词 + 全部口径名与别名。"""
    vocab: set[str] = set()
    try:
        for m in get_effective_metrics():
            vocab.update(str(d) for d in (m.get("dims") or []) if d)
            nm = str(m.get("name") or "").lower()
            if nm:
                vocab.add(nm)
                base = re.sub(r"\(.*?\)", "", nm).strip()
                if base:
                    vocab.add(base)
            for a in (m.get("aliases") or []):
                if a:
                    vocab.add(str(a).lower())
    except Exception:
        pass
    vocab.update(["各", "每", "每个", "各个", "各种", "每种", "各类", "每类",
                  "所有", "全部", "按",
                  "每月", "每周", "每日", "每天", "本月", "当月", "上月", "上个月",
                  "下月", "今年", "去年", "本季度", "上月末", "近期",
                  # 单字时间量词（「7月产量」「6月产量」中数字截断中文扫描后残留的「月」，
                  # 不带会把「7月」当修饰语误杀命中 —— 2026-09-14 环比题实测）
                  "月", "年", "周", "日", "季", "季度",
                  # 2026-09-13 修复：守卫词表缺累计/均值/总量类合法修饰语，导致
                  # 「本年累计产量」「平均抽样数」「整个车间的总投入数量」等问法的
                  # 指标命中被误杀（find_metrics 返回空 → 编译回退 LLM）。
                  # 累计窗口词（与 metric_compiler._CUMULATIVE_* 同源）
                  "累计", "本年", "本季", "年初至今", "今年以来",
                  # 均值 / 总量 / 范围限定词
                  "平均", "均值", "总", "总共", "整体", "整个", "合计", "总计",
                  # 「近7天累计」中数字截断中文扫描后残留的单字（q[3:5]="天累计"）
                  "天",
                  # 事实表口语指代（表别名「设备停机记录表」剥不到子串「停机记录」）
                  "停机", "记录", "检验记录", "工单记录",
                  # 存在量词（「有多少种停机原因」）
                  "有", "有多少种", "多少种", "多少次", "有多少",
                  # 常见业务维度词（「各车间/整个工厂/按供应商」句式的合法左侧词，
                  # 与内置指标的 dims 词同级别；未注册口径防偷换目标不变：
                  # 「一次」「出货」这类口径修饰语仍不在词表，照常拦截）
                  "车间", "工厂", "班组", "班次", "供应商", "物料", "仓库", "区域",
                  # 2026-09-25 修复：「当前/现在」是合法时点修饰语（「当前库存还有多少」命中的
                  # 「库存还有多少」左侧剥不掉"当前"曾被误判 suspect → 零命中 → 回退 LLM）。
                  "当前", "现在",
                  # 2026-09-29 修复：疑问/祈使功能词（「哪些产线本月产量超过上个月」里
                  # 命中词左侧 run="哪些产线本月" 剥掉维度词与时间词后残留「哪些」，
                  # 被当成未注册口径的修饰语 → find_metrics 零命中 → 编译链根本不起步 → 掉 LLM）。
                  # 这些是句法功能词、不是口径修饰语：剥掉后「哪些一次合格率」仍剩「一次」→ 照常拦截。
                  "哪些", "哪条", "哪几", "哪个", "哪台", "哪道", "列出", "找出", "有哪些",
                  # 2026-09-24 修复：厂级范围词缺失，导致「全厂良率是多少」被守卫误杀
                  # （命中别名"良率是多少"，左侧"全厂"剥不掉 → suspect → 编译回退 LLM）。
                  # 与已有「整体」「整个」「工厂」同类，属合法范围限定词。
                  "全厂", "全公司", "全部产线", "整体产线",
                  # 2026-09-29 修复（同类问题第三次复发）：纯强调/限定副词缺失，
                  # 「各工序的**实际**良率是多少」「各产线的**实际**良率」被守卫误杀 ——
                  # 命中"良率"后左侧残留"实际"，剥不掉 → suspect → find_metrics 零命中
                  # → 编译整体回退 LLM。实测同一句去掉"实际"即命中、加回即不命中，
                  # 直接后果是确定性编译失效、退化到直生 SQL：耗时 0.7s → 26~69s，
                  # 且产物退化成「合格数量 / 投入数量」两列并列，**没有算良率**。
                  # 这几个词只表强调或时点，不改变口径语义（与"一次""出货"这类
                  # 会偷换口径的真修饰语不同），故进词表。
                  "实际", "真实", "确切", "具体", "目前", "如今", "时下",
                  # 2026-10-01 修复：祈使动词缺失，导致「分析各工序良率」「统计各产线产量」
                  # 这类最常见的「动词 + 紧邻指标」句式被守卫整批误杀 —— 实测
                  # 「各工序良率」确定性编译 0.7s 出正确 SQL，「分析各工序良率」左侧
                  # run="分析各工序" 剥掉"工序""各"后残留"分析" → suspect → find_metrics
                  # 零命中 → 回退 LLM 直生（26~69s）。15 个常见动词 × 5 个指标 = 75 条
                  # 问法全部零命中。动词是句法功能词、不是口径修饰语：剥掉动词后
                  # 「分析一次合格率」仍剩「一次」→ 照常拦截，防偷换能力不受影响。
                  "分析", "对比", "统计", "查看", "计算", "查询", "展示", "显示",
                  "汇总", "梳理", "列一下", "看一下", "查一下", "算一下",
                  "分析一下", "统计一下", "对比一下", "汇总一下",
                  "给我", "帮我", "我要", "我想", "请", "麻烦", "看看",
                  # 2026-10-01 修复（同日第二次）：最高级词（句首极值）缺失。
                  # 「最低各工序良率」「分析最低良率的工序」左侧 run="最低各工序" 剥掉"工序"
                  # "各"后残留"最低" → suspect → 零命中 → 回退 LLM（60s+）。
                  # 但同义的右置说法「良率最低的工序」早已能编译（ORDER BY "良率" ASC）——
                  # 同一语义因语序不同一个 0.1s 一个 60s，是最难察觉的一类不一致。
                  # 安全性已用 4 词 × 3 动词 × 5 维度 × 8 指标 = 480 条网格实测：
                  # 全部编译命中，且**都有 GROUP BY + 方向正确**（最低/最差→ASC，最高/最长→DESC）。
                  # 无维度时（「最低良率」）由 metric_compiler._auto_rank_dim 自动补维度，
                  # 不会退化成"返回全厂汇总值"这种静默错误。真正的口径修饰语（"出货""来料"）
                  # 不在其中，「最低出货合格率」仍零命中。
                  "最低", "最高", "最少", "最多", "最小", "最大",
                  "最优", "最差", "最好", "最佳", "最长", "最短", "最久",
                  # 2026-10-01 修复（同日第三次）：同比/环比词缺位。
                  # 句首「同比各工序良率」「环比各产线产量」中命中词左侧 run 残"同比"/"环比"
                  # 剥不掉 → suspect → find_metrics 零命中 → 编译回退 LLM。
                  # 同义右置「各工序良率同比」此前虽能命中指标，却被普通编译静默降级成
                  # 单期聚合（丢掉对比语义，0.1s 高置信错答）。这两类词是句法功能词、
                  # 不是口径修饰语，进词表后可被剥离；真修饰语（"出货""来料"）仍不在其中。
                  # 同时 metric_compiler.compile_period_compare_grouped 已放开「同比/环比
                  # + 维度」入口，命中后正确产出本期/上期两期对比 SQL（见同次改动）。
                  "同比", "环比", "去年同期", "较去年同期", "较上月", "较上期",
                  "较上季", "较上季度", "同比增长", "环比增长",
                  # 2026-10-04 修复：口语祈使动词「看」与时间粒度词「周度/月度」缺位。
                  # 实测「按周看产量」命中"产量"后左侧 run="按周看" —— 词表只有"看看"/
                  # "看一下"、没有裸"看"，剥不掉 → suspect → find_metrics 零命中 →
                  # 编译整体回退 LLM。同类「周度产量趋势」run="周度"，词表只有"周"，
                  # "周度".endswith("周") 为假 → 同样剥不掉。
                  # 这些是句法功能词/粒度词，不改变口径语义（真修饰语"出货""来料"
                  # 仍不在词表，防偷换能力不受影响）。
                  "看", "周度", "月度",
                  # 「这几个X分别有多少」句式的指示代词短语（run="这几个" 剥不掉）。
                  # 与已有的"哪些/哪条/每个/各个"同类，属句法功能词。
                  "这几个", "这几种", "这几种状态",
                  # 2026-10-04 第二批：口语祈使/指示成分缺位（均属句法功能词，非口径修饰语）。
                  #   「库存那边让我看下各仓库还有多少货」→ run="库存那边让我看下各仓库"
                  #   逐字剥到"库存"后停住（"看下/那边/让我/库存"缺位）→ 命中被误杀。
                  # "库存" 与已有 "停机/记录/检验记录/工单记录" 同类，是事实表的口语指代。
                  "看下", "看一下情况", "那边", "让我", "分别", "库存", "数据"])
    return vocab


def _left_modifier_suspect(q: str, pos: int) -> bool:
    """命中词左侧紧贴的中文修饰语剥离维度/量词后仍非空 → True（疑似未注册口径偷换）。

    例：q="每个车间的一次合格率"，命中"合格率"→ 左侧"一次"剥不掉 → suspect；
        q="各产线良率"，命中"良率"→ 左侧"各产线"剥成空 → 干净；
        q="各车间的合格率"，"的"截断 → 左侧空 → 干净。
    """
    if pos <= 0:
        return False
    i = pos - 1
    while i >= 0 and "\u4e00" <= q[i] <= "\u9fff" and q[i] not in _LEFT_STOP_CHARS:
        i -= 1
    run = q[i + 1: pos]
    if not run:
        return False
    # ── 2026-09-29：左侧若是**库里真实存在的枚举值**（不是修饰语），直接放行 ──
    # 背景：本守卫的设计前提是「左侧残留 = 未注册口径的修饰语」。但左侧残留也可能是
    # **取值**——「贴片机设备一共有多少台」里「贴片机」是 dim_equipment.equipment_type
    # 的真实取值，"贴片机设备数量"就是"设备数量（贴片机）"，口径没变。
    # 这类问法此前被判 suspect → 零命中 → 编译回退 LLM，而 LLM 会自作主张换表
    # （实测这题被答成"停过机的设备数"，源表从 dim_equipment 换成了 eqp_downtime_record）。
    # 判据用已建好的枚举倒排索引（_literal_table_index，按库落盘缓存 1h），命中即放行；
    # 索引不可用时返回 False（保持旧行为，只做放宽不做收紧）。
    # 注意只认**整段 run 恰为某个枚举值**，不认子串，避免「一次合格率」这种
    # 真修饰语因为碰巧是某列取值而被误放行。
    try:
        from agent.llm_service import _literal_table_index as _lti
        _idx = _lti() or {}
        if isinstance(_idx, dict):
            for _val in _idx.keys():
                if isinstance(_val, str) and _val and _val == run:
                    return False
    except Exception:
        pass
    vocab = _left_guard_vocab()
    changed = True
    while changed and run:
        changed = False
        # 近N天/近N个月 等动态时间窗口词（_CUMULATIVE_NDAYS_RE 同源）：固定词表无法枚举。
        # 2026-10-04 修复：原正则只认阿拉伯数字 `\d+`，**中文数词一律漏剥** ——
        # 「找出最近一个月不良数量最高的产品」的命中词「不良数量」左侧 run="找出最近一个月"，
        # 剥掉"找出"后残留"最近一个月"，`\d+` 匹配不到"一" → 判定为未注册口径修饰语
        # → find_metrics 零命中 → 整条确定性编译链根本不起步 → 回退 LLM（且该题在 LLM 侧
        # 也生成失败，用户看到的就是"AI 未能生成查询 SQL"）。
        # 而 metric_compiler._build_time_filter 本就支持中文数词窗口
        # （实测「最近一个月」→ INTERVAL '1 months'），故此处补齐数词字符集即可，
        # 不改变口径语义、也不存在"丢掉时间条件"的风险。
        _NUM = r"(?:\d+|[一两二三四五六七八九十百]+)"
        _stripped = re.sub(rf"(?:近|最近)\s*{_NUM}\s*(?:天|日|周|个月|月|年)", "", run)
        if _stripped != run:
            run = _stripped
            changed = True
            continue
        for w in sorted(vocab, key=len, reverse=True):
            if run.endswith(w):
                run = run[: len(run) - len(w)]
                changed = True
                break
    return bool(run)


def find_metrics(query: str, limit: int | None = 3) -> list[dict]:
    """按用户问题命中指标（最长匹配优先，避免矛盾口径同时注入）。

    limit=None 表示**不截断**，返回全部命中（调用方自行决定上限/回退）。
    2026-10-03 新增：此前 limit=3 的截断发生在函数内部，导致调用方
    「超过 3 个指标就回退 LLM」的守卫拿到的是已截断结果 → 守卫恒为假、
    变成死代码，并列 4 个指标时静默丢掉第一个（见 metric_compiler 调用点注释）。

    防 bug 背景：「质检不合格率」会同时命中 良率(别名"合格率")、不良率(别名"不合格率")、
    质检不合格率 三个指标，若同时注入 prompt 会给出互相矛盾的口径。
    策略：计算每个命中指标的「匹配词长度」，只返回匹配词**最长**的指标
    （并列最长的都返回，如"产量和良率"同时命中两个独立指标）。
    匹配前先剥离表名/表别名，避免「工序产量表」这类表别名中的指标词干扰命中。
    """
    if not query:
        return []
    q = _strip_table_names(query).lower()
    scored: list[tuple[int, str, dict]] = []
    for m in get_effective_metrics():
        words = [m["name"]] + (m.get("aliases") or [])
        # 去括号后缀：如 "采购金额(123库)" 也能命中 "采购金额"（括号是库别名标注，非业务词）
        base = re.sub(r"\(.*?\)", "", m["name"]).strip()
        if base and base != m["name"]:
            words.append(base)
        best = ""
        for w in words:
            if w and w.lower() in q and len(w) > len(best):
                best = w.lower()
        if best:
            scored.append((len(best), best, m, q.find(best)))
    if not scored:
        return []
    # ③ 后缀式别名防偷换（2026-09-11 批测发现）：「一次合格率」「出货合格率」这类
    # 未注册口径包含良率别名"合格率"，子串命中会把整题静默偷换成良率口径（编译产物
    # 别名甚至就叫"良率"）。守卫：命中词左侧紧贴的中文修饰语，剥离维度词/量词后
    # 若仍非空（如"一次""出货"）→ 判定为另一个未注册口径，本条命中作废 → 走 LLM
    # 直生并标注「AI 生成请核对」。正面例不受影响：「各产线良率」剥掉"各/产线"后干净；
    # 「各车间的合格率」的"的"天然截断左侧。
    scored = [(ln, w, m) for (ln, w, m, pos) in scored if not _left_modifier_suspect(q, pos)]
    if not scored:
        return []
    # ④ 右侧「率」后缀防劫持（2026-10-01 黄金题库 id33）：「各产线的不良率」里长别名
    # 「各产线的不良」(6字) 子串命中「产线不良数」，压过真正的「不良率」(3字) →
    # 按产线汇总不良【件数】(22690) 冒充不良【率】(2.41%)，量纲完全错了还看不出来。
    # 守卫：命中词右侧紧贴「率」而命中词自身又不以「率」结尾 → 右侧拼出了另一个
    # 「……率」词（率类口径或未注册口径），本条命中作废，让位给率类指标/LLM。
    # 正面例不受影响：「不良率」「合格率」本身以「率」结尾；「各产线的不良数」右侧是「数」。
    _kept = []
    for _ln, _w, _m in scored:
        _p = q.find(_w)
        if not _w.endswith("率") and _p >= 0 and _p + len(_w) < len(q) and q[_p + len(_w)] == "率":
            continue
        _kept.append((_ln, _w, _m))
    scored = _kept
    if not scored:
        return []
    # ① 前缀状态口径最优先（全量命中里 query 以该词开头）：query 以精确口径词开头时
    # 泛口径（如"工单数"命中"等待生产的工单数量"里的"工单数量"）让位 ——
    # 「等待生产的工单数量」→ 待生产工单数；「已完成工单有多少」→ 已完成工单数。
    prefix_hits: dict[str, dict] = {}
    for _ln, w, m in scored:
        if q.startswith(w) and len(w) >= 3:
            prefix_hits.setdefault(w, m)
    if prefix_hits:
        # R4 并列问句防护：「在产工单数和已完成工单数各是多少」——「在产工单数」恰好是 query 前缀，
        # 若按前缀唯一化会把并列的第二口径截断 → LLM 47s 空转。检测前缀词后紧跟并列连词
        # （和/与/及/、+ 另一完整口径名）→ 放弃前缀唯一，交给 ② 完整口径名并列（→ ambiguous 0s 澄清）。
        _joined = any(re.match(r"\s*(?:和|与|及|、|,|，)\s*[\u4e00-\u9fa5]", q[len(w):])
                      for w in list(prefix_hits))
        if not _joined:
            # 嵌套前缀过滤（2026-09-29）：短前缀别名是更长前缀别名的子串时丢弃短者
            # （如「当前库存」⊂「当前库存总量」、「未达产」⊂「未达产工单」、
            # 「检验合格率」⊂「检验合格率按批」）——两者都满足 q.startswith(w) 时会
            # 同时进 prefix_hits，返回多个 → render_exec_sql_metric 唯一性让位 → 回退 LLM。
            # 判据与第②档完整名嵌套过滤一致：长别名去掉短别名后剩的是否定词
            # （不/非/无/未/欠）→ 视为对立指标（如「不良率」=「不」+「良率」）都保留；
            # 其余（纯限定后缀）→ 丢短者，只留最精确的。
            _NEG = ("不", "非", "无", "未", "欠")
            _pfx = sorted(prefix_hits, key=len, reverse=True)
            _pfx_out: list[dict] = []
            for w in _pfx:
                _nested = [x for x in _pfx if w in x and w != x]
                if _nested and not any(x.replace(w, "", 1) in _NEG for x in _nested):
                    continue
                _pfx_out.append(prefix_hits[w])
            if _pfx_out:
                return _pfx_out[:limit]
    # ② 句中完整口径名/完整别名（2026-10-01 重写，黄金题库 id46/47/70/71）：
    # 指标【名称】或【别名】出现在 query 中（无前缀命中时），多个并存 → 全部返回。
    # 旧逻辑只收「名称==命中词」，别名命中的指标（缺陷件数 的别名「缺陷数量」、
    # 不良数 的别名「不良数量」）被遗漏，反而让更短的名称命中抢答：
    #   「各严重程度的缺陷数量」→ 缺陷数(COUNT 条数) 抢答 缺陷件数(SUM 件数)，量纲错；
    #   「产量、合格数量、不良数量」→ 不良数量(别名命中) 整列丢失。
    # 收齐后做「跨度感知」嵌套过滤（替代旧的纯字符串包含判断）：
    #   - 短词 w 的**所有**出现都落在长词 w2 的出现范围内（w ⊂ w2）→ 嵌套限定，丢短者
    #     （「未完工工单数」里的「工单数」）；若 query 带并列连词且 w2 去掉 w 后以否定词
    #     开头（停机时长 和 非计划停机时长）→ 用户并列点名的对立口径，都保留。
    #   - 短词与长词部分重叠但不互为子串（「各产线的不良[数]」：不良数 与 各产线的不良）
    #     → 更长词优先，丢短者。
    #   - 互不重叠的并列（产量、合格数量、不良数量）→ 全部保留。
    full_hits: dict[tuple[str, str], dict] = {}
    for _ln, w, m in scored:
        nm = str(m.get("name") or "").lower()
        if w and w in q:
            full_hits.setdefault((w, nm), m)
    if full_hits:
        _NEG_HEAD = ("不", "非", "无", "未", "欠")
        _has_conn = bool(re.search(r"和|与|及|、|，|分别|各是|分别是", query))

        def _spans_of(hay: str, needle: str) -> list[tuple[int, int]]:
            _sp, _i = [], hay.find(needle)
            while _i >= 0:
                _sp.append((_i, _i + len(needle)))
                _i = hay.find(needle, _i + 1)
            return _sp

        _keys = sorted(full_hits, key=lambda k: len(k[0]), reverse=True)
        _out: list[dict] = []
        for _k in _keys:
            _w = _k[0]
            _sp_w = _spans_of(q, _w)
            _dropped = False
            for _k2 in _keys:
                _w2 = _k2[0]
                if _w2 == _w or len(_w2) <= len(_w):
                    continue
                _sp_2 = _spans_of(q, _w2)
                if _w in _w2 and all(
                        any(a >= s and b <= e for (s, e) in _sp_2) for (a, b) in _sp_w):
                    _rem = _w2.replace(_w, "", 1)
                    if _has_conn and _rem and _rem[0] in _NEG_HEAD:
                        continue  # 并列点名的否定对立口径（…和 非计划停机时长）→ 都保留
                    _dropped = True
                    break
                if _w not in _w2 and _w2 not in _w and any(
                        a < e2 and b > s2 for (a, b) in _sp_w for (s2, e2) in _sp_2):
                    _dropped = True  # 部分重叠（非包含）→ 长词优先
                    break
            if not _dropped:
                _out.append((_w, full_hits[_k]))
        if _out:
            # ⑤ 同表同算式去重（2026-10-02 黄金题库 id31/33/34/69）：同一命中词
            # （如「良率」）会同时命中「良率」与「工序良率」这类同表、同算式的
            # 命名变体，全部返回会让合并 SQL 产出两列一模一样的值（5行×2列=10值，
            # 用户要一列却看到两列）。判据：tables 相同且 sql_expression（退化为
            # formula）相同 → 视为同一口径，只保留一个；优先「名称恰等于命中词」
            # 的（问「良率」列名叫「良率」），其次比「名称出现在问句中」。
            # 算式不同的并列（停机时长 vs 非计划停机时长、良率 vs 不良率）不受影响。
            _dedup: list[tuple[str, dict]] = []
            _by_expr: dict[tuple, int] = {}
            for _w, _m in _out:
                _key = (tuple(sorted(_m.get("tables") or [])),
                        str(_m.get("sql_expression") or _m.get("formula") or "").strip())
                _pi = _by_expr.get(_key)
                if _pi is None:
                    _by_expr[_key] = len(_dedup)
                    _dedup.append((_w, _m))
                    continue
                _pw, _pm = _dedup[_pi]
                _cur = (str(_m.get("name")).lower() == _w, str(_m.get("name")) in q)
                _old = (str(_pm.get("name")).lower() == _pw, str(_pm.get("name")) in q)
                if _cur > _old:
                    _dedup[_pi] = (_w, _m)
            _res = [m for _, m in _dedup]
            return _res if limit is None else _res[:limit]
    # ③ 最长匹配兜底（含 123 库去重）
    scored.sort(key=lambda x: x[0], reverse=True)
    max_len = scored[0][0]
    seen_word: dict[str, dict] = {}
    for ln, w, m in scored:
        if ln != max_len:
            continue
        if w not in seen_word:
            seen_word[w] = m
    # 2026-09-14 v4 题库题2 修复：并列连词连接多指标时，别名命中的不同指标不应被
    # 「最长匹配」截断成单个——「合格品数量与不良品数量」里「合格品数量」(5字)与
    # 「不良品数」(4字) 分别命中「合格数量」「不良数」，长度不同但都是用户明确并列
    # 点名的指标。此时按指标名去重，返回全部并列命中（非并列保持原「最长唯一」）。
    if re.search(r"和|与|及|、|，|分别|各是|分别是", query):
        _by_name: dict[str, dict] = {}
        for _ln, _w, _m in scored:
            _by_name.setdefault(str(_m.get("name") or ""), _m)
        _res = list(_by_name.values())
        return _res if limit is None else _res[:limit]
    _res = list(seen_word.values())
    return _res if limit is None else _res[:limit]


def _retrieve_for_query(query: str, top_k: int = 3) -> list[dict]:
    """蓝图第三层统一检索入口：先关键词精确命中（确定性，最高优先级），

    未命中再走指标向量/BM25 检索补回最相关口径（覆盖同义/换说法查询）。
    get_metric_hint（prompt 注入）与 _build_metric_hint（白盒溯源）共用本函数，
    保证「LLM 实际看到的口径」与「白盒展示的口径」始终一致，不出现割裂。

    RAG 命中会做「显著弱相关剔除」：相似度不足最高项 50% 的指标丢弃，
    避免"订单"问题被注入"工单"口径（"订单/工单"共享"单"字）诱导 LLM 选错表。
    """
    hits = find_metrics(query)
    if not hits:
        try:
            from agent.metric_memory import get_metric_memory
            # 2026-09-13：与 get_metric_hint 同一注入下限（_HINT_RAG_FLOOR），
            # 保证「白盒展示的口径」与「LLM 实际注入的口径」一致（弱相关召回
            # 曾把「周转天数」题展示/注入成「库存分档统计」，两头一起带偏）。
            rag = get_metric_memory().retrieve(query, top_k=top_k, with_scores=True,
                                               threshold=_HINT_RAG_FLOOR)
            if rag:
                hits = [x["metric"] for x in rag]
        except Exception as e:
            _logger.warning("指标 RAG 检索失败，回退空命中: %s", e)
    return hits


def get_metric_hint(query: str) -> str:
    """生成可注入 SQL Prompt 的「指标口径」段落（无命中返回空串）。

    蓝图第三层增强：关键词命中优先（确定性，保持原行为）；
    关键词未命中时，用指标向量/BM25 检索补回最相关口径（开卷考试）。
    embedding 不可用时自动退回纯关键词匹配（与改造前行为一致，零回归）。

    2026-09-13（用户实测驱动）：语义召回的弱相关口径曾把 LLM 带偏——
    「库存周转天数最高的5个产品」召回「库存分档统计/总库存」后，LLM 用
    MAX(available_qty) 冒充周转天数出了 20 行"一本正经的错误答案"。
    对策：① RAG-only 命中（关键词 0 命中）的注入下限抬到 _HINT_RAG_FLOOR
    （远高于 metric_memory 默认 0.12 的 ABS_FLOOR）；② 注入时明确标注
    「仅供参考、严禁套用冒充用户所问指标」。关键词命中不受影响（仍是
    确定性口径，照常注入）。
    """
    kw_hits = find_metrics(query)
    if kw_hits:
        hits = kw_hits
        rag_only = False
    else:
        rag_only = True
        hits = []
        try:
            from agent.metric_memory import get_metric_memory
            rag = get_metric_memory().retrieve(query, top_k=3, with_scores=True,
                                               threshold=_HINT_RAG_FLOOR)
            hits = [x["metric"] for x in rag]
        except Exception as e:
            _logger.warning("指标 RAG 检索失败，回退空命中: %s", e)
    if not hits:
        return ""
    lines = []
    for m in hits:
        expr = m.get("sql_expression") or ""
        unit = m.get("unit") or ""
        # description 可能含"演示库数据分布"等给人看的备注，注入 LLM 前截断为纯口径说明；
        # sql_expression 为空（跨表/多步口径）时不显示空段，直接引导看业务公式。
        desc = (m.get("description", "") or "").split("（演示库")[0].split("(演示库")[0].strip()
        if len(desc) > 70:
            desc = desc[:70] + "…"
        expr_part = f"口径算式 SQL = {expr}" if expr else "口径为跨表/多步算法"
        lines.append(
            f"- 指标「{m['name']}」（{desc}，单位 {unit}）：{expr_part}；"
            f"业务公式 = {m.get('formula', expr)}"
        )
    if rag_only:
        # 语义召回 ≠ 用户所问口径：必须显式告诉 LLM 这些只是"相似参考"，
        # 否则它会把其中某个算式直接冒充用户指标（实测：周转天数→MAX(库存量)）。
        return ("以下为与问题语义相似的既有口径，仅供参考（用户所问指标未在口径库注册）：\n"
                "注意：这些口径与用户所问指标不一定是同一指标，严禁直接把其中某个算式"
                "冒充/套用为用户所问指标；只能参考其字段用法与写法。用户所问指标必须"
                "按候选表列名的真实语义重新推导；若现有字段无法推导出该指标，"
                "请在 risks 中明确说明并降低 confidence，不得用无关列近似。\n"
                + "\n".join(lines))
    return "\n".join(lines)


def render_exec_sql_metric(query: str) -> dict | None:
    """高级口径确定性执行（2026-09-08）：指标带 exec_sql（人工验证过的完整只读 SQL）时，
    命中即绕过 LLM 直接渲染执行 —— 解决「注册口径但编译器不支持跨表/多步算法，LLM 又
    生成不稳」的死角。仅当 query 唯一命中该指标才启用；SQL 仍走安全校验与表权限链。

    支持 {limit} 占位（如「前10个工单」→ LIMIT 10），默认 10。
    """
    try:
        hits = find_metrics(query)
    except Exception:
        return None
    # 从命中集中取「唯一带 exec_sql」的指标（find_metrics 最长匹配可能把普通指标
    # 与高级指标并列返回；只要 exec 指标唯一，就应以人工验证 SQL 为准）。
    # exec_sql_by_dim（按维度模板）也算 exec 型：指标带「按维度分组的完整 SQL 集」，
    # 如 计划达成率（产线/车间/产品/日期/整体 各一份人工验证模板）。
    execs = [m for m in hits
             if str(m.get("exec_sql") or "").strip() or m.get("exec_sql_by_dim")]
    if len(execs) != 1:
        return None
    m = execs[0]
    # 字符串型 exec_sql_by_dim 让位（2026-09-29 实测修复）：历史遗留的字符串模板
    # （如「SELECT {dim} ... GROUP BY {dim}」）本意是按维度分组，但本函数只认 dict 型
    # （按「产线/车间/产品/日期/仓库」选模板）。若问句带「各X/按X/每个X」分组信号而
    # exec_sql_by_dim 又是字符串，强行用 exec_sql（整体单值）会**静默降级**——用户问
    # 「各产线的设备可用率」却拿到一个整体百分比。这些口径的 sql_expression 非空且是
    # 单事实表，普通编译器 try_compile_metric 已能正确 GROUP BY + JOIN 展示名，
    # 故此处 return None 让位给普通编译（不损失确定性，还修好分组）。
    if isinstance(m.get("exec_sql_by_dim"), str) and re.search(
            r"各[\u4e00-\u9fa5]{1,8}|按[\u4e00-\u9fa5]{1,8}|每个[\u4e00-\u9fa5]{1,8}|每(?!次|天|日|月|周|季度|年)[\u4e00-\u9fa5]{1,8}",
            query):
        return None
    limit = 10
    mm = re.search(r"(?:前|最大|最多|最高|TOP|top)\s*(\d+)", query)
    if mm:
        limit = int(mm.group(1))
    limit = min(max(limit, 1), 100)
    by_dim = m.get("exec_sql_by_dim") if isinstance(m.get("exec_sql_by_dim"), dict) else None
    if by_dim:
        # 按问题里的维度词选模板（后出现的词优先级不冲突；多个同时出现取更细的分组，
        # 顺序即 产线→车间→产品→日期 中在问题里命中的第一个）。
        # 2026-09-25：补「仓库」维度词（当前库存量等库存口径按仓库分组模板）。
        tpl = ""
        # 2026-10-04：「设备/设备类型」是**指标名自身的组成部分**（「综合设备停机率」里就有
        # 「设备」）。实测无分组信号时会被误判成「按设备分组」——「综合设备停机率」返回了
        # 40 行设备明细而非 1 个整体值。故这两个**新增**维度键仅在问句确有分组意图时启用；
        # 历史维度键（产线/车间/产品/日期/仓库）保持原判据，确保零回归。
        _group_sig = bool(re.search(
            r"各|每|按|分别|逐|排行|排名|最高|最低|TOP|top|前\s*\d+", query))
        for dim_word, key in (("产线", "产线"), ("车间", "车间"),
                              # 2026-10-04：新增「设备类型/设备」两个维度键（停机率/稼动率按设备分组）。
                              # 此前无此键，「各设备类型的停机率/稼动率」会落到 _default 模板 →
                              # 用户问分组却拿到一个整体单值（静默降级）。顺序上「设备类型」先于
                              # 「设备」，保证「各设备类型…」不会被更短的「设备」抢先命中。
                              ("设备类型", "设备类型"), ("设备", "设备"),
                              ("产品", "产品"), ("日期", "日期"),
                              ("每天", "日期"), ("每日", "日期"), ("按月", "日期"),
                              ("仓库", "仓库")):
            if dim_word in query and str(by_dim.get(key) or "").strip():
                if key in ("设备", "设备类型") and not _group_sig:
                    continue
                tpl = str(by_dim[key]).strip()
                break
        if not tpl:
            tpl = str(by_dim.get("_default") or "").strip()
    else:
        tpl = str(m.get("exec_sql") or "").strip()
    if not tpl:
        return None
    try:
        sql = tpl.format(limit=limit)
    except Exception:
        sql = tpl.replace("{limit}", str(limit))
    return {
        "sql": sql,
        "title": f"{m['name']}查询",
        "metric": m["name"],
        "metrics": [m["name"]],
        "tables": list(m.get("tables") or []),
        "compiled": True,
        "mql": {
            "metric": m["name"],
            "metric_key": m["name"],
            "sql_expression": "",
            "dimensions": [],
            "compiled_by": "exec_sql",
        },
        "drillable": None,
        "note": "命中注册高级口径（人工验证 SQL），确定性执行",
    }


def metric_required_fields(metric: dict) -> list[str]:
    """口径 SQL 引用的聚合字段名（用于冲突校验）"""
    expr = (metric.get("sql_expression") or "").lower()
    if not expr:
        return []
    return [f for f in _METRIC_FIELD_NAMES if re.search(rf"\b{re.escape(f)}\b", expr)]


# ── 口径意图解析（P1：口径管理交互）────────────────────────
# 在 LLM 生成 SQL 之前判定「问题是否涉及指标口径」及命中状态，供前端
# 渲染 未命中反馈条 / 歧义澄清卡 / 定义入口，全程不阻塞非指标类问题。

# 统计/聚合意图词：命中任一即视为「有指标意图」。
# 注意（重要修复）：只保留「比率」类指标词（率），而**不再**包含「多少/数量/总数/总量/
# 次数/时长/天数/个数/几单/几台/排名/排行/最高/最低/平均/均值/合计/产值/库存/金额/总额/
# 总价/占比/比例/趋势/环比/同比/增减」等通用计数/聚合/金额/比例/排序/时间词——
# 否则"各设备类型有多少台设备""各工厂的工资总额""各状态考勤记录占比"这类普通
# COUNT/SUM/占比 查询会被误判为「未定义口径(no_hit)」而触发二次确认弹窗，导致本该直接
# 出数的简单查询不生成 SQL。
# 仅未注册的「率」类指标（如"订单满足率""库存周转率""报废率"）才需要 no_hit 确认口径。
# ── 架构修复 2026-08-31 ──
# 原实现 _STAT_INTENT_RE 只匹配「率」一个字，导致「停机原因分析」「各产线产量对比」
# 「设备分布」这类**分析型/多指标/口径模糊**查询无任何统计意图命中 → 判 skip →
# 静默放行 LLM 直接生成 SQL（违背「未注册口径必须二次确认」的架构强约定）。
# 扩展为「分析型意图词」：分析/对比/排名/分布/趋势/原因 等词意味着口径由 LLM 自定义、
# 结果不可审计，必须弹窗确认；而「多少台/几次/几个」等简单计数词（不在此表）仍放行。
_STAT_INTENT_RE = re.compile(
    r"率|分析|统计|对比|排名|排行|分布|趋势|平均|合计|占比|明细|构成|结构|原因|清单|汇总|占比|Top|TOP"
)
# 并列连词：query 明确要多个指标（"产量和良率"）时不算歧义，直接多注入
_JOIN_WORDS = ("和", "与", "及", "、", "，", ",", "加", "还有", "分别", "对比")
# 澄清重问标记（前端 ClarifyCard 选择后："按【X】口径：<原问题>"）：
# 命中该标记时强制锁定指标，避免再次命中多个候选造成死循环（P3 收敛）
_FORCED_METRIC_RE = re.compile(r"按【(.+?)】口径")


def has_metric_intent(query: str) -> bool:
    """判断问题是否含指标口径意图（命中注册表 或 出现统计意图词）。"""
    if not query:
        return False
    if find_metrics(query):
        return True
    return bool(_STAT_INTENT_RE.search(query))


def _guess_metric_words(query: str) -> list[str]:
    """启发式提取疑似指标词（供「未命中」场景预填定义表单）。

    策略：注册表候选词（名称/别名）中，完整出现在 query 的直接取；
    否则取候选词的 2-3 字前缀（如"不良率"在问"不良"时命中"不良"）。
    按在 query 中出现位置排序，最多 3 个。
    """
    if not query:
        return []
    q = query
    cand: set[str] = set()
    for m in get_effective_metrics():
        for w in [m["name"]] + (m.get("aliases") or []):
            w = str(w).strip()
            if len(w) < 2 or w in cand:
                continue
            if w in q:
                cand.add(w)
            else:
                for k in (2, 3):
                    if len(w) > k and w[:k] in q:
                        cand.add(w[:k])
                        break
    return sorted(cand, key=lambda x: (q.find(x), -len(x)))[:3]


def resolve_metric_intent(query: str, acl=None) -> dict:
    """口径意图结构化解析：{status, hits, hints}。

    - skip      无指标意图（普通问句）→ 前端零打扰
    - no_hit    有统计意图但注册表 0 命中 → 非阻塞反馈条（hints=疑似指标词）
    - hit       单命中（或并列连词连接多指标）→ 静默注入执行
    - ambiguous ≥2 并列命中且无并列连词 → 阻塞澄清（hits 带公式/单位/denied）
    """
    if not query:
        return {"status": "skip", "hits": [], "hints": []}
    # 「标准良率 / std_yield_rate」是 dim_process 的预设列，不是计算指标「良率」（合格/投入）。
    # 子串匹配会把「标准良率」误命中为「良率」口径（SUM(good_qty)/SUM(input_qty)），
    # 导致「各工序的标准良率」被编译成计算良率而非直接取 std_yield_rate 列 → 直接放行给 LLM 查列。
    # 2026-09-14 收窄：此前是**无条件 skip**，于是「标准良率」这个注册口径永远走不到，
    # 全部交给 LLM 自己写列 —— 实测它把已是百分数的 std_yield_rate(=96.5) 又乘 100 → 9650。
    # 现在只在「标准良率」口径确实没命中（即可能被误命中成计算指标「良率」）时才 skip。
    if "标准良率" in query or "std_yield_rate" in query.lower():
        if not any(str(m.get("name")) == "标准良率" for m in find_metrics(query)):
            return {"status": "skip", "hits": [], "hints": []}
    # UI 状态判定只用关键词命中（用户明确说出的指标词，确定性）；
    # 向量/BM25 检索结果留给 get_metric_hint 做 prompt 注入，不参与歧义/未命中判定，
    # 否则"满足率"这类未注册词会被弱相关检索误判成 ambiguous（防打扰原则）。
    hits = find_metrics(query)
    # 澄清重问标记（P3 收敛）："按【X】口径：..." → 只保留指定指标，防歧义死循环
    _fm = _FORCED_METRIC_RE.search(query)
    if _fm and hits:
        forced = _fm.group(1).strip()
        if forced:
            forced_l = forced.lower()
            keep = [m for m in hits
                    if m["name"].lower() == forced_l
                    or forced_l in {str(a).lower() for a in (m.get("aliases") or [])}]
            if keep:
                hits = keep
            else:
                # 指定指标不在当前关键词命中中 → 按名称精确找回（如仅命中别名）
                for _m in get_effective_metrics():
                    if _m["name"].lower() == forced_l:
                        hits = [_m]
                        break
    # 有命中即视为指标意图；无命中时仅统计意图词触发（避免普通问句弹窗）
    if not hits and not _STAT_INTENT_RE.search(query):
        # 分析结构信号：问句含「各X的/按X/排行/排名/TOP/分布/占比/是多少/有多少」等
        # 分组对比结构，即使意图词表未命中也视为统计意图（如「各产品的库存周转天数」——
        # 含明确指标词但「周转天数」不在意图词表，此前误判 skip → 静默走 LLM，
        # 违背「未定义口径必弹窗」红线；2026-09-01 收紧）。
        if not re.search(r"各[\u4e00-\u9fa5]{1,8}的|按[\u4e00-\u9fa5]{1,8}(?!\s*(天|日|月|周|季度|年))|排行|排名|TOP\s*\d+|分布|占比|是多少|有多少", query):
            return {"status": "skip", "hits": [], "hints": []}
    if not hits:
        return {"status": "no_hit", "hits": [], "hints": _guess_metric_words(query)}

    # 拷贝 + 角色指标级权限判定（denied 候选前端置灰）
    out_hits: list[dict] = []
    for m in hits:
        item = dict(m)
        item["denied"] = False
        out_hits.append(item)
    if acl is not None and (acl.denied_metrics or acl.metric_overrides):
        try:
            from security.enforcer import apply_metric_acl
            allowed_names = {x["name"] for x in apply_metric_acl(hits, acl)}
            for item in out_hits:
                if item["name"] not in allowed_names:
                    item["denied"] = True
        except Exception as e:
            _logger.warning("指标 ACL 权限标记失败: %s", e)

    if len(out_hits) >= 2:
        # 比较词连接两个指标（如"计划产量>实际产量"）→ 是 WHERE 过滤条件，不是多指标并列请求，
        # 应放行给普通查询链路，而非弹「歧义澄清」阻断出数。
        if re.search(r"大于|小于|超过|高于|低于|不少于|不低于|至少|最多|[<>]=?|≠|不等于", query):
            return {"status": "skip", "hits": [], "hints": []}
        # 用户显式写出列名（如"(available_qty)"）→ 是技术化精确查询，直接查列即可，
        # 不应被「可用库存 vs 总库存」这类别名重叠误判为口径歧义而阻断出数。
        if re.search(r"\([a-zA-Z_][a-zA-Z0-9_]*\)", query):
            return {"status": "skip", "hits": [], "hints": []}
        # 并列连词连接多个口径（「产量和良率」「良率和不良率」）→ 放行给确定性编译：
        # try_compile_metric 已支持**同表**多指标（v4 题库题1 实测「良率和不良率」能正确
        # 编出两列）。此前的注释「编译与 LLM 均无法一次输出多口径」已过时（那时双指标会
        # 跨表 INNER JOIN 丢维度行，同表单指标聚合并无此问题）。无并列连词的 ≥2 命中
        # （用户没说清要哪个）仍走歧义澄清。
        if re.search(r"和|与|及|、|，|分别|各是|分别是", query):
            return {"status": "hit", "hits": out_hits, "hints": []}
        # 2026-09-29（可用性修正）：无并列连词的 ≥2 命中**不再一律阻塞澄清**，改为
        # 「能按最长匹配定出唯一主口径 → 直接执行主口径，其余挂备选；否则才澄清」。
        # 为什么要改：阻塞的代价是一次澄清往返，用户点完还得再跑一遍完整链路；
        # 而「严重缺陷占比」这类词本身就带唯一正确解——它是 hits 里最长的那个名称，
        # 短名（严重缺陷数）只是被它包含的子串，词面已能判定，不该推给人再点一次。
        # 判据（关键）：只有当**最长名严格长于其余所有候选**时才算主口径明确。
        # 并列最长（如「产量」与「良率」同为 2 字、无连词）是真歧义 → 仍走阻塞，
        # 这正是用户要求保留的「拒绝权」——拒绝的代价是再跑一次昂贵抽取，
        # 但把真有歧义的题硬猜，等于把「答得慢」换成「答得糙」。
        _names = [str(h.get("name") or "") for h in out_hits]
        _maxlen = max((len(n) for n in _names), default=0)
        _longest = [h for h in out_hits if len(str(h.get("name") or "")) == _maxlen]
        if len(_longest) == 1:
            primary = _longest[0]
            rest = [h for h in out_hits if h is not primary]
            return {"status": "ambiguous", "primary": primary,
                    "hits": out_hits, "hints": [], "alternatives": rest}
        return {"status": "ambiguous", "primary": None,
                "hits": out_hits, "hints": [], "alternatives": out_hits}
    return {"status": "hit", "hits": out_hits, "hints": []}


# ── 指标入库（P2：定义闭环，供 /api/metrics 与审批流共用）──

def create_user_metric(metric: dict) -> dict:
    """入库新指标：名称冲突校验（区分同义/不同义）+ 保存 + 重建向量库；冲突抛 ValueError。"""
    name = str(metric.get("name") or "").strip()
    if not name:
        raise ValueError("指标名不能为空")
    # P0-3 指标类型校验：显式声明必须合法；cumulative 须带合法 accumulate.window
    mtype = metric.get("metric_type")
    if mtype and mtype not in _METRIC_TYPES:
        raise ValueError(f"不支持的指标类型「{mtype}」，可选: {', '.join(_METRIC_TYPES)}")
    if mtype == "cumulative" or metric.get("accumulate"):
        win = (metric.get("accumulate") or {}).get("window")
        if win not in _CUMULATIVE_WINDOWS:
            raise ValueError(f"累计指标须声明 accumulate.window（可选: {', '.join(_CUMULATIVE_WINDOWS)}）")
    if mtype == "conversion":
        conv = metric.get("conversion") or {}
        if not conv.get("base_cond") or not conv.get("target_cond"):
            raise ValueError("转换率指标须声明 conversion.base_cond 与 conversion.target_cond（SQL 布尔条件）")
    conflict = [m for m in get_all_metrics() if m["name"] == name and _is_effective(m)]
    if conflict:
        existing = conflict[0]
        new_expr = str(metric.get("sql_expression") or metric.get("formula") or "").strip()
        old_expr = str(existing.get("sql_expression") or existing.get("formula") or "").strip()
        # 同义判定先规范化（去空白 + 小写），避免大小写/空格差异误判为不同义
        def _norm(s: str) -> str:
            return re.sub(r"\s+", "", s).lower() if s else ""
        if new_expr and old_expr and _norm(new_expr) == _norm(old_expr):
            raise ValueError(f"指标「{name}」已存在且口径一致，无需重复注册")
        # 同名不同义：给出消歧提示（当前接口不支持自动覆盖，需先停用/删除原指标）
        raise ValueError(
            f"指标「{name}」已存在但口径不同（同名不同义）：\n"
            f"  现有口径：{old_expr or '（未定义表达式）'}\n"
            f"  新口径：{new_expr or '（未定义表达式）'}\n"
            f"请先停用或删除原指标后重新注册，或改用区分性命名（如「财务_{name}」/「销售_{name}」）避免口径漂移。"
        )
    # P1-B 口径版本（对标 FineBI 经营记忆）：同名历史（含已停用）存在时，
    # 新指标 version = 历史最大 version + 1；口径变更时审计留痕（旧→新），
    # 配合 valid_from/valid_to 实现「旧口径结果打标签、不与新口径混淆」。
    history = [m for m in get_all_metrics() if m["name"] == name]
    versions = [int(m.get("version") or 1) for m in history]
    new_version = (max(versions) + 1) if versions else 1
    metric = {**metric, "version": new_version}
    if history:
        # 取版本号最大的作为"旧口径"（列表顺序不等于时间序，避免误用非最新历史）
        old = max(history, key=lambda m: int(m.get("version") or 1))
        old_expr = str(old.get("sql_expression") or old.get("formula") or "").strip()
        new_expr = str(metric.get("sql_expression") or metric.get("formula") or "").strip()
        if old_expr and new_expr and old_expr != new_expr:
            try:
                from auth import audit
                audit("metric_version_change", name=name,
                      old_version=old.get("version", 1), new_version=new_version,
                      old_expr=old_expr[:200], new_expr=new_expr[:200])
            except Exception:
                pass
    metrics = _load_user_metrics()
    metrics.append(metric)
    save_user_metrics(metrics)
    try:
        from agent.metric_memory import get_metric_memory
        get_metric_memory().rebuild()
    except Exception as e:
        _logger.warning("指标向量库重建失败: %s", e)
    return {"metric": metric}
