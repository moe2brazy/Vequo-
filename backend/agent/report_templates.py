"""预置报告模板 —— 把「生产周报 / 月报」「不良设备报告」这类周期性报告做成一等公民。

为什么不复用 LLM 现写 SQL：周报月报是**周期性、口径固定**的东西。
每周算的都该是同一个口径，今天用 SUM(input_qty) 明天用 SUM(good_qty)，
用户是看不出来的，但两个月的数字就没法比了。所以这里把口径写死成模板，
LLM 只负责把已经算准的数字讲成人话。

三个约束（都来自实际踩过的坑）：
1. **库存类必须取最新快照**。inv_inventory_snapshot 是历史快照表，
   直接 SUM 会得到全历史累加（实测与真实库存差几十倍）。模板里统一用
   `snapshot_date = (SELECT MAX(snapshot_date) ...)`。
2. **表级权限在取数前判**。模板声明自己用哪几张表，无权就整章跳过并在报告里
   写明原因——不能让用户看到一份"数字少了但不告诉你为什么"的报告。
3. **行级权限包一层子查询**，不改原语句（原语句多为聚合/JOIN）。

对外入口：
    get_template(kind) -> Template | None
    collect(template, req, allowed_tables, row_filters) -> dict
"""

from __future__ import annotations

import datetime
import html as html_mod
from dataclasses import dataclass, field

from agent.report_intent import ReportRequest


@dataclass
class Section:
    """报告里的一个章节：一条 SQL 一行图表。"""

    key: str
    title: str
    sql: str                      # 用 {start} {end} 占位
    chart: str = "bar"            # bar / line / donut / kpi / table
    hint: str = ""                # 给 LLM 的解读提示（这节该看什么）
    dim: str = ""                 # 图表维度列（中文别名）
    meas: str = ""                # 图表数值列（中文别名）


@dataclass
class Template:
    """一份报告的骨架。"""

    kind: str
    label: str
    tables: list[str]             # 涉及的表，用于表级权限判定
    sections: list[Section]

    def section_by_key(self, key: str) -> Section | None:
        for s in self.sections:
            if s.key == key:
                return s
        return None


# ═══════════════════════════════════════════════════════════════
# 模板定义（口径写死在这里，改口径改这里，不要在渲染层改）
# ═══════════════════════════════════════════════════════════════

_LINE_JOIN = ("mes_process_output o "
              "JOIN dim_production_line l ON o.line_id = l.line_id")

PRODUCTION = Template(
    kind="production",
    label="生产",
    tables=["mes_process_output", "mes_work_order", "dim_product",
            "dim_production_line", "dim_process"],
    sections=[
        Section(
            key="summary", title="生产总览", chart="kpi",
            hint="总量、合格率、不良率的整体水位，和上一周期比是升是降",
            sql="""
SELECT SUM(input_qty)                                        AS "投入总数",
       SUM(good_qty)                                         AS "合格数",
       SUM(defect_qty)                                       AS "不良数",
       SUM(rework_qty)                                       AS "返工数",
       ROUND(100.0 * SUM(good_qty) / NULLIF(SUM(input_qty), 0), 2) AS "综合良率"
FROM mes_process_output
WHERE stat_date BETWEEN '{start}' AND '{end}'
""",
        ),
        Section(
            key="by_line", title="各产线产出与良率", chart="bar",
            dim="产线", meas="投入", hint="哪条产线在扛产量、哪条良率掉队",
            sql="""
SELECT l.line_name                    AS "产线",
       SUM(o.input_qty)               AS "投入",
       SUM(o.good_qty)                AS "合格",
       SUM(o.defect_qty)              AS "不良",
       ROUND(100.0 * SUM(o.good_qty) / NULLIF(SUM(o.input_qty), 0), 2) AS "良率"
FROM mes_process_output o
JOIN dim_production_line l ON o.line_id = l.line_id
WHERE o.stat_date BETWEEN '{start}' AND '{end}'
GROUP BY l.line_name
ORDER BY SUM(o.input_qty) DESC
""",
        ),
        Section(
            key="by_category", title="各产品类别产出", chart="bar",
            dim="产品类别", meas="投入", hint="产出结构是否均衡，有没有单点依赖",
            sql="""
SELECT p.product_category            AS "产品类别",
       SUM(o.input_qty)              AS "投入",
       SUM(o.good_qty)               AS "合格",
       SUM(o.defect_qty)             AS "不良"
FROM mes_process_output o
JOIN dim_product p ON o.product_id = p.product_id
WHERE o.stat_date BETWEEN '{start}' AND '{end}'
GROUP BY p.product_category
ORDER BY SUM(o.input_qty) DESC
""",
        ),
        Section(
            key="by_process", title="各工序不良", chart="bar",
            dim="工序", meas="不良数", hint="不良集中在哪道工序，是不是关键工序",
            sql="""
SELECT pr.process_name               AS "工序",
       SUM(o.defect_qty)             AS "不良数",
       SUM(o.input_qty)              AS "投入",
       ROUND(100.0 * SUM(o.defect_qty) / NULLIF(SUM(o.input_qty), 0), 2) AS "不良率"
FROM mes_process_output o
JOIN dim_process pr ON o.process_id = pr.process_id
WHERE o.stat_date BETWEEN '{start}' AND '{end}'
GROUP BY pr.process_name
ORDER BY SUM(o.defect_qty) DESC
""",
        ),
        Section(
            key="daily", title="每日投入与产出趋势", chart="line",
            dim="日期", meas="投入", hint="有没有某天塌陷或异常尖峰",
            sql="""
SELECT stat_date                     AS "日期",
       SUM(input_qty)                AS "投入",
       SUM(good_qty)                 AS "合格",
       SUM(defect_qty)               AS "不良"
FROM mes_process_output
WHERE stat_date BETWEEN '{start}' AND '{end}'
GROUP BY stat_date
ORDER BY stat_date
""",
        ),
        Section(
            key="orders", title="工单状态分布", chart="donut",
            dim="工单状态", meas="工单数", hint="有多少工单还压着没做完",
            sql="""
SELECT order_status                  AS "工单状态",
       COUNT(*)                      AS "工单数",
       SUM(plan_qty)                 AS "计划数量"
FROM mes_work_order
WHERE start_date BETWEEN '{start}' AND '{end}'
GROUP BY order_status
ORDER BY COUNT(*) DESC
""",
        ),
    ],
)

EQUIPMENT = Template(
    kind="equipment_defect",
    label="设备不良",
    tables=["eqp_downtime_record", "dim_equipment", "dim_production_line",
            "qms_defect_detail", "qms_inspection"],
    sections=[
        Section(
            key="summary", title="停机总览", chart="kpi",
            hint="非计划停机占比越高越说明设备管理被动",
            sql="""
SELECT COUNT(*)                                                  AS "停机次数",
       SUM(downtime_minutes)                                     AS "停机总分钟",
       SUM(CASE WHEN is_planned THEN downtime_minutes ELSE 0 END)     AS "计划停机分钟",
       SUM(CASE WHEN NOT is_planned THEN downtime_minutes ELSE 0 END) AS "非计划停机分钟",
       ROUND(100.0 * SUM(CASE WHEN NOT is_planned THEN downtime_minutes ELSE 0 END)
             / NULLIF(SUM(downtime_minutes), 0), 2)               AS "非计划占比"
FROM eqp_downtime_record
WHERE start_time::date BETWEEN '{start}' AND '{end}'
""",
        ),
        Section(
            key="top_equipment", title="停机时长最高的设备", chart="bar",
            dim="设备", meas="停机分钟", hint="是不是同几台设备反复出问题",
            sql="""
SELECT e.equipment_name              AS "设备",
       SUM(d.downtime_minutes)       AS "停机分钟",
       COUNT(*)                      AS "停机次数",
       ROUND(AVG(d.downtime_minutes), 1) AS "平均单次分钟"
FROM eqp_downtime_record d
JOIN dim_equipment e ON d.equipment_id = e.equipment_id
WHERE d.start_time::date BETWEEN '{start}' AND '{end}'
GROUP BY e.equipment_name
ORDER BY SUM(d.downtime_minutes) DESC
LIMIT 12
""",
        ),
        Section(
            key="by_reason", title="停机原因分布", chart="donut",
            dim="停机原因", meas="停机分钟", hint="主要矛盾是计划检修还是突发故障",
            sql="""
SELECT downtime_reason                AS "停机原因",
       SUM(downtime_minutes)          AS "停机分钟",
       COUNT(*)                       AS "次数"
FROM eqp_downtime_record
WHERE start_time::date BETWEEN '{start}' AND '{end}'
GROUP BY downtime_reason
ORDER BY SUM(downtime_minutes) DESC
""",
        ),
        Section(
            key="by_line", title="各产线停机时长", chart="bar",
            dim="产线", meas="停机分钟", hint="停机是否集中在某条产线",
            sql="""
SELECT l.line_name                    AS "产线",
       SUM(d.downtime_minutes)        AS "停机分钟",
       SUM(CASE WHEN NOT d.is_planned THEN d.downtime_minutes ELSE 0 END) AS "非计划分钟",
       COUNT(*)                       AS "停机次数"
FROM eqp_downtime_record d
JOIN dim_production_line l ON d.line_id = l.line_id
WHERE d.start_time::date BETWEEN '{start}' AND '{end}'
GROUP BY l.line_name
ORDER BY SUM(d.downtime_minutes) DESC
""",
        ),
        Section(
            key="defect_type", title="缺陷类型分布", chart="bar",
            dim="缺陷类型", meas="缺陷数", hint="设备问题最终落在哪类缺陷上",
            sql="""
SELECT dd.defect_type                 AS "缺陷类型",
       SUM(dd.defect_qty)             AS "缺陷数",
       COUNT(*)                       AS "记录数"
FROM qms_defect_detail dd
JOIN qms_inspection i ON dd.inspection_id = i.inspection_id
WHERE i.inspection_date BETWEEN '{start}' AND '{end}'
GROUP BY dd.defect_type
ORDER BY SUM(dd.defect_qty) DESC
""",
        ),
        Section(
            key="severity", title="缺陷严重度分布", chart="donut",
            dim="严重度", meas="缺陷数", hint="严重缺陷有没有被压住",
            sql="""
SELECT dd.severity_level              AS "严重度",
       SUM(dd.defect_qty)             AS "缺陷数",
       COUNT(*)                       AS "记录数"
FROM qms_defect_detail dd
JOIN qms_inspection i ON dd.inspection_id = i.inspection_id
WHERE i.inspection_date BETWEEN '{start}' AND '{end}'
GROUP BY dd.severity_level
ORDER BY SUM(dd.defect_qty) DESC
""",
        ),
    ],
)

QUALITY = Template(
    kind="quality",
    label="质量",
    tables=["qms_inspection", "qms_defect_detail", "dim_product", "dim_process"],
    sections=[
        Section(
            key="summary", title="检验总览", chart="kpi",
            hint="不良率整体水位",
            sql="""
SELECT COUNT(*)                       AS "检验批次",
       SUM(sample_qty)                AS "抽检总数",
       SUM(defect_qty)                AS "不良数",
       ROUND(100.0 * SUM(defect_qty) / NULLIF(SUM(sample_qty), 0), 2) AS "不良率"
FROM qms_inspection
WHERE inspection_date BETWEEN '{start}' AND '{end}'
""",
        ),
        Section(
            key="result", title="检验结果分布", chart="donut",
            dim="检验结果", meas="批次数", hint="合格/不合格/让步接收各占多少",
            sql="""
SELECT inspection_result              AS "检验结果",
       COUNT(*)                       AS "批次数",
       SUM(defect_qty)                AS "不良数"
FROM qms_inspection
WHERE inspection_date BETWEEN '{start}' AND '{end}'
GROUP BY inspection_result
ORDER BY COUNT(*) DESC
""",
        ),
        Section(
            key="by_product", title="各产品不良", chart="bar",
            dim="产品", meas="不良数", hint="哪个产品最不稳定",
            sql="""
SELECT p.product_name                 AS "产品",
       SUM(i.sample_qty)              AS "抽检数",
       SUM(i.defect_qty)              AS "不良数",
       ROUND(100.0 * SUM(i.defect_qty) / NULLIF(SUM(i.sample_qty), 0), 2) AS "不良率"
FROM qms_inspection i
JOIN dim_product p ON i.product_id = p.product_id
WHERE i.inspection_date BETWEEN '{start}' AND '{end}'
GROUP BY p.product_name
ORDER BY SUM(i.defect_qty) DESC
LIMIT 15
""",
        ),
        Section(
            key="by_process", title="各工序不良", chart="bar",
            dim="工序", meas="不良数", hint="不良是不是某道工序带出来的",
            sql="""
SELECT pr.process_name                AS "工序",
       SUM(i.sample_qty)              AS "抽检数",
       SUM(i.defect_qty)              AS "不良数"
FROM qms_inspection i
JOIN dim_process pr ON i.process_id = pr.process_id
WHERE i.inspection_date BETWEEN '{start}' AND '{end}'
GROUP BY pr.process_name
ORDER BY SUM(i.defect_qty) DESC
""",
        ),
        Section(
            key="defect_type", title="缺陷类型分布", chart="bar",
            dim="缺陷类型", meas="缺陷数", hint="主要缺陷形态",
            sql="""
SELECT dd.defect_type                 AS "缺陷类型",
       SUM(dd.defect_qty)             AS "缺陷数",
       COUNT(*)                       AS "记录数"
FROM qms_defect_detail dd
JOIN qms_inspection i ON dd.inspection_id = i.inspection_id
WHERE i.inspection_date BETWEEN '{start}' AND '{end}'
GROUP BY dd.defect_type
ORDER BY SUM(dd.defect_qty) DESC
""",
        ),
    ],
)

INVENTORY = Template(
    kind="inventory",
    label="库存",
    tables=["inv_inventory_snapshot", "dim_product"],
    sections=[
        Section(
            key="summary", title="库存总览（最新快照）", chart="kpi",
            hint="多少品种低于安全库存",
            sql="""
SELECT SUM(available_qty)                                  AS "可用库存",
       SUM(frozen_qty)                                     AS "冻结库存",
       SUM(CASE WHEN available_qty < safety_stock_qty THEN 1 ELSE 0 END) AS "低于安全库存品种",
       COUNT(DISTINCT product_id)                          AS "涉及品种数"
FROM inv_inventory_snapshot
WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM inv_inventory_snapshot)
""",
        ),
        Section(
            key="by_product", title="库存最高的产品", chart="bar",
            dim="产品", meas="可用库存", hint="有没有明显压货",
            sql="""
SELECT COALESCE(p.product_name, s.product_id) AS "产品",
       SUM(s.available_qty)                   AS "可用库存",
       SUM(s.safety_stock_qty)                AS "安全库存"
FROM inv_inventory_snapshot s
LEFT JOIN dim_product p ON s.product_id = p.product_id
WHERE s.snapshot_date = (SELECT MAX(snapshot_date) FROM inv_inventory_snapshot)
GROUP BY COALESCE(p.product_name, s.product_id)
ORDER BY SUM(s.available_qty) DESC
LIMIT 15
""",
        ),
        Section(
            key="below_safety", title="低于安全库存的品种", chart="table",
            hint="需要补料清单",
            sql="""
SELECT COALESCE(p.product_name, s.product_id) AS "产品",
       SUM(s.available_qty)                   AS "可用库存",
       SUM(s.safety_stock_qty)                AS "安全库存",
       SUM(s.safety_stock_qty - s.available_qty) AS "缺口"
FROM inv_inventory_snapshot s
LEFT JOIN dim_product p ON s.product_id = p.product_id
WHERE s.snapshot_date = (SELECT MAX(s.snapshot_date) FROM inv_inventory_snapshot)
GROUP BY COALESCE(p.product_name, s.product_id), s.safety_stock_qty
HAVING SUM(s.available_qty) < SUM(s.safety_stock_qty)
ORDER BY SUM(s.safety_stock_qty - s.available_qty) DESC
LIMIT 30
""",
        ),
        Section(
            key="by_warehouse", title="各仓库库存分布", chart="donut",
            dim="仓库", meas="可用库存", hint="库存集中在哪个仓",
            sql="""
SELECT warehouse_code                 AS "仓库",
       SUM(available_qty)             AS "可用库存",
       SUM(frozen_qty)                AS "冻结库存"
FROM inv_inventory_snapshot
WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM inv_inventory_snapshot)
GROUP BY warehouse_code
ORDER BY SUM(available_qty) DESC
""",
        ),
    ],
)

TEMPLATES: dict[str, Template] = {
    "production": PRODUCTION,
    "quality": QUALITY,
    "equipment_defect": EQUIPMENT,
    "inventory": INVENTORY,
}

# 有意图但当前库里没有对应数据源的类型 → 明确告诉用户，不假装有
NO_DATASOURCE = {
    "sales": "当前连接的库里没有销售/订单金额相关的数据表（只有生产、质量、设备、库存四类），"
             "所以生成不了销售报告。可以改用「生产周报」「质量报告」「设备不良报告」「库存报告」。",
}


def get_template(kind: str) -> Template | None:
    return TEMPLATES.get(kind)


# ═══════════════════════════════════════════════════════════════
# 取数
# ═══════════════════════════════════════════════════════════════

def _fill_period(sql: str, req: ReportRequest) -> str:
    return sql.replace("{start}", req.start).replace("{end}", req.end)


def _wrap_row_filter(sql: str, cond: str) -> str:
    """把行级权限条件包成子查询，避免破坏原语句的 GROUP BY / JOIN。"""
    return f"SELECT * FROM (\n{sql.strip().rstrip(';')}\n) AS _acl_wrapped WHERE {cond}"


def _pick_row_filter(sql: str, row_filters: dict[str, str] | None) -> str:
    """挑出与本条 SQL 相关的行过滤条件。

    只在语句确实引用了被限表的裸名时才套——套错了会报「列不存在」，
    而不是静默少数据（少数据更难发现）。
    """
    if not row_filters:
        return ""
    import re as _re
    low = (sql or "").lower()
    parts = []
    for tbl, cond in row_filters.items():
        if not tbl or not cond:
            continue
        if _re.search(r"(?<![a-z0-9_])" + _re.escape(str(tbl).lower()) + r"(?![a-z0-9_])", low):
            parts.append(f"({cond})")
    return " AND ".join(parts)


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _digest(section: Section, cols: list[str], rows: list[dict]) -> str:
    """确定性小结：直接从数字算，不经过 LLM。

    报告里最容易被质疑的就是"AI 说的对不对"，所以凡是能从数据直接算出来的结论，
    都不交给模型转述——模型只写它擅长的因果推测和建议。
    """
    if not rows:
        return "本区间没有数据。"

    if section.chart == "kpi":
        row = rows[0]
        bits = []
        for c in cols:
            v = row.get(c)
            if v is None:
                continue
            unit = "%" if ("率" in c or "占比" in c) else ""
            bits.append(f"{c} {v}{unit}")
        return "；".join(bits) + "。"

    if not section.dim or not section.meas:
        return ""

    pairs = [(str(r.get(section.dim) or "—"), _num(r.get(section.meas))) for r in rows]
    pairs = [(k, v) for k, v in pairs if v is not None]
    if not pairs:
        return ""
    total = sum(v for _, v in pairs)
    top = max(pairs, key=lambda x: x[1])
    low = min(pairs, key=lambda x: x[1])
    share = (100.0 * top[1] / total) if total else 0.0
    out = f"共 {len(pairs)} 项，合计 {_fmt(total)}。"
    if len(pairs) > 1:
        out += f" 最高是「{top[0]}」（{_fmt(top[1])}，占 {share:.1f}%），最低是「{low[0]}」（{_fmt(low[1])}）。"
    else:
        out += f" 为「{top[0]}」（{_fmt(top[1])}）。"
    return out


def _fmt(v: float) -> str:
    if v == int(v) and abs(v) < 1e15:
        return f"{int(v):,}"
    return f"{v:,.2f}"


def collect(template: Template, req: ReportRequest,
            allowed_tables: set[str] | None = None,
            row_filters: dict[str, str] | None = None) -> dict:
    """按模板逐章节取数。

    表级权限：allowed_tables 非 None 时，章节涉及的表只要有一张不在授权内，
    该章节整章跳过并记 skipped + 原因。**不静默少数据**。
    """
    from db.executor import execute_sql

    allowed = {str(t).lower() for t in allowed_tables} if allowed_tables else None
    out_sections: list[dict] = []
    skipped: list[dict] = []

    for sec in template.sections:
        raw_sql = _fill_period(sec.sql, req)
        # 章节级表级权限：所有引用的表都必须在授权内
        if allowed is not None:
            import re as _re
            refs = set(_re.findall(r"(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)", raw_sql, _re.I))
            denied = sorted(t for t in refs if t.lower() not in allowed)
            if denied:
                skipped.append({"key": sec.key, "title": sec.title,
                                "reason": "、".join(denied)})
                out_sections.append({
                    "key": sec.key, "title": sec.title, "ok": False,
                    "sql": raw_sql, "columns": [], "rows": [],
                    "error": f"无权访问：{'、'.join(denied)}", "skipped": True,
                    "digest": "", "chart": sec.chart,
                })
                continue

        exec_sql = raw_sql
        rf = _pick_row_filter(raw_sql, row_filters)
        if rf:
            exec_sql = _wrap_row_filter(raw_sql, rf)

        res = execute_sql(exec_sql)
        if not res.get("success") and rf:
            # 包了行过滤跑不通（多为字段不在结果列里）→ 退回原语句，
            # 但这事要在报告里留痕，不能悄悄降级
            res = execute_sql(raw_sql)
            if res.get("success"):
                res["_acl_degraded"] = True

        cols = res.get("columns") or []
        rows = res.get("rows") or []
        ok = bool(res.get("success"))
        out_sections.append({
            "key": sec.key, "title": sec.title, "ok": ok,
            "sql": exec_sql if ok else raw_sql,
            "columns": cols, "rows": rows,
            "error": "" if ok else str(res.get("error") or "查询失败")[:200],
            "skipped": False,
            "digest": _digest(sec, cols, rows) if ok else "",
            "chart": sec.chart,
            "dim": sec.dim, "meas": sec.meas, "hint": sec.hint,
            "acl_degraded": bool(res.get("_acl_degraded")),
        })

    return {
        "template": template.kind,
        "label": template.label,
        "sections": out_sections,
        "skipped": skipped,
        "ok": any(s["ok"] for s in out_sections),
    }
